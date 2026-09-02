#!/usr/bin/env python3
"""WAL watchdog tests — cache_warmer._wal_guard.

The guard keeps the main wealth.db WAL bounded and ALARMS (log.error + audit)
when a hung reader pins it (the 7.5 GB silent-bloat failure). Network-free:
uses a temp DB and monkeypatches os.path.getsize for the alarm path.
"""
import os
import sys
import tempfile

os.environ["AUGUR_DB_PATH"] = tempfile.mktemp(suffix="_walg.db")

import database as db          # noqa: E402
import aj_db                   # noqa: E402
import cache_warmer as cw      # noqa: E402

aj_db.aj_init()
db.get_conn().execute("CREATE TABLE IF NOT EXISTS t(x)")
db.get_conn().execute("INSERT INTO t VALUES (1)")
db.get_conn().commit()

_FAILS = []


def check(cond, msg):
    print(("  ok: " if cond else "  FAIL: ") + msg)
    if not cond:
        _FAILS.append(msg)


def test_normal_path_no_alarm():
    audits = []
    orig = aj_db.audit
    aj_db.audit = lambda kind, payload=None, **k: audits.append((kind, payload))
    try:
        cw._wal_guard()               # small WAL -> no alarm, no raise
    finally:
        aj_db.audit = orig
    check(not any(k == "alert" for k, _ in audits),
          "normal path: small WAL raises no wal_bloat alert")


def test_alarm_when_reader_pins():
    audits = []
    orig_audit, orig_getsize = aj_db.audit, cw.os.path.getsize
    aj_db.audit = lambda kind, payload=None, **k: audits.append((kind, payload))
    cw.os.path.getsize = lambda p: cw.WAL_WARN_BYTES + 1   # WAL stays huge post-checkpoint
    try:
        cw._wal_guard()
    finally:
        cw.os.path.getsize = orig_getsize
        aj_db.audit = orig_audit
    fired = [p for k, p in audits if k == "alert" and (p or {}).get("kind") == "wal_bloat"]
    check(len(fired) == 1, "alarm path: a pinned WAL fires exactly one wal_bloat audit")
    check(bool(fired) and fired[0].get("wal_mb", 0) >= cw.WAL_WARN_BYTES // 1024 // 1024,
          "alarm payload reports the WAL size in MB")


def test_never_raises_on_bad_db():
    # A checkpoint failure must degrade, not kill the warmer thread.
    orig = db.get_conn
    db.get_conn = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    try:
        cw._wal_guard()               # must swallow and return
        check(True, "never raises when the DB connection errors")
    except Exception as e:
        check(False, "raised on bad DB: {}".format(e))
    finally:
        db.get_conn = orig


def main():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for t in tests:
        print(t.__name__)
        t()
    print("\n{} checks failed".format(len(_FAILS)) if _FAILS
          else "\nALL PASSED ({} tests)".format(len(tests)))
    sys.exit(1 if _FAILS else 0)


if __name__ == "__main__":
    main()
