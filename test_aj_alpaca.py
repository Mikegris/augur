#!/usr/bin/env python3
"""AJTA Alpaca + secrets tests (VERIFY-ALPACA, §22.3).

The Alpaca adapter is exercised against a MOCKED HTTP layer (no network). Covers
fail-closed gating (no VERIFY / no keys), status + fill mapping, idempotent fill
recording across reconciliation polls, and the secrets-broker lease contract.
"""
import os
import sys
import tempfile

os.environ["AUGUR_DB_PATH"] = tempfile.mktemp(suffix="_ajalpaca.db")

import database as db          # noqa: E402
import aj_db                    # noqa: E402
import aj_config                # noqa: E402
import aj_secrets               # noqa: E402
import aj_broker                # noqa: E402
import aj_alpaca                # noqa: E402
import aj_execution             # noqa: E402

aj_db.aj_init()


def _reset():
    conn = db.get_conn()
    for t in ("aj_orders", "aj_fills", "aj_proposals", "aj_audit"):
        conn.execute("DELETE FROM {}".format(t))
    conn.execute("DELETE FROM settings WHERE key LIKE 'aj_%' OR key LIKE '__aj_%'")
    conn.commit()
    try:
        db._invalidate_settings_cache()
    except Exception:
        pass


def _setup_keys_and_verify():
    aj_secrets.store("alpaca_key_id", "PK_TEST")
    aj_secrets.store("alpaca_secret_key", "SK_TEST")
    db.set_setting("aj_verify_alpaca", "pass")


# ── secrets broker ────────────────────────────────────────────────────────────

def test_secret_encrypted_not_plaintext():
    _reset()
    aj_secrets.store("alpaca_key_id", "PK_SUPERSECRET")
    row = db.get_conn().execute(
        "SELECT value FROM settings WHERE key='__aj_sec_alpaca_key_id'").fetchone()
    assert row and "PK_SUPERSECRET" not in row["value"]      # ciphertext only
    lease = aj_secrets.lease("alpaca_key_id", caller="test")
    assert lease.value == "PK_SUPERSECRET"
    assert "PK_SUPERSECRET" not in str(lease)                 # redacted repr


def test_secret_lease_expiry_and_delete():
    _reset()
    aj_secrets.store("k", "v")
    assert aj_secrets.lease("k", ttl_s=0).value is None       # immediately expired
    aj_secrets.delete("k")
    assert aj_secrets.has("k") is False
    assert aj_secrets.lease("k") is None


# ── Alpaca gating (fail-closed) ───────────────────────────────────────────────

def test_alpaca_fails_closed_without_verify():
    _reset()
    aj_secrets.store("alpaca_key_id", "PK"); aj_secrets.store("alpaca_secret_key", "SK")
    # keys present but VERIFY gate not passed
    try:
        aj_alpaca.AlpacaBroker()
        assert False, "should fail closed without VERIFY-ALPACA"
    except aj_broker.BrokerNotEnabled as e:
        assert "VERIFY" in str(e)


def test_alpaca_fails_closed_without_keys():
    _reset()
    db.set_setting("aj_verify_alpaca", "pass")     # verified but no keys
    try:
        aj_alpaca.AlpacaBroker()
        assert False, "should fail closed without keys"
    except aj_broker.BrokerNotEnabled as e:
        assert "keys" in str(e)


def test_get_broker_alpaca_self_gates():
    _reset()
    aj_config.set_config({"default_broker": "alpaca"})
    try:
        aj_broker.get_broker("alpaca")
        assert False, "alpaca should fail closed when unconfigured"
    except aj_broker.BrokerNotEnabled:
        pass


def test_alpaca_paper_base_when_live_off():
    _reset(); _setup_keys_and_verify()
    aj_config.set_config({"live_trading_enabled": False})
    b = aj_alpaca.AlpacaBroker()
    assert b.mode == "paper" and b.base == aj_alpaca.PAPER_BASE
    aj_config.set_config({"live_trading_enabled": True})
    b2 = aj_alpaca.AlpacaBroker()
    assert b2.mode == "live" and b2.base == aj_alpaca.LIVE_BASE


# ── status + fill mapping (mocked HTTP) ───────────────────────────────────────

def _mock_broker(responses):
    """Build an AlpacaBroker with _request stubbed to return canned responses
    keyed by 'METHOD path-prefix'."""
    _setup_keys_and_verify()
    b = aj_alpaca.AlpacaBroker()

    def _req(method, path, payload=None):
        for key, val in responses.items():
            m, prefix = key.split(" ", 1)
            if method == m and path.startswith(prefix):
                return val(payload) if callable(val) else val
        raise aj_broker.BrokerError("unmocked {} {}".format(method, path))
    b._request = _req
    return b


def test_alpaca_status_mapping():
    assert aj_alpaca.map_status("new") == "accepted"
    assert aj_alpaca.map_status("partially_filled") == "partially_filled"
    assert aj_alpaca.map_status("filled") == "filled"
    assert aj_alpaca.map_status("canceled") == "canceled"
    assert aj_alpaca.map_status("rejected") == "rejected"
    assert aj_alpaca.map_status("expired") == "expired"
    assert aj_alpaca.map_status("weird_unknown") == "unknown"


def test_alpaca_submit_and_get_order_with_fills():
    _reset()
    b = _mock_broker({
        "POST /v2/orders": {"id": "al_1", "status": "accepted", "filled_qty": "0"},
        "GET /v2/orders/al_1": {"id": "al_1", "status": "filled", "filled_qty": "5",
                                 "filled_avg_price": "801.5"},
        "GET /v2/account/activities/FILL": [
            {"id": "fill_1", "qty": "5", "price": "801.5",
             "transaction_time": aj_db.utc_now_iso()}],
    })
    sub = b.submit({"symbol": "NVDA", "side": "buy", "qty": 5, "order_type": "market",
                    "client_order_id": "c1"})
    assert sub["broker_order_id"] == "al_1" and sub["state"] == "accepted"
    got = b.get_order("al_1")
    assert got["state"] == "filled" and got["filled_qty"] == 5
    assert len(got["fills"]) == 1 and got["fills"][0]["broker_fill_id"] == "fill_1"


def test_alpaca_reconcile_dedups_fills():
    _reset()
    # an accepted order whose broker truth resolves to filled with one fill
    oid = aj_db.insert("aj_orders", proposal_id=1, client_order_id="c1", broker="alpaca",
                       mode="paper", symbol="NVDA", side="buy", qty=5, order_type="market",
                       state="accepted", broker_order_id="al_1", created_at=aj_db.utc_now_iso())
    b = _mock_broker({
        "GET /v2/orders/al_1": {"id": "al_1", "status": "filled", "filled_qty": "5",
                                 "filled_avg_price": "801.5"},
        "GET /v2/account/activities/FILL": [
            {"id": "fill_1", "qty": "5", "price": "801.5",
             "transaction_time": aj_db.utc_now_iso()}],
        "GET /v2/positions": [{"symbol": "NVDA", "qty": "5", "avg_entry_price": "801.5"}],
    })
    # resolve twice — the dedup must keep exactly ONE fill
    aj_execution.reconcile(broker=b)
    aj_execution.reconcile(broker=b)
    fills = aj_db.query("SELECT COUNT(*) n FROM aj_fills WHERE order_id=?", (oid,))
    assert fills[0]["n"] == 1, fills
    o = aj_db.get_row("aj_orders", oid)
    assert o["state"] == "filled" and o["filled_qty"] == 5


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    print("aj_alpaca — {} tests".format(len(fns)))
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
