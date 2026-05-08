"""Load credentials from env vars (CI) or config/ files (local)."""
from __future__ import annotations
import json
import os
from dataclasses import dataclass
from pathlib import Path

THIS = Path(__file__).resolve()


@dataclass(frozen=True)
class Keys:
    gcp_sa_path: Path
    gcp_project: str
    openai_api: str
    openai_admin: str
    github_token: str
    github_user: str = "productionkhu-tech"
    github_repo: str = "nanobanana-tracker"


def _parse_gpt_keys(text: str) -> tuple[str, str]:
    api = admin = None
    cur = None
    for line in (l.strip() for l in text.splitlines()):
        if not line:
            continue
        if line.startswith("API"):
            cur = "api"
        elif line.startswith("어드민") or line.lower().startswith("admin"):
            cur = "admin"
        elif line.startswith("sk-proj-"):
            api = line
        elif line.startswith("sk-admin-"):
            admin = line
        elif line.startswith("sk-"):
            if cur == "api":
                api = line
            elif cur == "admin":
                admin = line
    if not (api and admin):
        raise RuntimeError("could not parse both keys from gpt key file")
    return api, admin


def _load_ci() -> Keys | None:
    """CI mode: env vars set. Returns None if not in CI."""
    if not os.environ.get("GCP_SA_JSON"):
        return None
    sa_json = os.environ["GCP_SA_JSON"]
    sa_data = json.loads(sa_json)
    project = sa_data.get("project_id")
    if not project:
        raise RuntimeError("GCP_SA_JSON: missing project_id")
    sa_path = THIS.parent.parent / "_gcp_sa.json"
    sa_path.write_text(sa_json, encoding="utf-8")

    admin = os.environ.get("OPENAI_ADMIN_KEY", "").strip()
    api = os.environ.get("OPENAI_API_KEY", "").strip()
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not admin:
        raise RuntimeError("OPENAI_ADMIN_KEY env not set")
    return Keys(
        gcp_sa_path=sa_path,
        gcp_project=project,
        openai_api=api,
        openai_admin=admin,
        github_token=token,  # `ghs_...` in CI
    )


def _load_local() -> Keys:
    """Local PC: config/ sits one level above the repo (outside git)."""
    # THIS = .../public_repo/app/keys.py → outer = parent.parent.parent
    config = THIS.parent.parent.parent / "config"
    if not config.is_dir():
        raise RuntimeError(f"config/ not found at {config}")

    sa_files = list(config.glob("*.json"))
    if not sa_files:
        raise RuntimeError(f"{config}: no GCP service account JSON found")
    sa_path = sa_files[0]
    sa_data = json.loads(sa_path.read_text(encoding="utf-8"))
    project = sa_data.get("project_id")
    if not project:
        raise RuntimeError(f"{sa_path}: missing project_id")

    gpt_file = None
    for f in config.glob("*.txt"):
        if f.name == "github_token.txt":
            continue
        text = f.read_text(encoding="utf-8", errors="ignore")
        if "sk-proj-" in text or "sk-admin-" in text:
            gpt_file = f
            break
    if not gpt_file:
        raise RuntimeError(f"{config}: no gpt key file (.txt with sk-...)")
    api, admin = _parse_gpt_keys(gpt_file.read_text(encoding="utf-8"))

    token_file = config / "github_token.txt"
    if not token_file.exists():
        raise RuntimeError(f"{token_file} missing")
    token = token_file.read_text(encoding="utf-8").strip()
    if not (token.startswith("ghp_") or token.startswith("github_pat_") or token.startswith("ghs_")):
        raise RuntimeError("github_token.txt: not a recognized GitHub token prefix")

    return Keys(
        gcp_sa_path=sa_path, gcp_project=project,
        openai_api=api, openai_admin=admin,
        github_token=token,
    )


def load() -> Keys:
    return _load_ci() or _load_local()
