"""Offline tests for aj_allocate — risk-aware portfolio construction.

Fully isolated: fresh temp DB, all market data / optimizer calls monkeypatched,
no network. Standalone runner: `./venv/bin/python test_aj_allocate.py`.
Prints PASS/FAILED and exits non-zero on any failure.
"""
import os
import sys
import tempfile

os.environ["AUGUR_DB_PATH"] = tempfile.mktemp(suffix=".db")

import database  # noqa: E402
database.init_db()
import aj_db  # noqa: E402
aj_db.aj_migrate()

import research_optimizer as RO  # noqa: E402
import aj_alpha as A  # noqa: E402
import aj_allocate as AL  # noqa: E402

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


def approx(a, b, tol=1e-6):
    return abs(float(a) - float(b)) <= tol


# ── canned-optimizer plumbing ──────────────────────────────────────────────────

def _canned_risk_parity(weights):
    def _fn(symbols, period="2y"):
        used = [s.upper() for s in symbols]
        return {"symbols": used,
                "weights": {s: weights.get(s, 0.0) for s in used},
                "converged": True}
    return _fn


def _canned_markowitz(weights):
    def _fn(symbols, objective="max_sharpe", constraints=None):
        used = [s.upper() for s in symbols]
        return {"symbols": used,
                "weights": {s: weights.get(s, 0.0) for s in used},
                "converged": True}
    return _fn


def _reset():
    RO.risk_parity = _canned_risk_parity({})
    RO.markowitz_optimize = _canned_markowitz({})
    AL._sector_of = lambda s: None
    A.account_equity = lambda *a, **k: 0.0
    AL._closes = lambda s, p="6mo": []
    # drop the per-cycle returns memo so each test's _closes patch is honored
    if hasattr(AL, "reset_cycle_cache"):
        AL.reset_cycle_cache()


# ── tests ───────────────────────────────────────────────────────────────────

def test_equal_method():
    _reset()
    w = AL.target_weights(["AAA", "BBB", "CCC", "DDD"],
                          {"alloc_method": "equal", "max_position_weight": 0.0,
                           "max_sector_weight": 0.0})
    check(len(w) == 4 and all(approx(v, 0.25) for v in w.values()),
          "equal method gives 1/N")
    check(approx(sum(w.values()), 1.0), "equal weights sum to 1")


def test_risk_parity_delegation_and_renorm():
    _reset()
    # canned RP weights that do NOT sum to 1 — module must renormalize
    RO.risk_parity = _canned_risk_parity({"AAA": 0.2, "BBB": 0.2, "CCC": 0.2})
    w = AL.target_weights(["AAA", "BBB", "CCC"],
                          {"alloc_method": "risk_parity",
                           "max_position_weight": 0.0, "max_sector_weight": 0.0})
    check(approx(sum(w.values()), 1.0, 1e-3),
          "risk_parity weights renormalized to 1")
    check(all(approx(v, 1.0 / 3, 1e-3) for v in w.values()),
          "risk_parity equal canned -> 1/3 each after renorm")


def test_max_position_cap():
    _reset()
    # AAA wildly over the cap. 4 names @ cap 0.25 IS feasible to sum to 1, so
    # the excess on AAA is redistributed onto the others and AAA pins at 0.25.
    RO.risk_parity = _canned_risk_parity(
        {"AAA": 0.7, "BBB": 0.1, "CCC": 0.1, "DDD": 0.1})
    w = AL.target_weights(["AAA", "BBB", "CCC", "DDD"],
                          {"alloc_method": "risk_parity",
                           "max_position_weight": 0.25, "max_sector_weight": 0.0})
    check(all(v <= 0.25 + 1e-6 for v in w.values()),
          "max_position_weight cap respected (all <= 0.25)")
    check(approx(sum(w.values()), 1.0, 1e-3), "capped weights still sum to 1")
    check(approx(w["AAA"], 0.25, 1e-3), "over-weight name pinned at the cap")


def test_max_position_cap_infeasible_sums_below_one():
    _reset()
    # 3 names @ cap 0.25 CANNOT sum to 1 (max 0.75). The contract is Σ ≤ 1, so
    # the cap is honoured and the total falls below 1 (no scaling back over cap).
    RO.risk_parity = _canned_risk_parity({"AAA": 0.7, "BBB": 0.2, "CCC": 0.1})
    w = AL.target_weights(["AAA", "BBB", "CCC"],
                          {"alloc_method": "risk_parity",
                           "max_position_weight": 0.25, "max_sector_weight": 0.0})
    check(all(v <= 0.25 + 1e-6 for v in w.values()),
          "infeasible cap still respected (all <= 0.25)")
    check(sum(w.values()) <= 1.0 + 1e-9 and approx(sum(w.values()), 0.75, 1e-3),
          "infeasible cap -> Σ falls to ~0.75 (≤ 1, never scaled over cap)")


def test_optimizer_error_fails_open():
    _reset()
    RO.risk_parity = lambda symbols, period="2y": {"error": "insufficient history"}
    w = AL.target_weights(["AAA", "BBB"],
                          {"alloc_method": "risk_parity"})
    check(w == {}, "optimizer error -> {} (fail-open to caller sizing)")


def test_optimizer_raises_fails_open():
    _reset()
    def _boom(symbols, period="2y"):
        raise RuntimeError("optimizer exploded")
    RO.risk_parity = _boom
    w = AL.target_weights(["AAA", "BBB"], {"alloc_method": "risk_parity"})
    check(w == {}, "thrown optimizer -> {} (fail-open)")


def test_max_sharpe_wired():
    _reset()
    seen = {}
    def _mk(symbols, objective="max_sharpe", constraints=None):
        seen["objective"] = objective
        seen["constraints"] = constraints or {}
        used = [s.upper() for s in symbols]
        return {"weights": {s: 0.5 for s in used}, "converged": True}
    RO.markowitz_optimize = _mk
    w = AL.target_weights(["AAA", "BBB"],
                          {"alloc_method": "max_sharpe",
                           "max_position_weight": 0.6, "max_sector_weight": 0.0})
    check(seen.get("objective") == "max_sharpe",
          "max_sharpe routes to markowitz_optimize(objective=max_sharpe)")
    check(approx(seen.get("constraints", {}).get("max_weight", 0), 0.6),
          "max_sharpe passes max_position_weight as the optimizer box")
    check(approx(sum(w.values()), 1.0), "max_sharpe weights renormalized")


def test_sector_cap_reduces_concentration():
    _reset()
    # Three tech names + one healthcare. Without a cap tech = 0.9. With a 0.40
    # sector cap the tech sleeve must be cut to ~0.40 and HEAL lifted.
    RO.risk_parity = _canned_risk_parity(
        {"AAA": 0.3, "BBB": 0.3, "CCC": 0.3, "HEAL": 0.1})
    sectors = {"AAA": "Technology", "BBB": "Technology",
               "CCC": "Technology", "HEAL": "Healthcare"}
    AL._sector_of = lambda s: sectors.get(s.upper())
    w = AL.target_weights(["AAA", "BBB", "CCC", "HEAL"],
                          {"alloc_method": "risk_parity",
                           "max_position_weight": 1.0,  # don't let pos-cap interfere
                           "max_sector_weight": 0.40})
    tech = w["AAA"] + w["BBB"] + w["CCC"]
    check(tech <= 0.40 + 1e-3, "sector cap holds Technology sleeve <= 0.40")
    check(w["HEAL"] > 0.1, "freed weight pushed onto under-cap Healthcare name")
    # Both sectors are subject to the 0.40 cap, so Healthcare also pins at 0.40
    # and the leftover (0.20) freed weight has no home -> Σ falls to ~0.80,
    # which the Σ ≤ 1 contract allows (caps win over full investment).
    check(w["HEAL"] <= 0.40 + 1e-3, "Healthcare sleeve also held at its 0.40 cap")
    check(sum(w.values()) <= 1.0 + 1e-9 and approx(sum(w.values()), 0.80, 1e-2),
          "sector-capped weights sum to ~0.80 (≤ 1; both sectors at cap)")


def test_sector_cap_fails_open_when_unavailable():
    _reset()
    RO.risk_parity = _canned_risk_parity({"AAA": 0.3, "BBB": 0.3, "CCC": 0.3})
    AL._sector_of = lambda s: None  # sectors unavailable
    w = AL.target_weights(["AAA", "BBB", "CCC"],
                          {"alloc_method": "risk_parity",
                           "max_position_weight": 1.0, "max_sector_weight": 0.40})
    # no sector cap applied -> equal 1/3 (only renormalized)
    check(all(approx(v, 1.0 / 3) for v in w.values()),
          "sector cap fails open when sectors unavailable")


def test_allocation_notional_equity_times_weight():
    _reset()
    A.account_equity = lambda *a, **k: 100000.0
    n = AL.allocation_notional("AAA", 0.2, {})
    check(approx(n, 20000.0), "notional = equity x weight (100k x 0.2 = 20k)")


def test_allocation_notional_clamped_to_cap():
    _reset()
    A.account_equity = lambda *a, **k: 100000.0
    n = AL.allocation_notional("AAA", 0.5, {"max_order_notional_usd": 10000})
    check(approx(n, 10000.0), "notional clamped to max_order_notional_usd")


def test_allocation_notional_zero_on_error():
    _reset()
    def _boom(*a, **k):
        raise RuntimeError("equity unavailable")
    A.account_equity = _boom
    n = AL.allocation_notional("AAA", 0.2, {})
    check(approx(n, 0.0), "account_equity error -> 0.0 notional")
    check(approx(AL.allocation_notional("AAA", 0.0, {}), 0.0),
          "non-positive weight -> 0.0 notional")


def test_correlation_cap_downweights_cluster():
    _reset()
    # AAA & BBB are a tightly-correlated cluster; CCC is independent.
    series = {
        "AAA": [1.0, 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7, 1.8, 1.9, 2.0, 2.1],
        "BBB": [2.0, 2.2, 2.4, 2.6, 2.8, 3.0, 3.2, 3.4, 3.6, 3.8, 4.0, 4.2],
        "CCC": [5.0, 4.0, 6.0, 3.0, 7.0, 2.0, 8.0, 1.0, 9.0, 4.0, 6.0, 5.0],
    }
    AL._closes = lambda s, p="6mo": list(series.get(s.upper(), []))
    # AAA/BBB corr ≈ 1.0 to each other, ≈ 0 to CCC, so AAA's avg-positive-corr
    # ≈ 0.5 (diluted by CCC); a 0.4 threshold trips the cluster but not CCC.
    base = {"AAA": 1.0 / 3, "BBB": 1.0 / 3, "CCC": 1.0 / 3}
    w = AL.correlation_capped_weights(base, {"correlation_cap": True,
                                             "correlation_cap_threshold": 0.4})
    check(approx(sum(w.values()), 1.0), "correlation-capped weights sum to 1")
    check(w["CCC"] > w["AAA"] and w["CCC"] > w["BBB"],
          "independent name up-weighted vs the correlated cluster")
    check(all(0.0 <= v <= 1.0 for v in w.values()),
          "correlation-capped weights bounded [0,1]")


def test_correlation_cap_disabled_is_noop():
    _reset()
    base = {"AAA": 0.5, "BBB": 0.3, "CCC": 0.2}
    w = AL.correlation_capped_weights(base, {"correlation_cap": False})
    check(all(approx(w[k], base[k]) for k in base),
          "correlation_cap disabled -> renormalized passthrough (no-op)")


def test_target_weights_empty_input():
    _reset()
    check(AL.target_weights([], {}) == {}, "empty symbol list -> {}")
    check(AL.target_weights(None, {}) == {}, "None symbol list -> {}")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    print("aj_allocate — {} tests\n".format(len(tests)))
    for fn in tests:
        try:
            fn()
        except Exception as e:
            FAIL.append(fn.__name__)
            print("  [XX] {}: unexpected {}: {}".format(
                fn.__name__, type(e).__name__, e))
    print("\n{} passed, {} failed".format(len(PASS), len(FAIL)))
    if FAIL:
        print("FAILED:")
        for f in FAIL:
            print("  - " + f)
    sys.exit(1 if FAIL else 0)
