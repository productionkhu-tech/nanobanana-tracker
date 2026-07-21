"""Whitelist-add → secret-scan → commit → push to public_repo on GitHub.

Token only flows through git remote URL stored in public_repo/.git/config (local).
Never logged. Never written to a tracked file."""
from __future__ import annotations
import os
import re
import subprocess
import sys
from pathlib import Path

from keys import load

# In both local and CI: repo root is the dir holding app/, docs/, .github/.
REPO = Path(__file__).resolve().parent.parent

# Files allowed to be committed. .gitignore is the primary gate; this is layer 2.
WHITELIST_PATTERNS = [
    ".gitignore",
    "README.md",
    "requirements.txt",
    "docs/",
    "app/",       # .py files only (gitignore enforces)
    ".github/",   # workflow files
]

# Patterns that, if found in any staged file, abort the push.
SECRET_PATTERNS = [
    re.compile(r"sk-proj-[A-Za-z0-9_\-]{30,}"),
    re.compile(r"sk-admin-[A-Za-z0-9_\-]{30,}"),
    re.compile(r"\bghp_[A-Za-z0-9]{30,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{30,}"),
    re.compile(r"\bAKAP[A-Za-z0-9+/=]{30,}"),   # BytePlus Access Key
    re.compile(r"-----BEGIN (RSA |EC )?PRIVATE KEY-----"),
    re.compile(r'"private_key_id"\s*:'),
    re.compile(r'"client_email"\s*:\s*"[^"]+iam\.gserviceaccount\.com"'),
    re.compile(r'"client_secret"\s*:'),
]


def run(cmd: list[str], *, cwd: Path, env: dict | None = None,
        check: bool = True, capture: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, cwd=str(cwd), env=env,
        capture_output=capture, text=True, encoding="utf-8", check=check,
    )


def ensure_repo(token: str, user: str, repo: str) -> None:
    if not (REPO / ".git").exists():
        run(["git", "init", "-b", "main"], cwd=REPO)
    # Always (re)set user identity — CI runners don't have it by default.
    run(["git", "config", "user.email", "tracker@local"], cwd=REPO)
    run(["git", "config", "user.name", "nanobanana-tracker"], cwd=REPO)
    # set/update remote with token-embedded URL (local-only file)
    remote_url = f"https://x-access-token:{token}@github.com/{user}/{repo}.git"
    existing = run(["git", "remote"], cwd=REPO).stdout.split()
    if "origin" in existing:
        run(["git", "remote", "set-url", "origin", remote_url], cwd=REPO)
    else:
        run(["git", "remote", "add", "origin", remote_url], cwd=REPO)


def is_whitelisted(rel_path: str) -> bool:
    for p in WHITELIST_PATTERNS:
        if p.endswith("/"):
            if rel_path.startswith(p):
                return True
        elif rel_path == p:
            return True
    return False


def scan_for_secrets(paths: list[Path]) -> list[str]:
    hits = []
    for p in paths:
        try:
            content = p.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            hits.append(f"{p}: read failed ({e})")
            continue
        for pat in SECRET_PATTERNS:
            m = pat.search(content)
            if m:
                hits.append(f"{p}: matched secret pattern {pat.pattern[:60]!r}")
                break
    return hits


def stage_whitelisted() -> list[str]:
    """Stage only whitelisted pathspecs (handles add/modify/delete). Return list."""
    run(["git", "reset"], cwd=REPO, check=False)
    # `git add -A -- <pathspec>` covers additions, modifications AND deletions.
    # The .gitignore whitelist is the primary gate; this is the second layer.
    for ps in WHITELIST_PATTERNS:
        run(["git", "add", "-A", "--", ps.rstrip("/")], cwd=REPO, check=False)
    # Verify nothing snuck in outside the whitelist
    out = run(["git", "diff", "--cached", "--name-only"], cwd=REPO).stdout
    staged = [l.strip() for l in out.splitlines() if l.strip()]
    bad = [p for p in staged if not is_whitelisted(p)]
    if bad:
        run(["git", "reset"], cwd=REPO, check=False)
        raise RuntimeError(f"non-whitelisted paths staged: {bad}")
    return staged


def staged_files() -> list[Path]:
    out = run(["git", "diff", "--cached", "--name-only"], cwd=REPO).stdout
    return [REPO / line for line in out.splitlines() if line.strip()]


def has_changes() -> bool:
    out = run(["git", "status", "--porcelain"], cwd=REPO).stdout
    return bool(out.strip())


def commit_and_push(token: str, user: str, repo: str) -> str:
    # Avoid leaking token into env-derived logs
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    diff = run(["git", "diff", "--cached", "--stat"], cwd=REPO).stdout.strip()
    msg = "update data.json\n\n" + (diff or "(no diff)")
    run(["git", "commit", "-m", msg], cwd=REPO, env=env)
    # Race-resilient push: parallel writers (CI + local) can race, so attempt
    # push, and if rejected, fetch+rebase keeping OUR freshly-built data.json.
    # NOTE: in `git rebase`, -X option sides are INVERTED vs merge — "ours" is
    # the base (origin/main) and "theirs" is the commit being replayed (ours!).
    # Using -X ours here silently replaced our fresh data with the remote's
    # (possibly $0 from a failed CI run) — 2026-07-21 incident. -X theirs keeps
    # the local rebuild, which is always the freshest aggregation.
    for attempt in range(3):
        push = run(["git", "push", "-u", "origin", "main"], cwd=REPO, env=env, check=False)
        if push.returncode == 0:
            break
        print(f"[push] attempt {attempt+1} rejected — rebasing on origin/main (keep local data)")
        run(["git", "fetch", "origin", "main"], cwd=REPO, env=env)
        run(["git", "rebase", "-X", "theirs", "origin/main"], cwd=REPO, env=env, check=False)
    else:
        raise RuntimeError("push failed after 3 attempts")
    return run(["git", "rev-parse", "HEAD"], cwd=REPO).stdout.strip()


def main() -> int:
    keys = load()
    ensure_repo(keys.github_token, keys.github_user, keys.github_repo)

    added = stage_whitelisted()
    print(f"[push] whitelisted staged: {len(added)} files")

    files = staged_files()
    if not files:
        print("[push] nothing staged; skipping")
        return 0

    hits = scan_for_secrets(files)
    if hits:
        print("[push] SECRET SCAN FAILED — aborting:")
        for h in hits:
            print(f"  {h}")
        run(["git", "reset"], cwd=REPO, check=False)
        return 2

    if not has_changes():
        print("[push] no changes vs HEAD; skipping commit")
        return 0

    sha = commit_and_push(keys.github_token, keys.github_user, keys.github_repo)
    print(f"[push] ok @ {sha[:8]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
