"""Pull usage from GCP Billing Export (BigQuery) and OpenAI Admin API into SQLite."""
from __future__ import annotations
import time
import traceback
from datetime import datetime, timezone

import requests
from google.cloud import bigquery
from google.oauth2 import service_account

from db import conn, set_meta, update_sync, upsert_usage
from keys import Keys, load

GCP_BILLING_TABLE = (
    "studiofreewillusion-ta.nanobananaTA."
    "gcp_billing_export_resource_v1_01432A_47DF68_7BCECB"
)


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


def _openai_paginate(endpoint: str, admin_key: str, start: int, end: int,
                     limit: int = 31, group_by: list[str] | None = None):
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
    while True:
        r = requests.get(url, headers=headers, params=params, timeout=60)
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

    # cost_by_line[(date, line_item)] = cost
    cost_by_line: dict[tuple[str, str], float] = {}
    # images_by_date[date] = (images, requests)
    images_by_date: dict[str, tuple[int, int]] = {}
    last_data_ts = None

    for bucket in _openai_paginate("costs", keys.openai_admin, start, end,
                                   limit=180, group_by=["line_item"]):
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

    for bucket in _openai_paginate("usage/images", keys.openai_admin, start, end):
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


def run() -> dict:
    keys = load()
    out = {}
    for name, fn in (("gcp", collect_gcp), ("openai", collect_openai)):
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
