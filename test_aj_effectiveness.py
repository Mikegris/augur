#!/usr/bin/env python3
"""Integration tests for the effectiveness layer wired into the operator loop.

Proves the opt-in flags actually change run_once behavior: cross-sectional
selection, cost gate, event blackout, limit entry, portfolio cap, and the
supplementary time-stop / profit-ladder exits. Network-free: scan, forecasts,
quotes, marks, and propose are all injected. We monkeypatch _propose_and_execute
to a recorder so we assert the operator's WIRING (what reaches propose, with what
sizing) independently of the already-tested gate/execution internals.
"""
import os
import sys
import tempfile

os.environ["AUGUR_DB_PATH"] = tempfile.mktemp(suffix="_ajeff.db")

import database as db          # noqa: E402
import aj_db                    # noqa: E402
import aj_config                # noqa: E402
import aj_operator              # noqa: E402
import aj_positions             # noqa: E402
import aj_execution_alpha       # noqa: E402
import aj_allocate              # noqa: E402

aj_db.aj_init()

# capture the real implementations so each test starts from a clean slate
# (monkeypatches below must not leak into sibling tests run in the same process)
_REAL = {
    "event_blackout": aj_execution_alpha.event_blackout,
    "entry_price": aj_execution_alpha.entry_price,
    "cost_gate": aj_execution_alpha.cost_gate,
    "time_stop": aj_execution_alpha.time_stop,
    "profit_ladder": aj_execution_alpha.profit_ladder,
    "tw": aj_allocate.target_weights,
    "an": aj_allocate.allocation_notional,
    "paper_book": aj_positions.paper_book,
}


def _restore():
    aj_execution_alpha.event_blackout = _REAL["event_blackout"]
    aj_execution_alpha.entry_price = _REAL["entry_price"]
    aj_execution_alpha.cost_gate = _REAL["cost_gate"]
    aj_execution_alpha.time_stop = _REAL["time_stop"]
    aj_execution_alpha.profit_ladder = _REAL["profit_ladder"]
    aj_allocate.target_weights = _REAL["tw"]
    aj_allocate.allocation_notional = _REAL["an"]
    aj_positions.paper_book = _REAL["paper_book"]

_PROPOSED = []   # records (symbol, decision, sizing) reaching _propose_and_execute


def _recorder(cycle_id, symbol, decision, sizing, cfg, instrument=None):
    _PROPOSED.append({"symbol": symbol, "decision": dict(decision), "sizing": dict(sizing)})
    return {"proposal_id": len(_PROPOSED), "result": "executed",
            "exec": {"qty": sizing.get("qty", 0)}}


def _cfg(**over):
    cfg = {"trading_enabled": True, "universe_mode": "market_screen",
           "auto_approve_paper": True, "default_broker": "paper",
           "max_order_notional_usd": 100000, "max_trades_per_day": 999,
           "max_daily_loss_usd": 1e9, "min_edge_pct_pts": 0.0,
           "order_notional_target_usd": 1000, "buy_prob_threshold": 0.55,
           "session_whitelist": ["premarket", "regular", "afterhours"],
           "council_enabled": False, "trade_options": False}
    cfg.update(over)
    aj_config.set_config(cfg)


def _setup(forecasts, scan=None):
    """forecasts: {sym: (prob_up, edge_pts)}. Wire scan + per-symbol forecast +
    a flat sizing path + the propose recorder + neutral housekeeping."""
    global _PROPOSED
    _PROPOSED = []
    _restore()      # clear any sibling test's monkeypatches
    for t in ("aj_fills", "aj_orders", "aj_proposals", "aj_cycles"):
        db.get_conn().execute("DELETE FROM " + t)
    db.get_conn().commit()
    syms = scan if scan is not None else list(forecasts)
    aj_operator._scan_universe = lambda: list(syms)
    import aj_alpha
    aj_alpha.rank_universe = lambda s, cfg: list(s)
    aj_operator._forecast = lambda sym, h: (
        {"ensemble": {"prob_up": forecasts[sym][0], "edge_pct_pts": forecasts[sym][1],
                      "conviction": "high"}} if sym in forecasts else None)
    # flat sizer: 100 notional @ $10 -> 10 sh (keeps math trivial)
    import aj_strategy
    aj_strategy.size_order = lambda symbol, side, cfg, held, decision: {
        "qty": 10.0, "price": 10.0, "notional": 100.0,
        "order_type": "market", "limit_price": None}
    aj_operator._propose_and_execute = _recorder
    # neutralize exit/housekeeping paths
    import aj_rules
    aj_rules.exit_signals = lambda cfg: []
    aj_rules.in_cooldown = lambda sym, cfg: False


def _run():
    return aj_operator.run_once("paper")


def _proposed_syms():
    return {p["symbol"] for p in _PROPOSED}


# ── cross-sectional selection ────────────────────────────────────────────────

def test_cross_sectional_narrows_to_top_n():
    _setup({"AAA": (0.90, 30.0), "BBB": (0.70, 12.0), "CCC": (0.60, 4.0)})
    _cfg(cross_sectional_selection=True, cross_sectional_top_n=2)
    out = _run()
    assert out["ok"], out
    # only the 2 strongest-edge names should reach propose
    assert _proposed_syms() == {"AAA", "BBB"}, (_proposed_syms(), out.get("cross_sectional"))


def test_cross_sectional_off_considers_all():
    _setup({"AAA": (0.90, 30.0), "BBB": (0.70, 12.0), "CCC": (0.60, 4.0)})
    _cfg(cross_sectional_selection=False)
    _run()
    assert _proposed_syms() == {"AAA", "BBB", "CCC"}, _proposed_syms()


# ── cost gate ─────────────────────────────────────────────────────────────────

def test_cost_gate_blocks_thin_edge():
    _setup({"AAA": (0.56, 0.2)})        # tiny 0.2pp edge
    _cfg(cost_gate=True, assumed_spread_bps=50.0, cost_edge_multiple=2.0)
    out = _run()
    assert "AAA" not in _proposed_syms()
    assert any(p.get("result") == "cost_blocked" for p in out["proposals"]), out["proposals"]


def test_cost_gate_passes_fat_edge():
    _setup({"AAA": (0.90, 40.0)})       # huge edge clears any sane cost
    _cfg(cost_gate=True, assumed_spread_bps=10.0, cost_edge_multiple=1.5)
    _run()
    assert "AAA" in _proposed_syms()


# ── event blackout ────────────────────────────────────────────────────────────

def test_event_blackout_blocks_entry():
    _setup({"AAA": (0.90, 30.0)})
    _cfg(event_blackout_days=5)
    import aj_execution_alpha
    aj_execution_alpha.event_blackout = lambda sym, cfg, now=None: {
        "blocked": True, "reason": "earnings in 2d"}
    out = _run()
    assert "AAA" not in _proposed_syms()
    assert any(p.get("result") == "event_blackout" for p in out["proposals"]), out["proposals"]


# ── limit entry ───────────────────────────────────────────────────────────────

def test_limit_entry_sets_limit_order():
    _setup({"AAA": (0.90, 30.0)})
    _cfg(limit_entry=True, limit_entry_offset_bps=20.0)
    import aj_execution_alpha
    # force a deterministic limit decision
    aj_execution_alpha.entry_price = lambda sym, side, q, cfg: {
        "order_type": "limit", "limit_price": 9.5, "reason": "pullback"}
    _run()
    assert _PROPOSED, "nothing proposed"
    sz = _PROPOSED[0]["sizing"]
    assert sz["order_type"] == "limit" and sz["limit_price"] == 9.5, sz


# ── portfolio construction cap ────────────────────────────────────────────────

def test_portfolio_cap_shrinks_notional():
    _setup({"AAA": (0.90, 30.0)})
    _cfg(portfolio_construction=True, alloc_method="equal")
    import aj_allocate
    aj_allocate.target_weights = lambda syms, cfg: {"AAA": 0.05}
    aj_allocate.allocation_notional = lambda sym, w, cfg: 40.0   # < the 100 flat sizing
    _run()
    sz = _PROPOSED[0]["sizing"]
    assert abs(sz["notional"] - 40.0) < 1e-6 and abs(sz["qty"] - 4.0) < 1e-6, sz


def test_portfolio_cap_never_increases():
    _setup({"AAA": (0.90, 30.0)})
    _cfg(portfolio_construction=True)
    import aj_allocate
    aj_allocate.target_weights = lambda syms, cfg: {"AAA": 0.9}
    aj_allocate.allocation_notional = lambda sym, w, cfg: 5000.0  # > flat sizing -> ignored
    _run()
    sz = _PROPOSED[0]["sizing"]
    assert abs(sz["notional"] - 100.0) < 1e-6, sz   # unchanged (cap only shrinks)


# ── supplementary exits: time-stop + profit ladder ───────────────────────────

def _seed_position(sym, qty, avg, opened_at):
    # use the paper book's own buy path so realized/marks math stays consistent
    aj_positions.record_fill(sym, "buy", qty, avg, fees=0.0, ts=opened_at,
                             asset_type="stock") if hasattr(aj_positions, "record_fill") else None


def test_time_stop_exits_stale_position():
    _setup({})                      # no buys
    _cfg(time_stop_days=10, time_stop_min_gain_pct=5.0)
    # one held position, opened 20 days ago, flat (not up 5%)
    old = (aj_db.utc_now().replace(microsecond=0)).isoformat()
    import datetime
    old = (aj_db.utc_now() - datetime.timedelta(days=20)).replace(microsecond=0).isoformat()
    aj_positions.paper_book = lambda mode="paper": {"positions": {
        "ZZZ": {"qty": 10.0, "avg_cost": 100.0, "opened_at": old}}}
    import aj_risk
    aj_risk._marks = lambda syms: {"ZZZ": 100.0}      # flat -> stale
    out = _run()
    sells = [p for p in _PROPOSED if p["decision"].get("side") == "sell"]
    assert any(p["symbol"] == "ZZZ" for p in sells), _PROPOSED


def test_profit_ladder_trims_once_per_rung():
    _setup({})
    _cfg(profit_ladder=True)
    aj_positions.paper_book = lambda mode="paper": {"positions": {
        "WIN": {"qty": 100.0, "avg_cost": 100.0, "opened_at": aj_db.utc_now_iso()}}}
    import aj_risk
    aj_risk._marks = lambda syms: {"WIN": 118.0}      # +18% -> trips the 15% rung
    aj_db.set_setting_raw("__aj_ladder_WIN", "")      # fresh
    _run()
    trims = [p for p in _PROPOSED if p["symbol"] == "WIN"]
    assert len(trims) == 1, ("first cycle trims once", _PROPOSED)
    # second cycle: same +18%, rung already marked -> NO new trim
    _PROPOSED.clear()
    _run()
    assert not any(p["symbol"] == "WIN" for p in _PROPOSED), ("rung must not re-trim", _PROPOSED)


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    print("aj_effectiveness — {} tests".format(len(fns)))
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
