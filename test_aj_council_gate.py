#!/usr/bin/env python3
"""Phase 3 tests — council advisory contract in aj_operator._council_advise.

Proves the HARD invariant: the council can VETO or SHRINK a BUY, never create a
buy, never boost size >1.0, never gate a sell, and always fails OPEN (proceed
rule-based) when inactive/over-budget/errored. The authoritative risk gate is
untouched (not exercised here — these tests isolate the advisory decision).
"""
import os
import sys
import tempfile

os.environ["AUGUR_DB_PATH"] = tempfile.mktemp(suffix="_ajgate.db")

import database as db          # noqa: E402
import aj_db                    # noqa: E402
import aj_config                # noqa: E402
import aj_operator              # noqa: E402
import aj_council               # noqa: E402
from aj_schemas import CouncilDecision, Rating, Action  # noqa: E402

aj_db.aj_init()

_BUY = {"side": "buy", "thesis": "rule buy"}
_SELL = {"side": "sell", "thesis": "rule sell"}


def _cfg(policy="advisory", topk=3, enabled=True):
    c = aj_config.get_config()
    c["council_enabled"] = enabled
    c["council_policy"] = policy
    c["council_topk"] = topk
    return c


def _patch_run(dec):
    aj_council.run = lambda symbol, cfg=None, cycle_id=None, force=False, call=None: dec


_ORIG_RUN = aj_council.run


def _activate(on=True):
    db.set_setting("aj_verify_council", "pass" if on else "no")


def teardown():
    aj_council.run = _ORIG_RUN


def test_sell_never_gated_even_when_active():
    _activate(True)
    budget = {"n": 0, "max": 3}
    # even if council would say SELL, a rule SELL proceeds untouched
    _patch_run(CouncilDecision(symbol="X", rating=Rating.SELL, action=Action.SELL, conviction=1.0, status="ok"))
    r = aj_operator._council_advise("X", _SELL, _cfg(), "cyc", budget)
    assert r["verdict"] == "proceed" and r["factor"] == 1.0
    assert budget["n"] == 0          # council not even consulted for a sell
    teardown()


def test_inactive_proceeds_without_consulting():
    _activate(False)   # VERIFY-COUNCIL not passed => not active
    called = {"n": 0}
    aj_council.run = lambda *a, **k: called.__setitem__("n", called["n"] + 1) or CouncilDecision(symbol="X")
    r = aj_operator._council_advise("X", _BUY, _cfg(enabled=True), "cyc", {"n": 0, "max": 3})
    assert r["verdict"] == "proceed" and r["factor"] == 1.0
    assert called["n"] == 0          # never ran the council
    teardown()


def test_advisory_vetoes_bearish_trims_neutral():
    _activate(True)
    # actively bearish (SELL) -> veto
    _patch_run(CouncilDecision(symbol="X", rating=Rating.SELL, action=Action.SELL, conviction=0.9, status="ok"))
    r = aj_operator._council_advise("X", _BUY, _cfg("advisory"), "cyc", {"n": 0, "max": 3})
    assert r["verdict"] == "veto", r
    # neutral HOLD -> PROCEED at the trim factor (not veto) with the default 0.5
    _patch_run(CouncilDecision(symbol="X", rating=Rating.HOLD, action=Action.HOLD, conviction=0.2, status="ok"))
    r = aj_operator._council_advise("X", _BUY, _cfg("advisory"), "cyc", {"n": 0, "max": 3})
    assert r["verdict"] == "proceed" and abs(r["factor"] - 0.5) < 1e-9, r
    # legacy strict: council_hold_size_factor=0 restores HOLD == veto
    cfg0 = _cfg("advisory"); cfg0["council_hold_size_factor"] = 0.0
    _patch_run(CouncilDecision(symbol="X", rating=Rating.HOLD, action=Action.HOLD, conviction=0.2, status="ok"))
    r = aj_operator._council_advise("X", _BUY, cfg0, "cyc", {"n": 0, "max": 3})
    assert r["verdict"] == "veto", r
    teardown()


def test_advisory_shrinks_on_low_conviction_never_boosts():
    _activate(True)
    _patch_run(CouncilDecision(symbol="X", rating=Rating.BUY, action=Action.BUY, conviction=0.0, status="ok"))
    r = aj_operator._council_advise("X", _BUY, _cfg("advisory"), "cyc", {"n": 0, "max": 3})
    assert r["verdict"] == "proceed" and r["factor"] == 0.5   # low conv -> half size
    _patch_run(CouncilDecision(symbol="X", rating=Rating.BUY, action=Action.BUY, conviction=1.0, status="ok"))
    r = aj_operator._council_advise("X", _BUY, _cfg("advisory"), "cyc", {"n": 0, "max": 3})
    assert r["verdict"] == "proceed" and r["factor"] == 1.0   # high conv -> full (never >1)
    assert r["factor"] <= 1.0
    teardown()


def test_confirm_requires_council_buy():
    _activate(True)
    _patch_run(CouncilDecision(symbol="X", rating=Rating.HOLD, action=Action.HOLD, conviction=0.5, status="ok"))
    r = aj_operator._council_advise("X", _BUY, _cfg("confirm"), "cyc", {"n": 0, "max": 3})
    assert r["verdict"] == "veto"
    _patch_run(CouncilDecision(symbol="X", rating=Rating.BUY, action=Action.BUY, conviction=0.6, status="ok"))
    r = aj_operator._council_advise("X", _BUY, _cfg("confirm"), "cyc", {"n": 0, "max": 3})
    assert r["verdict"] == "proceed" and r["factor"] == 1.0
    teardown()


def test_coequal_does_not_boost_before_phase5():
    _activate(True)
    _patch_run(CouncilDecision(symbol="X", rating=Rating.BUY, action=Action.BUY, conviction=1.0, status="ok"))
    r = aj_operator._council_advise("X", _BUY, _cfg("coequal"), "cyc", {"n": 0, "max": 3})
    assert r["factor"] <= 1.0          # coequal cannot grow the order yet (Phase 5 gate)
    teardown()


def test_budget_exhaustion_proceeds_rule_based():
    _activate(True)
    called = {"n": 0}
    aj_council.run = lambda *a, **k: called.__setitem__("n", called["n"] + 1) or CouncilDecision(
        symbol="X", rating=Rating.SELL, action=Action.SELL, conviction=1.0, status="ok")
    budget = {"n": 3, "max": 3}       # already exhausted
    r = aj_operator._council_advise("X", _BUY, _cfg(topk=3), "cyc", budget)
    assert r["verdict"] == "proceed"  # not vetoed despite council SELL — budget spent
    assert called["n"] == 0
    teardown()


def test_council_error_fails_open():
    _activate(True)
    def boom(*a, **k):
        raise RuntimeError("council blew up")
    aj_council.run = boom
    r = aj_operator._council_advise("X", _BUY, _cfg(), "cyc", {"n": 0, "max": 3})
    assert r["verdict"] == "proceed" and r["factor"] == 1.0   # error => rule-based
    teardown()


def test_council_skipped_status_fails_open():
    _activate(True)
    _patch_run(CouncilDecision(symbol="X", status="skipped"))
    r = aj_operator._council_advise("X", _BUY, _cfg(), "cyc", {"n": 0, "max": 3})
    assert r["verdict"] == "proceed"
    teardown()


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    print("aj_council_gate — {} tests".format(len(fns)))
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
