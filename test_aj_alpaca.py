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
# Isolate the secrets master key PER PROCESS — otherwise every temp-DB test
# shares one .aj_secret_key in $TMPDIR and a key left by another run can be
# inconsistent with this run's stored ciphertext (flaky lease failures).
from cryptography.fernet import Fernet                    # noqa: E402
os.environ["AUGUR_SECRETS_KEY"] = Fernet.generate_key().decode()

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
    # audit_maintenance: aj_audit is append-only (v10 triggers); the test
    # reset is explicit maintenance and must use the scoped unlock.
    with aj_db.audit_maintenance():
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


def test_secret_rotate_key_round_trip():
    _reset()
    import base64
    aj_secrets.store("alpaca_key_id", "PK_ROTATE_ME")
    aj_secrets.store("alpaca_secret_key", "SK_ROTATE_ME")
    old_key = os.environ["AUGUR_SECRETS_KEY"]
    r = aj_secrets.rotate_master_key()
    assert r["ok"] is True and r["rotated"] == 2, r
    assert r["env_key_active"] is True                     # env is this test's source
    # key material never in the result (counts/paths only)
    assert old_key not in str(r) and os.environ["AUGUR_SECRETS_KEY"] not in str(r)
    # round trip: stored secrets still lease fine under the NEW key
    assert aj_secrets.lease("alpaca_key_id", caller="test").value == "PK_ROTATE_ME"
    assert aj_secrets.lease("alpaca_secret_key", caller="test").value == "SK_ROTATE_ME"
    # the OLD key must no longer decrypt the stored blob
    row = db.get_conn().execute(
        "SELECT value FROM settings WHERE key='__aj_sec_alpaca_key_id'").fetchone()
    try:
        Fernet(old_key.encode()).decrypt(base64.b64decode(row["value"]))
        assert False, "old key still decrypts after rotation"
    except AssertionError:
        raise
    except Exception:
        pass
    # the new key was persisted to the key-file convention as well
    assert r.get("key_file") and os.path.exists(r["key_file"])
    with open(r["key_file"], "rb") as f:
        assert f.read().strip() == os.environ["AUGUR_SECRETS_KEY"].encode()


def test_secret_rotate_aborts_on_undecryptable_blob():
    _reset()
    import base64
    aj_secrets.store("good", "GOOD_VALUE")
    # a blob the current key can NOT decrypt (foreign key / corruption)
    foreign = Fernet(Fernet.generate_key()).encrypt(b"orphan")
    db.set_setting("__aj_sec_orphan", base64.b64encode(foreign).decode("ascii"))
    before = db.get_conn().execute(
        "SELECT value FROM settings WHERE key='__aj_sec_good'").fetchone()["value"]
    old_env = os.environ["AUGUR_SECRETS_KEY"]
    r = aj_secrets.rotate_master_key()
    assert r["ok"] is False and "orphan" in r["error"], r   # names the scope only
    assert "GOOD_VALUE" not in str(r)
    # NOTHING changed: same ciphertext, same active key, still leasable
    after = db.get_conn().execute(
        "SELECT value FROM settings WHERE key='__aj_sec_good'").fetchone()["value"]
    assert after == before and os.environ["AUGUR_SECRETS_KEY"] == old_env
    assert aj_secrets.lease("good", caller="test").value == "GOOD_VALUE"


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
    # live OFF => alpaca routes to the INTERNAL PaperBroker (uniform with
    # ccxt/robinhood; paper fills land in the book reconcile treats as truth).
    aj_config.set_config({"default_broker": "alpaca", "live_trading_enabled": False})
    assert isinstance(aj_broker.get_broker("alpaca"), aj_broker.PaperBroker)
    # live ON but unverified => fail closed (no path to a live order).
    aj_config.set_config({"live_trading_enabled": True})
    try:
        aj_broker.get_broker("alpaca")
        assert False, "alpaca should fail closed when live-on + unverified"
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


# ── PaperBroker partial fills (paper_partial_fills, §12.5 extension) ──────────

def _patch_paper_cfg(**over):
    """Overlay keys onto aj_config.get_config for one test. Needed because the
    partial-fill keys live in aj_config DEFAULTS owned by another writer; the
    broker only cfg.get()s them (absent → flag OFF). Returns the original
    get_config for the caller's finally-restore."""
    orig = aj_config.get_config

    def patched():
        cfg = dict(orig())
        cfg.update(over)
        return cfg
    aj_config.get_config = patched
    return orig


def test_paper_partial_fills_off_is_single_full_fill():
    _reset()
    import fetcher
    orig_q = fetcher.get_quote
    fetcher.get_quote = lambda s: {"price": 100.0}
    orig_cfg = _patch_paper_cfg(paper_partial_fills=False, fee_bps=0.0,
                                paper_slippage_bps=0.0, paper_spread_fraction=0.0,
                                min_fee_usd=0.0)
    try:
        b = aj_broker.PaperBroker()
        # $100k notional dwarfs the $25k default budget — but the flag is OFF,
        # so behavior must be the legacy one: one instant, complete fill.
        r = b.submit({"symbol": "NVDA", "side": "buy", "qty": 1000,
                      "order_type": "market", "asset_type": "stock",
                      "client_order_id": "pp0"})
        assert r["state"] == "filled" and r["filled_qty"] == 1000.0, r
        assert len(r["fills"]) == 1 and r["fills"][0]["qty"] == 1000.0, r["fills"]
        assert r["avg_fill_price"] == 100.0, r
        # polling a filled order must not mutate it
        o = b.get_order(r["broker_order_id"])
        assert o["state"] == "filled" and len(o["fills"]) == 1, o
    finally:
        fetcher.get_quote = orig_q
        aj_config.get_config = orig_cfg


def test_paper_partial_fill_then_completion_on_poll():
    _reset()
    import fetcher
    orig_q = fetcher.get_quote
    fetcher.get_quote = lambda s: {"price": 100.0}
    orig_cfg = _patch_paper_cfg(paper_partial_fills=True,
                                paper_fill_liquidity_usd=25000,
                                fee_bps=0.0, paper_slippage_bps=0.0,
                                paper_spread_fraction=0.0, min_fee_usd=0.0)
    try:
        b = aj_broker.PaperBroker()
        r = b.submit({"symbol": "NVDA", "side": "buy", "qty": 1000,
                      "order_type": "market", "asset_type": "stock",
                      "client_order_id": "pp1"})
        # $25k budget / $100 = 250 shares now; 750 rest as partially_filled
        assert r["state"] == "partially_filled", r
        assert abs(r["filled_qty"] - 250.0) < 1e-9, r
        assert len(r["fills"]) == 1 and abs(r["raw"]["remaining"] - 750.0) < 1e-9, r
        # deterministic participation — same inputs, same partial fill
        r2 = b.submit({"symbol": "NVDA", "side": "buy", "qty": 1000,
                       "order_type": "market", "asset_type": "stock",
                       "client_order_id": "pp2"})
        assert abs(r2["filled_qty"] - 250.0) < 1e-9, r2
        # a later poll completes the remainder AT THE CURRENT QUOTE,
        # appending a second fill (price moved 100 → 102 meanwhile)
        fetcher.get_quote = lambda s: {"price": 102.0}
        o = b.get_order(r["broker_order_id"])
        assert o["state"] == "filled" and abs(o["filled_qty"] - 1000.0) < 1e-9, o
        assert len(o["fills"]) == 2 and abs(o["fills"][1]["qty"] - 750.0) < 1e-9, o["fills"]
        assert abs(o["fills"][1]["price"] - 102.0) < 1e-9, o["fills"]
        # aggregates must stay consistent with the fills list — downstream
        # _apply_broker_result/_recompute_order trusts them verbatim
        vwap = sum(f["qty"] * f["price"] for f in o["fills"]) / o["filled_qty"]
        assert abs(o["avg_fill_price"] - vwap) < 1e-9, (o["avg_fill_price"], vwap)
        assert abs(vwap - (250 * 100.0 + 750 * 102.0) / 1000.0) < 1e-9, vwap
        # a second poll is idempotent — no third fill appears
        o2 = b.get_order(r["broker_order_id"])
        assert len(o2["fills"]) == 2 and o2["state"] == "filled", o2
    finally:
        fetcher.get_quote = orig_q
        aj_config.get_config = orig_cfg


def test_paper_partial_fill_small_order_unaffected():
    _reset()
    import fetcher
    orig_q = fetcher.get_quote
    fetcher.get_quote = lambda s: {"price": 100.0}
    orig_cfg = _patch_paper_cfg(paper_partial_fills=True,
                                paper_fill_liquidity_usd=25000,
                                fee_bps=0.0, paper_slippage_bps=0.0,
                                paper_spread_fraction=0.0, min_fee_usd=0.0)
    try:
        b = aj_broker.PaperBroker()
        # $10k notional < $25k budget → fills fully even with the flag ON
        r = b.submit({"symbol": "NVDA", "side": "buy", "qty": 100,
                      "order_type": "market", "asset_type": "stock",
                      "client_order_id": "pp3"})
        assert r["state"] == "filled" and r["filled_qty"] == 100.0, r
        assert len(r["fills"]) == 1, r["fills"]
    finally:
        fetcher.get_quote = orig_q
        aj_config.get_config = orig_cfg


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
