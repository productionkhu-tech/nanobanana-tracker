"""Read SQLite, build docs/data.json. Pure aggregation; no secrets touched."""
from __future__ import annotations
import json
import re
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DB_PATH = REPO / "app" / "db.sqlite"
OUT_PATH = REPO / "docs" / "data.json"

NANOBANANA_SERVICES = {"Gemini API", "Vertex AI"}


def is_image_line(line_item: str) -> bool:
    s = (line_item or "").lower()
    return s.startswith("gpt-image") or "dall-e" in s or s.startswith("dalle")


def consolidate_openai_model(line_item: str) -> str:
    s = (line_item or "").lower()
    m = re.match(r"(gpt-image-\d+)", s)
    if m: return m.group(1)
    m = re.match(r"(dall-e\s*\d+)", s)
    if m: return m.group(1).replace("  ", " ")
    return line_item


def utc_today_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def fetch_rows(c):
    return list(c.execute(
        "SELECT source, category, date, request_count, image_count, cost_usd, gross_cost "
        "FROM usage_daily ORDER BY date"
    ))


def fetch_sync(c):
    out = {}
    for r in c.execute("SELECT source, last_run_ts, last_data_ts, status, error_msg FROM sync_log"):
        out[r["source"]] = dict(r)
    return out


def fetch_meta(c):
    out = {}
    try:
        for r in c.execute("SELECT key, value FROM meta"):
            out[r["key"]] = r["value"]
    except Exception:
        pass
    return out


def iso_week_monday(d_iso: str) -> str:
    """Return the Monday-of-week date (ISO 8601) for the given YYYY-MM-DD string."""
    dt = datetime.fromisoformat(d_iso)
    monday = dt - timedelta(days=dt.weekday())
    return monday.strftime("%Y-%m-%d")


def build_source_view(rows, source, include_pred, normalize_cat, primary_label):
    today = utc_today_str()
    today_dt = datetime.fromisoformat(today)
    yesterday = (today_dt - timedelta(days=1)).strftime("%Y-%m-%d")
    week_start = (today_dt - timedelta(days=6)).strftime("%Y-%m-%d")
    last_week_same_day = (today_dt - timedelta(days=7)).strftime("%Y-%m-%d")
    month_start = today_dt.replace(day=1).strftime("%Y-%m-%d")
    last_month_dt = (today_dt.replace(day=1) - timedelta(days=1)).replace(day=1)
    last_month_start = last_month_dt.strftime("%Y-%m-%d")
    last_month_end = today_dt.replace(day=1).strftime("%Y-%m-%d")
    year_start = today_dt.replace(month=1, day=1).strftime("%Y-%m-%d")
    year_ago = (today_dt - timedelta(days=364)).strftime("%Y-%m-%d")

    daily_cost: dict[str, float] = {}
    by_cat_cost: dict[str, dict[str, float]] = {}

    for r in rows:
        if r["source"] != source: continue
        if not include_pred(r["category"]): continue
        d = r["date"]
        cost = r["cost_usd"] or 0.0
        cat = normalize_cat(r["category"])
        daily_cost[d] = daily_cost.get(d, 0.0) + cost
        by_cat_cost.setdefault(cat, {})
        by_cat_cost[cat][d] = by_cat_cost[cat].get(d, 0.0) + cost

    sumr = lambda src, lo, hi=None: sum(v for d, v in src.items() if d >= lo and (hi is None or d < hi))
    today_cost = daily_cost.get(today, 0.0)
    yesterday_cost = daily_cost.get(yesterday, 0.0)
    week_cost = sumr(daily_cost, week_start)
    last_week_same_cost = daily_cost.get(last_week_same_day, 0.0)
    month_cost = sumr(daily_cost, month_start)
    last_month_cost = sumr(daily_cost, last_month_start, last_month_end)
    year_cost = sumr(daily_cost, year_start)

    series = []
    cur = datetime.fromisoformat(year_ago)
    while cur <= today_dt:
        d = cur.strftime("%Y-%m-%d")
        series.append({"date": d, "cost": round(daily_cost.get(d, 0.0), 4)})
        cur += timedelta(days=1)

    # Monthly buckets (last 12 months)
    months: dict[str, float] = {}
    for d, v in daily_cost.items():
        m = d[:7]
        months[m] = months.get(m, 0.0) + v
    monthly = []
    md = today_dt.replace(day=1)
    for _ in range(12):
        k = md.strftime("%Y-%m")
        monthly.append({"month": k, "cost": round(months.get(k, 0.0), 4)})
        md = (md - timedelta(days=1)).replace(day=1)
    monthly.reverse()

    cat_table = []
    for cat, by_date in by_cat_cost.items():
        ctoday = by_date.get(today, 0.0)
        cmonth = sumr(by_date, month_start)
        ctotal = sum(by_date.values())
        spark_start_dt = today_dt - timedelta(days=29)
        sparkline = []
        cur2 = spark_start_dt
        while cur2 <= today_dt:
            sparkline.append(round(by_date.get(cur2.strftime("%Y-%m-%d"), 0.0), 4))
            cur2 += timedelta(days=1)
        cat_table.append({
            "category": cat,
            "today": round(ctoday, 4),
            "month": round(cmonth, 4),
            "total": round(ctotal, 4),
            "sparkline": sparkline,
        })
    cat_table.sort(key=lambda x: -x["total"])

    days_into_month = today_dt.day
    last_day_of_month = (today_dt.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    days_in_month_n = last_day_of_month.day
    month_projection = (month_cost / max(days_into_month, 1)) * days_in_month_n if days_into_month >= 1 else 0

    return {
        "primary_label": primary_label,
        "totals": {
            "today":              {"cost": round(today_cost, 4)},
            "yesterday":          {"cost": round(yesterday_cost, 4)},
            "week":               {"cost": round(week_cost, 4)},
            "last_week_same_day": {"cost": round(last_week_same_cost, 4)},
            "month":              {"cost": round(month_cost, 4)},
            "last_month":         {"cost": round(last_month_cost, 4)},
            "month_projection":   {"cost": round(month_projection, 4)},
            "year":               {"cost": round(year_cost, 4)},
        },
        "category_table": cat_table,
        "daily": series,
        "monthly": monthly,
    }


def main() -> None:
    c = sqlite3.connect(str(DB_PATH))
    c.row_factory = sqlite3.Row
    rows = fetch_rows(c)
    sync = fetch_sync(c)
    meta = fetch_meta(c)
    c.close()

    nano = build_source_view(rows, source="gcp",
                             include_pred=lambda cat: cat in NANOBANANA_SERVICES,
                             normalize_cat=lambda cat: cat,
                             primary_label="NanoBanana")
    openai = build_source_view(rows, source="openai",
                               include_pred=is_image_line,
                               normalize_cat=consolidate_openai_model,
                               primary_label="GPT Image 2")

    combined = {}
    for k in ("today", "yesterday", "week", "last_week_same_day",
              "month", "last_month", "month_projection", "year"):
        combined[k] = {"cost": round(
            nano["totals"][k]["cost"] + openai["totals"][k]["cost"], 4)}

    # Combined daily and monthly series for Overview / History tabs
    combined_daily = []
    for i, dnano in enumerate(nano["daily"]):
        date = dnano["date"]
        cn = dnano["cost"]
        cg = openai["daily"][i]["cost"] if i < len(openai["daily"]) else 0
        combined_daily.append({"date": date, "nano": cn, "gpt": cg, "total": round(cn + cg, 4)})

    combined_monthly = []
    for i, mnano in enumerate(nano["monthly"]):
        month = mnano["month"]
        cn = mnano["cost"]
        cg = openai["monthly"][i]["cost"] if i < len(openai["monthly"]) else 0
        combined_monthly.append({"month": month, "nano": cn, "gpt": cg, "total": round(cn + cg, 4)})

    # Weekly buckets (ISO Monday-Sunday) — last 12 weeks
    today_dt = datetime.fromisoformat(utc_today_str())
    this_monday = today_dt - timedelta(days=today_dt.weekday())
    weeks_acc = {}
    for d in combined_daily:
        wkmon = iso_week_monday(d["date"])
        if wkmon not in weeks_acc:
            weeks_acc[wkmon] = {"week_start": wkmon, "nano": 0.0, "gpt": 0.0}
        weeks_acc[wkmon]["nano"] += d["nano"]
        weeks_acc[wkmon]["gpt"]  += d["gpt"]
    combined_weekly = []
    for i in range(11, -1, -1):
        wmon = (this_monday - timedelta(weeks=i))
        wkey = wmon.strftime("%Y-%m-%d")
        wsun = wmon + timedelta(days=6)
        b = weeks_acc.get(wkey, {"nano": 0.0, "gpt": 0.0})
        combined_weekly.append({
            "week_start": wkey,
            "week_end": wsun.strftime("%Y-%m-%d"),
            "label": f"{wmon.month}/{wmon.day} – {wsun.month}/{wsun.day}",
            "nano": round(b["nano"], 4),
            "gpt":  round(b["gpt"],  4),
            "total": round(b["nano"] + b["gpt"], 4),
        })

    # Single unified last-refresh timestamp
    sync_ts_candidates = [v.get("last_run_ts") for v in sync.values() if v.get("last_run_ts")]
    last_refresh_ts = max(sync_ts_candidates) if sync_ts_candidates else None
    sync_status = "ok" if all(v.get("status") == "ok" for v in sync.values()) else "error"

    fx_rate = 1450.0
    fx_source = "fallback"
    try:
        if meta.get("krw_per_usd"):
            fx_rate = float(meta["krw_per_usd"])
            fx_source = meta.get("fx_source", "gcp_billing_export")
    except Exception:
        pass

    payload = {
        "schema_version": 5,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "approx_krw_per_usd": round(fx_rate, 4),
        "fx_source": fx_source,
        "last_refresh_ts": last_refresh_ts,
        "sync_status": sync_status,
        "totals_combined": combined,
        "combined_daily": combined_daily,
        "combined_weekly": combined_weekly,
        "combined_monthly": combined_monthly,
        "sources": {
            "nanobanana": {**nano, "tag_color": "#7c3aed"},
            "openai":     {**openai, "tag_color": "#10a37f"},
        },
        "notes": {
            "currency": "USD 정가(list) 기준 — Free Tier 차감 전 사용량 측정값",
            "fx": "1 USD ≈ 1,450원으로 환산 (실제 GCP 청구 KRW와 환율·세금으로 다를 수 있음)",
            "delays": "GCP 빌링 export 1~24시간 지연, OpenAI Admin API 30~60분 지연",
            "schedule": "GitHub Actions cron 5분 주기 자동 수집 (PC 무관)",
        },
    }

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {OUT_PATH} ({OUT_PATH.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
