#!/usr/bin/env python3
"""AJTA contract tests (AJTA-SPEC-1.0 §23.2).

Covers: VERIFY-MCP-READ frozen read-tool contract; PaperBroker slippage/fee
model + limit resting; gated live brokers fail-closed; BrokerClient conformance
against a mock; reconciliation; crash recovery with NO double-submit (§20.1);
operator paper cycle; LIVE NEVER auto-executes (§19); ModelRouter privacy
(private => local-only, never egress) + telemetry; kill switch cancels +
disables.

Fresh temp DB; no network, no LLM, no real broker.
"""
import os
import sys
import tempfile

os.environ["AUGUR_DB_PATH"] = tempfile.mktemp(suffix="_ajcontract.db")

import database as db          # noqa: E402
import fetcher                  # noqa: E402
import aj_db                    # noqa: E402
import aj_config                # noqa: E402
import aj_risk                  # noqa: E402
import aj_broker                # noqa: E402
import aj_execution             # noqa: E402
import aj_positions             # noqa: E402
import aj_mcp_read              # noqa: E402

aj_db.aj_init()
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


# ── VERIFY-MCP-READ ───────────────────────────────────────────────────────────

def test_mcp_frozen_contract():
    assert aj_mcp_read.contract_ok() is True
    assert tuple(aj_mcp_read.TOOLS.keys()) == aj_mcp_read.FROZEN_TOOLS
    # no mutating verb may be exposed
    for name in aj_mcp_read.TOOLS:
        assert name not in ("place_order", "submit_order", "kill_switch",
                            "execute_trade", "set_config", "approve")
    # unknown tool rejected; known read tool dispatches
    assert "error" in aj_mcp_read.invoke("definitely_not_a_tool", {})
    _quotes({"NVDA": 800})
    q = aj_mcp_read.invoke("quote", {"symbol": "NVDA"})
    assert q.get("price") == 800


def test_mcp_contract_breaks_if_tampered():
    # simulate adding a tool without updating FROZEN_TOOLS -> contract fails
    aj_mcp_read.TOOLS["sneaky_tool"] = {"fn": lambda a: {}, "description": "x"}
    try:
        assert aj_mcp_read.contract_ok() is False
    finally:
        del aj_mcp_read.TOOLS["sneaky_tool"]
    assert aj_mcp_read.contract_ok() is True


# ── PaperBroker fill model (§12.5) ────────────────────────────────────────────

def test_paper_market_slippage_and_fees():
    _reset(); _quotes({"NVDA": 800}); _full(paper_slippage_bps=2.0, paper_spread_fraction=0.5)
    b = aj_broker.PaperBroker()
    r = b.submit({"symbol": "NVDA", "side": "buy", "qty": 10, "order_type": "market",
                  "asset_type": "stock", "client_order_id": "c1"})
    # adverse = 2 + 0.5*4 = 4 bps => 800 * 1.0004 = 800.32 (buyer pays more)
    assert r["state"] == "filled" and r["avg_fill_price"] == 800.32
    # sell side fills lower
    r2 = b.submit({"symbol": "NVDA", "side": "sell", "qty": 10, "order_type": "market",
                   "asset_type": "stock", "client_order_id": "c2"})
    assert r2["avg_fill_price"] == 799.68


def test_paper_limit_rests_when_not_marketable():
    _reset(); _quotes({"NVDA": 800}); _full()
    b = aj_broker.PaperBroker()
    # buy limit BELOW market -> not marketable -> rests
    r = b.submit({"symbol": "NVDA", "side": "buy", "qty": 1, "order_type": "limit",
                  "limit_price": 700, "client_order_id": "l1"})
    assert r["state"] == "accepted" and r["filled_qty"] == 0
    # buy limit ABOVE market -> marketable -> fills at <= limit
    r2 = b.submit({"symbol": "NVDA", "side": "buy", "qty": 1, "order_type": "limit",
                   "limit_price": 900, "client_order_id": "l2"})
    assert r2["state"] == "filled" and r2["avg_fill_price"] <= 900


def test_paper_crypto_fees_modeled():
    _reset(); _quotes({"BTC-USD": 60000}); _full(symbol_allowlist=["BTC"], crypto_fee_bps=10.0)
    b = aj_broker.PaperBroker()
    r = b.submit({"symbol": "BTC", "side": "buy", "qty": 1, "order_type": "market",
                  "asset_type": "crypto", "client_order_id": "c1"})
    # fee = notional * 10bps; notional ~ 60024 => fee ~ 60.02
    assert r["fees_usd"] > 0 and abs(r["fees_usd"] - 60.02) < 1.0, r["fees_usd"]


# ── gated live brokers fail-closed (§17) ──────────────────────────────────────

def test_gated_brokers_fail_closed():
    _reset(); _full(live_trading_enabled=False)
    # ccxt/robinhood are gated-live stubs => forced to internal paper when live off
    assert isinstance(aj_broker.get_broker("ccxt"), aj_broker.PaperBroker)
    assert isinstance(aj_broker.get_broker("robinhood"), aj_broker.PaperBroker)
    # constructing a gated live broker directly raises (switch off)
    try:
        aj_broker.CCXTBroker()
        assert False, "should have raised BrokerNotEnabled"
    except aj_broker.BrokerNotEnabled:
        pass
    # alpaca: live off => internal PaperBroker (uniform with ccxt/robinhood);
    # the live fail-closed path is covered in test_aj_alpaca.
    assert isinstance(aj_broker.get_broker("alpaca"), aj_broker.PaperBroker)


# ── BrokerClient conformance against a mock ───────────────────────────────────

class _MockBroker(aj_broker.BrokerClient):
    name = "mock"

    def __init__(self):
        super().__init__(mode="paper")
        self._submitted = {}

    def submit(self, order):
        boid = "mock_" + order.get("client_order_id", "x")
        res = {"broker_order_id": boid, "state": "filled",
               "filled_qty": order["qty"], "avg_fill_price": 500.0,
               "fees_usd": 1.0, "fills": [{"qty": order["qty"], "price": 500.0,
                                            "fees_usd": 1.0, "broker_fill_id": "mf1",
                                            "filled_at": aj_db.utc_now_iso()}], "raw": {}}
        self._submitted[boid] = res
        return res

    def cancel(self, boid):
        return {"broker_order_id": boid, "state": "canceled"}

    def get_order(self, boid):
        return self._submitted.get(boid, {"state": "unknown"})

    def positions(self):
        return aj_positions.positions_list("paper")

    def cash(self):
        return 0.0


def test_broker_conformance_and_fills():
    _reset(); _quotes({"NVDA": 500}); _full()
    # patch the factory to return the mock for venue 'mock'
    orig = aj_broker._BROKERS.get("mock")
    aj_broker._BROKERS["mock"] = _MockBroker
    aj_config.set_config({"default_broker": "mock"})
    try:
        pid = aj_db.insert("aj_proposals", created_at=aj_db.utc_now_iso(), cycle_id="c",
                           symbol="NVDA", side="buy", qty=4, order_type="market", status="proposed")
        prop = {"id": pid, "symbol": "NVDA", "side": "buy", "qty": 4, "order_type": "market"}
        d = aj_risk.evaluate(prop)
        assert d["decision"] == "pass"
        ex = aj_execution.execute_trade(prop, d)
        assert ex["state"] == "filled" and ex["filled_qty"] == 4
        # fill recorded + aggregated
        order = aj_db.get_row("aj_orders", ex["order_id"])
        assert order["avg_fill_price"] == 500.0 and order["fees_usd"] == 1.0
        assert order["broker_order_id"].startswith("mock_")
    finally:
        if orig is None:
            del aj_broker._BROKERS["mock"]
        else:
            aj_broker._BROKERS["mock"] = orig


# ── reconciliation + crash recovery (§13, §20.1) ──────────────────────────────

def test_reconcile_paper_matches():
    _reset(); _quotes({"NVDA": 800}); _full()
    pid = aj_db.insert("aj_proposals", created_at=aj_db.utc_now_iso(), cycle_id="c",
                       symbol="NVDA", side="buy", qty=2, order_type="market", status="proposed")
    prop = {"id": pid, "symbol": "NVDA", "side": "buy", "qty": 2, "order_type": "market"}
    aj_execution.execute_trade(prop, aj_risk.evaluate(prop))
    rec = aj_execution.reconcile(venue="paper")
    assert rec["status"] == "match"


def test_crash_recovery_no_double_submit():
    _reset(); _quotes({"NVDA": 800}); _full()
    aj_db.open_cycle("paper")          # left running (crash)
    oid = aj_db.insert("aj_orders", proposal_id=1, client_order_id="stuck",
                       broker="paper", mode="paper", symbol="NVDA", side="buy",
                       qty=5, order_type="market", state="new", filled_qty=0,
                       fees_usd=0, created_at=aj_db.utc_now_iso())
    rec = aj_execution.recover_crashed_cycles(current_cycle="newcyc")
    o = aj_db.get_row("aj_orders", oid)
    fills = aj_db.query("SELECT COUNT(*) n FROM aj_fills WHERE order_id=?", (oid,))[0]["n"]
    assert rec["crashed"] == 1
    assert o["state"] == "expired" and fills == 0   # never re-submitted


def test_reconcile_resolves_unknown_via_broker_truth():
    _reset(); _full()
    mock = _MockBroker()
    # an order parked unknown but the broker can confirm it filled
    oid = aj_db.insert("aj_orders", proposal_id=1, client_order_id="u1",
                       broker="mock", mode="paper", symbol="NVDA", side="buy",
                       qty=3, order_type="market", state="unknown",
                       broker_order_id="mock_u1", created_at=aj_db.utc_now_iso())
    mock._submitted["mock_u1"] = {"state": "filled", "fills": [
        {"qty": 3, "price": 510, "fees_usd": 0.5, "broker_fill_id": "f",
         "filled_at": aj_db.utc_now_iso()}]}
    res = aj_execution.reconcile(broker=mock)
    o = aj_db.get_row("aj_orders", oid)
    assert o["state"] == "filled" and o["filled_qty"] == 3


# ── operator cycle: paper executes, LIVE never auto (§19) ─────────────────────

def _mock_bullish(prob=0.66):
    import forecast_ensemble, forecast_accountability, research_tracker
    forecast_ensemble.ensemble_forecast = lambda sym, h=20: {
        "symbol": sym, "ensemble": {"prob_up": prob, "direction": "up",
                                    "edge_pct_pts": abs(prob - 0.5) * 100,
                                    "conviction": "moderate", "consensus": 0.7}}
    forecast_accountability.log_ensemble = lambda *a, **k: None
    research_tracker.score_due_forecasts = lambda max_rows=200: {"scored": 0, "errors": 0}


def test_operator_paper_cycle_executes():
    _reset(); _quotes({"NVDA": 800}); _full()
    _mock_bullish()
    import aj_operator
    out = aj_operator.run_once("paper")
    assert out["ok"] and out["proposals"]
    assert any(p.get("result") == "executed" for p in out["proposals"]), out["proposals"]
    # a paper fill exists
    assert aj_db.query("SELECT COUNT(*) n FROM aj_fills")[0]["n"] >= 1


def test_operator_live_never_auto_executes():
    _reset(); _quotes({"NVDA": 800})
    _full(live_trading_enabled=True, default_broker="alpaca")  # a live venue
    _mock_bullish()
    import aj_operator
    out = aj_operator.run_once("live")
    # proposal made + approved-pending, but NO order/fill auto-created
    assert any(p.get("result") == "approved_pending" for p in out["proposals"]), out["proposals"]
    assert aj_db.query("SELECT COUNT(*) n FROM aj_fills")[0]["n"] == 0
    props = aj_db.query("SELECT status FROM aj_proposals")
    assert all(p["status"] == "approved" for p in props), props


def test_single_instance_blocks_overlap():
    lock = aj_db.SingleInstanceLock("operator")
    assert lock.acquire()
    try:
        import aj_operator
        out = aj_operator.run_once("paper")   # second cycle must refuse
        assert out["ok"] is False and "running" in out["reason"]
    finally:
        lock.release()


# ── routing privacy + telemetry (§10) ─────────────────────────────────────────

def test_routing_private_never_egress():
    _reset()
    import aj_routing, ai_summarizer
    ai_summarizer._ollama_ready = lambda: False
    leaked = {"egressed": False}

    def _spy(*a, **k):
        leaked["egressed"] = True
        return "CLOUD"
    ai_summarizer.chat_any = _spy
    r = aj_routing.route([{"role": "user", "content": "what is my portfolio P&L"}],
                         role="judge", quality_floor=1.0)
    assert r["sensitivity"] == "private"
    assert r["ok"] is False and "none" in r["model"]
    assert leaked["egressed"] is False, "PRIVATE PROMPT EGRESSED TO CLOUD"


def test_routing_records_telemetry():
    _reset()
    import aj_routing, ai_summarizer
    ai_summarizer._ollama_ready = lambda: True
    ai_summarizer._ollama_chat = lambda m, max_tokens=None: "local ok answer"
    aj_routing.route([{"role": "user", "content": "my holdings summary"}],
                     role="size", cycle_id="c", quality_floor=1.0)
    rows = aj_db.query("SELECT role, sensitivity, ok FROM aj_routing")
    assert len(rows) == 1 and rows[0]["sensitivity"] == "private" and rows[0]["ok"] == 1


# ── kill switch proven (§11.4) ────────────────────────────────────────────────

def test_kill_switch_cancels_open_orders():
    _reset(); _quotes({"NVDA": 800}); _full()
    # a resting open order
    aj_db.insert("aj_orders", proposal_id=1, client_order_id="o1", broker="paper",
                 mode="paper", symbol="NVDA", side="buy", qty=1, order_type="limit",
                 limit_price=700, state="accepted", created_at=aj_db.utc_now_iso())
    out = aj_risk.kill_switch("contract test")
    assert out["ok"] and out["orders_canceled"] >= 1
    assert aj_config.get_config()["trading_enabled"] is False
    assert aj_db.query("SELECT state FROM aj_orders")[0]["state"] == "canceled"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    print("aj_contract — {} tests".format(len(fns)))
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
