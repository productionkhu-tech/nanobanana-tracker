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


def consolidate_seedream_model(config_name: str) -> str:
    """BytePlus ConfigName 정리: 리소스팩 접미사 제거해 모델 단위로 묶음."""
    s = config_name or "(unknown)"
    return re.sub(r"-Pack-.*$", "", s, flags=re.IGNORECASE)


def utc_today_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def pacific_today_str() -> str:
    """US/Pacific date — matches GCP billing day boundary."""
    # PDT (Mar–Nov) = UTC-7, PST = UTC-8. Conservative approx using -7
    # is wrong for winter; use zoneinfo for correctness.
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d")
    except Exception:
        # Fallback: assume PDT (May-Oct in NA). Sufficient for our use case.
        return (datetime.now(timezone.utc) - timedelta(hours=7)).strftime("%Y-%m-%d")


def today_str_for_source(source: str) -> str:
    """Return the 'today' date string in the timezone the collector used for that source.

    GCP rows are bucketed in America/Los_Angeles (matches GCP billing UI).
    OpenAI rows are bucketed in UTC (matches OpenAI Admin API).
    BytePlus rows are bucketed in the provider's billing day (~UTC+8).
    """
    if source == "gcp":
        return pacific_today_str()
    if source == "byteplus":
        return (datetime.now(timezone.utc) + timedelta(hours=8)).strftime("%Y-%m-%d")
    return utc_today_str()


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
    # Use the date convention this source's collector wrote (Pacific for GCP, UTC for OpenAI).
    today = today_str_for_source(source)
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

    # Monthly buckets — ALL months that have data + current month (zero-fill)
    # Capped at 36 months to keep payload reasonable; tables can scroll.
    months: dict[str, float] = {}
    for d, v in daily_cost.items():
        months[d[:7]] = months.get(d[:7], 0.0) + v
    if today_dt.strftime("%Y-%m") not in months:
        months[today_dt.strftime("%Y-%m")] = 0.0
    sorted_months = sorted(months.keys())[-36:]
    monthly = [{"month": k, "cost": round(months[k], 4)} for k in sorted_months]

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


def _load_prev_output():
    """직전 발행된 data.json (CI에선 직전 커밋본). 없거나 깨졌으면 None."""
    try:
        if OUT_PATH.exists():
            return json.loads(OUT_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
    return None


def main() -> None:
    c = sqlite3.connect(str(DB_PATH))
    c.row_factory = sqlite3.Row
    rows = fetch_rows(c)
    sync = fetch_sync(c)
    meta = fetch_meta(c)
    c.close()

    prev = _load_prev_output()

    def view_or_carry(source_key, prev_key, build_fn):
        """수집 실패로 이 소스의 rows가 0인데 직전 data.json에 유효 데이터가 있으면
        직전 뷰를 그대로 유지(carry-forward). 일시 API 장애 때 $0으로 덮어써
        사용량이 '사라져 보이는' 사고 방지 (2026-07-21 OpenAI timeout 사고)."""
        has_rows = any(r["source"] == source_key for r in rows)
        if not has_rows and prev:
            prev_src = (prev.get("sources") or {}).get(prev_key) or {}
            prev_year = ((prev_src.get("totals") or {}).get("year") or {}).get("cost", 0)
            if prev_year and prev_year > 0:
                print(f"[build] {prev_key}: no fresh rows — carrying forward previous data "
                      f"(year=${prev_year})")
                keep = {k: prev_src[k] for k in
                        ("primary_label", "totals", "category_table", "daily", "monthly")
                        if k in prev_src}
                return keep, True
        return build_fn(), False

    nano, nano_carried = view_or_carry("gcp", "nanobanana", lambda: build_source_view(
        rows, source="gcp",
        include_pred=lambda cat: cat in NANOBANANA_SERVICES,
        normalize_cat=lambda cat: cat,
        primary_label="NanoBanana"))
    openai, oa_carried = view_or_carry("openai", "openai", lambda: build_source_view(
        rows, source="openai",
        include_pred=is_image_line,
        normalize_cat=consolidate_openai_model,
        primary_label="GPT Image 2"))
    seed, seed_carried = view_or_carry("byteplus", "seedream", lambda: build_source_view(
        rows, source="byteplus",
        include_pred=lambda cat: True,
        normalize_cat=consolidate_seedream_model,
        primary_label="Seedream"))

    combined = {}
    for k in ("today", "yesterday", "week", "last_week_same_day",
              "month", "last_month", "month_projection", "year"):
        combined[k] = {"cost": round(
            nano["totals"][k]["cost"] + openai["totals"][k]["cost"] + seed["totals"][k]["cost"], 4)}

    # Combined daily series — 날짜 키로 정렬해 합침.
    # (소스마다 '오늘'의 시간대가 달라서(Pacific/UTC/UTC+8) 배열 인덱스로 짝지으면
    #  마지막 날짜가 다를 때 하루씩 밀림 — 날짜 합집합 기준으로 만들어 어긋남 방지)
    nano_by_date = {d["date"]: d["cost"] for d in nano["daily"]}
    gpt_by_date  = {d["date"]: d["cost"] for d in openai["daily"]}
    seed_by_date = {d["date"]: d["cost"] for d in seed["daily"]}
    all_dates = sorted(set(nano_by_date) | set(gpt_by_date) | set(seed_by_date))[-366:]
    combined_daily = []
    for date in all_dates:
        cn = nano_by_date.get(date, 0.0)
        cg = gpt_by_date.get(date, 0.0)
        cs = seed_by_date.get(date, 0.0)
        combined_daily.append({"date": date, "nano": cn, "gpt": cg, "seed": cs,
                               "total": round(cn + cg + cs, 4)})

    # Build combined_monthly by zipping on month key (length may differ between sources)
    nano_by_month = {m["month"]: m["cost"] for m in nano["monthly"]}
    gpt_by_month  = {m["month"]: m["cost"] for m in openai["monthly"]}
    seed_by_month = {m["month"]: m["cost"] for m in seed["monthly"]}
    all_months = sorted(set(nano_by_month) | set(gpt_by_month) | set(seed_by_month))[-36:]
    combined_monthly = []
    for m in all_months:
        cn = nano_by_month.get(m, 0.0)
        cg = gpt_by_month.get(m, 0.0)
        cs = seed_by_month.get(m, 0.0)
        combined_monthly.append({"month": m, "nano": cn, "gpt": cg, "seed": cs,
                                 "total": round(cn + cg + cs, 4)})

    # Weekly buckets (ISO Monday-Sunday) — last 12 weeks
    today_dt = datetime.fromisoformat(utc_today_str())
    this_monday = today_dt - timedelta(days=today_dt.weekday())
    weeks_acc = {}
    for d in combined_daily:
        wkmon = iso_week_monday(d["date"])
        if wkmon not in weeks_acc:
            weeks_acc[wkmon] = {"week_start": wkmon, "nano": 0.0, "gpt": 0.0, "seed": 0.0}
        weeks_acc[wkmon]["nano"] += d["nano"]
        weeks_acc[wkmon]["gpt"]  += d["gpt"]
        weeks_acc[wkmon]["seed"] += d["seed"]
    combined_weekly = []
    today_str = today_dt.strftime("%Y-%m-%d")
    for i in range(11, -1, -1):
        wmon = (this_monday - timedelta(weeks=i))
        wkey = wmon.strftime("%Y-%m-%d")
        wsun = wmon + timedelta(days=6)
        wsun_str = wsun.strftime("%Y-%m-%d")
        in_progress = (wkey <= today_str <= wsun_str)
        b = weeks_acc.get(wkey, {"nano": 0.0, "gpt": 0.0, "seed": 0.0})
        combined_weekly.append({
            "week_start": wkey,
            "week_end": wsun_str,
            "label": f"{wmon.month}/{wmon.day} – {wsun.month}/{wsun.day}" + (" (진행 중)" if in_progress else ""),
            "in_progress": in_progress,
            "nano": round(b["nano"], 4),
            "gpt":  round(b["gpt"],  4),
            "seed": round(b["seed"], 4),
            "total": round(b["nano"] + b["gpt"] + b["seed"], 4),
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

    today_dt = datetime.fromisoformat(utc_today_str())
    payload = {
        "schema_version": 7,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "approx_krw_per_usd": round(fx_rate, 4),
        "fx_source": fx_source,
        "last_refresh_ts": last_refresh_ts,
        "sync_status": sync_status,
        "current_year": today_dt.year,
        "year_start_date": today_dt.strftime("%Y-01-01"),
        "today_date": today_dt.strftime("%Y-%m-%d"),
        "totals_combined": combined,
        "combined_daily": combined_daily,
        "combined_weekly": combined_weekly,
        "combined_monthly": combined_monthly,
        "sources": {
            "nanobanana": {**nano, "tag_color": "#7c3aed", "carried_forward": nano_carried},
            "openai":     {**openai, "tag_color": "#10a37f", "carried_forward": oa_carried},
            "seedream":   {**seed, "tag_color": "#2563eb", "carried_forward": seed_carried},
        },
        "notes": {
            "currency": "USD 정가(list) 기준 — Free Tier 차감 전 사용량 측정값",
            "fx": "1 USD ≈ 1,450원으로 환산 (실제 GCP 청구 KRW와 환율·세금으로 다를 수 있음)",
            "delays": "GCP 빌링 export 1~24시간 지연, OpenAI Admin API 30~60분 지연, BytePlus 분할청구 30분~1일 지연",
            "schedule": "GitHub Actions cron 5분 주기 자동 수집 (PC 무관)",
        },
    }

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {OUT_PATH} ({OUT_PATH.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
