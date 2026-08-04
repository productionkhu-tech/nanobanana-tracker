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
        "SELECT source, category, date, request_count, image_count, cost_usd, gross_cost, "
        "       cost_krw "
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


def build_source_view(rows, source, include_pred, normalize_cat, primary_label, fx_rate=None):
    """fx_rate: 원본이 USD인 소스(OpenAI/BytePlus)의 KRW 추정에 쓰는 최신 환율.
    GCP는 행에 실제 청구 KRW(cost_krw)가 있으므로 환율을 쓰지 않는다."""
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
    # 시계열 시작은 '364일 전'과 '연초' 중 더 이른 쪽.
    # 364일만 쓰면 윤년 12/31에 series가 1/2부터 시작해 1/1이 빠지고,
    # totals.year(원본 dict 기준)와 Σdaily 가 어긋난다. (2028-12-31 재현 확인)
    year_ago = min((today_dt - timedelta(days=364)).strftime("%Y-%m-%d"), year_start)

    daily_cost: dict[str, float] = {}
    by_cat_cost: dict[str, dict[str, float]] = {}
    daily_krw: dict[str, float] = {}
    by_cat_krw: dict[str, dict[str, float]] = {}
    krw_exact = False   # True면 실제 청구 원화, False면 최신 환율로 추정

    for r in rows:
        if r["source"] != source: continue
        if not include_pred(r["category"]): continue
        d = r["date"]
        cost = r["cost_usd"] or 0.0
        cat = normalize_cat(r["category"])
        daily_cost[d] = daily_cost.get(d, 0.0) + cost
        by_cat_cost.setdefault(cat, {})
        by_cat_cost[cat][d] = by_cat_cost[cat].get(d, 0.0) + cost

        # 원본 KRW가 있으면 그대로(=그달 GCP 적용 환율 반영), 없으면 최신 환율 추정
        raw_krw = r["cost_krw"] if "cost_krw" in r.keys() else None
        if raw_krw is not None:
            krw = float(raw_krw)
            krw_exact = True
        else:
            krw = cost * (fx_rate or 0)
        daily_krw[d] = daily_krw.get(d, 0.0) + krw
        by_cat_krw.setdefault(cat, {})
        by_cat_krw[cat][d] = by_cat_krw[cat].get(d, 0.0) + krw

    sumr = lambda src, lo, hi=None: sum(v for d, v in src.items() if d >= lo and (hi is None or d < hi))
    today_cost = daily_cost.get(today, 0.0)
    yesterday_cost = daily_cost.get(yesterday, 0.0)
    week_cost = sumr(daily_cost, week_start)
    last_week_same_cost = daily_cost.get(last_week_same_day, 0.0)
    month_cost = sumr(daily_cost, month_start)
    last_month_cost = sumr(daily_cost, last_month_start, last_month_end)
    year_cost = sumr(daily_cost, year_start)

    # 지난달 '같은 기간'(1일~오늘과 같은 일자). 이번 달 누적(MTD)과 공정하게 비교하려면
    # 지난달 '전체'가 아니라 같은 날짜까지만 잘라야 한다.
    # (예: 8/4에 8월 4일치를 7월 31일치 전체와 비교하면 항상 -90%대가 나옴)
    lm_len = (today_dt.replace(day=1) - timedelta(days=1)).day
    n_days = min(today_dt.day, lm_len)
    lm_td_end = (last_month_end if n_days >= lm_len
                 else last_month_dt.replace(day=n_days + 1).strftime("%Y-%m-%d"))
    last_month_td_cost = sumr(daily_cost, last_month_start, lm_td_end)

    # 같은 기간을 KRW로도 집계 (GCP는 실제 청구액 합, 그 외는 최신 환율 추정)
    today_krw = daily_krw.get(today, 0.0)
    yesterday_krw = daily_krw.get(yesterday, 0.0)
    week_krw = sumr(daily_krw, week_start)
    last_week_same_krw = daily_krw.get(last_week_same_day, 0.0)
    month_krw = sumr(daily_krw, month_start)
    last_month_krw = sumr(daily_krw, last_month_start, last_month_end)
    year_krw = sumr(daily_krw, year_start)
    last_month_td_krw = sumr(daily_krw, last_month_start, lm_td_end)

    series = []
    cur = datetime.fromisoformat(year_ago)
    while cur <= today_dt:
        d = cur.strftime("%Y-%m-%d")
        series.append({"date": d,
                       "cost": round(daily_cost.get(d, 0.0), 4),
                       "krw": round(daily_krw.get(d, 0.0), 2)})
        cur += timedelta(days=1)

    # Monthly buckets — ALL months that have data + current month (zero-fill)
    # Capped at 36 months to keep payload reasonable; tables can scroll.
    months: dict[str, float] = {}
    months_krw: dict[str, float] = {}
    for d, v in daily_cost.items():
        months[d[:7]] = months.get(d[:7], 0.0) + v
    for d, v in daily_krw.items():
        months_krw[d[:7]] = months_krw.get(d[:7], 0.0) + v
    if today_dt.strftime("%Y-%m") not in months:
        months[today_dt.strftime("%Y-%m")] = 0.0
    sorted_months = sorted(months.keys())[-36:]
    monthly = [{"month": k,
                "cost": round(months[k], 4),
                "krw": round(months_krw.get(k, 0.0), 2)} for k in sorted_months]

    cat_table = []
    for cat, by_date in by_cat_cost.items():
        by_date_krw = by_cat_krw.get(cat, {})
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
            "today_krw": round(by_date_krw.get(today, 0.0), 2),
            "month_krw": round(sumr(by_date_krw, month_start), 2),
            "total_krw": round(sum(by_date_krw.values()), 2),
            "sparkline": sparkline,
        })
    cat_table.sort(key=lambda x: -x["total"])

    days_into_month = today_dt.day
    last_day_of_month = (today_dt.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    days_in_month_n = last_day_of_month.day
    month_projection = (month_cost / max(days_into_month, 1)) * days_in_month_n if days_into_month >= 1 else 0
    month_projection_krw = (month_krw / max(days_into_month, 1)) * days_in_month_n if days_into_month >= 1 else 0

    def pair(usd_v, krw_v):
        return {"cost": round(usd_v, 4), "krw": round(krw_v, 2)}

    # 소스마다 청구 하루 경계가 달라(GCP=US/Pacific, OpenAI=UTC, BytePlus=UTC+8)
    # 같은 시각에도 'today'가 가리키는 날짜가 다르다. 화면에서 오해가 없도록
    # 각 소스가 기준으로 삼은 날짜와 타임존을 함께 내보낸다.
    DATE_BASIS = {"gcp": "US/Pacific", "openai": "UTC", "byteplus": "UTC+8"}

    return {
        "primary_label": primary_label,
        "krw_exact": krw_exact,   # True = 실제 청구 원화, False = 최신 환율 추정
        "date_basis": DATE_BASIS.get(source, "UTC"),
        "today_date": today,      # 이 소스의 'today'가 실제로 가리키는 날짜
        "totals": {
            "today":              pair(today_cost, today_krw),
            "yesterday":          pair(yesterday_cost, yesterday_krw),
            "week":               pair(week_cost, week_krw),
            "last_week_same_day": pair(last_week_same_cost, last_week_same_krw),
            "month":              pair(month_cost, month_krw),
            "last_month":         pair(last_month_cost, last_month_krw),
            # 지난달 1일~오늘과 같은 일자까지 (MTD 비교용)
            "last_month_to_date": pair(last_month_td_cost, last_month_td_krw),
            "month_projection":   pair(month_projection, month_projection_krw),
            "year":               pair(year_cost, year_krw),
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

    # 최신 환율 — USD 청구 소스(OpenAI/BytePlus)의 원화 '추정'에만 사용.
    # GCP는 행에 실제 청구 KRW가 있어 이 값을 쓰지 않는다.
    fx_rate = 1450.0
    fx_source = "fallback"
    try:
        if meta.get("krw_per_usd"):
            fx_rate = float(meta["krw_per_usd"])
            fx_source = meta.get("fx_source", "gcp_billing_export")
    except Exception:
        pass

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
                        ("primary_label", "totals", "category_table", "daily", "monthly",
                         "krw_exact", "date_basis", "today_date")
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
        primary_label="GPT Image 2", fx_rate=fx_rate))
    seed, seed_carried = view_or_carry("byteplus", "seedream", lambda: build_source_view(
        rows, source="byteplus",
        include_pred=lambda cat: True,
        normalize_cat=consolidate_seedream_model,
        primary_label="Seedream", fx_rate=fx_rate))

    # 소스별 '마지막 성공 수집 시각'. 정상 수집이면 지금, carry-forward면 직전
    # 발행본의 값을 계승 → "언제부터 갱신 지연 중"인지 대시보드가 표시할 수 있음.
    now_ts_ = int(datetime.now(timezone.utc).timestamp())
    def last_ok_ts(prev_key, carried):
        if not carried:
            return now_ts_
        if prev:
            prev_src = (prev.get("sources") or {}).get(prev_key) or {}
            return prev_src.get("last_ok_ts") or prev.get("last_refresh_ts")
        return None
    nano_ok_ts = last_ok_ts("nanobanana", nano_carried)
    oa_ok_ts   = last_ok_ts("openai", oa_carried)
    seed_ok_ts = last_ok_ts("seedream", seed_carried)

    _krw = lambda src, k: (src["totals"][k].get("krw") or 0.0)
    combined = {}
    for k in ("today", "yesterday", "week", "last_week_same_day",
              "month", "last_month", "last_month_to_date", "month_projection", "year"):
        combined[k] = {
            "cost": round(nano["totals"][k]["cost"] + openai["totals"][k]["cost"]
                          + seed["totals"][k]["cost"], 4),
            "krw": round(_krw(nano, k) + _krw(openai, k) + _krw(seed, k), 2),
        }

    # Combined daily series — 날짜 키로 정렬해 합침.
    # (소스마다 '오늘'의 시간대가 달라서(Pacific/UTC/UTC+8) 배열 인덱스로 짝지으면
    #  마지막 날짜가 다를 때 하루씩 밀림 — 날짜 합집합 기준으로 만들어 어긋남 방지)
    nano_by_date = {d["date"]: d["cost"] for d in nano["daily"]}
    gpt_by_date  = {d["date"]: d["cost"] for d in openai["daily"]}
    seed_by_date = {d["date"]: d["cost"] for d in seed["daily"]}
    nano_krw_date = {d["date"]: d.get("krw", 0.0) for d in nano["daily"]}
    gpt_krw_date  = {d["date"]: d.get("krw", 0.0) for d in openai["daily"]}
    seed_krw_date = {d["date"]: d.get("krw", 0.0) for d in seed["daily"]}
    all_dates = sorted(set(nano_by_date) | set(gpt_by_date) | set(seed_by_date))[-366:]
    combined_daily = []
    for date in all_dates:
        cn = nano_by_date.get(date, 0.0)
        cg = gpt_by_date.get(date, 0.0)
        cs = seed_by_date.get(date, 0.0)
        kn = nano_krw_date.get(date, 0.0)
        kg = gpt_krw_date.get(date, 0.0)
        ks = seed_krw_date.get(date, 0.0)
        combined_daily.append({"date": date, "nano": cn, "gpt": cg, "seed": cs,
                               "total": round(cn + cg + cs, 4),
                               "nano_krw": round(kn, 2), "gpt_krw": round(kg, 2),
                               "seed_krw": round(ks, 2), "total_krw": round(kn + kg + ks, 2)})

    # Build combined_monthly by zipping on month key (length may differ between sources)
    nano_by_month = {m["month"]: m["cost"] for m in nano["monthly"]}
    gpt_by_month  = {m["month"]: m["cost"] for m in openai["monthly"]}
    seed_by_month = {m["month"]: m["cost"] for m in seed["monthly"]}
    nano_krw_month = {m["month"]: m.get("krw", 0.0) for m in nano["monthly"]}
    gpt_krw_month  = {m["month"]: m.get("krw", 0.0) for m in openai["monthly"]}
    seed_krw_month = {m["month"]: m.get("krw", 0.0) for m in seed["monthly"]}
    all_months = sorted(set(nano_by_month) | set(gpt_by_month) | set(seed_by_month))[-36:]
    combined_monthly = []
    for m in all_months:
        cn = nano_by_month.get(m, 0.0)
        cg = gpt_by_month.get(m, 0.0)
        cs = seed_by_month.get(m, 0.0)
        kn = nano_krw_month.get(m, 0.0)
        kg = gpt_krw_month.get(m, 0.0)
        ks = seed_krw_month.get(m, 0.0)
        combined_monthly.append({"month": m, "nano": cn, "gpt": cg, "seed": cs,
                                 "total": round(cn + cg + cs, 4),
                                 "nano_krw": round(kn, 2), "gpt_krw": round(kg, 2),
                                 "seed_krw": round(ks, 2), "total_krw": round(kn + kg + ks, 2)})

    # Weekly buckets (ISO Monday-Sunday) — last 12 weeks
    today_dt = datetime.fromisoformat(utc_today_str())
    this_monday = today_dt - timedelta(days=today_dt.weekday())
    weeks_acc = {}
    ZERO_W = {"nano": 0.0, "gpt": 0.0, "seed": 0.0,
              "nano_krw": 0.0, "gpt_krw": 0.0, "seed_krw": 0.0}
    for d in combined_daily:
        wkmon = iso_week_monday(d["date"])
        if wkmon not in weeks_acc:
            weeks_acc[wkmon] = {"week_start": wkmon, **ZERO_W}
        for f in ("nano", "gpt", "seed", "nano_krw", "gpt_krw", "seed_krw"):
            weeks_acc[wkmon][f] += d.get(f, 0.0)
    combined_weekly = []
    today_str = today_dt.strftime("%Y-%m-%d")
    for i in range(11, -1, -1):
        wmon = (this_monday - timedelta(weeks=i))
        wkey = wmon.strftime("%Y-%m-%d")
        wsun = wmon + timedelta(days=6)
        wsun_str = wsun.strftime("%Y-%m-%d")
        in_progress = (wkey <= today_str <= wsun_str)
        b = weeks_acc.get(wkey, dict(ZERO_W))
        combined_weekly.append({
            "week_start": wkey,
            "week_end": wsun_str,
            "label": f"{wmon.month}/{wmon.day} – {wsun.month}/{wsun.day}" + (" (진행 중)" if in_progress else ""),
            "in_progress": in_progress,
            "nano": round(b["nano"], 4),
            "gpt":  round(b["gpt"],  4),
            "seed": round(b["seed"], 4),
            "total": round(b["nano"] + b["gpt"] + b["seed"], 4),
            "nano_krw": round(b["nano_krw"], 2),
            "gpt_krw":  round(b["gpt_krw"],  2),
            "seed_krw": round(b["seed_krw"], 2),
            "total_krw": round(b["nano_krw"] + b["gpt_krw"] + b["seed_krw"], 2),
        })

    # Single unified last-refresh timestamp
    sync_ts_candidates = [v.get("last_run_ts") for v in sync.values() if v.get("last_run_ts")]
    last_refresh_ts = max(sync_ts_candidates) if sync_ts_candidates else None
    sync_status = "ok" if all(v.get("status") == "ok" for v in sync.values()) else "error"

    # fx_rate / fx_source 는 main() 상단에서 이미 계산됨 (소스 뷰 생성 시 필요)

    today_dt = datetime.fromisoformat(utc_today_str())
    payload = {
        "schema_version": 9,   # +totals.last_month_to_date (MTD 공정 비교용)
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
            "nanobanana": {**nano, "tag_color": "#7c3aed", "carried_forward": nano_carried,
                           "last_ok_ts": nano_ok_ts},
            "openai":     {**openai, "tag_color": "#10a37f", "carried_forward": oa_carried,
                           "last_ok_ts": oa_ok_ts},
            "seedream":   {**seed, "tag_color": "#2563eb", "carried_forward": seed_carried,
                           "last_ok_ts": seed_ok_ts},
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
