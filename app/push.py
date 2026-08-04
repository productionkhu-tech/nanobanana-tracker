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

DATA_JSON_REL = "docs/data.json"
# CI만 data.json을 발행한다.
#   CI  : 매 실행 빈 DB에서 전 기간을 새로 수집 → 항상 완전한 집계
#   로컬: DB가 영속이라 과거 월이 갱신 안 될 수 있음(BytePlus는 당월만 재조회)
#         → 로컬 빌드 결과를 올리면 과거 데이터가 과소 집계로 덮일 위험
# 로컬에서도 코드/HTML은 정상적으로 push 된다. data.json 은 다음 CI(5분)가 발행.
IS_CI = bool(os.environ.get("GITHUB_ACTIONS") or os.environ.get("CI"))

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
    # 로컬에서는 data.json 을 커밋 대상에서 제외하고 원본(HEAD)으로 되돌린다.
    # → 워킹트리가 항상 '발행본'과 같아져 다음 rebase 충돌도 사라진다.
    if not IS_CI:
        staged_now = run(["git", "diff", "--cached", "--name-only"], cwd=REPO).stdout
        if DATA_JSON_REL in staged_now:
            run(["git", "reset", "--", DATA_JSON_REL], cwd=REPO, check=False)
            run(["git", "checkout", "--", DATA_JSON_REL], cwd=REPO, check=False)
            print(f"[push] 로컬 실행 — {DATA_JSON_REL} 는 발행하지 않음 (CI가 담당)")

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
    # Race-resilient push. 충돌 시 **파일 단위로 통째** 우리 커밋을 채택한다.
    #   * -X ours/theirs 는 hunk 단위 3-way 병합이라 서로 다른 수집 결과가
    #     뒤섞인 franken-JSON 이 나올 수 있어 쓰지 않는다.
    #   * data.json 은 CI만 발행하고(IS_CI), CI 실행은 concurrency group 으로
    #     직렬화되므로 지금 push 하는 쪽이 항상 가장 최신·완전한 수집 결과다.
    #   * rebase 중 --theirs = 지금 replay 되는 우리 커밋 (--ours 는 upstream).
    for attempt in range(3):
        push = run(["git", "push", "-u", "origin", "main"], cwd=REPO, env=env, check=False)
        if push.returncode == 0:
            break
        print(f"[push] attempt {attempt+1} rejected — rebasing on origin/main")
        run(["git", "fetch", "origin", "main"], cwd=REPO, env=env)
        rb = run(["git", "rebase", "origin/main"], cwd=REPO, env=env, check=False)
        if rb.returncode != 0:
            conflicted = [l.strip() for l in run(
                ["git", "diff", "--name-only", "--diff-filter=U"],
                cwd=REPO, env=env, check=False).stdout.splitlines() if l.strip()]
            print(f"[push] conflicts: {conflicted}")
            for f in conflicted:
                run(["git", "checkout", "--theirs", "--", f], cwd=REPO, env=env, check=False)
                run(["git", "add", "--", f], cwd=REPO, env=env, check=False)
            cont = run(["git", "-c", "core.editor=true", "rebase", "--continue"],
                       cwd=REPO, env=env, check=False)
            if cont.returncode != 0:
                # 우리 변경분이 전부 upstream 것으로 대체돼 커밋할 게 없는 경우
                run(["git", "-c", "core.editor=true", "rebase", "--skip"],
                    cwd=REPO, env=env, check=False)
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
