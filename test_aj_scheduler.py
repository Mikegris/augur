#!/usr/bin/env python3
"""Auto-run scheduler tests — the daemon timer that ticks run_scheduled.

Network-free and thread-free: run_once and market_session are injected, so we
exercise the scheduling LOGIC (gating, interval clock, stamp-only-on-success,
tick state) deterministically without spawning the loop or hitting a broker.
"""
import os
import sys
import tempfile

os.environ["AUGUR_DB_PATH"] = tempfile.mktemp(suffix="_ajsched.db")

import database as db          # noqa: E402
import aj_db                    # noqa: E402
import aj_config                # noqa: E402
import aj_autonomy as A         # noqa: E402
import aj_operator              # noqa: E402

aj_db.aj_init()


def _cfg(**over):
    cfg = {"auto_run_enabled": True, "auto_run_interval_min": 10,
           "session_whitelist": ["regular"], "health_autohalt": False}
    cfg.update(over)
    aj_config.set_config(cfg)
    return aj_config.get_config()


def _reset():
    aj_db.set_setting_raw(A._LAST_RUN_KEY, "")
    with A._sched_state_lock:
        A._sched_state.update({"last_tick": None, "last_fire": None,
                               "last_reason": None, "ticks": 0, "fires": 0, "errors": 0})
    # default: a tradable session + a successful cycle
    aj_db.market_session = lambda: "regular"
    aj_operator.run_once = lambda mode="paper": {"ok": True, "cycle_id": "cyc_test",
                                                 "proposals": [{"symbol": "X"}]}


def test_due_when_enabled_session_ok_and_no_prior_run():
    _reset(); cfg = _cfg()
    g = A.due_to_run(cfg)
    assert g["due"] is True, g


def test_not_due_when_disabled():
    _reset(); cfg = _cfg(auto_run_enabled=False)
    assert A.due_to_run(cfg)["due"] is False


def test_not_due_when_session_not_whitelisted():
    _reset()
    aj_db.market_session = lambda: "closed"
    cfg = _cfg(session_whitelist=["regular"])
    g = A.due_to_run(cfg)
    assert g["due"] is False and "not tradable" in g["reason"]


def test_not_due_within_interval():
    _reset(); cfg = _cfg(auto_run_interval_min=10)
    # stamp a run "now" -> 0 minutes elapsed < 10
    aj_db.set_setting_raw(A._LAST_RUN_KEY, aj_db.utc_now_iso())
    g = A.due_to_run(cfg)
    assert g["due"] is False and "since last run" in g["reason"], g


def test_run_scheduled_stamps_on_success():
    _reset(); _cfg()
    assert A.minutes_since_last_run() is None       # clean clock
    out = A.run_scheduled()
    assert out["ran"] is True and out["proposals"] == 1, out
    # clock is now stamped -> immediately not-due again
    assert A.minutes_since_last_run() is not None
    assert A.due_to_run()["due"] is False


def test_run_scheduled_does_not_stamp_when_locked():
    _reset(); _cfg()
    # simulate a manual cycle holding the single-instance lock
    aj_operator.run_once = lambda mode="paper": {"ok": False, "reason": "another cycle is running"}
    out = A.run_scheduled()
    assert out["ran"] is False and "another cycle" in out["reason"], out
    # interval clock must NOT have advanced -> still due so the next tick retries
    assert A.minutes_since_last_run() is None
    assert A.due_to_run()["due"] is True


def test_run_scheduled_blocks_on_health_autohalt():
    _reset(); _cfg(health_autohalt=True)
    A.health_check = lambda cfg=None: {"halted": True, "summary": "audit chain broken"}
    out = A.run_scheduled()
    assert out["ran"] is False and "health auto-halt" in out["reason"], out
    assert A.minutes_since_last_run() is None        # didn't run -> not stamped


def test_tick_updates_state_and_never_raises():
    _reset(); _cfg()
    res = A._scheduler_tick()
    assert res["ran"] is True
    st = A.scheduler_status()
    assert st["ticks"] == 1 and st["fires"] == 1 and st["errors"] == 0
    assert st["last_fire"] is not None and st["last_reason"] == "ran"


def test_tick_swallows_run_scheduled_exception():
    _reset(); _cfg()
    def _boom(mode="paper"):
        raise RuntimeError("broker exploded")
    aj_operator.run_once = _boom
    res = A._scheduler_tick()          # must not raise
    assert res["ran"] is False
    st = A.scheduler_status()
    # run_scheduled swallows the error into reason="error: ..."; tick counts it
    assert st["ticks"] == 1 and st["fires"] == 0 and st["errors"] == 1, st


def test_scheduler_status_shape():
    _reset(); _cfg(auto_run_interval_min=10)
    st = A.scheduler_status()
    for k in ("thread_running", "started", "auto_run_enabled", "interval_min",
              "tick_seconds", "due_now", "ticks", "fires", "errors"):
        assert k in st, "missing " + k
    assert st["auto_run_enabled"] is True and st["interval_min"] == 10


def test_start_scheduler_idempotent():
    # first start spawns; second is a no-op (returns False). Daemon thread exits
    # with the test process; it self-gates so it won't run real cycles here.
    first = A.start_scheduler()
    second = A.start_scheduler()
    assert first is True and second is False
    assert A.scheduler_status()["thread_running"] is True


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    print("aj_scheduler — {} tests".format(len(fns)))
    failed = 0
    for fn in fns:
        try:
            fn()
            print("  [OK] {}".format(fn.__name__))
        except AssertionError as e:
            failed += 1
            print("  [XX] {}: {}".format(fn.__name__, e))
        except Exception as e:
            failed += 1
            print("  [XX] {}: unexpected {}: {}".format(fn.__name__, type(e).__name__, e))
    print("PASS" if failed == 0 else "{} FAILED".format(failed))
    sys.exit(1 if failed else 0)
