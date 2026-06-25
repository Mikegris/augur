#!/usr/bin/env python3
"""AJTA enhancement tests — the 25 features. Fresh temp DB; no network/LLM."""
import os
import sys
import tempfile

os.environ["AUGUR_DB_PATH"] = tempfile.mktemp(suffix="_ajenh.db")

import database as db          # noqa: E402
import fetcher                  # noqa: E402
import aj_db                    # noqa: E402
import aj_config                # noqa: E402
import aj_risk                  # noqa: E402
import aj_positions             # noqa: E402
import aj_strategy              # noqa: E402
import aj_rules                 # noqa: E402
import aj_analytics             # noqa: E402

aj_db.aj_init()
aj_db.market_session = lambda dt=None: "regular"

_TABLES = ("aj_orders", "aj_fills", "aj_proposals", "aj_risk_events", "aj_audit",
           "aj_routing", "aj_recon", "aj_cycles", "aj_equity", "aj_cycle_log",
           "aj_position_state")


def _reset():
    conn = db.get_conn()
    for t in _TABLES:
        conn.execute("DELETE FROM {}".format(t))
    conn.execute("DELETE FROM settings WHERE key LIKE 'aj_%' OR key LIKE '__aj_%'")
    conn.commit()
    try:
        db._invalidate_settings_cache()
    except Exception:
        pass


def _quotes(m):
    fetcher.get_quote = lambda s: {"price": m.get(s.upper())}
    fetcher.get_quotes_batch = lambda syms: {s: {"price": m.get(s.upper())} for s in syms}


def _full(**over):
    cfg = {"trading_enabled": True, "symbol_allowlist": ["NVDA"],
           "max_order_notional_usd": 10000, "max_trades_per_day": 50,
           "max_daily_loss_usd": 99999}
    cfg.update(over)
    aj_config.set_config(cfg)


def _fill(sym, side, qty, price, fees=0.0):
    oid = aj_db.insert("aj_orders", proposal_id=1, client_order_id=sym + side + str(qty) + str(price),
                       broker="paper", mode="paper", symbol=sym, side=side, qty=qty,
                       order_type="market", state="filled", created_at=aj_db.utc_now_iso())
    aj_db.insert("aj_fills", order_id=oid, qty=qty, price=price, fees_usd=fees,
                 filled_at=aj_db.utc_now_iso())


# ── ① conviction sizing ───────────────────────────────────────────────────────

def test_conviction_sizing():
    _reset(); _quotes({"NVDA": 800}); _full(conviction_sizing=True, order_notional_target_usd=4000)
    weak = aj_strategy.size_order("NVDA", "buy", aj_config.get_config(), 0, {"edge_pts": 4})
    strong = aj_strategy.size_order("NVDA", "buy", aj_config.get_config(), 0, {"edge_pts": 20})
    assert strong["notional"] > weak["notional"]            # stronger edge => bigger


# ── ② min order notional ───────────────────────────────────────────────────────

def test_min_order_notional():
    _reset(); _quotes({"NVDA": 800}); _full(min_order_notional_usd=5000, order_notional_target_usd=1000)
    assert aj_strategy.size_order("NVDA", "buy", aj_config.get_config(), 0) is None   # below floor


# ── ③ limit entries ───────────────────────────────────────────────────────────

def test_limit_entry_offset():
    _reset(); _quotes({"NVDA": 800}); _full(entry_order_type="limit", entry_limit_offset_bps=10, order_notional_target_usd=4000)
    s = aj_strategy.size_order("NVDA", "buy", aj_config.get_config(), 0)
    assert s["order_type"] == "limit" and s["limit_price"] < 800   # passive below


# ── ⑤⑥⑦ exit signals ─────────────────────────────────────────────────────────

def test_take_profit_and_stop_loss():
    _reset(); _full(take_profit_pct=5, stop_loss_pct=5)
    _fill("NVDA", "buy", 10, 800.0)
    aj_rules.update_position_state()
    _quotes({"NVDA": 850})                                   # +6.25% -> take-profit
    sigs = aj_rules.exit_signals()
    assert any("take-profit" in s["reason"] for s in sigs)
    _quotes({"NVDA": 740})                                   # -7.5% -> stop-loss
    sigs = aj_rules.exit_signals()
    assert any("stop-loss" in s["reason"] for s in sigs)


def test_trailing_stop():
    _reset(); _full(trailing_stop_pct=5)
    _fill("NVDA", "buy", 10, 800.0)
    _quotes({"NVDA": 900}); aj_rules.update_position_state()  # peak = 900
    _quotes({"NVDA": 850})                                    # 5.5% off peak
    sigs = aj_rules.exit_signals()
    assert any("trailing" in s["reason"] for s in sigs)


# ── ⑧ cooldown ────────────────────────────────────────────────────────────────

def test_exit_cooldown():
    _reset(); _full(exit_cooldown_min=60)
    # mark a recent exit
    aj_db.insert("aj_position_state", symbol="NVDA", opened_at=None, peak_mark=None,
                 last_mark=800, updated_at=aj_db.utc_now_iso(), last_exit_at=aj_db.utc_now_iso())
    assert aj_rules.in_cooldown("NVDA") is True
    assert aj_rules.in_cooldown("AAPL") is False


# ── ⑨ max open positions ───────────────────────────────────────────────────────

def test_max_open_positions_gate():
    _reset(); _quotes({"NVDA": 800, "AAA": 10, "BBB": 10}); _full(max_open_positions=2)
    _fill("AAA", "buy", 10, 10.0); _fill("BBB", "buy", 10, 10.0)   # 2 open
    r = aj_rules.enhanced_gate("NVDA", "buy", 1, 800, aj_config.get_config())
    assert r and "max open positions" in r


# ── ⑩ per-symbol weight cap ────────────────────────────────────────────────────

def test_symbol_weight_cap_gate():
    _reset(); _quotes({"AAA": 100}); _full(max_symbol_weight_pct=40)
    _fill("AAA", "buy", 1, 100.0)   # book = $100 of AAA
    # buying $1000 more AAA -> ~100% weight -> blocked
    r = aj_rules.enhanced_gate("AAA", "buy", 10, 100, aj_config.get_config())
    assert r and "% of book" in r


# ── ⑪ per-symbol daily cap ─────────────────────────────────────────────────────

def test_per_symbol_daily_cap():
    _reset(); _quotes({"NVDA": 800}); _full(max_trades_per_symbol_per_day=1)
    aj_db.insert("aj_orders", proposal_id=1, client_order_id="z", broker="paper", mode="paper",
                 symbol="NVDA", side="buy", qty=1, order_type="market", state="filled",
                 created_at=aj_db.utc_now_iso(), submitted_at=aj_db.utc_now_iso())
    r = aj_rules.enhanced_gate("NVDA", "buy", 1, 800, aj_config.get_config())
    assert r and "per-symbol daily" in r


# ── ⑫ time-of-day filter ───────────────────────────────────────────────────────

def test_time_of_day_filter():
    _reset(); _quotes({"NVDA": 800}); _full(trade_skip_open_min=30)
    import aj_rules as r
    orig = r._minutes_into_regular
    r._minutes_into_regular = lambda: 10        # 10 min into session
    try:
        out = r.enhanced_gate("NVDA", "buy", 1, 800, aj_config.get_config())
        assert out and "first 30 min" in out
    finally:
        r._minutes_into_regular = orig


# ── ⑭ risk-off VIX ─────────────────────────────────────────────────────────────

def test_risk_off_vix_gate():
    _reset(); _quotes({"NVDA": 800}); _full(risk_off_vix=25)
    import aj_rules as r
    orig = r.current_vix
    r.current_vix = lambda: 30.0
    try:
        out = r.enhanced_gate("NVDA", "buy", 1, 800, aj_config.get_config())
        assert out and "risk-off" in out
    finally:
        r.current_vix = orig


# ── gate integration: held-sell bypasses allowlist ────────────────────────────

def test_held_sell_bypasses_allowlist():
    _reset(); _quotes({"ZZZ": 50}); _full(symbol_allowlist=["NVDA"])   # ZZZ not allowed
    _fill("ZZZ", "buy", 10, 50.0)                                       # but held
    d = aj_risk.evaluate({"symbol": "ZZZ", "side": "sell", "qty": 5, "order_type": "market"})
    assert d["decision"] == "pass", d                                  # can always close


# ── ⑯⑰⑱⑲⑳ analytics ─────────────────────────────────────────────────────────

def test_equity_snapshot_and_curve():
    _reset(); _quotes({"NVDA": 850}); _full()
    _fill("NVDA", "buy", 10, 800.0)
    aj_analytics.snapshot_equity()
    curve = aj_analytics.equity_curve()
    assert curve and curve[-1]["equity_usd"] is not None


def test_trade_stats_and_attribution():
    _reset(); _quotes({"NVDA": 900}); _full()
    _fill("NVDA", "buy", 10, 800.0); _fill("NVDA", "sell", 10, 900.0)   # +1000 win
    st = aj_analytics.trade_stats()
    assert st["trades"] == 1 and st["wins"] == 1 and st["net"] == 1000.0
    attr = aj_analytics.attribution()
    assert any(a["symbol"] == "NVDA" and a["realized"] == 1000.0 for a in attr)


def test_sharpe_needs_history():
    _reset()
    assert aj_analytics.sharpe_like()["sharpe"] is None      # no data
    for i, eq in enumerate([100, 110, 105, 120]):
        aj_db.insert("aj_equity", ts=aj_db.utc_now_iso(), date="2026-06-{:02d}".format(20 + i),
                     realized_usd=0, unrealized_usd=0, equity_usd=eq, day_pnl_usd=eq - 100)
    assert aj_analytics.sharpe_like()["sharpe"] is not None


def test_positions_detail():
    _reset(); _quotes({"NVDA": 900}); _full()
    _fill("NVDA", "buy", 10, 800.0)
    aj_rules.update_position_state()
    pd = aj_analytics.positions_detail()
    assert pd["count"] == 1
    p = pd["positions"][0]
    assert p["symbol"] == "NVDA" and p["mark"] == 900 and p["unrealized"] == 1000.0
    assert p["unrealized_pct"] == 12.5 and p["weight_pct"] == 100.0


def test_position_aging():
    _reset(); _quotes({"NVDA": 800}); _full()
    _fill("NVDA", "buy", 5, 800.0)
    aj_rules.update_position_state()
    aging = aj_analytics.position_aging()
    assert any(a["symbol"] == "NVDA" for a in aging)


# ── ㉒ cycle log + ㉔ journal ──────────────────────────────────────────────────

def test_cycle_log_and_journal():
    _reset(); _quotes({"NVDA": 800}); _full()
    aj_analytics.log_cycle({"cycle_id": "c1", "mode": "paper", "session": "regular",
                            "proposals": [{"result": "executed"}, {"result": "no_edge"}]})
    hist = aj_analytics.cycle_history()
    assert hist and hist[0]["n_executed"] == 1
    _fill("NVDA", "buy", 3, 800.0)
    csv = aj_analytics.journal_csv()
    assert "symbol" in csv and "NVDA" in csv


# ── ㉓ presets ─────────────────────────────────────────────────────────────────

def test_presets():
    _reset()
    assert aj_config.apply_preset("bogus") is None
    c = aj_config.apply_preset("conservative")
    assert c["max_open_positions"] == 3 and c["stop_loss_pct"] == 5.0
    c = aj_config.apply_preset("aggressive")
    assert c["max_order_notional_usd"] == 2500
    # presets must NOT touch the safety switches
    assert c["live_trading_enabled"] is False


# ── ㉑ backtest-lite ───────────────────────────────────────────────────────────

def test_backtest_lite_safe():
    out = aj_analytics.backtest_lite()
    assert out.get("enables_live", False) is False or "does NOT enable live" in out.get("note", "")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    print("aj_enhance — {} tests".format(len(fns)))
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
