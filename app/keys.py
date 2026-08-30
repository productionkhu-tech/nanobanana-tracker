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
    byteplus_ak: str = ""   # BytePlus IAM Access Key (Seedream) — 없으면 수집 스킵
    byteplus_sk: str = ""
    # GPT Image 2 비용 필터용 api_key_id 목록(쉼표 구분). 키 로테이션 시 옛 id 를
    # 남겨둬야 과거 비용이 조회된다 (OpenAI costs 는 키별 귀속이라 새 id 만 쓰면
    # 로테이션 이전 이력이 통째로 빠짐 — 2026-08-28 로테이션 때 확인).
    openai_image_key_ids: str = ""
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
        byteplus_ak=os.environ.get("BYTEPLUS_AK", "").strip(),
        byteplus_sk=os.environ.get("BYTEPLUS_SK", "").strip(),
        openai_image_key_ids=os.environ.get("OPENAI_IMAGE_KEY_ID", "").strip(),
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

    # BytePlus AK/SK (선택) — config/byteplus_key.txt: 1행 AK, 2행 SK
    bp_ak = bp_sk = ""
    bp_file = config / "byteplus_key.txt"
    if bp_file.exists():
        lines = [l.strip() for l in bp_file.read_text(encoding="utf-8").splitlines() if l.strip()]
        for line in lines:
            if line.startswith("AK"):
                bp_ak = line
            elif bp_ak and not bp_sk:
                bp_sk = line
        if not (bp_ak and bp_sk):
            raise RuntimeError("byteplus_key.txt: expected AK line then SK line")

    # GPT Image 2 api_key_id 목록 (선택) — config/openai_image_key_ids.txt
    ids_file = config / "openai_image_key_ids.txt"
    image_ids = ids_file.read_text(encoding="utf-8").strip() if ids_file.exists() else ""

    return Keys(
        gcp_sa_path=sa_path, gcp_project=project,
        openai_api=api, openai_admin=admin,
        github_token=token,
        byteplus_ak=bp_ak, byteplus_sk=bp_sk,
        openai_image_key_ids=image_ids,
    )


def load() -> Keys:
    return _load_ci() or _load_local()
