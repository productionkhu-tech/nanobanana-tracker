"""Read SQLite, build public_repo/data.json. Pure aggregation; no secrets touched."""
from __future__ import annotations
import json
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path

# REPO root holds app/, docs/, .github/ — both locally and in CI.
REPO = Path(__file__).resolve().parent.parent
DB_PATH = REPO / "app" / "db.sqlite"
OUT_PATH = REPO / "docs" / "data.json"

NANOBANANA_SERVICES = {"Gemini API", "Vertex AI"}  # GCP service.description matches


def is_image_line(line_item: str) -> bool:
    s = (line_item or "").lower()
    return s.startswith("gpt-image") or "dall-e" in s or s.startswith("dalle")


def utc_today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def fetch_rows(c: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(c.execute(
        "SELECT source, category, date, request_count, image_count, cost_usd, gross_cost "
        "FROM usage_daily ORDER BY date"
    ))


def fetch_sync(c: sqlite3.Connection) -> dict[str, dict]:
    out = {}
    for r in c.execute("SELECT source, last_run_ts, last_data_ts, status, error_msg FROM sync_log"):
        out[r["source"]] = dict(r)
    return out


def date_in_range(d: str, start: str, end: str) -> bool:
    return start <= d <= end


def build_source_view(rows: list[sqlite3.Row], source: str,
                      include_pred, primary_label: str) -> dict:
    """Cost-only view; non-primary categories are filtered out (per user request)."""
    today = utc_today()
    today_dt = datetime.fromisoformat(today)
    month_start = today_dt.replace(day=1).strftime("%Y-%m-%d")
    year_start = today_dt.replace(month=1, day=1).strftime("%Y-%m-%d")
    year_ago = (today_dt - timedelta(days=364)).strftime("%Y-%m-%d")

    daily_cost: dict[str, float] = {}
    by_category_total: dict[str, float] = {}

    totals = {"today": 0.0, "month": 0.0, "year": 0.0}

    for r in rows:
        if r["source"] != source:
            continue
        cat = r["category"]
        if not include_pred(cat):
            continue
        d = r["date"]
        cost = r["cost_usd"] or 0.0
        daily_cost[d] = daily_cost.get(d, 0.0) + cost
        by_category_total[cat] = by_category_total.get(cat, 0.0) + cost

        if d == today:    totals["today"] += cost
        if d >= month_start: totals["month"] += cost
        if d >= year_start:  totals["year"]  += cost

    series = []
    cur = datetime.fromisoformat(year_ago)
    while cur <= today_dt:
        d = cur.strftime("%Y-%m-%d")
        series.append({"date": d, "cost": round(daily_cost.get(d, 0.0), 4)})
        cur += timedelta(days=1)

    cat_breakdown = sorted(
        ({"category": k, "cost": round(v, 4)} for k, v in by_category_total.items()),
        key=lambda x: -x["cost"],
    )

    return {
        "primary_label": primary_label,
        "totals": {k: {"cost": round(v, 4)} for k, v in totals.items()},
        "category_breakdown": cat_breakdown,
        "daily": series,
    }


def main() -> None:
    c = sqlite3.connect(str(DB_PATH))
    c.row_factory = sqlite3.Row
    rows = fetch_rows(c)
    sync = fetch_sync(c)
    c.close()

    nano = build_source_view(
        rows, source="gcp",
        include_pred=lambda cat: cat in NANOBANANA_SERVICES,
        primary_label="NanoBanana (Gemini + Vertex)",
    )
    openai = build_source_view(
        rows, source="openai",
        include_pred=is_image_line,
        primary_label="GPT Image 2",
    )

    payload = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "sources": {
            "nanobanana": {
                **nano,
                "sync": sync.get("gcp", {}),
                "delay_note": "GCP billing export delays usage data by several hours up to ~24h.",
            },
            "openai": {
                **openai,
                "sync": sync.get("openai", {}),
                "delay_note": "OpenAI usage updates within minutes.",
            },
        },
    }

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {OUT_PATH} ({OUT_PATH.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
