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
    "canceled": "canceled", "pending_cancel": "canceled",
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
        self._key, self._secret = self._lease_keys()

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
        return kid.value, sec.value

    # ── HTTP ──────────────────────────────────────────────────────────────────
    def _headers(self) -> Dict[str, str]:
        return {"APCA-API-KEY-ID": self._key,
                "APCA-API-SECRET-KEY": self._secret,
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
        body = {
            "symbol": str(order.get("symbol") or "").upper(),
            "qty": str(order.get("qty")),
            "side": str(order.get("side") or "").lower(),
            "type": str(order.get("order_type") or "market").lower(),
            "time_in_force": "day",
            "client_order_id": order.get("client_order_id"),
        }
        if body["type"] == "limit":
            body["limit_price"] = str(order.get("limit_price"))
        data = self._request("POST", "/v2/orders", body)
        return self._to_result(data, with_fills=False)

    def cancel(self, broker_order_id: str) -> Dict[str, Any]:
        try:
            self._request("DELETE", "/v2/orders/{}".format(broker_order_id))
        except Exception:
            log.debug("alpaca cancel failed for %s", broker_order_id, exc_info=True)
        return {"broker_order_id": broker_order_id, "state": "canceled"}

    def get_order(self, broker_order_id: str) -> Dict[str, Any]:
        data = self._request("GET", "/v2/orders/{}".format(broker_order_id))
        return self._to_result(data, with_fills=True)

    def positions(self) -> List[Dict[str, Any]]:
        data = self._request("GET", "/v2/positions") or []
        return [{"symbol": p.get("symbol"), "qty": float(p.get("qty") or 0),
                 "avg_cost": float(p.get("avg_entry_price") or 0)} for p in data]

    def cash(self) -> float:
        data = self._request("GET", "/v2/account") or {}
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
        out = []
        for a in (acts or []):
            out.append({
                "qty": float(a.get("qty") or 0),
                "price": float(a.get("price") or 0),
                "fees_usd": 0.0,
                "broker_fill_id": a.get("id"),
                "filled_at": a.get("transaction_time") or aj_db.utc_now_iso(),
            })
        return out
