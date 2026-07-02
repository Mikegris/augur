#!/usr/bin/env python3
"""Full-cycle integration test — operator run_once with the screener + options.

Closes the "no end-to-end cycle test" gap. Network-free: the universe, forecast,
quotes, and option chain are all injected.
"""
import os
import sys
import tempfile

os.environ["AUGUR_DB_PATH"] = tempfile.mktemp(suffix="_ajcycle.db")

import database as db          # noqa: E402
import aj_db                    # noqa: E402
import aj_config                # noqa: E402
import aj_operator              # noqa: E402
import aj_options               # noqa: E402
import aj_positions             # noqa: E402
import fetcher                  # noqa: E402

aj_db.aj_init()


def _full_cfg(**over):
    cfg = {"trading_enabled": True, "universe_mode": "market_screen",
           "auto_approve_paper": True, "default_broker": "paper",
           "max_order_notional_usd": 100000, "max_trades_per_day": 50,
           "max_daily_loss_usd": 100000, "min_edge_pct_pts": 0.0,
           "order_notional_target_usd": 5000,
           "session_whitelist": ["premarket", "regular", "afterhours"],
           "council_enabled": False}
    cfg.update(over)
    aj_config.set_config(cfg)


def _reset_state():
    conn = db.get_conn()
    for t in ("aj_fills", "aj_orders", "aj_proposals", "aj_cycles"):
        conn.execute("DELETE FROM " + t)
    conn.commit()


def _mocks(scan, fc):
    aj_operator._scan_universe = lambda: scan
    aj_operator._forecast = lambda sym, h: fc
    fetcher.get_quotes_batch = lambda syms: {s: {"price": 150.0, "volume": 1e7,
                                                 "market_cap": 1e11, "change_pct": 2.0}
                                             for s in syms}
    aj_options.mark = lambda s, **k: 510.0


def test_cycle_equity_buy_when_options_off():
    _reset_state()
    _full_cfg(trade_options=False)
    _mocks(["AAPL"], {"ensemble": {"prob_up": 0.80, "edge_pct_pts": 10.0}})
    out = aj_operator.run_once("paper")
    assert out["ok"], out
    props = out["proposals"]
    assert any(p.get("symbol") == "AAPL" and p.get("result") == "executed" for p in props), props
    # a plain equity position
    book = aj_positions.paper_book().get("positions") or {}
    assert "AAPL" in book and book["AAPL"]["asset_type"] == "stock"


def test_cycle_bullish_becomes_long_call_when_options_on():
    _reset_state()
    _full_cfg(trade_options=True)
    _mocks(["AAPL"], {"ensemble": {"prob_up": 0.80, "edge_pct_pts": 10.0}})
    aj_options.pick_contract = lambda und, side, cfg, **k: {
        "symbol": aj_options.format_symbol(und, "2026-07-17", "call", 150.0),
        "underlying": und, "option_type": "call", "strike": 150.0,
        "expiry": "2026-07-17", "premium_contract": 510.0, "contract_multiplier": 100,
        "instrument_type": "option"}
    out = aj_operator.run_once("paper")
    assert out["ok"], out
    book = aj_positions.paper_book().get("positions") or {}
    opt = [s for s in book if s.startswith("OPT:") and ":C:" in s]
    assert opt, "bullish signal should have opened a long CALL: " + str(list(book))
    assert book[opt[0]]["asset_type"] == "option"


def test_cycle_bearish_becomes_long_put_when_options_on():
    _reset_state()
    _full_cfg(trade_options=True, option_trade_puts=True)
    _mocks(["XYZ"], {"ensemble": {"prob_up": 0.20, "edge_pct_pts": 12.0}})   # bearish
    aj_options.mark = lambda s, **k: 300.0          # mark == picked premium (chain-consistent)
    aj_options.pick_contract = lambda und, side, cfg, **k: {
        "symbol": aj_options.format_symbol(und, "2026-07-17", "put", 140.0),
        "underlying": und, "option_type": "put", "strike": 140.0,
        "expiry": "2026-07-17", "premium_contract": 300.0, "contract_multiplier": 100,
        "instrument_type": "option"}
    out = aj_operator.run_once("paper")
    assert out["ok"], out
    book = aj_positions.paper_book().get("positions") or {}
    puts = [s for s in book if s.startswith("OPT:") and ":P:" in s]
    assert puts, "bearish signal should have opened a long PUT: " + str(list(book))


def test_cycle_audit_chain_intact_after_full_cycle():
    _reset_state()
    _full_cfg(trade_options=False)
    _mocks(["AAPL"], {"ensemble": {"prob_up": 0.80, "edge_pct_pts": 10.0}})
    aj_operator.run_once("paper")
    chain = aj_db.verify_audit_chain()
    assert chain["ok"], chain


def test_explain_cli_prints_decision_funnel():
    # `aj explain` renders the aj_cycle_stats snapshot a cycle just wrote —
    # network-free, plain text, and it must find the latest row by default.
    _reset_state()
    _full_cfg(trade_options=False)
    _mocks(["AAPL"], {"ensemble": {"prob_up": 0.80, "edge_pct_pts": 10.0}})
    out = aj_operator.run_once("paper")
    assert out["ok"], out
    import io
    import contextlib
    import aj_cli
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = aj_cli.cmd_explain([])
    text = buf.getvalue()
    assert (rc or 0) == 0, text
    assert "funnel" in text and "executed" in text and "AAPL" in text, text
    # the cycle id printed is queryable back through --cycle
    row = aj_db.query("SELECT cycle_id FROM aj_cycle_stats ORDER BY ts DESC LIMIT 1")[0]
    buf2 = io.StringIO()
    with contextlib.redirect_stdout(buf2):
        rc2 = aj_cli.cmd_explain(["--cycle", row["cycle_id"]])
    assert (rc2 or 0) == 0 and row["cycle_id"] in buf2.getvalue()
    # unknown cycle -> non-zero, friendly message
    buf3 = io.StringIO()
    with contextlib.redirect_stdout(buf3):
        rc3 = aj_cli.cmd_explain(["--cycle", "nope"])
    assert rc3 == 1 and "no cycle stats" in buf3.getvalue()


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    print("aj_cycle — {} tests".format(len(fns)))
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
