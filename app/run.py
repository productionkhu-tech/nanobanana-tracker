"""Single scheduler entrypoint: collect → build → push.

Designed to be tolerant: if any one stage fails, log and continue —
we still want to publish whatever data we have."""
from __future__ import annotations
import sys
import time
import traceback

from build import main as build_main
from collector import run as collect_run
from push import main as push_main

LOG = sys.stdout


def stage(name: str, fn) -> bool:
    t0 = time.time()
    print(f"=== {name} ===", flush=True)
    try:
        fn()
        print(f"--- {name} ok ({time.time()-t0:.1f}s) ---", flush=True)
        return True
    except Exception as e:
        print(f"!!! {name} FAILED: {type(e).__name__}: {e}", flush=True)
        traceback.print_exc()
        return False


def main() -> int:
    ok_collect = stage("collect", collect_run)
    # Unify sync timestamps — both sources share the same "last refresh" moment.
    try:
        import sqlite3
        from db import DB_PATH
        ts = int(time.time())
        con = sqlite3.connect(str(DB_PATH))
        con.execute("UPDATE sync_log SET last_run_ts = ? WHERE status='ok'", (ts,))
        con.commit(); con.close()
    except Exception as e:
        print(f"[sync-unify] skipped: {e}", flush=True)
    ok_build = stage("build", build_main)
    ok_push = stage("push", push_main)
    return 0 if (ok_build and ok_push) else 1


if __name__ == "__main__":
    sys.exit(main())
