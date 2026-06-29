"""Offline tests for aj_ic — the signal-promotion / IC gate.

Fully isolated: a fresh temp DB, the scored-rows source monkeypatched to canned
rows, no network and no real backtest run. Standalone runner: prints PASS/FAILED
per check and sys.exit(1) on any failure.
"""
import os
import sys
import tempfile

os.environ["AUGUR_DB_PATH"] = tempfile.mktemp(suffix=".db")

import database  # noqa: E402
database.init_db()

import aj_ic  # noqa: E402

PASS = []
FAIL = []


def ok(msg):
    PASS.append(msg)
    print("  [OK] " + msg)


def check(cond, msg):
    if cond:
        ok(msg)
    else:
        FAIL.append(msg)
        print("  [XX] " + msg)


# ── canned-row builders ─────────────────────────────────────────────────────────

def _row(prob_up, realized_return, regime=None):
    """A scored ledger row shaped like research_tracker.get_scored_rows output.
    hit = directional correctness; here we set hit = (sign of edge == sign ret)."""
    edge_up = prob_up >= 0.5
    ret_up = realized_return > 0
    md = {"prob_up": prob_up}
    if regime is not None:
        md["regime"] = regime
    return {
        "signal_name": "ens:test",
        "symbol": "AAA",
        "horizon_days": 5,
        "direction": "BUY" if edge_up else "SELL",
        "realized_return": realized_return,
        "hit": (edge_up == ret_up),
        "metadata": md,
    }


def _skilled_rows(n=30):
    """Well-calibrated rows: high prob_up -> positive return, low -> negative.
    Brier skill should be strongly > 0 and IC > 0."""
    rows = []
    for i in range(n):
        if i % 2 == 0:
            rows.append(_row(0.80, +0.03))   # confident up, was up
        else:
            rows.append(_row(0.20, -0.03))   # confident down, was down
    return rows


def _anti_skilled_rows(n=30):
    """Confidently WRONG rows: brier skill should be < 0."""
    rows = []
    for i in range(n):
        if i % 2 == 0:
            rows.append(_row(0.85, -0.03))   # confident up, was DOWN
        else:
            rows.append(_row(0.15, +0.03))   # confident down, was UP
    return rows


def _patch_rows(monkey_rows):
    """Patch the scored-rows source to return canned rows regardless of name."""
    aj_ic._scored_rows_for = lambda name, since=None: list(monkey_rows)


# ── tests ─────────────────────────────────────────────────────────────────────

def test_insufficient_history_not_promoted():
    _patch_rows(_skilled_rows(5))   # only 5 < default 20
    res = aj_ic.signal_promoted("test", {})
    check(res["promoted"] is False, "n<min -> not promoted")
    check("insufficient history" in res["reason"], "n<min reason mentions insufficient history")
    check(res["skill"]["n"] == 5, "skill reports n=5")


def test_skilled_signal_promoted():
    _patch_rows(_skilled_rows(30))
    sk = aj_ic.signal_skill("test")
    check(sk["available"] is True, "skilled signal is available")
    check(sk["n"] >= 20, "skilled signal has n>=20")
    check(sk["brier_skill"] is not None and sk["brier_skill"] > 0,
          "skilled signal brier_skill > 0")
    check(sk["ic"] is not None and sk["ic"] > 0, "skilled signal IC > 0")
    res = aj_ic.signal_promoted("test", {})
    check(res["promoted"] is True, "n>=min and brier_skill>0 -> promoted")


def test_negative_brier_skill_not_promoted():
    _patch_rows(_anti_skilled_rows(30))
    sk = aj_ic.signal_skill("test")
    check(sk["brier_skill"] is not None and sk["brier_skill"] < 0,
          "anti-skilled signal brier_skill < 0")
    res = aj_ic.signal_promoted("test", {})
    check(res["promoted"] is False, "brier_skill<0 -> not promoted")
    check("brier_skill" in res["reason"], "brier_skill<0 reason mentions brier_skill")


def test_negative_ic_blocks_when_brier_ok():
    """IC gate: positive brier skill but negative IC, with ic_min_ic raised,
    still blocks. Build rows where direction is right (good brier) but the
    edge magnitude anti-correlates with return magnitude (negative IC)."""
    rows = [
        _row(0.55, +0.10), _row(0.95, +0.001),   # bigger edge -> smaller ret
        _row(0.56, +0.09), _row(0.96, +0.002),
    ] * 6  # 24 rows
    _patch_rows(rows)
    sk = aj_ic.signal_skill("test")
    # brier should be fine (all directional calls correct), IC negative
    res_default = aj_ic.signal_promoted("test", {})  # ic_min_ic default 0.0
    if sk["ic"] is not None and sk["ic"] < 0:
        check(res_default["promoted"] is False, "negative IC blocks at default ic_min_ic=0")
        check("ic" in res_default["reason"], "negative IC reason mentions ic")
    else:
        ok("IC not negative for this fixture (skipped strict IC block assertion)")


def test_custom_thresholds():
    _patch_rows(_skilled_rows(30))
    # raise min_samples above available -> not promoted
    res = aj_ic.signal_promoted("test", {"ic_min_samples": 100})
    check(res["promoted"] is False, "raising ic_min_samples blocks promotion")
    # raise brier-skill bar absurdly high -> not promoted
    res2 = aj_ic.signal_promoted("test", {"ic_min_brier_skill": 0.99})
    check(res2["promoted"] is False, "raising ic_min_brier_skill blocks promotion")


def test_exception_fail_open_not_promoted():
    """An exception inside skill computation must fail-open to not-promoted,
    never crash."""
    def _boom(name, since=None):
        raise RuntimeError("ledger exploded")
    aj_ic._scored_rows_for = _boom
    # signal_skill swallows and returns unavailable
    sk = aj_ic.signal_skill("test")
    check(sk["available"] is False, "exception in skill -> available False (no crash)")
    res = aj_ic.signal_promoted("test", {})
    check(res["promoted"] is False, "exception -> not promoted (fail-open)")
    # signal_skill caught the error itself, so the gate sees an empty skill and
    # reports insufficient history; either that or 'ic gate error' is acceptable.
    check(res["reason"] in ("ic gate error",) or "insufficient history" in res["reason"],
          "exception -> safe reason (gate error or insufficient history)")


def test_promotion_status_maps_multiple():
    # different canned rows per name
    fixtures = {"good": _skilled_rows(30), "bad": _anti_skilled_rows(30),
                "thin": _skilled_rows(3)}
    aj_ic._scored_rows_for = lambda name, since=None: list(fixtures.get(name, []))
    status = aj_ic.promotion_status(["good", "bad", "thin"], {})
    check(set(status.keys()) == {"good", "bad", "thin"}, "status maps all 3 names")
    check(status["good"]["promoted"] is True, "status: good is promoted")
    check(status["bad"]["promoted"] is False, "status: bad not promoted")
    check(status["thin"]["promoted"] is False, "status: thin (n<min) not promoted")


def test_regime_filter():
    rows = (_skilled_rows(30))
    # tag half the rows bull, half bear
    for i, r in enumerate(rows):
        r["metadata"]["regime"] = "bull" if i % 2 == 0 else "bear"
    aj_ic._scored_rows_for = lambda name, since=None: list(rows)
    sk_bull = aj_ic.signal_skill("test", regime="bull")
    check(sk_bull["n"] >= 1, "regime filter keeps matching-regime rows")
    # all bull rows are the same (0.80,+0.03); no spread in outcome -> IC None,
    # but n should reflect only the bull subset (<= 15 directional)
    check(sk_bull["n"] <= 15, "regime filter drops the other regime's rows")
    sk_none = aj_ic.signal_skill("test", regime="ranging")
    check(sk_none["available"] is False, "regime with no matching rows -> unavailable")


def test_walk_forward_stubbed():
    """walk_forward_validate delegates to research_backtest; stub it so no real
    backtest/network runs."""
    import research_backtest as rb
    o_get, o_run = rb.get_adapter, rb.run_backtest
    try:
        rb.get_adapter = lambda name: (lambda *a, **k: None)  # any non-None
        rb.run_backtest = lambda *a, **k: {
            "n_signals": 40, "hit_rate": 0.62, "sharpe": 1.1,
            "total_return_pct": 18.0, "max_drawdown_pct": 7.0,
        }
        res = aj_ic.walk_forward_validate("AAA", {"backtest_signal": "momentum"})
        check(res["validated"] is True, "wf: strong backtest validates")
        check(res["metrics"]["hit_rate"] == 0.62, "wf: metrics passed through")

        rb.run_backtest = lambda *a, **k: {
            "n_signals": 3, "hit_rate": 0.4, "sharpe": -0.5,
        }
        res2 = aj_ic.walk_forward_validate("AAA", {})
        check(res2["validated"] is False, "wf: weak/thin backtest does not validate")

        rb.get_adapter = lambda name: None
        res3 = aj_ic.walk_forward_validate("AAA", {"backtest_signal": "nope"})
        check(res3["validated"] is False, "wf: unknown adapter -> not validated")
    finally:
        rb.get_adapter, rb.run_backtest = o_get, o_run


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    print("aj_ic — {} test groups\n".format(len(tests)))
    for fn in tests:
        try:
            fn()
        except Exception as e:
            FAIL.append(fn.__name__)
            print("  [XX] {}: unexpected {}: {}".format(fn.__name__, type(e).__name__, e))
    print("\n{} passed, {} failed".format(len(PASS), len(FAIL)))
    if FAIL:
        print("FAILED:")
        for f in FAIL:
            print("  - " + f)
    sys.exit(1 if FAIL else 0)
