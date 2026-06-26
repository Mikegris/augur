#!/usr/bin/env python3
"""Phase C tests — options engine + v4 schema (foundation, pre-wiring).

Network-free: chain / spot / expirations are injected.
"""
import os
import sys
import tempfile
from datetime import datetime, timezone, timedelta

os.environ["AUGUR_DB_PATH"] = tempfile.mktemp(suffix="_ajopt.db")

import database as db          # noqa: E402
import aj_db                    # noqa: E402
import aj_options as O          # noqa: E402

aj_db.aj_init()


def _future(days):
    return (datetime.now(timezone.utc) + timedelta(days=days)).strftime("%Y-%m-%d")


def _chain(calls):
    return lambda und, exp: {"calls": calls, "puts": []}


def test_symbol_roundtrip():
    s = O.format_symbol("AAPL", "2026-07-17", "call", 150.0)
    assert s == "OPT:AAPL:20260717:C:150.0"
    assert O.is_option(s) and not O.is_option("AAPL")
    m = O.parse_symbol(s)
    assert m["underlying"] == "AAPL" and m["option_type"] == "call"
    assert m["strike"] == 150.0 and m["expiry"] == "2026-07-17"
    assert O.parse_symbol("AAPL") is None


def test_pick_contract_atm_call():
    exp = _future(40)
    calls = [
        {"strike": 140, "bid": 12.0, "ask": 12.4, "lastPrice": 12.2},
        {"strike": 150, "bid": 5.0, "ask": 5.2, "lastPrice": 5.1},   # ATM (spot 151)
        {"strike": 160, "bid": 1.0, "ask": 1.2, "lastPrice": 1.1},
    ]
    c = O.pick_contract("AAPL", "buy", {"option_target_dte": 35, "option_moneyness": 0.0},
                        spot=151.0, expirations=[exp], chain_fn=_chain(calls))
    assert c is not None
    assert c["strike"] == 150            # nearest to spot
    assert c["option_type"] == "call" and c["instrument_type"] == "option"
    # per-contract premium = mid(5.0,5.2)=5.1 × 100
    assert abs(c["premium_contract"] - 510.0) < 1e-6
    assert c["symbol"].startswith("OPT:AAPL:")


def test_pick_contract_otm_via_moneyness():
    exp = _future(40)
    calls = [{"strike": 150, "bid": 5, "ask": 5.2}, {"strike": 160, "bid": 1, "ask": 1.2}]
    c = O.pick_contract("AAPL", "buy", {"option_moneyness": 0.06},   # ~+6% OTM of 151 = 160
                        spot=151.0, expirations=[exp], chain_fn=_chain(calls))
    assert c["strike"] == 160


def test_pick_contract_sell_returns_none():
    assert O.pick_contract("AAPL", "sell", {}, spot=100, expirations=[_future(40)],
                           chain_fn=_chain([{"strike": 100, "bid": 1, "ask": 1.2}])) is None


def test_pick_contract_failopen():
    assert O.pick_contract("AAPL", "buy", {}, spot=None, expirations=[], chain_fn=_chain([])) is None
    assert O.pick_contract("", "buy", {}, spot=100, expirations=[_future(40)]) is None


def test_mark_held_option():
    exp = _future(20)
    sym = O.format_symbol("AAPL", exp, "call", 150.0)
    calls = [{"strike": 150, "bid": 7.0, "ask": 7.4, "lastPrice": 7.2}]
    m = O.mark(sym, chain_fn=_chain(calls))
    assert abs(m - 720.0) < 1e-6          # mid 7.2 × 100


def test_mark_expired_is_zero():
    sym = O.format_symbol("AAPL", _future(-2), "call", 150.0)
    assert O.mark(sym, chain_fn=_chain([])) == 0.0


def test_v4_migration_adds_option_columns():
    aj_db.aj_init()
    assert aj_db._applied_version() >= 4
    conn = db.get_conn()
    for table in ("aj_proposals", "aj_orders"):
        cols = {r["name"] for r in conn.execute("PRAGMA table_info({})".format(table)).fetchall()}
        for c in ("instrument_type", "underlying", "option_type", "strike", "expiry",
                  "contract_multiplier"):
            assert c in cols, "{}.{} missing".format(table, c)
    # idempotent
    assert aj_db.aj_migrate() >= 4
    # can insert an option proposal via the generic helper
    pid = aj_db.insert("aj_proposals", created_at=aj_db.utc_now_iso(), cycle_id="c1",
                       symbol="OPT:AAPL:20260717:C:150.0", side="buy", qty=2,
                       notional_usd=1020.0, order_type="limit", limit_price=510.0,
                       instrument_type="option", underlying="AAPL", option_type="call",
                       strike=150.0, expiry="2026-07-17", contract_multiplier=100,
                       status="proposed")
    assert pid > 0
    row = conn.execute("SELECT instrument_type, strike FROM aj_proposals WHERE id=?", (pid,)).fetchone()
    assert row["instrument_type"] == "option" and row["strike"] == 150.0


def test_option_proposal_executes_paper():
    """Full path: option proposal -> gate (open universe, OPT: bypasses the equity
    regex) -> PaperBroker fills at the chain premium -> option position booked."""
    import aj_config, aj_operator, aj_positions, aj_broker, aj_risk
    aj_db.aj_init()
    # fresh agent state
    conn = db.get_conn()
    for t in ("aj_fills", "aj_orders", "aj_proposals"):
        conn.execute("DELETE FROM " + t)
    conn.commit()
    aj_config.set_config({"trading_enabled": True, "universe_mode": "market_screen",
                          "allow_any_symbol": False, "auto_approve_paper": True,
                          "default_broker": "paper", "max_order_notional_usd": 100000,
                          "max_trades_per_day": 50, "max_daily_loss_usd": 100000,
                          "session_whitelist": ["premarket", "regular", "afterhours"],
                          "trade_options": True})
    # price the option at $510/contract everywhere (gate uses limit_price; broker
    # + marks use aj_options.mark)
    import aj_options
    _orig_mark = aj_options.mark
    aj_options.mark = lambda s, **k: 510.0
    sym = "OPT:AAPL:20260717:C:150.0"
    sizing = {"qty": 2, "price": 510.0, "notional": 1020.0,
              "order_type": "limit", "limit_price": 510.0}
    instrument = {"instrument_type": "option", "underlying": "AAPL",
                  "option_type": "call", "strike": 150.0, "expiry": "2026-07-17",
                  "contract_multiplier": 100}
    try:
        out = aj_operator._propose_and_execute(
            "cyc-opt", sym, {"side": "buy", "thesis": "bull"}, sizing,
            aj_config.get_config(), instrument=instrument)
        assert out.get("result") == "executed", out
        book = aj_positions.paper_book().get("positions") or {}
        assert sym in book, list(book.keys())
        pos = book[sym]
        assert pos["asset_type"] == "option" and abs(pos["qty"] - 2) < 1e-9
        assert abs(pos["avg_cost"] - 510.0) < 1e-6      # per-contract premium
        # the proposal persisted its option metadata
        row = db.get_conn().execute(
            "SELECT instrument_type, underlying, strike FROM aj_proposals "
            "WHERE symbol=? ORDER BY id DESC LIMIT 1", (sym,)).fetchone()
        assert row["instrument_type"] == "option" and row["underlying"] == "AAPL"
    finally:
        aj_options.mark = _orig_mark
        aj_config.set_config({"trade_options": False})


def test_option_marks_via_chain():
    """aj_risk._marks routes OPT: symbols through aj_options.mark (per-contract)."""
    import aj_risk, aj_options
    _orig = aj_options.mark
    aj_options.mark = lambda s, **k: 333.0
    try:
        m = aj_risk._marks(["OPT:AAPL:20260717:C:150.0", "AAPL"])
        assert m["OPT:AAPL:20260717:C:150.0"] == 333.0
    finally:
        aj_options.mark = _orig


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    print("aj_options — {} tests".format(len(fns)))
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
