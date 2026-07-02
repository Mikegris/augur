#!/usr/bin/env python3
"""AJTA unit tests (AJTA-SPEC-1.0 §23.1).

Covers: money rounding; market session; append-only audit hash chain + tamper
detection; single-instance lock; migrations idempotency; risk-gate ordering +
fail-closed behavior; day-P&L FIFO with fees/marks; kill switch / halt / rearm;
order state machine incl 'unknown' + illegal transitions; idempotency key reuse.

Runs against a FRESH temp DB (never the user's wealth.db). No network, no LLM.
"""
import os
import sys
import tempfile

os.environ["AUGUR_DB_PATH"] = tempfile.mktemp(suffix="_ajunit.db")

import database as db          # noqa: E402
import fetcher                  # noqa: E402
import aj_db                    # noqa: E402
import aj_config                # noqa: E402
import aj_risk                  # noqa: E402
import aj_positions             # noqa: E402
import aj_execution             # noqa: E402

aj_db.aj_init()
_REAL_SESSION = aj_db.market_session                # keep the genuine calendar
aj_db.market_session = lambda dt=None: "regular"    # deterministic for gate tests

_TABLES = ("aj_orders", "aj_fills", "aj_proposals", "aj_risk_events",
           "aj_audit", "aj_routing", "aj_recon", "aj_cycles")


def _reset():
    conn = db.get_conn()
    # audit_maintenance: aj_audit is append-only (v10 triggers); the test
    # reset is explicit maintenance and must use the scoped unlock.
    with aj_db.audit_maintenance():
        for t in _TABLES:
            conn.execute("DELETE FROM {}".format(t))
    conn.execute("DELETE FROM settings WHERE key LIKE 'aj_%' OR key LIKE '__aj_%'")
    conn.commit()
    try:
        db._invalidate_settings_cache()
    except Exception:
        pass


def _quotes(price_map):
    fetcher.get_quote = lambda s: {"price": price_map.get(s.upper())}
    fetcher.get_quotes_batch = lambda syms: {s: {"price": price_map.get(s.upper())} for s in syms}


def _full_config(**over):
    # universe_mode='allowlist' so the allowlist gate is active (the global
    # default is now 'market_screen' = open universe; these unit tests exercise
    # the allowlist-blocking path explicitly).
    cfg = {"trading_enabled": True, "symbol_allowlist": ["NVDA"],
           "universe_mode": "allowlist", "allow_any_symbol": False,
           "max_order_notional_usd": 10000, "max_trades_per_day": 10,
           "max_daily_loss_usd": 5000}
    cfg.update(over)
    aj_config.set_config(cfg)


# ── foundation ────────────────────────────────────────────────────────────────

def test_money_rounding():
    assert aj_db.money(0.1 + 0.2) == 0.3
    assert aj_db.money(1234.567) == 1234.57
    assert aj_db.money("99.005") == 99.01
    assert aj_db.money(None) == 0.0
    assert aj_db.money(float("inf")) == 0.0


def test_session_calendar():
    from datetime import datetime, timezone
    # 2026-06-24 is a Wednesday: 14:30Z = 10:30 ET (regular)
    assert _REAL_SESSION(datetime(2026, 6, 24, 14, 30, tzinfo=timezone.utc)) == "regular"
    assert _REAL_SESSION(datetime(2026, 6, 24, 12, 0, tzinfo=timezone.utc)) == "premarket"   # 08:00 ET
    assert _REAL_SESSION(datetime(2026, 6, 24, 21, 0, tzinfo=timezone.utc)) == "afterhours"  # 17:00 ET
    assert _REAL_SESSION(datetime(2026, 6, 27, 14, 30, tzinfo=timezone.utc)) == "closed"     # Saturday
    assert _REAL_SESSION(datetime(2026, 7, 3, 14, 30, tzinfo=timezone.utc)) == "closed"      # Jul-4 observed Fri


def test_audit_chain_and_tamper():
    _reset()
    cid = aj_db.open_cycle("paper")
    aj_db.audit("proposal", {"x": 1}, cycle_id=cid)
    aj_db.audit("order", {"y": 2}, cycle_id=cid)
    assert aj_db.verify_audit_chain()["ok"] is True
    # tamper: edit a payload out of band -> chain must break. The unlock
    # simulates an attacker with RAW DB access (who could drop the v10
    # append-only triggers); the hash chain is the detection layer there.
    conn = db.get_conn()
    with aj_db.audit_maintenance():
        conn.execute("UPDATE aj_audit SET payload_json='{\"x\":999}' WHERE id=(SELECT MIN(id) FROM aj_audit)")
        conn.commit()
    v = aj_db.verify_audit_chain()
    assert v["ok"] is False, v


def test_single_instance_lock():
    a = aj_db.SingleInstanceLock("unit")
    b = aj_db.SingleInstanceLock("unit")
    assert a.acquire() is True
    assert b.acquire() is False
    a.release()
    assert b.acquire() is True
    b.release()


def test_migrations_idempotent():
    v1 = aj_db.aj_migrate()
    v2 = aj_db.aj_migrate()
    assert v1 == v2 == aj_db.AJ_SCHEMA_TARGET


def test_config_schema_complete():
    # Every DEFAULTS key MUST be covered by exactly one typed key set — an
    # untyped key loads back as a raw string with no clamping (the
    # option_target_dte bug class). _build_schema records violations.
    assert aj_config._UNTYPED_KEYS == [], \
        "untyped config keys: {}".format(aj_config._UNTYPED_KEYS)
    schema = aj_config.config_schema()
    assert set(schema) == set(aj_config.DEFAULTS)
    for k, s in schema.items():
        assert s["type"] in ("bool", "list", "float", "int", "str"), (k, s)
        assert s["default"] == aj_config.DEFAULTS[k]
    # enum keys carry their choices for UI introspection
    assert "advisory" in schema["council_policy"]["enum"]
    assert schema["buy_prob_threshold"]["max"] == 1.0


# ── risk gate ordering + fail-closed (§11.3) ──────────────────────────────────

def test_gate_blocks_by_default():
    _reset()
    _quotes({"NVDA": 800})
    d = aj_risk.evaluate({"symbol": "NVDA", "side": "buy", "qty": 1, "order_type": "market"})
    assert d["decision"] == "block" and "master switch" in d["reason"]


def test_gate_order_allowlist_then_caps():
    _reset(); _quotes({"NVDA": 800})
    aj_config.set_config({"trading_enabled": True, "universe_mode": "allowlist",
                          "allow_any_symbol": False})
    d = aj_risk.evaluate({"symbol": "NVDA", "side": "buy", "qty": 1, "order_type": "market"})
    assert d["decision"] == "block" and "allowlist" in d["reason"]
    aj_config.set_config({"symbol_allowlist": ["NVDA"]})
    d = aj_risk.evaluate({"symbol": "NVDA", "side": "buy", "qty": 1, "order_type": "market"})
    assert d["decision"] == "block" and "per-order cap" in d["reason"]   # caps still 0


def test_gate_passes_when_fully_configured():
    _reset(); _quotes({"NVDA": 800}); _full_config()
    d = aj_risk.evaluate({"symbol": "NVDA", "side": "buy", "qty": 2, "order_type": "market"})
    assert d["decision"] == "pass" and d["mode"] == "paper"
    assert d["order_notional"] == 1600.0


def test_open_universe_off_allowlist():
    # default: off-allowlist symbol blocked. allow_any_symbol: passes (still
    # bounded by all other caps), but an invalid symbol is still blocked.
    _reset(); _quotes({"TSLA": 250}); _full_config(symbol_allowlist=["AAPL"])
    d = aj_risk.evaluate({"symbol": "TSLA", "side": "buy", "qty": 1, "order_type": "market"})
    assert d["decision"] == "block" and "allowlist" in d["reason"]
    aj_config.set_config({"allow_any_symbol": True})
    d = aj_risk.evaluate({"symbol": "TSLA", "side": "buy", "qty": 1, "order_type": "market"})
    assert d["decision"] == "pass", d
    bad = aj_risk.evaluate({"symbol": "@@", "side": "buy", "qty": 1, "order_type": "market"})
    assert bad["decision"] == "block" and "invalid symbol" in bad["reason"]
    # other caps still bind in open-universe mode
    aj_config.set_config({"max_order_notional_usd": 100})
    capped = aj_risk.evaluate({"symbol": "TSLA", "side": "buy", "qty": 5, "order_type": "market"})
    assert capped["decision"] == "block" and "per-order cap" in capped["reason"]


def test_gate_per_order_cap():
    _reset(); _quotes({"NVDA": 800}); _full_config(max_order_notional_usd=1000)
    d = aj_risk.evaluate({"symbol": "NVDA", "side": "buy", "qty": 10, "order_type": "market"})
    assert d["decision"] == "block" and "per-order cap" in d["reason"]


def test_gate_trades_per_day_cap():
    _reset(); _quotes({"NVDA": 800}); _full_config(max_trades_per_day=1)
    # record one non-rejected order today
    aj_db.insert("aj_orders", proposal_id=1, client_order_id="x1", broker="paper",
                 mode="paper", symbol="NVDA", side="buy", qty=1, order_type="market",
                 state="filled", created_at=aj_db.utc_now_iso(),
                 submitted_at=aj_db.utc_now_iso())
    d = aj_risk.evaluate({"symbol": "NVDA", "side": "buy", "qty": 1, "order_type": "market"})
    assert d["decision"] == "block" and "trade cap" in d["reason"]


def test_gate_missing_quote_blocks():
    _reset(); _quotes({}); _full_config()
    d = aj_risk.evaluate({"symbol": "NVDA", "side": "buy", "qty": 1, "order_type": "market"})
    assert d["decision"] == "block" and ("quote" in d["reason"] or "price" in d["reason"])


def test_gate_exception_fails_closed():
    _reset(); _full_config()
    orig = aj_risk._order_price
    aj_risk._order_price = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    try:
        d = aj_risk.evaluate({"symbol": "NVDA", "side": "buy", "qty": 1, "order_type": "market"})
        assert d["decision"] == "block" and "fail-closed" in d["reason"]
    finally:
        aj_risk._order_price = orig


# ── day-P&L FIFO + fees/marks (§11.2) ─────────────────────────────────────────

def test_day_pnl_fifo_with_fees():
    _reset(); _quotes({"NVDA": 850})
    # two BUY lots then a SELL — FIFO realized
    o1 = aj_db.insert("aj_orders", proposal_id=1, client_order_id="b1", broker="paper",
                      mode="paper", symbol="NVDA", side="buy", qty=10, order_type="market",
                      state="filled", created_at=aj_db.utc_now_iso())
    aj_db.insert("aj_fills", order_id=o1, qty=10, price=800, fees_usd=1.0,
                 filled_at=aj_db.utc_now_iso())
    o2 = aj_db.insert("aj_orders", proposal_id=2, client_order_id="s1", broker="paper",
                      mode="paper", symbol="NVDA", side="sell", qty=4, order_type="market",
                      state="filled", created_at=aj_db.utc_now_iso())
    aj_db.insert("aj_fills", order_id=o2, qty=4, price=850, fees_usd=0.5,
                 filled_at=aj_db.utc_now_iso())
    book = aj_positions.paper_book()
    # realized today = (850-800)*4 - 1.0(buy fee) - 0.5(sell fee) = 200 - 1.5 = 198.5
    assert book["realized_today"] == 198.5, book["realized_today"]
    assert abs(book["positions"]["NVDA"]["qty"] - 6) < 1e-9
    pnl = aj_risk.compute_day_pnl()
    # unrealized: 6 @ basis 800 marked 850 = +300; session-open snapshot = current => delta 0
    assert pnl["realized"] == 198.5
    assert pnl["unrealized_now"] == 300.0


def test_day_pnl_missing_quote_warns_not_trades():
    _reset(); _quotes({})   # no marks
    o1 = aj_db.insert("aj_orders", proposal_id=1, client_order_id="b1", broker="paper",
                      mode="paper", symbol="NVDA", side="buy", qty=10, order_type="market",
                      state="filled", created_at=aj_db.utc_now_iso())
    aj_db.insert("aj_fills", order_id=o1, qty=10, price=800, fees_usd=0,
                 filled_at=aj_db.utc_now_iso())
    pnl = aj_risk.compute_day_pnl()
    assert any("no quote" in w for w in pnl["warnings"])
    assert pnl["unrealized_now"] == 0.0   # missing quote contributes 0, never errors


# ── kill / halt / rearm ───────────────────────────────────────────────────────

def test_kill_switch_disables_and_blocks():
    _reset(); _quotes({"NVDA": 800}); _full_config()
    assert aj_risk.is_halted() is False
    aj_risk.kill_switch("unit")
    assert aj_risk.is_halted() is True
    # robinhood also forced off
    assert aj_config.get_config()["robinhood_enabled"] is False
    d = aj_risk.evaluate({"symbol": "NVDA", "side": "buy", "qty": 1, "order_type": "market"})
    assert d["decision"] == "block"


def test_daily_loss_halts():
    _reset(); _quotes({"NVDA": 700}); _full_config(max_daily_loss_usd=100)
    # create a realized loss today: buy 10@800 then sell 10@700 = -1000
    o1 = aj_db.insert("aj_orders", proposal_id=1, client_order_id="b1", broker="paper",
                      mode="paper", symbol="NVDA", side="buy", qty=10, order_type="market",
                      state="filled", created_at=aj_db.utc_now_iso())
    aj_db.insert("aj_fills", order_id=o1, qty=10, price=800, fees_usd=0, filled_at=aj_db.utc_now_iso())
    o2 = aj_db.insert("aj_orders", proposal_id=2, client_order_id="s1", broker="paper",
                      mode="paper", symbol="NVDA", side="sell", qty=10, order_type="market",
                      state="filled", created_at=aj_db.utc_now_iso())
    aj_db.insert("aj_fills", order_id=o2, qty=10, price=700, fees_usd=0, filled_at=aj_db.utc_now_iso())
    d = aj_risk.evaluate({"symbol": "NVDA", "side": "buy", "qty": 1, "order_type": "market"})
    assert d["decision"] == "halt"
    assert aj_risk.is_halted() is True
    # halt event recorded
    ev = aj_db.query("SELECT * FROM aj_risk_events WHERE decision='halt'")
    assert len(ev) >= 1


def test_rearm_after_halt():
    _reset(); _full_config()
    aj_risk.kill_switch("unit")
    assert aj_risk.is_halted()
    r = aj_risk.rearm(actor="unit")
    assert r["ok"] is True and aj_risk.is_halted() is False


# ── order state machine + idempotency ─────────────────────────────────────────

def test_state_machine_transitions():
    assert aj_execution.valid_transition("new", "submitted")
    assert aj_execution.valid_transition("submitted", "filled")
    assert aj_execution.valid_transition("accepted", "unknown")
    assert aj_execution.valid_transition("partially_filled", "partially_filled")
    assert not aj_execution.valid_transition("filled", "submitted")   # terminal
    assert not aj_execution.valid_transition("rejected", "filled")
    assert not aj_execution.valid_transition("new", "filled")         # must submit first
    assert aj_execution.valid_transition("new", "expired")            # crash abandon


def test_idempotency_keys_unique():
    _reset(); _quotes({"NVDA": 800}); _full_config()
    import aj_broker
    ids = set()
    for _ in range(5):
        pid = aj_db.insert("aj_proposals", created_at=aj_db.utc_now_iso(),
                           cycle_id="c", symbol="NVDA", side="buy", qty=1,
                           order_type="market", status="proposed")
        prop = {"id": pid, "symbol": "NVDA", "side": "buy", "qty": 1, "order_type": "market"}
        d = aj_risk.evaluate(prop)
        ex = aj_execution.execute_trade(prop, d)
        row = aj_db.get_row("aj_orders", ex["order_id"])
        assert row["client_order_id"] not in ids
        ids.add(row["client_order_id"])
    assert len(ids) == 5


def test_classify_exit_edge_cases():
    """classify_exit is the single source of truth for the risk-reducing-exit
    semantics consumed by aj_risk.evaluate AND aj_rules.enhanced_gate."""
    ce = aj_risk.classify_exit
    # flat: nothing to close; a buy opens a NEW name
    r = ce("NVDA", "sell", 5, 0.0)
    assert not r["closing"] and not r["confirmed"] and not r["flips"], r
    r = ce("NVDA", "buy", 5, 0.0)
    assert r["opening_new"] and not r["closing"], r
    # long: a bounded sell is a confirmed close; a buy adds (not opening_new)
    r = ce("NVDA", "sell", 5, 10.0)
    assert r["closing"] and r["confirmed"] and not r["flips"], r
    r = ce("NVDA", "buy", 5, 10.0)
    assert not r["closing"] and not r["opening_new"], r
    # short: a bounded buy-to-cover is a confirmed close (never opening_new)
    r = ce("NVDA", "buy", 5, -10.0)
    assert r["closing"] and r["confirmed"] and not r["opening_new"], r
    r = ce("NVDA", "sell", 5, -10.0)
    assert not r["closing"], r          # adds to the short — risk-increasing
    # oversize: qty beyond |held| FLIPS the position — NOT a close
    r = ce("NVDA", "sell", 15, 10.0)
    assert r["flips"] and not r["closing"] and not r["confirmed"], r
    r = ce("NVDA", "buy", 15, -10.0)
    assert r["flips"] and not r["closing"] and not r["confirmed"], r
    # exact-size close (within the 1e-9 tolerance) is NOT a flip
    r = ce("NVDA", "sell", 10, 10.0)
    assert r["closing"] and not r["flips"], r
    # unknown-held (book outage): a SELL is closing-but-UNCONFIRMED (exits must
    # never be blocked by an outage; cap exemptions require confirmation) — a
    # BUY is neither closing nor opening_new
    r = ce("NVDA", "sell", 5, None)
    assert r["closing"] and not r["confirmed"] and not r["flips"], r
    r = ce("NVDA", "buy", 5, None)
    assert not r["closing"] and not r["confirmed"] and not r["opening_new"], r
    # qty unknown yet (evaluate's allowlist step): classify without flip check
    r = ce("NVDA", "sell", None, 10.0)
    assert r["closing"] and r["confirmed"] and not r["flips"], r


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    print("aj_unit — {} tests".format(len(fns)))
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
