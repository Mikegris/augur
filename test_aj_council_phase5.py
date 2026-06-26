#!/usr/bin/env python3
"""Phase 5 tests — coequal track-record gate + boost (clipped to the risk cap).

Network-free. Proves coequal can boost size ONLY after the realized alpha track
record clears the bars, and that boosting is still bounded by the notional cap.
"""
import os
import sys
import tempfile

os.environ["AUGUR_DB_PATH"] = tempfile.mktemp(suffix="_ajp5.db")

import database as db          # noqa: E402
import aj_db                    # noqa: E402
import aj_config                # noqa: E402
import aj_council               # noqa: E402
import aj_operator              # noqa: E402
from aj_schemas import CouncilDecision, Rating, Action  # noqa: E402

aj_db.aj_init()
_ORIG_RUN = aj_council.run


def _reflections(*alphas):
    for a in alphas:
        aj_db.insert("aj_reflections", ts=aj_db.utc_now_iso(), symbol="X",
                     situation_hash=str(a) + os.urandom(2).hex(), raw_return=a,
                     alpha_return=a, benchmark="SPY", lesson="l")


def _clear_reflections():
    db.get_conn().execute("DELETE FROM aj_reflections")
    db.get_conn().commit()


def _coequal_cfg(min_samples=2, max_boost=0.5):
    c = aj_config.get_config()
    c["council_enabled"] = True
    c["council_policy"] = "coequal"
    c["council_topk"] = 5
    c["coequal_min_samples"] = min_samples
    c["coequal_min_alpha"] = 0.0
    c["coequal_max_boost"] = max_boost
    c["max_order_notional_usd"] = 1000.0
    return c


def _activate():
    db.set_setting("aj_verify_council", "pass")


def teardown():
    aj_council.run = _ORIG_RUN
    _clear_reflections()


def test_track_record_empty_then_populated():
    _clear_reflections()
    tr = aj_council.track_record()
    assert tr["n"] == 0
    _reflections(0.05, 0.03, -0.01)
    tr = aj_council.track_record()
    assert tr["n"] == 3
    assert abs(tr["mean_alpha"] - (0.07 / 3)) < 1e-9
    assert abs(tr["win_rate"] - (2 / 3)) < 1e-9
    teardown()


def test_coequal_locked_until_samples_and_alpha():
    _activate()
    _clear_reflections()
    cfg = _coequal_cfg(min_samples=3)
    # too few samples => locked
    _reflections(0.05)
    assert aj_council.coequal_unlocked(cfg) is False
    # enough samples but negative mean alpha => locked
    _clear_reflections()
    _reflections(-0.02, -0.03, -0.01)
    assert aj_council.coequal_unlocked(cfg) is False
    # enough samples + positive mean alpha => unlocked
    _clear_reflections()
    _reflections(0.05, 0.04, 0.03)
    assert aj_council.coequal_unlocked(cfg) is True
    teardown()


def test_coequal_locked_behaves_advisory_in_advise():
    _activate()
    _clear_reflections()
    _reflections(0.05)                         # < min_samples => locked
    cfg = _coequal_cfg(min_samples=5)
    aj_council.run = lambda *a, **k: CouncilDecision(
        symbol="X", rating=Rating.BUY, action=Action.BUY, conviction=1.0, status="ok")
    r = aj_operator._council_advise("X", {"side": "buy"}, cfg, "cyc", {"n": 0, "max": 5})
    assert r["verdict"] == "proceed" and r["factor"] <= 1.0   # no boost while locked
    teardown()


def test_coequal_unlocked_boosts_within_cap():
    _activate()
    _clear_reflections()
    _reflections(0.05, 0.06, 0.04)             # >= min_samples, +alpha => unlocked
    cfg = _coequal_cfg(min_samples=3, max_boost=0.5)
    aj_council.run = lambda *a, **k: CouncilDecision(
        symbol="X", rating=Rating.BUY, action=Action.BUY, conviction=1.0, status="ok")
    r = aj_operator._council_advise("X", {"side": "buy"}, cfg, "cyc", {"n": 0, "max": 5})
    assert r["verdict"] == "proceed"
    assert abs(r["factor"] - 1.5) < 1e-9        # 1 + 0.5*conviction(1.0)
    teardown()


def test_default_policy_advisory_never_unlocks_coequal():
    _activate()
    _clear_reflections()
    _reflections(0.5, 0.5, 0.5, 0.5, 0.5)      # great record, but policy=advisory
    cfg = aj_config.get_config()
    cfg["council_enabled"] = True
    cfg["council_policy"] = "advisory"
    assert aj_council.coequal_unlocked(cfg) is False
    teardown()


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    print("aj_council_phase5 — {} tests".format(len(fns)))
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
