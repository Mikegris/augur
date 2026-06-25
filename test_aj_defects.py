#!/usr/bin/env python3
"""AJTA defect-sweep regression tests.

Pins the ~55-issue audit fixes so they can't silently regress. Fresh temp DB;
no network/LLM/broker.
"""
import os
import sys
import tempfile

os.environ["AUGUR_DB_PATH"] = tempfile.mktemp(suffix="_ajdef.db")

import database as db          # noqa: E402
import fetcher                  # noqa: E402
import aj_db                    # noqa: E402
import aj_config                # noqa: E402
import aj_risk                  # noqa: E402
import aj_positions             # noqa: E402
import aj_execution             # noqa: E402
import aj_broker                # noqa: E402
import aj_routing               # noqa: E402
import aj_opencode              # noqa: E402
import aj_mcp_read              # noqa: E402

aj_db.aj_init()
_REAL_SESSION = aj_db.market_session
aj_db.market_session = lambda dt=None: "regular"

_TABLES = ("aj_orders", "aj_fills", "aj_proposals", "aj_risk_events",
           "aj_audit", "aj_routing", "aj_recon", "aj_cycles")


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
           "max_order_notional_usd": 10000, "max_trades_per_day": 10,
           "max_daily_loss_usd": 5000}
    cfg.update(over)
    aj_config.set_config(cfg)


# ── foundation ────────────────────────────────────────────────────────────────

def test_raw_setting_roundtrips_hidden_keys():
    # get_settings() hides __ keys; the raw reader must NOT.
    aj_db.set_setting_raw("__aj_test_key", "hello")
    assert db.get_settings().get("__aj_test_key") is None      # hidden
    assert aj_db.get_setting_raw("__aj_test_key") == "hello"   # raw sees it


def test_lease_lock_actually_excludes():
    # Force the lease fallback (bypass fcntl) and prove mutual exclusion — the
    # old code read via get_settings() and always granted.
    a = aj_db.SingleInstanceLock("leasetest")
    b = aj_db.SingleInstanceLock("leasetest")
    assert a._acquire_lease() is True
    assert b._acquire_lease() is False
    a.release()
    assert b._acquire_lease() is True
    b.release()


def test_audit_chain_detects_prefix_deletion():
    _reset()
    for i in range(4):
        aj_db.audit("k", {"i": i})
    assert aj_db.verify_audit_chain()["ok"] is True
    # delete the first row → new first row has a non-null prev_hash
    conn = db.get_conn()
    conn.execute("DELETE FROM aj_audit WHERE id=(SELECT MIN(id) FROM aj_audit)")
    conn.commit()
    v = aj_db.verify_audit_chain()
    assert v["ok"] is False and "prefix" in v["reason"]


def test_audit_chain_detects_prevhash_tamper():
    _reset()
    aj_db.audit("k", {"i": 1}); aj_db.audit("k", {"i": 2})
    conn = db.get_conn()
    conn.execute("UPDATE aj_audit SET prev_hash='deadbeef' WHERE id=(SELECT MAX(id) FROM aj_audit)")
    conn.commit()
    assert aj_db.verify_audit_chain()["ok"] is False


def test_calendar_observed_nyd_same_year():
    from datetime import datetime, timezone
    # 2022-01-01 is a Saturday; the observed shift must NOT file a prior-year
    # date. A normal mid-year regular session still resolves.
    hols = aj_db._us_market_holidays(2022)
    assert all(h.year == 2022 for h in hols)
    assert _REAL_SESSION(datetime(2026, 6, 24, 14, 30, tzinfo=timezone.utc)) == "regular"


# ── config validation ─────────────────────────────────────────────────────────

def test_config_default_broker_validated():
    _reset()
    aj_config.set_config({"default_broker": "papper"})   # typo
    assert aj_config.get_config()["default_broker"] == "paper"
    aj_config.set_config({"default_broker": "alpaca"})    # real venue kept
    assert aj_config.get_config()["default_broker"] == "alpaca"


def test_config_clamps_and_drops_closed_session():
    _reset()
    aj_config.set_config({"max_trades_per_day": -5, "max_order_notional_usd": -100,
                          "session_whitelist": ["closed", "regular"],
                          "buy_prob_threshold": 9.0, "scan_universe_max": -3})
    c = aj_config.get_config()
    assert c["max_trades_per_day"] == 0 and c["max_order_notional_usd"] == 0.0
    assert "closed" not in c["session_whitelist"] and "regular" in c["session_whitelist"]
    assert 0.0 <= c["buy_prob_threshold"] <= 1.0
    assert c["scan_universe_max"] >= 1


# ── FIFO short-cover ───────────────────────────────────────────────────────────

def _fill(sym, side, qty, price, fees=0.0):
    oid = aj_db.insert("aj_orders", proposal_id=1, client_order_id=sym + side + str(qty) + str(price),
                       broker="paper", mode="paper", symbol=sym, side=side, qty=qty,
                       order_type="market", state="filled", created_at=aj_db.utc_now_iso())
    aj_db.insert("aj_fills", order_id=oid, qty=qty, price=price, fees_usd=fees,
                 filled_at=aj_db.utc_now_iso())


def test_fifo_buy_covers_short():
    _reset()
    # sell 10 @ 100 (opens short), then buy 10 @ 90 (covers, +$100 realized)
    _fill("ZZZ", "sell", 10, 100.0)
    _fill("ZZZ", "buy", 10, 90.0)
    book = aj_positions.paper_book()
    assert "ZZZ" not in book["positions"]                  # flat
    assert book["realized_total"] == 100.0, book["realized_total"]  # short profit booked


# ── risk gate ──────────────────────────────────────────────────────────────────

def test_gate_blocks_nan_notional():
    _reset(); _quotes({"NVDA": 800}); _full()
    d = aj_risk.evaluate({"symbol": "NVDA", "side": "buy",
                          "notional_usd": float("inf"), "order_type": "market"})
    assert d["decision"] == "block"


def test_gate_sees_kill_via_fresh_read():
    _reset(); _quotes({"NVDA": 800}); _full()
    # simulate a kill landing in the raw store after cfg was cached
    aj_db.set_setting_raw("aj_trading_enabled", "false")
    d = aj_risk.evaluate({"symbol": "NVDA", "side": "buy", "qty": 1, "order_type": "market"})
    assert d["decision"] == "block" and "master switch" in d["reason"]


def test_daily_loss_sees_unrealized():
    # The session-open snapshot must persist so unrealized drawdown counts.
    _reset(); _full(max_daily_loss_usd=100, daily_loss_basis="realized_plus_unrealized")
    # open a long, then mark it DOWN so unrealized is a big loss
    _quotes({"NVDA": 800})
    _fill("NVDA", "buy", 100, 800.0)
    aj_risk.compute_day_pnl()                  # stamps session-open unrealized at ~0
    _quotes({"NVDA": 790})                      # -$1000 unrealized
    pnl = aj_risk.compute_day_pnl()
    assert pnl["unrealized_now"] <= -900, pnl   # the snapshot didn't move with price
    d = aj_risk.evaluate({"symbol": "NVDA", "side": "buy", "qty": 1, "order_type": "market"})
    assert d["decision"] == "halt", d


# ── execution ──────────────────────────────────────────────────────────────────

def test_idless_fill_dedup():
    _reset()
    oid = aj_db.insert("aj_orders", proposal_id=1, client_order_id="x", broker="paper",
                       mode="paper", symbol="NVDA", side="buy", qty=5, order_type="market",
                       state="accepted", created_at=aj_db.utc_now_iso())
    f = {"qty": 5, "price": 800, "fees_usd": 0, "filled_at": aj_db.utc_now_iso()}  # no broker_fill_id
    aj_execution._record_fill(oid, f, None)
    aj_execution._record_fill(oid, f, None)   # re-report
    n = aj_db.query("SELECT COUNT(*) AS n FROM aj_fills WHERE order_id=?", (oid,))[0]["n"]
    assert n == 1, n


def test_set_state_compare_and_set():
    _reset()
    oid = aj_db.insert("aj_orders", proposal_id=1, client_order_id="y", broker="paper",
                       mode="paper", symbol="NVDA", side="buy", qty=1, order_type="market",
                       state="new", created_at=aj_db.utc_now_iso())
    assert aj_execution._set_state(oid, "submitted") is True
    # a transition asserting the OLD from-state must lose (state already moved)
    aj_db.update("aj_orders", oid, state="filled")
    assert aj_execution._set_state(oid, "canceled") is False or \
        aj_db.get_row("aj_orders", oid)["state"] == "filled"


def test_deterministic_client_order_id_dedup():
    _reset(); _quotes({"NVDA": 800}); _full()
    pid = aj_db.insert("aj_proposals", created_at=aj_db.utc_now_iso(), cycle_id="c",
                       symbol="NVDA", side="buy", qty=1, order_type="market", status="proposed")
    prop = {"id": pid, "symbol": "NVDA", "side": "buy", "qty": 1, "order_type": "market"}
    d = aj_risk.evaluate(prop)
    ex1 = aj_execution.execute_trade(prop, d)
    ex2 = aj_execution.execute_trade(prop, d)   # double-submit same proposal
    assert ex1.get("order_id")
    assert ex2.get("ok") is False and "duplicate" in str(ex2.get("reason"))
    n = aj_db.query("SELECT COUNT(*) AS n FROM aj_orders WHERE proposal_id=?", (pid,))[0]["n"]
    assert n == 1


# ── broker ─────────────────────────────────────────────────────────────────────

def test_paper_limit_without_price_rejected():
    _reset(); _quotes({"NVDA": 800}); _full()
    b = aj_broker.PaperBroker()
    r = b.submit({"symbol": "NVDA", "side": "sell", "qty": 1, "order_type": "limit",
                  "limit_price": None, "client_order_id": "c"})
    assert r["state"] == "rejected"


def test_alpaca_forced_paper_when_live_off():
    _reset(); _full(default_broker="alpaca", live_trading_enabled=False)
    assert isinstance(aj_broker.get_broker("alpaca"), aj_broker.PaperBroker)


# ── routing privacy ────────────────────────────────────────────────────────────

def test_is_private_catches_money_and_shares():
    assert aj_routing.is_private("should I trim my TSLA exposure given the 20% gain")
    assert aj_routing.is_private("is $1,200 too much for one name")
    assert aj_routing.is_private("I own 50 shares of NVDA")
    assert aj_routing.is_private("how is my book doing")
    assert not aj_routing.is_private("what is the VIX regime today")


# ── overlays ───────────────────────────────────────────────────────────────────

def test_opencode_forbidden_midpath():
    assert aj_opencode.path_allowed("/app/secrets/key") is False
    assert aj_opencode.path_allowed("/var/run/config/risk.yaml") is False
    assert aj_opencode.path_allowed("/tmp/some/wealth.db") is False
    assert aj_opencode.path_allowed("research/candidate.py") is True


def test_mcp_contract_rejects_mutating_names():
    aj_mcp_read.TOOLS["add_holding"] = {"fn": lambda a: {}, "description": "x"}
    try:
        assert aj_mcp_read.contract_ok() is False
    finally:
        del aj_mcp_read.TOOLS["add_holding"]
    assert aj_mcp_read.contract_ok() is True


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    print("aj_defects — {} tests".format(len(fns)))
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
