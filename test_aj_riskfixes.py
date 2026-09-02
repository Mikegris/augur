#!/usr/bin/env python3
"""Risk-fix tests — the v3.25 expectancy post-mortem remediations.

Three opt-in, fail-closed guardrails, each pinned here (default 0 => legacy):
  A  per-asset-class notional cap (crypto sizing)     — aj_strategy.size_order
  B  option sizing budget (own target + hard cap)     — aj_operator._option_order
  C  robust (winsorized) realized expectancy          — aj_risk_governor

Network-free: prices are stubbed; the governor reads a seeded temp DB and its
drawdown/vix/regime inputs are injected.
"""
import os
import sys
import tempfile

os.environ["AUGUR_DB_PATH"] = tempfile.mktemp(suffix="_riskfix.db")

import aj_db                    # noqa: E402
import aj_strategy              # noqa: E402
import aj_operator             # noqa: E402
import aj_risk                  # noqa: E402
import aj_positions             # noqa: E402
import aj_risk_governor as RG   # noqa: E402
import aj_alpha                 # noqa: E402
import aj_rules                 # noqa: E402

aj_db.aj_init()

_FAILS = []


def check(cond, msg):
    if cond:
        print("  ok:", msg)
    else:
        print("  FAIL:", msg)
        _FAILS.append(msg)


# ════════════════════════════════════════════════════════════════════════════
#  Fix A — per-asset-class notional cap (crypto)
# ════════════════════════════════════════════════════════════════════════════

_BASE_A = {"max_order_notional_usd": 2500.0, "order_notional_target_usd": 2000.0}


def _with_price_and_type(price, atype):
    """Stub the two live inputs size_order needs: order price + asset type."""
    aj_risk._order_price = lambda *a, **k: price
    aj_positions.infer_asset_type = lambda s: atype


def test_A_crypto_cap_clamps_buy():
    orig_p, orig_t = aj_risk._order_price, aj_positions.infer_asset_type
    try:
        _with_price_and_type(100.0, "crypto")
        cfg = dict(_BASE_A, max_crypto_notional_usd=500.0)
        s = aj_strategy.size_order("BTC-USD", "buy", cfg, 0.0, None)
        check(s is not None and abs(s["notional"] - 500.0) < 1e-6,
              "A: crypto buy clamped to max_crypto_notional_usd (500, not 2000)")
    finally:
        aj_risk._order_price, aj_positions.infer_asset_type = orig_p, orig_t


def test_A_cap_does_not_touch_equity():
    orig_p, orig_t = aj_risk._order_price, aj_positions.infer_asset_type
    try:
        _with_price_and_type(100.0, "equity")
        cfg = dict(_BASE_A, max_crypto_notional_usd=500.0)
        s = aj_strategy.size_order("AAPL", "buy", cfg, 0.0, None)
        check(s is not None and abs(s["notional"] - 2000.0) < 1e-6,
              "A: equity buy unaffected by the crypto cap (full 2000 target)")
    finally:
        aj_risk._order_price, aj_positions.infer_asset_type = orig_p, orig_t


def test_A_zero_is_legacy():
    orig_p, orig_t = aj_risk._order_price, aj_positions.infer_asset_type
    try:
        _with_price_and_type(100.0, "crypto")
        cfg = dict(_BASE_A, max_crypto_notional_usd=0.0)   # disabled
        s = aj_strategy.size_order("BTC-USD", "buy", cfg, 0.0, None)
        check(s is not None and abs(s["notional"] - 2000.0) < 1e-6,
              "A: max_crypto_notional_usd=0 => no clamp (legacy behavior)")
    finally:
        aj_risk._order_price, aj_positions.infer_asset_type = orig_p, orig_t


def test_A_never_caps_a_sell():
    orig_p, orig_t = aj_risk._order_price, aj_positions.infer_asset_type
    try:
        _with_price_and_type(100.0, "crypto")
        # A sell is an exit sized from held_qty (50 units), NOT from target — the
        # crypto cap must never shrink it and strand risk.
        cfg = dict(_BASE_A, max_crypto_notional_usd=100.0)
        s = aj_strategy.size_order("BTC-USD", "sell", cfg, 50.0, None)
        check(s is not None and abs(s["qty"] - 50.0) < 1e-6,
              "A: crypto cap never shrinks a SELL (full held qty exits)")
    finally:
        aj_risk._order_price, aj_positions.infer_asset_type = orig_p, orig_t


# ════════════════════════════════════════════════════════════════════════════
#  Fix B — option sizing budget (pure _option_order)
# ════════════════════════════════════════════════════════════════════════════

def _opt(prem):
    return {"symbol": "OPT:X:20260101:C:100.0", "underlying": "X",
            "option_type": "call", "strike": 100.0, "expiry": "20260101",
            "premium_contract": prem, "contract_multiplier": 100}


def test_B_target_sizes_fewer_contracts():
    # Equity target 2000 would buy 4 contracts @ $500; option target 900 -> 1.
    oo = aj_operator._option_order(_opt(500.0), 2000.0, "t",
                                   cap_notional=2500.0,
                                   cfg={"option_notional_target_usd": 900.0})
    check(oo is not None and oo["sizing"]["qty"] == 1,
          "B: option_notional_target_usd sizes to its own (smaller) budget")


def test_B_hard_cap_bounds_premium():
    # target 5000 -> 10 contracts @ $500 = $5000, but option cap 1200 -> 2.
    oo = aj_operator._option_order(_opt(500.0), 5000.0, "t",
                                   cap_notional=5000.0,
                                   cfg={"max_option_notional_usd": 1200.0})
    check(oo is not None and oo["sizing"]["qty"] == 2
          and oo["sizing"]["notional"] <= 1200.0 + 1e-6,
          "B: max_option_notional_usd caps total premium (2 x 500 <= 1200)")


def test_B_single_contract_over_cap_refused():
    # One $778 contract (the real ATEX lot) exceeds a $400 option cap -> None,
    # so the caller falls back to the equity leg instead of a lottery ticket.
    oo = aj_operator._option_order(_opt(778.0), 2000.0, "t",
                                   cap_notional=2500.0,
                                   cfg={"max_option_notional_usd": 400.0})
    check(oo is None,
          "B: a single contract breaching the option cap is refused (None)")


def test_B_zero_is_legacy():
    # No option budget set: legacy path — size from target, and size-up-to-one
    # under cap_notional when the target can't cover a contract.
    oo1 = aj_operator._option_order(_opt(200.0), 900.0, "t", cap_notional=2500.0,
                                    cfg={"max_option_notional_usd": 0.0,
                                         "option_notional_target_usd": 0.0})
    check(oo1 is not None and oo1["sizing"]["qty"] == 4,
          "B: no option budget => legacy sizing from equity target (900/200=4)")
    oo2 = aj_operator._option_order(_opt(1500.0), 900.0, "t", cap_notional=2500.0,
                                    cfg={})
    check(oo2 is not None and oo2["sizing"]["qty"] == 1,
          "B: legacy size-up-to-one contract under cap_notional preserved")


# ════════════════════════════════════════════════════════════════════════════
#  Fix C — robust (winsorized) realized expectancy
# ════════════════════════════════════════════════════════════════════════════

def test_C_winsorize_helper():
    vals = [-80.0] + [3.0] * 20         # one catastrophe, 20 small wins
    plain = sum(vals) / len(vals)
    wins = RG._winsorize(vals, 5.0)
    wmean = sum(wins) / len(wins)
    check(plain < 0 < wmean,
          "C: winsorize flips an outlier-contaminated mean positive "
          "(plain {:.2f} -> winsor {:.2f})".format(plain, wmean))
    check(RG._winsorize([1.0, 2.0], 10.0) == [1.0, 2.0],
          "C: <3 samples is a no-op")
    check(RG._winsorize(vals, 0.0) == vals,
          "C: winsor_pct=0 is a no-op (legacy)")


def _reset_labels():
    """Isolate each Fix C test: the module-level temp DB is shared across tests,
    so clear the label table before seeding a fresh window."""
    with aj_db.db._write_lock:
        conn = aj_db.db.get_conn()
        conn.execute("DELETE FROM aj_trade_labels")
        conn.commit()


def _seed_labels(returns):
    _reset_labels()
    for i, r in enumerate(returns):
        aj_db.insert("aj_trade_labels", symbol="SYM{}".format(i), side="buy",
                     opened_at="2026-01-{:02d}T00:00:00+00:00".format((i % 27) + 1),
                     closed_at="2026-02-{:02d}T00:00:00+00:00".format((i % 27) + 1),
                     features_json="{}", realized_return_pct=float(r),
                     realized_pnl_usd=0.0, label=1 if r > 0 else 0,
                     created_at="2026-02-01T00:00:00+00:00")


def test_C_realized_expectancy_reads_winsor():
    _seed_labels([-80.0] + [3.0] * 20)
    plain, n = RG._realized_expectancy(60, 0.0)
    wins, n2 = RG._realized_expectancy(60, 5.0)
    check(plain is not None and plain < 0,
          "C: plain realized expectancy is negative ({:.2f}%)".format(plain))
    check(wins is not None and wins > 0,
          "C: winsorized realized expectancy is positive ({:.2f}%)".format(wins))
    check(n == 21 and n2 == 21, "C: n reports the raw sample count (21), not trimmed")


def test_C_breaker_clears_under_winsor():
    # End-to-end: the SAME book that trips the breaker on a plain mean clears it
    # once winsorization is on. Drawdown/VIX/regime injected benign.
    _seed_labels([-80.0] + [3.0] * 20)   # plain -0.95%, winsor +3.0%
    real = (aj_alpha.current_drawdown_pct, aj_rules.current_vix, aj_alpha.detect_regime)
    aj_alpha.current_drawdown_pct = lambda: 0.0
    aj_rules.current_vix = lambda: 15.0
    aj_alpha.detect_regime = lambda: "bull"
    RG.reset_memo()
    try:
        cfg = {"risk_governor_enabled": True, "risk_governor_max": 1.0,
               "risk_governor_min": 0.0, "rg_drawdown_derisk_pct": 10.0,
               "rg_drawdown_breaker_pct": 20.0, "rg_vix_derisk": 30.0,
               "rg_alpha_decay_min_trades": 20, "rg_alpha_decay_floor_pct": 0.1,
               "rg_lever_unlock_trades": 50, "rg_lever_min_expectancy_pct": 0.5}
        off = RG.exposure_multiplier(dict(cfg, rg_expectancy_winsor_pct=0.0))
        RG.reset_memo()
        on = RG.exposure_multiplier(dict(cfg, rg_expectancy_winsor_pct=5.0))
        check(off["breaker"] is True and off["G"] == 0.0,
              "C: plain mean trips the breaker (G=0)")
        check(on["breaker"] is False and on["G"] > 0.0,
              "C: winsorized expectancy clears the breaker (G>0)")
    finally:
        aj_alpha.current_drawdown_pct, aj_rules.current_vix, aj_alpha.detect_regime = real
        RG.reset_memo()


# ════════════════════════════════════════════════════════════════════════════

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
