"""Pull usage from GCP Billing Export (BigQuery), OpenAI Admin API, and
BytePlus Billing API (Seedream) into SQLite."""
from __future__ import annotations
import hashlib
import hmac
import time
import traceback
import urllib.parse
from datetime import datetime, timedelta, timezone

import requests
from google.cloud import bigquery
from google.oauth2 import service_account

from db import conn, get_meta, set_meta, update_sync, upsert_usage
from keys import Keys, load

GCP_BILLING_TABLE = (
    "studiofreewillusion-ta.nanobananaTA."
    "gcp_billing_export_resource_v1_01432A_47DF68_7BCECB"
)

# ── BytePlus (Seedream) ──
BP_HOST = "open.byteplusapi.com"
BP_REGION = "ap-southeast-1"
BP_SERVICE = "billing"
BP_VERSION = "2022-01-01"
BP_IMAGE_PRODUCT = "Smart_Drawing_T2I"   # ModelArk 이미지 생성(Seedream) 청구 제품명
BP_BACKFILL_START = "2026-04"            # 최초 1회 백필 시작 월


def _now_ts() -> int:
    return int(time.time())


def collect_gcp(keys: Keys) -> tuple[int, int | None, str | None]:
    """Returns (rows_written, last_data_ts, error_msg)."""
    creds = service_account.Credentials.from_service_account_file(
        str(keys.gcp_sa_path),
        scopes=["https://www.googleapis.com/auth/bigquery"],
    )
    client = bigquery.Client(credentials=creds, project=keys.gcp_project)

    # Match GCP Billing UI semantics exactly:
    #   * include ALL cost_types (regular + tax + adjustment) — tax rows arrive
    #     at month-end; excluding them under-reports the actual invoice amount.
    #   * use SUM(cost) without subtracting credits — this matches the
    #     user's reference query and "Total billed amount" on the invoice.
    #   * convert to USD using each row's currency_conversion_rate (the rate
    #     GCP itself applied for that day's billing).
    # Date bucketing uses 'US/Pacific' to align with GCP billing day boundaries
    # (Korean accounts still bill on Pacific time).
    sql = f"""
    SELECT
      CAST(DATE(usage_start_time, 'America/Los_Angeles') AS STRING) AS day,
      service.description AS service,
      SUM(SAFE_DIVIDE(CAST(cost AS NUMERIC), currency_conversion_rate)) AS cost_usd,
      SUM(SAFE_DIVIDE(CAST(cost AS NUMERIC), currency_conversion_rate)) AS gross_cost
    FROM `{GCP_BILLING_TABLE}`
    WHERE usage_start_time >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 400 DAY)
      AND currency_conversion_rate IS NOT NULL
      AND currency_conversion_rate > 0
    GROUP BY day, service
    ORDER BY day DESC, service
    """

    rows_out = []
    latest_day = None
    for row in client.query(sql).result():
        day = row["day"]
        rows_out.append({
            "source": "gcp",
            "category": row["service"] or "(unknown)",
            "date": day,
            "request_count": None,
            "image_count": None,
            "cost_usd": float(row["cost_usd"] or 0),
            "gross_cost": float(row["gross_cost"] or 0),
        })
        if latest_day is None or day > latest_day:
            latest_day = day

    last_data_ts = None
    if latest_day:
        last_data_ts = int(datetime.fromisoformat(latest_day).replace(tzinfo=timezone.utc).timestamp())

    # Pull the latest currency_conversion_rate (today's GCP-applied USD→local rate)
    fx_rate = None
    try:
        fx_sql = f"""
        SELECT currency, currency_conversion_rate AS rate
        FROM `{GCP_BILLING_TABLE}`
        WHERE currency_conversion_rate IS NOT NULL
          AND currency_conversion_rate > 0
        ORDER BY usage_start_time DESC
        LIMIT 1
        """
        for r in client.query(fx_sql).result():
            fx_rate = float(r["rate"] or 0)
            print(f"[fx] latest GCP rate: {r['currency']}/USD = {fx_rate}")
            break
    except Exception as e:
        print(f"[fx] could not fetch latest rate: {e}")

    with conn() as c:
        n = upsert_usage(c, rows_out)
        update_sync(c, "gcp", _now_ts(), last_data_ts, "ok")
        if fx_rate and fx_rate > 100:  # sanity check (KRW typically 1300-1500)
            set_meta(c, "krw_per_usd", f"{fx_rate:.4f}")
            set_meta(c, "fx_source", "gcp_billing_export")
    return n, last_data_ts, None


def _find_api_key_id(admin_key: str, target_secret: str) -> str | None:
    """Locate the api_key_id whose redacted_value matches the last 4 chars of target_secret.

    Org has many keys (chat / audio / etc.); we want costs filtered to just ours.
    Result is cached in meta table so we don't re-query every cron run.
    """
    suffix = target_secret[-4:]
    headers = {"Authorization": f"Bearer {admin_key}"}
    try:
        projs = requests.get("https://api.openai.com/v1/organization/projects",
                             headers=headers, timeout=30).json().get("data", [])
    except Exception as e:
        print(f"[oa] project list failed: {e}")
        return None
    for p in projs:
        try:
            keys = requests.get(
                f"https://api.openai.com/v1/organization/projects/{p['id']}/api_keys",
                headers=headers, timeout=30).json().get("data", [])
        except Exception:
            continue
        for k in keys:
            redacted = k.get("redacted_value") or ""
            if redacted.endswith(suffix):
                print(f"[oa] resolved api_key_id={k['id']} (name={k.get('name')!r}, project={p['id']})")
                return k["id"]
    print(f"[oa] no api_key matched suffix '{suffix}'")
    return None


def _openai_paginate(endpoint: str, admin_key: str, start: int, end: int,
                     limit: int = 31, group_by: list[str] | None = None,
                     api_key_ids: list[str] | None = None):
    url = f"https://api.openai.com/v1/organization/{endpoint}"
    headers = {"Authorization": f"Bearer {admin_key}"}
    params: dict = {
        "start_time": start,
        "end_time": end,
        "bucket_width": "1d",
        "limit": limit,
    }
    if group_by:
        params["group_by"] = group_by
    if api_key_ids:
        params["api_key_ids"] = api_key_ids
    while True:
        # OpenAI Admin API가 간헐적으로 매우 느려짐(CI에서 60s 초과 관측).
        # 페이지당 3회 재시도 + 90s 타임아웃으로 일시 지연을 흡수.
        last_err = None
        for attempt in range(3):
            try:
                r = requests.get(url, headers=headers, params=params, timeout=90)
                break
            except requests.exceptions.Timeout as e:
                last_err = e
                print(f"[oa] {endpoint} timeout (attempt {attempt+1}/3), retrying...")
        else:
            raise RuntimeError(f"openai {endpoint}: timed out after 3 attempts") from last_err
        if r.status_code != 200:
            raise RuntimeError(f"openai {endpoint} {r.status_code}: {r.text[:300]}")
        body = r.json()
        for bucket in body.get("data", []):
            yield bucket
        if not body.get("has_more"):
            break
        next_page = body.get("next_page")
        if not next_page:
            break
        params["page"] = next_page


def collect_openai(keys: Keys) -> tuple[int, int | None, str | None]:
    # IMPORTANT: end_time MUST be in the future, otherwise OpenAI omits the
    # current (in-progress) day bucket from the response — this is why "today"
    # showed $0 even though the official OpenAI dashboard had usage. Using
    # +24h ensures the partial today bucket is included.
    end = _now_ts() + 24 * 3600
    start = _now_ts() - 400 * 24 * 3600

    # Resolve & cache the api_key_id matching keys.openai_api. Cache is keyed
    # by the secret's last-4-suffix so rotating the OpenAI key auto-invalidates.
    suffix = keys.openai_api[-4:]
    cache_key = f"openai_api_key_id__{suffix}"
    with conn() as c:
        cached_id = get_meta(c, cache_key)
    api_key_id = cached_id
    if not api_key_id:
        api_key_id = _find_api_key_id(keys.openai_admin, keys.openai_api)
        if api_key_id:
            with conn() as c:
                set_meta(c, cache_key, api_key_id)
    key_filter = [api_key_id] if api_key_id else None
    print(f"[oa] filtering costs by api_key_id={api_key_id or '(none — fallback to org-wide)'}")

    # OpenAI's api_key_ids filter only works for time ranges starting on or after
    # ~2025-12-05. Clamp start when we're using the filter. (GPT Image 2 launched
    # 2026-04-21 so we lose no relevant data.)
    if key_filter:
        MIN_START_WITH_FILTER = 1764979200  # 2025-12-05 UTC
        if start < MIN_START_WITH_FILTER:
            start = MIN_START_WITH_FILTER
            print(f"[oa] clamped start to {datetime.fromtimestamp(start, tz=timezone.utc):%Y-%m-%d} (api_key filter limit)")

    # cost_by_line[(date, line_item)] = cost
    cost_by_line: dict[tuple[str, str], float] = {}
    # images_by_date[date] = (images, requests)
    images_by_date: dict[str, tuple[int, int]] = {}
    last_data_ts = None

    for bucket in _openai_paginate("costs", keys.openai_admin, start, end,
                                   limit=180, group_by=["line_item"],
                                   api_key_ids=key_filter):
        ts = bucket.get("start_time")
        if ts is None:
            continue
        date = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
        for res in bucket.get("results", []):
            line_item = res.get("line_item") or "(unknown)"
            money = res.get("amount") or {}
            amt = float(money.get("value") or 0)
            if amt == 0:
                continue
            key = (date, line_item)
            cost_by_line[key] = cost_by_line.get(key, 0.0) + amt
        if last_data_ts is None or ts > last_data_ts:
            last_data_ts = ts

    # usage/images는 대시보드에서 안 쓰는 부가 데이터(카운트 부정확)인데,
    # 이 엔드포인트가 CI에서 60초 타임아웃 나며 collect_openai 전체를 죽여
    # GPT 비용이 $0으로 발행된 사고가 있었음(2026-07-21). 실패해도 costs
    # 수집 결과는 살리도록 격리 + 어떤 예외도 무시.
    try:
        for bucket in _openai_paginate("usage/images", keys.openai_admin, start, end,
                                       api_key_ids=key_filter):
            ts = bucket.get("start_time")
            if ts is None:
                continue
            date = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
            imgs = 0
            reqs = 0
            for res in bucket.get("results", []):
                imgs += int(res.get("images") or 0)
                reqs += int(res.get("num_model_requests") or 0)
            prev = images_by_date.get(date, (0, 0))
            images_by_date[date] = (prev[0] + imgs, prev[1] + reqs)
    except Exception as e:
        print(f"[oa] usage/images skipped (non-critical): {type(e).__name__}: {e}")

    rows_out: list[dict] = []
    # one row per (date, line_item) with cost only
    for (date, line_item), cost in cost_by_line.items():
        rows_out.append({
            "source": "openai",
            "category": line_item,
            "date": date,
            "request_count": None,
            "image_count": None,
            "cost_usd": cost,
            "gross_cost": cost,
        })
    # separate row per date carrying image counts (no cost — already in line items)
    for date, (imgs, reqs) in images_by_date.items():
        if imgs == 0 and reqs == 0:
            continue
        rows_out.append({
            "source": "openai",
            "category": "_images_count",
            "date": date,
            "request_count": reqs,
            "image_count": imgs,
            "cost_usd": 0.0,
            "gross_cost": 0.0,
        })

    with conn() as c:
        n = upsert_usage(c, rows_out)
        update_sync(c, "openai", _now_ts(), last_data_ts, "ok")
    return n, last_data_ts, None


def _bp_sign_and_get(ak: str, sk: str, action: str, query: dict) -> dict:
    """BytePlus OpenAPI V4 서명(HMAC-SHA256) GET 호출."""
    now = datetime.now(timezone.utc)
    x_date = now.strftime("%Y%m%dT%H%M%SZ")
    short_date = x_date[:8]

    params = {"Action": action, "Version": BP_VERSION}
    params.update(query)
    cq = "&".join(
        f"{urllib.parse.quote(k, safe='-_.~')}={urllib.parse.quote(str(v), safe='-_.~')}"
        for k, v in sorted(params.items())
    )
    payload_hash = hashlib.sha256(b"").hexdigest()
    content_type = "application/x-www-form-urlencoded"
    canonical_headers = (
        f"content-type:{content_type}\nhost:{BP_HOST}\n"
        f"x-content-sha256:{payload_hash}\nx-date:{x_date}\n"
    )
    signed_headers = "content-type;host;x-content-sha256;x-date"
    canonical_request = "\n".join(["GET", "/", cq, canonical_headers, signed_headers, payload_hash])
    scope = f"{short_date}/{BP_REGION}/{BP_SERVICE}/request"
    string_to_sign = "\n".join(
        ["HMAC-SHA256", x_date, scope, hashlib.sha256(canonical_request.encode()).hexdigest()]
    )
    key = sk.encode()
    for part in (short_date, BP_REGION, BP_SERVICE, "request"):
        key = hmac.new(key, part.encode(), hashlib.sha256).digest()
    signature = hmac.new(key, string_to_sign.encode(), hashlib.sha256).hexdigest()

    r = requests.get(
        f"https://{BP_HOST}/?{cq}",
        headers={
            "Content-Type": content_type,
            "X-Date": x_date,
            "X-Content-Sha256": payload_hash,
            "Authorization": (
                f"HMAC-SHA256 Credential={ak}/{scope}, "
                f"SignedHeaders={signed_headers}, Signature={signature}"
            ),
        },
        timeout=60,
    )
    if r.status_code != 200:
        raise RuntimeError(f"byteplus {action} {r.status_code}: {r.text[:300]}")
    return r.json()


def _bp_month_rows(ak: str, sk: str, month: str) -> tuple[list[dict], int | None]:
    """한 달치 Seedream(이미지 생성) 청구 라인 → (date, model)별 usage_daily 행."""
    agg: dict[tuple[str, str], dict] = {}
    latest_date = None
    offset, limit, total = 0, 300, 0
    while True:
        body = _bp_sign_and_get(ak, sk, "ListSplitBillDetail", {
            "BillPeriod": month, "Limit": str(limit), "Offset": str(offset),
            "GroupTerm": "0", "GroupPeriod": "2", "NeedRecordNum": "1",
        })
        result = body.get("Result") or {}
        items = result.get("List") or []
        total = result.get("Total") or 0
        for it in items:
            if (it.get("Product") or "") != BP_IMAGE_PRODUCT:
                continue
            d_raw = str(it.get("ExpenseTime") or "")[:10]
            date = d_raw if len(d_raw) == 10 and d_raw[:4].isdigit() else f"{month}-01"
            model = it.get("ConfigName") or "(unknown)"
            key = (date, model)
            if key not in agg:
                agg[key] = {"cost": 0.0, "pretax": 0.0, "count": 0}
            agg[key]["cost"] += float(it.get("PosttaxAmount") or 0)      # 실청구(세후)
            agg[key]["pretax"] += float(it.get("PretaxAmount") or 0)
            agg[key]["count"] += 1
            if latest_date is None or date > latest_date:
                latest_date = date
        offset += limit
        if offset >= total or not items:
            break

    rows = [{
        "source": "byteplus",
        "category": model,
        "date": date,
        "request_count": v["count"],
        "image_count": None,
        "cost_usd": round(v["cost"], 6),
        "gross_cost": round(v["pretax"], 6),
    } for (date, model), v in agg.items()]

    last_ts = None
    if latest_date:
        last_ts = int(datetime.fromisoformat(latest_date).replace(tzinfo=timezone.utc).timestamp())
    return rows, last_ts


def collect_byteplus(keys: Keys) -> tuple[int, int | None, str | None]:
    """Seedream (BytePlus ModelArk 이미지 생성) 비용 수집.

    매 실행: 당월(+월초 3일까지는 전월) 재조회 — BytePlus 분할청구 귀속이
    ~1일 지연되므로 재조회로 과거 값이 자동 교정됨. 이전 달들은 DB에 남은
    값 유지(청구 확정 후 불변). 최초 1회는 BP_BACKFILL_START부터 백필.
    """
    if not (keys.byteplus_ak and keys.byteplus_sk):
        print("[bp] no BytePlus keys — skipped")
        with conn() as c:
            update_sync(c, "byteplus", _now_ts(), None, "ok", "skipped: no keys")
        return 0, None, None

    now = datetime.now(timezone.utc) + timedelta(hours=8)  # BytePlus 청구일 경계(UTC+8) 근사
    cur_month = now.strftime("%Y-%m")
    months = [cur_month]
    if now.day <= 3:
        prev = (now.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
        months.insert(0, prev)

    with conn() as c:
        backfilled = get_meta(c, "bp_backfilled")
    if not backfilled:
        y, m = int(BP_BACKFILL_START[:4]), int(BP_BACKFILL_START[5:7])
        months = []
        while (y, m) <= (now.year, now.month):
            months.append(f"{y}-{m:02d}")
            m += 1
            if m > 12:
                m, y = 1, y + 1
        print(f"[bp] first run — backfilling {months[0]}..{months[-1]}")

    rows_all: list[dict] = []
    last_ts = None
    for month in months:
        rows, ts = _bp_month_rows(keys.byteplus_ak, keys.byteplus_sk, month)
        rows_all.extend(rows)
        if ts and (last_ts is None or ts > last_ts):
            last_ts = ts

    with conn() as c:
        n = upsert_usage(c, rows_all)
        update_sync(c, "byteplus", _now_ts(), last_ts, "ok")
        if not backfilled:
            set_meta(c, "bp_backfilled", "1")
    return n, last_ts, None


def run() -> dict:
    keys = load()
    out = {}
    for name, fn in (("gcp", collect_gcp), ("openai", collect_openai), ("byteplus", collect_byteplus)):
        try:
            n, dts, _ = fn(keys)
            out[name] = {"rows": n, "last_data_ts": dts, "status": "ok"}
            print(f"[{name}] ok rows={n} last_data_ts={dts}")
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            print(f"[{name}] ERROR {err}")
            traceback.print_exc()
            with conn() as c:
                update_sync(c, name, _now_ts(), None, "error", err[:500])
            out[name] = {"rows": 0, "status": "error", "error": err}
    return out


if __name__ == "__main__":
    run()
