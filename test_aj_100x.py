"""Offline tests for the AJTA 100x layer (aj_alpha + aj_autonomy, enhancements
1-25) and their wiring into sizing / gates / exits / operator.

Fully isolated: a fresh temp DB, all market data monkeypatched, no network, no
LLM. Each enhancement is exercised both OFF (neutral / no-op) and ON.
"""
import os
import sys
import json
import tempfile

os.environ["AUGUR_DB_PATH"] = tempfile.mktemp(suffix=".db")

import database  # noqa: E402
database.init_db()
import aj_db  # noqa: E402
aj_db.aj_migrate()

import aj_alpha as A  # noqa: E402
import aj_autonomy as AU  # noqa: E402
import aj_config  # noqa: E402
import aj_strategy  # noqa: E402
import aj_rules  # noqa: E402

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


# ── helpers ───────────────────────────────────────────────────────────────────

def _trend(start, step, n):
    """A simple linear close series."""
    return [start + step * i for i in range(n)]


def _reset_alpha_patches():
    """Restore the monkeypatchable data wrappers to inert defaults."""
    A._closes = lambda s, p="1y": []
    A._market_closes = lambda p="1y": []
    A._bars = lambda s, p="6mo": []
    A._paper_book = lambda: {"positions": {}}
    A._realized_trades = lambda: []
    A._equity_curve = lambda: []
    A._current_equity = lambda: 0.0
    A._earnings_days_away = lambda s: None
    A._sector_of = lambda s: None


# ════════════════════════════════════════════════════════════════════════════
#  SIZING INTELLIGENCE (1-5)
# ════════════════════════════════════════════════════════════════════════════

def test_01_kelly_sizing():
    _reset_alpha_patches()
    # winning record → Kelly bets bigger than the 0.20 neutral reference
    A._realized_trades = lambda: (
        [{"symbol": "X", "realized": 100.0} for _ in range(7)] +
        [{"symbol": "X", "realized": -50.0} for _ in range(3)])
    cfg_on = {"kelly_sizing": True, "kelly_fraction": 0.5}
    f = A._kelly_factor(cfg_on, {"edge_pts": 10})
    check(f > 1.0, "01 Kelly: a 70%/2:1 record sizes above neutral (f={:.2f})".format(f))
    check(A._kelly_factor({}, {"edge_pts": 10}) == 1.0, "01 Kelly: off → 1.0")
    # losing record → Kelly shrinks toward the floor
    A._realized_trades = lambda: (
        [{"symbol": "X", "realized": 20.0} for _ in range(3)] +
        [{"symbol": "X", "realized": -80.0} for _ in range(7)])
    f2 = A._kelly_factor(cfg_on, {"edge_pts": 1})
    check(f2 < 1.0, "01 Kelly: a losing record sizes below neutral (f={:.2f})".format(f2))


def test_02_volatility_target():
    _reset_alpha_patches()
    # a calm name (low vol) gets upsized toward the target; a wild name downsized.
    # _ann_vol returns annualized vol as a PERCENT (portfolio_insights.annualized_vol
    # multiplies by 100), so 16.0 ≈ 1%/day and 64.0 ≈ 4%/day.
    A._ann_vol = lambda closes: 16.0          # ~1%/day
    calm = A._vol_target_factor("AAPL", {"volatility_target_pct": 2.0})
    A._ann_vol = lambda closes: 64.0          # ~4%/day
    wild = A._vol_target_factor("AAPL", {"volatility_target_pct": 2.0})
    check(calm > 1.0 and wild < 1.0, "02 vol-target: calm up ({:.2f}), wild down ({:.2f})".format(calm, wild))
    check(A._vol_target_factor("AAPL", {}) == 1.0, "02 vol-target: off → 1.0")


def test_03_compound_sizing():
    _reset_alpha_patches()
    A._current_equity = lambda: 5000.0
    f = A._compound_factor({"compound_sizing": True, "compound_base_equity_usd": 10000.0})
    check(abs(f - 1.5) < 1e-6, "03 compounding: +$5k on $10k base → 1.5x (f={:.2f})".format(f))
    check(A._compound_factor({}) == 1.0, "03 compounding: off → 1.0")


def test_04_symbol_performance():
    _reset_alpha_patches()
    cfg = {"symbol_performance_weighting": True}
    A._realized_trades = lambda: [{"symbol": "WIN", "realized": 50.0} for _ in range(4)]
    check(A._symbol_perf_factor("WIN", cfg) > 1.0, "04 symbol-perf: proven winner upsized")
    A._realized_trades = lambda: [{"symbol": "LOSE", "realized": -40.0} for _ in range(4)]
    check(A._symbol_perf_factor("LOSE", cfg) == 0.0, "04 symbol-perf: chronic loser → skip (0.0)")
    check(A._symbol_perf_factor("WIN", {}) == 1.0, "04 symbol-perf: off → 1.0")


def test_05_drawdown_throttle():
    _reset_alpha_patches()
    A._equity_curve = lambda: [{"equity_usd": 1000.0}, {"equity_usd": 900.0}]  # 10% dd
    f = A._drawdown_factor({"drawdown_throttle_pct": 20.0})
    check(abs(f - 0.5) < 1e-6, "05 drawdown: 10% dd vs 20% thr → 0.5x (f={:.2f})".format(f))
    A._equity_curve = lambda: [{"equity_usd": 1000.0}, {"equity_usd": 750.0}]  # 25% dd
    check(A._drawdown_factor({"drawdown_throttle_pct": 20.0}) == 0.0,
          "05 drawdown: past threshold → 0.0 (halt new size)")
    check(A._drawdown_factor({}) == 1.0, "05 drawdown: off → 1.0")


def test_sizing_multiplier_combines_and_vetoes():
    _reset_alpha_patches()
    check(A.sizing_multiplier("X", "buy", {}, {"edge_pts": 5}) == 1.0,
          "sizing_multiplier: all-off → 1.0")
    check(A.sizing_multiplier("X", "sell", {"kelly_sizing": True}, {"edge_pts": 5}) == 1.0,
          "sizing_multiplier: sells never resized → 1.0")
    A._equity_curve = lambda: [{"equity_usd": 1000.0}, {"equity_usd": 700.0}]
    check(A.sizing_multiplier("X", "buy", {"drawdown_throttle_pct": 20.0}, None) == 0.0,
          "sizing_multiplier: a 0 factor vetoes the order (0.0)")


def test_sizing_wired_into_strategy():
    # size_order must apply the multiplier; a vetoing multiplier → None (skip)
    _reset_alpha_patches()
    import aj_risk
    orig_price = aj_risk._order_price
    aj_risk._order_price = lambda *a, **k: 100.0
    try:
        A._equity_curve = lambda: [{"equity_usd": 1000.0}, {"equity_usd": 700.0}]
        cfg = {"max_order_notional_usd": 1000.0, "order_notional_target_usd": 500.0,
               "drawdown_throttle_pct": 20.0}
        check(aj_strategy.size_order("X", "buy", cfg, 0.0, {"edge_pts": 5}) is None,
              "wiring: size_order returns None when drawdown veto fires")
        # neutral cfg sizes normally
        s = aj_strategy.size_order("X", "buy", {"max_order_notional_usd": 1000.0,
                                                "order_notional_target_usd": 500.0}, 0.0, None)
        check(s is not None and s["notional"] > 0, "wiring: size_order sizes normally when off")
    finally:
        aj_risk._order_price = orig_price


# ════════════════════════════════════════════════════════════════════════════
#  ENTRY ALPHA (6-11, 18)
# ════════════════════════════════════════════════════════════════════════════

def test_06_momentum_filter():
    _reset_alpha_patches()
    A._closes = lambda s, p="1y": _trend(100, 1, 60)   # rising → SMA below price
    A._sma = lambda c, n: 120.0                          # force SMA above price
    r = A.entry_block_reason("X", "buy", 1, 100, {"momentum_filter_days": 50})
    check(r is not None and "momentum" in r, "06 momentum: blocks buy below SMA")
    A._sma = lambda c, n: 90.0                           # SMA below price → allow
    check(A.entry_block_reason("X", "buy", 1, 100, {"momentum_filter_days": 50}) is None,
          "06 momentum: allows buy above SMA")


def test_07_mean_reversion_rsi():
    _reset_alpha_patches()
    A._closes = lambda s, p="1y": _trend(100, 1, 60)
    A._rsi = lambda c, n=14: 85.0
    r = A.entry_block_reason("X", "buy", 1, 100, {"mean_reversion_rsi_max": 70})
    check(r is not None and "overbought" in r, "07 mean-rev: blocks overbought (RSI>max)")
    A._rsi = lambda c, n=14: 55.0
    check(A.entry_block_reason("X", "buy", 1, 100, {"mean_reversion_rsi_max": 70}) is None,
          "07 mean-rev: allows when RSI under the cap")


def test_08_relative_strength():
    _reset_alpha_patches()
    A._closes = lambda s, p="1y": _trend(100, 0.1, 60)   # weak symbol
    A._market_closes = lambda p="1y": _trend(100, 1.0, 60)  # strong market
    r = A.entry_block_reason("X", "buy", 1, 110, {"relative_strength_filter": True,
                                                  "relative_strength_lookback_days": 20})
    check(r is not None and "RS" in r, "08 rel-strength: blocks an underperformer")
    A._closes = lambda s, p="1y": _trend(100, 2.0, 60)   # strong symbol
    check(A.entry_block_reason("X", "buy", 1, 110, {"relative_strength_filter": True,
                                                    "relative_strength_lookback_days": 20}) is None,
          "08 rel-strength: allows an outperformer")


def test_09_correlation_gate():
    _reset_alpha_patches()
    A._paper_book = lambda: {"positions": {"AAA": {"qty": 1, "avg_cost": 10}}}
    series = _trend(100, 1, 60)
    A._closes = lambda s, p="6mo": series                 # identical → corr ~1.0
    r = A.entry_block_reason("X", "buy", 1, 100, {"max_book_correlation": 0.8})
    check(r is not None and "corr" in r, "09 correlation: blocks a highly-correlated add")
    # uncorrelated book (X trends smoothly, AAA oscillates) → allow
    osc = [100.0 + (5.0 if i % 2 else 0.0) for i in range(60)]
    A._closes = lambda s, p="6mo": (series if s == "X" else osc)
    A.reset_cycle_cache()   # re-patched _closes -> drop the per-cycle returns memo
    check(A.entry_block_reason("X", "buy", 1, 100, {"max_book_correlation": 0.8}) is None,
          "09 correlation: allows a diversifying name")


def test_10_earnings_blackout():
    _reset_alpha_patches()
    A._earnings_days_away = lambda s: 2
    r = A.entry_block_reason("X", "buy", 1, 100, {"earnings_blackout_days": 5})
    check(r is not None and "earnings" in r, "10 earnings: blocks inside the blackout window")
    A._earnings_days_away = lambda s: 20
    check(A.entry_block_reason("X", "buy", 1, 100, {"earnings_blackout_days": 5}) is None,
          "10 earnings: allows well clear of earnings")


def test_11_sector_cap():
    _reset_alpha_patches()
    import aj_risk
    orig = aj_risk._marks
    aj_risk._marks = lambda syms: {s: 100.0 for s in syms}
    try:
        A._paper_book = lambda: {"positions": {"AAA": {"qty": 10, "avg_cost": 100}}}
        A._sector_of = lambda s: "Technology"             # everything tech
        r = A.entry_block_reason("X", "buy", 10, 100, {"max_sector_weight_pct": 50})
        check(r is not None and "sector" in r, "11 sector: blocks over-concentration")
        A._sector_of = lambda s: ("Tech" if s == "X" else "Energy")
        check(A.entry_block_reason("X", "buy", 1, 100, {"max_sector_weight_pct": 50}) is None,
              "11 sector: allows a diversifying sector")
    finally:
        aj_risk._marks = orig


def test_18_pyramiding():
    _reset_alpha_patches()
    # held winner up 5%, no prior adds → allowed
    A._paper_book = lambda: {"positions": {"X": {"qty": 10, "avg_cost": 100}}}
    aj_db.query("DELETE FROM aj_position_state")
    cfg = {"pyramiding": True, "pyramid_min_gain_pct": 3.0, "pyramid_max_adds": 2}
    check(A.entry_block_reason("X", "buy", 1, 105, cfg) is None,
          "18 pyramid: allows adding to a winner up enough")
    # not up enough → blocked
    r = A.entry_block_reason("X", "buy", 1, 101, cfg)
    check(r is not None and "pyramid" in r, "18 pyramid: blocks an add on a flat position")


def test_entry_gate_wired_into_rules():
    _reset_alpha_patches()
    A._earnings_days_away = lambda s: 1
    r = aj_rules.enhanced_gate("X", "buy", 1, 100, {"earnings_blackout_days": 5})
    check(r is not None and "earnings" in r, "wiring: enhanced_gate surfaces alpha entry blocks")


# ════════════════════════════════════════════════════════════════════════════
#  EXIT INTELLIGENCE (12-15)
# ════════════════════════════════════════════════════════════════════════════

def _seed_state(symbol, opened_at=None, peak=None):
    aj_db.query("DELETE FROM aj_position_state WHERE symbol=?", (symbol,))
    aj_db.insert("aj_position_state", symbol=symbol, opened_at=opened_at,
                 peak_mark=peak, last_mark=peak, updated_at=aj_db.utc_now_iso(),
                 last_exit_at=None)


def test_12_time_stop():
    _reset_alpha_patches()
    import aj_risk
    orig = aj_risk._marks
    aj_risk._marks = lambda syms: {s: 100.0 for s in syms}
    try:
        A._paper_book = lambda: {"positions": {"X": {"qty": 5, "avg_cost": 95}}}
        old = (aj_db.utc_now() - __import__("datetime").timedelta(days=20)).isoformat()
        _seed_state("X", opened_at=old, peak=110)
        sigs = A.extra_exit_signals({"max_holding_days": 10})
        check(any(s["symbol"] == "X" and "max-holding" in s["reason"] for s in sigs),
              "12 time-stop: exits a position past max holding days")
        _seed_state("X", opened_at=aj_db.utc_now_iso(), peak=110)
        check(A.extra_exit_signals({"max_holding_days": 10}) == [],
              "12 time-stop: keeps a fresh position")
    finally:
        aj_risk._marks = orig


def test_13_profit_ratchet():
    _reset_alpha_patches()
    import aj_risk
    orig = aj_risk._marks
    # peaked at 130 (+30%), now back to 102 (+2%); lock floor 5% → exit
    aj_risk._marks = lambda syms: {s: 102.0 for s in syms}
    try:
        A._paper_book = lambda: {"positions": {"X": {"qty": 5, "avg_cost": 100}}}
        _seed_state("X", opened_at=aj_db.utc_now_iso(), peak=130)
        sigs = A.extra_exit_signals({"profit_ratchet_pct": 20.0, "profit_ratchet_lock_pct": 5.0})
        check(any("profit-ratchet" in s["reason"] for s in sigs),
              "13 ratchet: exits after giving back gains to the lock floor")
    finally:
        aj_risk._marks = orig


def test_14_tp_ladder():
    _reset_alpha_patches()
    import aj_risk
    orig = aj_risk._marks
    try:
        A._paper_book = lambda: {"positions": {"X": {"qty": 9, "avg_cost": 100}}}
        _seed_state("X", opened_at=aj_db.utc_now_iso(), peak=100)
        # +12% with tp 10 → rung-1, scale out ~1/3
        aj_risk._marks = lambda syms: {s: 112.0 for s in syms}
        sigs = A.extra_exit_signals({"tp_ladder": True, "take_profit_pct": 10.0})
        rung1 = [s for s in sigs if "rung-1" in s["reason"]]
        check(rung1 and abs(rung1[0]["qty"] - 3.0) < 0.1,
              "14 tp-ladder: rung-1 scales out ~1/3 of the position")
        # +25% → rung-3, sell the rest
        aj_risk._marks = lambda syms: {s: 125.0 for s in syms}
        sigs = A.extra_exit_signals({"tp_ladder": True, "take_profit_pct": 10.0})
        check(any("rung-3" in s["reason"] and abs(s["qty"] - 9.0) < 0.1 for s in sigs),
              "14 tp-ladder: rung-3 exits the remainder")
    finally:
        aj_risk._marks = orig


def test_15_atr_stop():
    _reset_alpha_patches()
    import aj_risk
    orig = aj_risk._marks
    aj_risk._marks = lambda syms: {s: 90.0 for s in syms}      # down to 90 from 100
    try:
        A._paper_book = lambda: {"positions": {"X": {"qty": 5, "avg_cost": 100}}}
        _seed_state("X", opened_at=aj_db.utc_now_iso(), peak=100)
        # ATR = 2 → 2x ATR stop = 4; 90 <= 100-4 → exit
        A._bars = lambda s, p="6mo": [{"high": 102, "low": 98, "close": 100}] * 30
        A._atr = lambda bars, period=14: 2.0
        sigs = A.extra_exit_signals({"atr_stop_mult": 2.0, "atr_period": 14})
        check(any("atr-stop" in s["reason"] for s in sigs),
              "15 atr-stop: exits when loss exceeds mult x ATR")
        aj_risk._marks = lambda syms: {s: 99.0 for s in syms}  # shallow loss → hold
        check(A.extra_exit_signals({"atr_stop_mult": 2.0}) == [],
              "15 atr-stop: holds within the ATR band")
    finally:
        aj_risk._marks = orig


def test_exits_off_are_noop():
    _reset_alpha_patches()
    check(A.extra_exit_signals({}) == [], "exits: all-off → no extra signals")


# ════════════════════════════════════════════════════════════════════════════
#  ADAPTIVE BRAIN (16-17)
# ════════════════════════════════════════════════════════════════════════════

def test_16_adaptive_thresholds():
    _reset_alpha_patches()
    base = {"buy_prob_threshold": 0.55, "min_edge_pct_pts": 3.0}
    # cold streak → demand more edge
    A._realized_trades = lambda: [{"realized": -10.0} for _ in range(18)] + \
                                 [{"realized": 5.0} for _ in range(2)]
    eff = A.effective_config(dict(base, adaptive_thresholds=True))
    check(eff["buy_prob_threshold"] > 0.55 and eff["min_edge_pct_pts"] > 3.0,
          "16 adaptive: a cold streak tightens entry thresholds")
    # hot streak → loosen slightly
    A._realized_trades = lambda: [{"realized": 10.0} for _ in range(16)] + \
                                 [{"realized": -5.0} for _ in range(4)]
    eff2 = A.effective_config(dict(base, adaptive_thresholds=True))
    check(eff2["buy_prob_threshold"] < 0.55, "16 adaptive: a hot streak loosens the buy threshold")
    check(A.effective_config(base) == base, "16 adaptive: off → cfg unchanged")


def test_17_regime_adaptive():
    _reset_alpha_patches()
    base = {"buy_prob_threshold": 0.55, "sell_prob_threshold": 0.45, "min_edge_pct_pts": 3.0}
    A._market_closes = lambda p="1y": _trend(100, 1.0, 220)      # uptrend → bull
    eff = A.effective_config(dict(base, regime_adaptive=True))
    check(eff.get("_regime") == "bull" and eff["buy_prob_threshold"] < 0.55,
          "17 regime: bull lowers the buy threshold")
    A._market_closes = lambda p="1y": _trend(300, -1.0, 220)     # downtrend → bear
    eff2 = A.effective_config(dict(base, regime_adaptive=True))
    check(eff2.get("_regime") == "bear" and eff2["buy_prob_threshold"] > 0.55,
          "17 regime: bear raises the buy threshold")


# ════════════════════════════════════════════════════════════════════════════
#  SCORECARD (19) + RADAR (20)
# ════════════════════════════════════════════════════════════════════════════

def test_19_scorecard():
    _reset_alpha_patches()
    aj_db.set_setting_raw(A._SCORECARD_KEY, "")
    A.note_entry_conviction("NVDA", "high")
    check(A.pop_entry_conviction("NVDA") == "high", "19 scorecard: entry conviction stored/recalled")
    A.scorecard_record("high", 100.0)
    A.scorecard_record("high", -30.0)
    A.scorecard_record("low", -10.0)
    sc = A.scorecard()["by_conviction"]
    check(sc["high"]["n"] == 2 and sc["high"]["wins"] == 1 and abs(sc["high"]["net_pnl"] - 70.0) < 1e-6,
          "19 scorecard: tallies win-rate and net P&L per conviction")


def test_20_opportunity_radar():
    _reset_alpha_patches()
    scores = {"A": 10.0, "B": 90.0, "C": 50.0, "D": 70.0}
    A._radar_score = lambda s: scores.get(s, 0.0)
    ranked = A.rank_universe(["A", "B", "C", "D"], {"opportunity_radar": True, "opportunity_radar_top_k": 2})
    check(ranked == ["B", "D"], "20 radar: returns the top-K by setup score")
    check(A.rank_universe(["A", "B"], {}) == ["A", "B"], "20 radar: off → universe unchanged")


# ════════════════════════════════════════════════════════════════════════════
#  AUTONOMY (21-25)
# ════════════════════════════════════════════════════════════════════════════

def test_21_scheduler():
    aj_db.set_setting_raw(AU._LAST_RUN_KEY, "")
    cfg = {"auto_run_enabled": True, "auto_run_interval_min": 30,
           "session_whitelist": ["regular", "premarket", "afterhours", "closed"]}
    # patch session to a tradable one
    orig = aj_db.market_session
    aj_db.market_session = lambda *a, **k: "regular"
    try:
        check(AU.due_to_run(cfg)["due"] is True, "21 scheduler: due on first run in-session")
        aj_db.set_setting_raw(AU._LAST_RUN_KEY, aj_db.utc_now_iso())
        check(AU.due_to_run(cfg)["due"] is False, "21 scheduler: not due right after a run")
        check(AU.due_to_run({})["due"] is False, "21 scheduler: off → never due")
    finally:
        aj_db.market_session = orig


def test_22_health_autohalt():
    # enable trading, then inject a fill-rate collapse and confirm auto-halt
    aj_config.set_config({"trading_enabled": True})
    import aj_metrics
    o_orders, o_recon = aj_metrics.order_stats, aj_metrics.recon_stats
    aj_metrics.order_stats = lambda: {"total": 10, "fill_rate": 0.1, "by_state": {}}
    aj_metrics.recon_stats = lambda: {}
    try:
        hc = AU.health_check({"health_autohalt": True})
        check(not hc["ok"] and hc["halted"], "22 health: auto-halts on a fill-rate collapse")
        import aj_risk
        check(aj_risk.is_halted(), "22 health: kill switch actually engaged")
        # clean metrics → healthy, no action
        aj_config.set_config({"trading_enabled": True})
        aj_metrics.order_stats = lambda: {"total": 2, "fill_rate": 1.0, "by_state": {}}
        hc2 = AU.health_check({"health_autohalt": True})
        check(hc2["ok"] and not hc2["halted"], "22 health: clean metrics → no halt")
    finally:
        aj_metrics.order_stats, aj_metrics.recon_stats = o_orders, o_recon


def test_23_preset_escalation():
    aj_db.set_setting_raw(AU._ESCALATION_KEY, "1")        # start at moderate
    import aj_analytics
    o_ts, o_sh = aj_analytics.trade_stats, aj_analytics.sharpe_like
    A._equity_curve = lambda: [{"equity_usd": 1000.0}]    # no drawdown
    aj_analytics.trade_stats = lambda: {"trades": 15, "win_rate": 0.6}
    aj_analytics.sharpe_like = lambda days=90: {"sharpe": 1.2}
    try:
        r = AU.maybe_escalate({"auto_preset_escalation": True})
        check(r["changed"] and r["to"] == "aggressive", "23 escalation: strong record steps up to aggressive")
        # deep drawdown steps back down. equity_usd rows are CUMULATIVE P&L;
        # current_drawdown_pct now adds the capital base (typed-config default
        # compound_base_equity_usd = 10k), so a 20% EQUITY drawdown is
        # peak 10k+1000 = 11k -> 10k-1200 = 8.8k (the old 1000->800 stub only
        # reads 20% under the buggy P&L-relative math).
        A._equity_curve = lambda: [{"equity_usd": 1000.0}, {"equity_usd": -1200.0}]  # 20% dd
        r2 = AU.maybe_escalate({"auto_preset_escalation": True})
        check(r2["changed"] and r2["to"] == "moderate", "23 escalation: a drawdown steps back down")
        check(AU.maybe_escalate({})["changed"] is False, "23 escalation: off → no change")
    finally:
        aj_analytics.trade_stats, aj_analytics.sharpe_like = o_ts, o_sh


def test_24_daily_reflection():
    import aj_analytics, aj_risk
    o_ts, o_at, o_pnl = aj_analytics.trade_stats, aj_analytics.attribution, aj_risk.compute_day_pnl
    aj_analytics.trade_stats = lambda: {"trades": 8, "win_rate": 0.62, "profit_factor": 1.8}
    aj_analytics.attribution = lambda: [{"symbol": "NVDA", "total": 120.0},
                                        {"symbol": "HUT", "total": -40.0}]
    aj_risk.compute_day_pnl = lambda *a, **k: {"day_pnl": 80.0}
    A._equity_curve = lambda: [{"equity_usd": 1000.0}]
    try:
        refl = AU.write_reflection()
        check(refl["win_rate"] == 0.62 and refl["best_name"]["symbol"] == "NVDA" and refl["takeaway"],
              "24 reflection: composes day P&L, best/worst, takeaway")
        got = AU.get_reflection(refl["date"])
        check(got and got["takeaway"] == refl["takeaway"], "24 reflection: persisted and re-readable")
    finally:
        aj_analytics.trade_stats, aj_analytics.attribution, aj_risk.compute_day_pnl = o_ts, o_at, o_pnl


def test_24b_journal_dual_write_and_weekly_rollup():
    # test_24 already ran write_reflection for today: the dual-write must have
    # landed exactly one 'reflection' journal row for today's ET date.
    today = AU._et_date()
    rows = aj_db.query(
        "SELECT payload_json FROM aj_journal WHERE kind='reflection' AND date=?",
        (today,))
    check(len(rows) == 1, "24b journal: reflection dual-written to aj_journal")
    payload = json.loads(rows[0]["payload_json"]) if rows else {}
    check(payload.get("date") == today and payload.get("takeaway"),
          "24b journal: journal payload mirrors the reflection")
    # first-write-wins: a repeat write_reflection must not add a second row
    AU.write_reflection()
    rows2 = aj_db.query(
        "SELECT id FROM aj_journal WHERE kind='reflection' AND date=?", (today,))
    check(len(rows2) == 1, "24b journal: repeat write is idempotent (1 row)")
    # ...and a direct duplicate journal write is refused by UNIQUE(kind,date)
    check(AU._journal_write("reflection", today, {"x": 1}) is False,
          "24b journal: UNIQUE(kind,date) rejects a second same-day write")

    # seed two earlier days so the weekly rollup has a real window to compose
    import datetime as _dt
    d1 = (_dt.date.fromisoformat(today) - _dt.timedelta(days=1)).isoformat()
    d2 = (_dt.date.fromisoformat(today) - _dt.timedelta(days=2)).isoformat()
    AU._journal_write("reflection", d1, {"date": d1, "day_pnl_usd": -30.0,
                                         "win_rate": 0.4, "takeaway": "rough day"})
    AU._journal_write("reflection", d2, {"date": d2, "day_pnl_usd": 200.0,
                                         "win_rate": 0.8, "takeaway": "great day"})
    roll = AU.weekly_rollup()
    check(len(roll["days"]) == 3 and roll["period"].endswith(today),
          "24b weekly: rollup covers the seeded 7-day window")
    check(roll["best_day"]["date"] == d2 and roll["worst_day"]["date"] == d1,
          "24b weekly: best/worst day picked by day P&L")
    # avg win-rate over the days that have one (0.62 from test_24 + 0.4 + 0.8);
    # weekly_rollup rounds to 4 decimals, so compare at that precision
    check(abs(roll["avg_win_rate"] - (0.62 + 0.4 + 0.8) / 3.0) < 1e-3,
          "24b weekly: avg win-rate composed across days")
    check(abs(roll["total_day_pnl"] - (80.0 - 30.0 + 200.0)) < 1e-6,
          "24b weekly: total day P&L summed across days")
    check(len(roll["takeaways"]) == 3, "24b weekly: takeaways collected")
    # idempotent persistence. test_24's write_reflection already bootstrapped a
    # 1-day weekly row (no prior weekly existed -> stale path); clear it so we
    # exercise the full bootstrap -> write -> re-run no-op sequence here.
    conn = database.get_conn()
    conn.execute("DELETE FROM aj_journal WHERE kind='weekly'")
    conn.commit()
    AU._maybe_write_weekly()
    AU._maybe_write_weekly()
    wk = aj_db.query("SELECT date, payload_json FROM aj_journal WHERE kind='weekly'")
    check(len(wk) == 1 and wk[0]["date"] == today,
          "24b weekly: _maybe_write_weekly idempotent via UNIQUE(kind,date)")
    check(json.loads(wk[0]["payload_json"])["days"] == roll["days"],
          "24b weekly: persisted rollup matches the composed one")
    # a fresh weekly row from today is NOT stale -> no rewrite on a weekday
    # (unless today happens to be the ET Sunday, when the last-day path fires)
    ent = AU.journal_entries(kind="reflection", limit=10)
    check([e["date"] for e in ent][:3] == [today, d1, d2],
          "24b journal: journal_entries newest-first with parsed payloads")
    check(ent[0]["payload"].get("takeaway") == payload.get("takeaway"),
          "24b journal: journal_entries payload parsed")


def test_25_premarket_briefing():
    _reset_alpha_patches()
    import aj_operator
    orig_scan = aj_operator._scan_universe
    aj_operator._scan_universe = lambda: ["A", "B", "C", "D", "E", "F"]
    A._radar_score = lambda s: {"A": 5, "B": 95, "C": 30, "D": 80, "E": 10, "F": 60}.get(s, 0)
    A._closes = lambda s, p="1y": _trend(100, 1, 60)
    A._market_closes = lambda p="1y": _trend(100, 1, 60)
    A._rsi = lambda c, n=14: 55.0
    try:
        b = AU.write_briefing({"opportunity_radar_top_k": 3})
        syms = [i["symbol"] for i in b["ideas"]]
        check(syms == ["B", "D", "F"], "25 briefing: ranks the top-K ideas")
        check(b.get("regime") in ("bull", "bear", "chop"), "25 briefing: tags the market regime")
        check(AU.get_briefing()["ideas"], "25 briefing: persisted and re-readable")
        # 24b: the briefing dual-writes into aj_journal under the ET date;
        # a repeat write_briefing (early-return path) must not duplicate it
        AU.write_briefing({"opportunity_radar_top_k": 3})
        jrows = aj_db.query(
            "SELECT payload_json FROM aj_journal WHERE kind='briefing' AND date=?",
            (AU._et_date(),))
        check(len(jrows) == 1, "25 briefing: dual-written to aj_journal once")
        check(json.loads(jrows[0]["payload_json"]).get("ideas"),
              "25 briefing: journal payload carries the ideas")
    finally:
        aj_operator._scan_universe = orig_scan


def test_config_defaults_fail_closed():
    # a never-touched 100x config must leave every enhancement disabled
    aj_db.set_setting_raw("aj_kelly_sizing", "")  # not set
    fresh = {k: v for k, v in aj_config.DEFAULTS.items()}
    for k in ("kelly_sizing", "compound_sizing", "opportunity_radar", "auto_run_enabled",
              "health_autohalt", "auto_preset_escalation", "regime_adaptive", "tp_ladder"):
        check(fresh[k] is False, "defaults: {} starts disabled".format(k))
    for k in ("volatility_target_pct", "drawdown_throttle_pct", "max_book_correlation",
              "profit_ratchet_pct"):
        check(fresh[k] == 0.0, "defaults: {} starts 0 (off)".format(k))
    # ATR stop is now the DEFAULT exit (risk-reducing, strictly safer than off)
    # per the quant audit (P4): it ships on with a non-zero multiplier.
    check(fresh["atr_stop_mult"] > 0.0, "defaults: atr_stop_mult on (default exit)")
    check(fresh["risk_based_sizing"] is False, "defaults: risk_based_sizing starts disabled")
    for k in ("momentum_filter_days", "earnings_blackout_days", "max_holding_days"):
        check(fresh[k] == 0, "defaults: {} starts 0 (off)".format(k))


def test_99_integration_full_cycle():
    """A real aj_operator.run_once() with a batch of enhancements ON — proves
    the whole wired pipeline (effective_config → radar → judge → sizing
    multiplier → entry gate → execute → scorecard) runs without crashing."""
    _reset_alpha_patches()
    import aj_operator, aj_risk, aj_metrics
    # clean, paper-only, tradable config + a batch of 100x flags
    aj_config.set_config({
        "trading_enabled": True, "live_trading_enabled": False,
        "symbol_allowlist": ["AAA", "BBB", "CCC"], "allow_any_symbol": False,
        "max_order_notional_usd": 1000.0, "order_notional_target_usd": 400.0,
        "max_trades_per_day": 10, "max_daily_loss_usd": 5000.0,
        "auto_approve_paper": True, "buy_prob_threshold": 0.55, "min_edge_pct_pts": 1.0,
        "session_whitelist": ["regular"],
        # enhancements on
        "kelly_sizing": True, "opportunity_radar": True, "opportunity_radar_top_k": 2,
        "regime_adaptive": True, "adaptive_thresholds": True, "signal_scorecard": True,
        "momentum_filter_days": 50, "tp_ladder": True, "take_profit_pct": 10.0,
    })
    # monkeypatch everything network-touching to be deterministic
    o_sess, o_price, o_marks, o_fc = (aj_db.market_session, aj_risk._order_price,
                                      aj_risk._marks, aj_operator._forecast)
    A._market_closes = lambda p="1y": _trend(100, 1.0, 220)        # bull regime
    A._closes = lambda s, p="1y": _trend(100, 1.0, 60)
    A._sma = lambda c, n: 90.0                                     # price above SMA → momentum OK
    A._radar_score = lambda s: {"AAA": 90, "BBB": 80, "CCC": 10}.get(s, 0)
    A._realized_trades = lambda: []
    A._equity_curve = lambda: []
    aj_db.market_session = lambda *a, **k: "regular"
    aj_risk._order_price = lambda *a, **k: 100.0
    aj_risk._marks = lambda syms: {s: 100.0 for s in syms}
    aj_operator._forecast = lambda sym, hz: {
        "ensemble": {"prob_up": 0.72, "edge_pct_pts": 22.0, "conviction": "high",
                     "direction": "UP", "verdict": "STRONG BUY"}}
    try:
        result = aj_operator.run_once("paper")
        check(result.get("ok") is True, "99 integration: cycle completes ok")
        # radar capped the universe to top-2 (AAA, BBB) → CCC never proposed
        proposed = {p.get("symbol") for p in result.get("proposals") or []}
        check("CCC" not in proposed, "99 integration: radar trimmed the universe (CCC excluded)")
        executed = [p for p in result.get("proposals") or [] if p.get("result") == "executed"]
        check(len(executed) >= 1, "99 integration: at least one buy executed end-to-end")
        # the executed buys recorded entry conviction for the scorecard
        check(A.pop_entry_conviction("AAA") == "high" or A.pop_entry_conviction("BBB") == "high",
              "99 integration: scorecard captured the entry conviction")
    finally:
        (aj_db.market_session, aj_risk._order_price, aj_risk._marks,
         aj_operator._forecast) = o_sess, o_price, o_marks, o_fc
        aj_config.set_config({"trading_enabled": False})


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    print("aj_100x — {} test groups\n".format(len(tests)))
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
