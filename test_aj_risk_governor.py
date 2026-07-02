#!/usr/bin/env python3
"""Risk Governor tests — the portfolio exposure multiplier + circuit breaker.

Network-free: drawdown / VIX / regime / realized-expectancy inputs are injected.
"""
import os
import sys
import tempfile

os.environ["AUGUR_DB_PATH"] = tempfile.mktemp(suffix="_rg.db")

import aj_db                    # noqa: E402
import aj_config                # noqa: E402
import aj_risk_governor as RG   # noqa: E402
import aj_alpha                 # noqa: E402
import aj_rules                 # noqa: E402

aj_db.aj_init()

_REAL = {"dd": aj_alpha.current_drawdown_pct, "vix": aj_rules.current_vix,
         "reg": aj_alpha.detect_regime, "exp": RG._realized_expectancy}


def _inject(dd=0.0, vix=15.0, regime="bull", exp=None, n=0):
    aj_alpha.current_drawdown_pct = lambda: dd
    aj_rules.current_vix = lambda: vix
    aj_alpha.detect_regime = lambda: regime
    RG._realized_expectancy = lambda window: (exp, n)
    RG.reset_memo()


def _restore():
    aj_alpha.current_drawdown_pct = _REAL["dd"]
    aj_rules.current_vix = _REAL["vix"]
    aj_alpha.detect_regime = _REAL["reg"]
    RG._realized_expectancy = _REAL["exp"]
    RG.reset_memo()


def _cfg(**over):
    c = {"risk_governor_enabled": True, "risk_governor_max": 1.0, "risk_governor_min": 0.0,
         "rg_drawdown_derisk_pct": 10.0, "rg_drawdown_breaker_pct": 20.0,
         "rg_vix_derisk": 30.0, "rg_alpha_decay_min_trades": 20,
         "rg_alpha_decay_floor_pct": 0.1, "rg_lever_unlock_trades": 50,
         "rg_lever_min_expectancy_pct": 0.5}
    c.update(over)
    return c


def test_disabled_is_noop():
    _inject(dd=50.0, vix=99.0, regime="bear", exp=-5.0, n=100)   # everything bad
    r = RG.exposure_multiplier({"risk_governor_enabled": False})
    assert r["G"] == 1.0 and r["enabled"] is False and r["breaker"] is False
    _restore()


def test_calm_market_full_exposure():
    _inject(dd=0.0, vix=14.0, regime="bull", exp=None, n=0)
    r = RG.exposure_multiplier(_cfg())
    assert abs(r["G"] - 1.0) < 1e-9 and not r["breaker"], r
    _restore()


def test_drawdown_taper_then_breaker():
    _inject(dd=15.0, vix=14.0, regime="bull")   # mid-band (10..20) -> shrink
    r = RG.exposure_multiplier(_cfg())
    assert 0.25 <= r["G"] < 1.0 and not r["breaker"], r
    _inject(dd=22.0, vix=14.0, regime="bull")   # >= breaker
    r = RG.exposure_multiplier(_cfg())
    assert r["G"] == 0.0 and r["breaker"], r
    _restore()


def test_high_vix_halves():
    _inject(dd=0.0, vix=35.0, regime="bull")
    r = RG.exposure_multiplier(_cfg())
    assert abs(r["G"] - 0.5) < 1e-9, r
    _restore()


def test_bear_regime_trims():
    _inject(dd=0.0, vix=14.0, regime="bear")
    r = RG.exposure_multiplier(_cfg())
    assert abs(r["G"] - 0.7) < 1e-9, r
    _restore()


def test_negative_realized_expectancy_trips_breaker():
    _inject(dd=0.0, vix=14.0, regime="bull", exp=-0.3, n=40)
    r = RG.exposure_multiplier(_cfg())
    assert r["G"] == 0.0 and r["breaker"], r
    _restore()


def test_weak_positive_expectancy_halves():
    _inject(dd=0.0, vix=14.0, regime="bull", exp=0.05, n=40)   # >0 but < floor 0.1
    r = RG.exposure_multiplier(_cfg())
    assert abs(r["G"] - 0.5) < 1e-9 and not r["breaker"], r
    _restore()


def test_alpha_arm_inactive_below_min_trades():
    _inject(dd=0.0, vix=14.0, regime="bull", exp=-5.0, n=5)    # negative but n<min -> ignored
    r = RG.exposure_multiplier(_cfg())
    assert abs(r["G"] - 1.0) < 1e-9 and not r["breaker"], r
    _restore()


def test_lever_up_only_when_proven_and_max_gt_1():
    # max=1.5, proven strong realized edge over enough trades -> G>1
    _inject(dd=0.0, vix=14.0, regime="bull", exp=1.0, n=80)
    r = RG.exposure_multiplier(_cfg(risk_governor_max=1.5))
    assert r["G"] > 1.0 and r["G"] <= 1.5, r
    # same edge but max=1.0 -> never levers
    _inject(dd=0.0, vix=14.0, regime="bull", exp=1.0, n=80)
    r = RG.exposure_multiplier(_cfg(risk_governor_max=1.0))
    assert abs(r["G"] - 1.0) < 1e-9, r
    _restore()


def test_takes_the_most_conservative_arm():
    # high VIX (x0.5) AND bear (x0.7) -> the smaller (0.5) wins
    _inject(dd=0.0, vix=35.0, regime="bear")
    r = RG.exposure_multiplier(_cfg())
    assert abs(r["G"] - 0.5) < 1e-9, r
    _restore()


def test_size_order_buy_shrinks_sell_unaffected():
    # integration: G applies to a BUY's notional, never to a SELL exit.
    import aj_strategy, aj_risk, aj_positions
    _inject(dd=15.0, vix=14.0, regime="bull")   # shrink ~ <1.0
    aj_config.set_config(_cfg(order_notional_target_usd=1000, max_order_notional_usd=100000,
                              conviction_sizing=False))
    aj_risk._order_price = lambda sym, ot, lp, at: 10.0
    aj_positions.infer_asset_type = lambda s: "stock"
    buy = aj_strategy.size_order("AAA", "buy", aj_config.get_config(), 0.0, {"edge_pts": 5.0})
    # default target = 1000; governor shrinks it
    assert buy is not None and buy["notional"] < 1000.0, buy
    # a sell exit closes the full held qty regardless of the governor
    sell = aj_strategy.size_order("AAA", "sell", aj_config.get_config(), 50.0, {"edge_pts": 0.0})
    assert sell is not None and abs(sell["qty"] - 50.0) < 1e-9, sell
    # breaker -> a BUY is refused (None), a SELL still allowed
    _inject(dd=25.0, vix=14.0, regime="bull")   # breaker
    assert aj_strategy.size_order("AAA", "buy", aj_config.get_config(), 0.0, {"edge_pts": 5.0}) is None
    sell2 = aj_strategy.size_order("AAA", "sell", aj_config.get_config(), 50.0, {"edge_pts": 0.0})
    assert sell2 is not None and abs(sell2["qty"] - 50.0) < 1e-9, sell2
    _restore()


def test_memo_invalidated_by_rg_config_change():
    # A governor-config change must take effect IMMEDIATELY, not after the
    # memo TTL: the fingerprint of rg_*/risk_governor_* values keys the memo.
    _inject(dd=15.0, vix=14.0, regime="bull")   # inside the 10..20 derisk band
    r1 = RG.exposure_multiplier(_cfg())
    assert 0.25 <= r1["G"] < 1.0, r1
    # memo hit with UNCHANGED config (no reset_memo — same fingerprint)
    assert RG.exposure_multiplier(_cfg()) is r1
    # widen the derisk band so dd=15 no longer shrinks — WITHOUT reset_memo;
    # the stale memoized G must not be served
    r2 = RG.exposure_multiplier(_cfg(rg_drawdown_derisk_pct=16.0))
    assert abs(r2["G"] - 1.0) < 1e-9, r2
    # and a risk_governor_* change invalidates too (max>1 alters the snapshot)
    _inject(dd=15.0, vix=14.0, regime="bull", exp=1.0, n=80)
    RG.exposure_multiplier(_cfg())
    r3 = RG.exposure_multiplier(_cfg(risk_governor_max=1.5,
                                     rg_drawdown_derisk_pct=16.0))
    assert r3["G"] > 1.0, r3
    _restore()


def test_snapshot_persisted_only_on_G_change():
    # Fresh computes persist ONE aj_risk_events decision='governor' row per
    # G change — steady state must not grow the table every cycle.
    def _n_rows():
        rows = aj_db.query(
            "SELECT COUNT(*) AS n FROM aj_risk_events WHERE decision='governor'")
        return int(rows[0]["n"])
    base = _n_rows()
    _inject(dd=0.0, vix=14.0, regime="bull")
    r = RG.exposure_multiplier(_cfg())          # G=1.0 -> new row
    assert _n_rows() == base + 1
    _inject(dd=0.0, vix=14.0, regime="bull")    # reset memo; SAME G recomputed
    RG.exposure_multiplier(_cfg())
    assert _n_rows() == base + 1                # unchanged G -> no new row
    _inject(dd=0.0, vix=35.0, regime="bull")    # high VIX -> G=0.5 -> new row
    r = RG.exposure_multiplier(_cfg())
    assert abs(r["G"] - 0.5) < 1e-9
    assert _n_rows() == base + 2
    # the persisted snapshot carries the components + first reason
    row = aj_db.query("SELECT reason, caps_json FROM aj_risk_events "
                      "WHERE decision='governor' ORDER BY id DESC LIMIT 1")[0]
    import json
    comps = json.loads(row["caps_json"])
    assert comps["G"] == 0.5 and comps["vix"] == 35.0 and "VIX" in row["reason"], row
    _restore()


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    print("aj_risk_governor — {} tests".format(len(fns)))
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
