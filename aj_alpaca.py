"""AJTA — Alpaca broker adapter (AJTA-SPEC-1.0 §26 phase 5, VERIFY-ALPACA).

A real `BrokerClient` over Alpaca's REST API. Supports Alpaca's PAPER endpoint
(fake money, live API) and the LIVE endpoint (real money). FAIL-CLOSED:
constructing it requires BOTH the VERIFY-ALPACA gate (`aj_verify_alpaca`=pass)
AND the API keys present in the secrets broker; the LIVE endpoint additionally
requires `live_trading_enabled`. Keys are LEASED from aj_secrets (never stored
in the model context).

Closing VERIFY-ALPACA (§27): confirm eligibility/fees, pin the order surface,
and run the live contract test against the paper sandbox, then set
`aj_verify_alpaca=pass`. Until then this adapter cannot place an order.

Endpoints:
  paper: https://paper-api.alpaca.markets
  live:  https://api.alpaca.markets
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import aj_db
import aj_config
import aj_secrets
from aj_broker import BrokerClient, BrokerError, BrokerNotEnabled

log = logging.getLogger("augur.aj_alpaca")

PAPER_BASE = "https://paper-api.alpaca.markets"
LIVE_BASE = "https://api.alpaca.markets"
_TIMEOUT = 15

# Alpaca order status -> AJ order state (§12.1)
_STATUS_MAP = {
    "new": "accepted", "accepted": "accepted", "accepted_for_bidding": "accepted",
    "pending_new": "submitted", "calculated": "accepted", "held": "accepted",
    "partially_filled": "partially_filled",
    "filled": "filled",
    # pending_cancel is NOT terminal: the order can still fill while the cancel
    # is pending — mapping it to 'canceled' made reconciliation stop watching
    # an order that could later fill with no local record.
    "canceled": "canceled", "pending_cancel": "accepted",
    "replaced": "accepted", "pending_replace": "accepted",
    "rejected": "rejected", "suspended": "rejected",
    "expired": "expired", "done_for_day": "expired", "stopped": "expired",
}


def map_status(alpaca_status: Any) -> str:
    return _STATUS_MAP.get(str(alpaca_status or "").lower(), "unknown")


class AlpacaBroker(BrokerClient):
    name = "alpaca"
    supports_client_id = True

    def __init__(self):
        cfg = aj_config.get_config()
        live = bool(cfg.get("live_trading_enabled"))
        super().__init__(mode="live" if live else "paper")
        self.base = LIVE_BASE if live else PAPER_BASE
        self._assert_enabled(live)
        # Hold the short-lived Lease objects (NOT the raw plaintext) so the
        # credentials don't outlive the lease TTL on a long-lived instance; the
        # value is read per-request and re-leased once the lease expires.
        self._kid_lease, self._sec_lease = self._lease_keys()

    # ── gating ────────────────────────────────────────────────────────────────
    def _assert_enabled(self, live: bool) -> None:
        import database as db
        if str(db.get_settings().get("aj_verify_alpaca")) != "pass":
            raise BrokerNotEnabled("alpaca: VERIFY-ALPACA gate not passed")
        if not aj_secrets.available():
            raise BrokerNotEnabled("alpaca: secrets broker unavailable")
        if not (aj_secrets.has("alpaca_key_id") and aj_secrets.has("alpaca_secret_key")):
            raise BrokerNotEnabled("alpaca: API keys not in secrets broker")
        if live and not aj_config.get_config().get("live_trading_enabled"):
            raise BrokerNotEnabled("alpaca: live switch off")

    def _lease_keys(self):
        kid = aj_secrets.lease("alpaca_key_id", caller="aj_alpaca")
        sec = aj_secrets.lease("alpaca_secret_key", caller="aj_alpaca")
        if not kid or not sec or not kid.value or not sec.value:
            raise BrokerNotEnabled("alpaca: could not lease keys (expired/missing)")
        return kid, sec

    def _creds(self):
        """Read the current key/secret from the held leases, re-leasing if the
        TTL has expired so plaintext credentials are never cached past the lease
        window. Fails closed if a fresh lease can't be obtained."""
        if self._kid_lease.value is None or self._sec_lease.value is None:
            self._kid_lease, self._sec_lease = self._lease_keys()
        return self._kid_lease.value, self._sec_lease.value

    # ── HTTP ──────────────────────────────────────────────────────────────────
    def _headers(self) -> Dict[str, str]:
        key, secret = self._creds()
        return {"APCA-API-KEY-ID": key,
                "APCA-API-SECRET-KEY": secret,
                "Content-Type": "application/json"}

    def _request(self, method: str, path: str,
                 payload: Optional[Dict[str, Any]] = None) -> Any:
        import requests
        url = self.base + path
        r = requests.request(method, url, headers=self._headers(),
                             json=payload, timeout=_TIMEOUT)
        if r.status_code >= 400:
            raise BrokerError("alpaca {} {} -> {} {}".format(
                method, path, r.status_code, (r.text or "")[:200]))
        return r.json() if (r.text or "").strip() else {}

    # ── BrokerClient ──────────────────────────────────────────────────────────
    def submit(self, order: Dict[str, Any]) -> Dict[str, Any]:
        # Re-assert gating against CURRENT config every submit: a long-lived
        # instance must not keep routing to a (possibly LIVE) endpoint after the
        # operator flipped live_trading_enabled. If the live flag no longer
        # matches the endpoint this instance was built for, fail closed rather
        # than send the order to the wrong (live) base.
        live_now = bool(aj_config.get_config().get("live_trading_enabled"))
        if live_now != (self.base == LIVE_BASE):
            raise BrokerNotEnabled(
                "alpaca: live_trading_enabled changed since this broker was "
                "constructed; refusing to route to a stale endpoint")
        self._assert_enabled(live_now)
        # Crypto needs the BTC/USD pair format + gtc/ioc TIF, which this equity
        # path doesn't model — reject rather than mis-route a malformed order.
        if str(order.get("asset_type") or "").lower() == "crypto":
            return {"broker_order_id": None, "state": "rejected", "filled_qty": 0.0,
                    "avg_fill_price": None, "fees_usd": 0.0, "fills": [],
                    "raw": {"reason": "alpaca adapter does not support crypto yet"}}
        body = {
            "symbol": str(order.get("symbol") or "").upper(),
            "side": str(order.get("side") or "").lower(),
            "type": str(order.get("order_type") or "market").lower(),
            "time_in_force": str(order.get("time_in_force") or "day").lower(),
            "client_order_id": order.get("client_order_id"),
        }
        # Send qty for share orders, else notional (fractional dollar) orders —
        # serializing qty='None' for a notional-only order gets it rejected.
        if order.get("qty") not in (None, ""):
            body["qty"] = str(order.get("qty"))
        elif order.get("notional") not in (None, ""):
            body["notional"] = str(order.get("notional"))
        if body["type"] == "limit":
            # str(None) would serialize as the literal "None" and 422 at the
            # venue, parking the order in 'unknown' — reject locally instead
            # (mirrors PaperBroker's malformed-limit reject).
            lp = order.get("limit_price")
            try:
                lp_ok = lp is not None and float(lp) > 0
            except (TypeError, ValueError):
                lp_ok = False
            if not lp_ok:
                return {"broker_order_id": None, "state": "rejected",
                        "filled_qty": 0.0, "avg_fill_price": None,
                        "fees_usd": 0.0, "fills": [],
                        "raw": {"reason": "limit order without a valid limit price"}}
            body["limit_price"] = str(lp)
        data = self._request("POST", "/v2/orders", body)
        res = self._to_result(data, with_fills=False)
        # If Alpaca already reports fills at submit (fast market order), pull
        # them now so the book reflects the trade immediately instead of being
        # marked 'filled' with zero recorded fills until a reconcile poll.
        if res.get("broker_order_id") and (
                res.get("filled_qty", 0) > 0
                or res.get("state") in ("filled", "partially_filled")):
            res["fills"] = self._fills_for(res["broker_order_id"])
        return res

    def cancel(self, broker_order_id: str) -> Dict[str, Any]:
        try:
            self._request("DELETE", "/v2/orders/{}".format(broker_order_id))
            # A DELETE 204 only means the cancel REQUEST was accepted — the
            # order enters pending_cancel and can still fill. Read the order
            # back rather than asserting a terminal 'canceled' the venue never
            # confirmed; on read failure report unknown for reconciliation.
            try:
                return self.get_order(broker_order_id)
            except Exception:
                return {"broker_order_id": broker_order_id, "state": "unknown"}
        except Exception as e:
            # Do NOT assert 'canceled' on failure — a still-live order would be
            # mistaken for canceled. Mark unknown; reconciliation confirms truth.
            log.warning("alpaca cancel failed for %s: %s", broker_order_id, e)
            return {"broker_order_id": broker_order_id, "state": "unknown",
                    "error": str(e)[:120]}

    def get_order(self, broker_order_id: str) -> Dict[str, Any]:
        data = self._request("GET", "/v2/orders/{}".format(broker_order_id))
        return self._to_result(data, with_fills=True)

    def get_order_by_client_id(self, client_order_id: str) -> Dict[str, Any]:
        """Resolve an order by our deterministic client_order_id — the recovery
        path for a submit that timed out AFTER Alpaca accepted the order (no
        broker_order_id was ever stored locally)."""
        data = self._request(
            "GET", "/v2/orders:by_client_order_id?client_order_id={}".format(
                client_order_id))
        return self._to_result(data, with_fills=True)

    def positions(self) -> List[Dict[str, Any]]:
        data = self._request("GET", "/v2/positions")
        data = data if isinstance(data, list) else []
        return [{"symbol": p.get("symbol"), "qty": float(p.get("qty") or 0),
                 "avg_cost": float(p.get("avg_entry_price") or 0)} for p in data]

    def cash(self) -> float:
        # Returns the account's USD *cash* field only (not buying_power, which
        # includes margin). Sizing/recon should treat this as deployable USD
        # cash. Warn (don't silently trust) if the account isn't USD-denominated.
        data = self._request("GET", "/v2/account") or {}
        cur = str(data.get("currency") or "USD").upper()
        if cur != "USD":
            log.warning("alpaca account currency is %s, not USD; cash() value "
                        "may not be USD-denominated", cur)
        return aj_db.money(data.get("cash") or 0)

    # ── mapping ────────────────────────────────────────────────────────────────
    def _to_result(self, data: Dict[str, Any], with_fills: bool) -> Dict[str, Any]:
        boid = data.get("id")
        state = map_status(data.get("status"))
        filled_qty = float(data.get("filled_qty") or 0)
        avg = data.get("filled_avg_price")
        avg = float(avg) if avg not in (None, "") else None
        fills: List[Dict[str, Any]] = []
        if with_fills and boid:
            fills = self._fills_for(boid)
        return {"broker_order_id": boid, "state": state, "filled_qty": filled_qty,
                "avg_fill_price": avg, "fees_usd": 0.0, "fills": fills, "raw": data}

    def _fills_for(self, broker_order_id: str) -> List[Dict[str, Any]]:
        """Individual fills via the activities feed; each has a unique id so
        aj_execution dedups across reconciliation polls."""
        try:
            acts = self._request(
                "GET", "/v2/account/activities/FILL?order_id={}".format(broker_order_id))
        except Exception:
            log.debug("alpaca fills fetch failed for %s", broker_order_id, exc_info=True)
            return []
        if not isinstance(acts, list):
            return []
        out = []
        for a in acts:
            out.append({
                "qty": float(a.get("qty") or 0),
                "price": float(a.get("price") or 0),
                # capture any fee the activity reports (crypto venues do) so
                # live P&L isn't optimistic; 0 for commission-free equities.
                "fees_usd": float(a.get("fee") or a.get("fees") or 0),
                "broker_fill_id": a.get("id"),
                "filled_at": a.get("transaction_time") or aj_db.utc_now_iso(),
            })
        return out
