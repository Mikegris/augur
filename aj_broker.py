"""AJTA — broker adapters (AJTA-SPEC-1.0 §12, §17).

`BrokerClient` is the execution interface every venue implements. `PaperBroker`
is the core, always-available venue: it fills against live AUGUR quotes with a
modeled slippage + fee curve (§12.5) so paper results are NOT optimistic. Live
adapters (Alpaca / ccxt / Robinhood) are VERIFY-gated stubs that fail-closed
until their switches AND verification gates pass (§17, §27).

Order dict shape (input to submit):
  {symbol, side('buy'|'sell'), qty, order_type('market'|'limit'),
   limit_price, client_order_id, asset_type}

Result dict shape (output of submit / get_order):
  {broker_order_id, state, filled_qty, avg_fill_price, fees_usd,
   fills:[{qty, price, fees_usd, broker_fill_id, filled_at}], raw}
"""
from __future__ import annotations

import abc
import logging
import uuid
from typing import Any, Dict, List, Optional

import aj_db
import aj_config

log = logging.getLogger("augur.aj_broker")

# A nominal half-spread (bps) the paper model crosses; spread_fraction scales it.
_PAPER_SPREAD_BPS = 4.0


class BrokerError(Exception):
    pass


class BrokerNotEnabled(BrokerError):
    pass


class BrokerClient(abc.ABC):
    name: str = "abstract"
    supports_client_id: bool = True   # if False, adapter MUST reconcile before resubmit

    def __init__(self, mode: str = "paper"):
        self.mode = mode

    def connect(self) -> None:
        pass

    def disconnect(self) -> None:
        pass

    @abc.abstractmethod
    def submit(self, order: Dict[str, Any]) -> Dict[str, Any]:
        ...

    @abc.abstractmethod
    def cancel(self, broker_order_id: str) -> Dict[str, Any]:
        ...

    @abc.abstractmethod
    def get_order(self, broker_order_id: str) -> Dict[str, Any]:
        ...

    @abc.abstractmethod
    def positions(self) -> List[Dict[str, Any]]:
        ...

    @abc.abstractmethod
    def cash(self) -> float:
        ...


# ── Paper broker (core, §12.5) ────────────────────────────────────────────────

class PaperBroker(BrokerClient):
    """Deterministic paper fills against live quotes with modeled slippage and
    fees. Market orders fill immediately; marketable limit orders fill at the
    limit or better; non-marketable limits rest (state 'accepted', 0 filled).

    Paper is its own source of truth, so reconciliation against it always
    matches — the recon machinery is still exercised for shape parity.
    """
    name = "paper"

    def __init__(self):
        super().__init__(mode="paper")
        self._orders: Dict[str, Dict[str, Any]] = {}

    # quote lookup (price only); crypto maps to SYM-USD
    def _quote(self, symbol: str, asset_type: str = "") -> Optional[float]:
        try:
            import fetcher
            sym = symbol.upper()
            if asset_type == "crypto" and not sym.endswith("-USD"):
                sym = sym + "-USD"
            q = fetcher.get_quote(sym) or {}
            p = q.get("price")
            if isinstance(p, (int, float)) and not isinstance(p, bool) and p == p and p > 0:
                return float(p)
        except Exception:
            log.debug("paper quote failed for %s", symbol, exc_info=True)
        return None

    def _fees(self, notional: float, asset_type: str) -> float:
        cfg = aj_config.get_config()
        bps = cfg.get("crypto_fee_bps") if asset_type == "crypto" else cfg.get("fee_bps")
        fee = max(float(cfg.get("min_fee_usd") or 0),
                  abs(notional) * float(bps or 0) / 1e4)
        return aj_db.money(fee)

    def _adverse_bps(self) -> float:
        cfg = aj_config.get_config()
        slip = float(cfg.get("paper_slippage_bps") or 0)
        frac = float(cfg.get("paper_spread_fraction") or 0)
        return slip + frac * _PAPER_SPREAD_BPS

    def submit(self, order: Dict[str, Any]) -> Dict[str, Any]:
        symbol = str(order.get("symbol") or "").upper()
        side = str(order.get("side") or "").lower()
        qty = float(order.get("qty") or 0)
        otype = str(order.get("order_type") or "market").lower()
        limit_price = order.get("limit_price")
        asset_type = str(order.get("asset_type") or "stock").lower()
        boid = "paper_" + uuid.uuid4().hex[:16]

        quote = self._quote(symbol, asset_type)
        if quote is None:
            # No price => cannot fill paper; mark rejected (data-quality, §20.5).
            res = {"broker_order_id": boid, "state": "rejected", "filled_qty": 0.0,
                   "avg_fill_price": None, "fees_usd": 0.0, "fills": [],
                   "raw": {"reason": "no quote"}}
            self._orders[boid] = res
            return res
        if qty <= 0:
            res = {"broker_order_id": boid, "state": "rejected", "filled_qty": 0.0,
                   "avg_fill_price": None, "fees_usd": 0.0, "fills": [],
                   "raw": {"reason": "qty<=0"}}
            self._orders[boid] = res
            return res

        adverse = self._adverse_bps() / 1e4
        fill_price = None
        if otype == "market":
            fill_price = quote * (1 + adverse) if side == "buy" else quote * (1 - adverse)
        else:  # limit
            lp = float(limit_price or 0)
            if side == "buy" and quote <= lp:
                fill_price = min(lp, quote * (1 + adverse))
            elif side == "sell" and quote >= lp:
                fill_price = max(lp, quote * (1 - adverse))
            # else: not marketable -> rest

        if fill_price is None:
            # resting limit order
            res = {"broker_order_id": boid, "state": "accepted", "filled_qty": 0.0,
                   "avg_fill_price": None, "fees_usd": 0.0, "fills": [],
                   "raw": {"resting": True, "quote": quote, "limit": limit_price}}
            self._orders[boid] = res
            return res

        fill_price = aj_db.money(fill_price)
        notional = fill_price * qty
        fees = self._fees(notional, asset_type)
        fill = {"qty": qty, "price": fill_price, "fees_usd": fees,
                "broker_fill_id": "pf_" + uuid.uuid4().hex[:12],
                "filled_at": aj_db.utc_now_iso()}
        res = {"broker_order_id": boid, "state": "filled", "filled_qty": qty,
               "avg_fill_price": fill_price, "fees_usd": fees, "fills": [fill],
               "raw": {"quote": quote, "adverse_bps": self._adverse_bps()}}
        self._orders[boid] = res
        return res

    def cancel(self, broker_order_id: str) -> Dict[str, Any]:
        o = self._orders.get(broker_order_id)
        if not o:
            return {"broker_order_id": broker_order_id, "state": "unknown"}
        if o["state"] in ("filled", "rejected"):
            return {"broker_order_id": broker_order_id, "state": o["state"]}
        o["state"] = "canceled"
        return {"broker_order_id": broker_order_id, "state": "canceled"}

    def get_order(self, broker_order_id: str) -> Dict[str, Any]:
        return self._orders.get(broker_order_id,
                                {"broker_order_id": broker_order_id, "state": "unknown"})

    def positions(self) -> List[Dict[str, Any]]:
        # Paper truth = the agent's FIFO paper book (ADR-001), NOT the user's
        # real portfolio — so reconciliation is self-consistent and real
        # holdings are never touched by paper trading.
        try:
            import aj_positions
            return aj_positions.positions_list("paper")
        except Exception:
            return []

    def cash(self) -> float:
        try:
            import database as db
            raw = db.get_settings().get("aj_paper_cash")
            return aj_db.money(raw) if raw is not None else 0.0
        except Exception:
            return 0.0


# ── VERIFY-gated live adapters (§17, §27) — fail-closed stubs ─────────────────

class _GatedLiveBroker(BrokerClient):
    """Base for live venues. Refuses to construct/operate unless the relevant
    config switches are on AND the venue's VERIFY gate has been recorded as
    passed in settings (`aj_verify_<gate>` = 'pass'). Until then every method
    raises BrokerNotEnabled — there is no path to a live order without an
    explicit, audited verification."""
    verify_gate: str = ""
    requires: tuple = ("trading_enabled", "live_trading_enabled")

    def __init__(self):
        super().__init__(mode="live")
        self._assert_enabled()

    def _assert_enabled(self) -> None:
        cfg = aj_config.get_config()
        for sw in self.requires:
            if not cfg.get(sw):
                raise BrokerNotEnabled(
                    "{}: switch '{}' is off".format(self.name, sw))
        try:
            import database as db
            v = db.get_settings().get("aj_verify_" + self.verify_gate)
        except Exception:
            v = None
        if str(v) != "pass":
            raise BrokerNotEnabled(
                "{}: VERIFY gate '{}' not passed".format(self.name, self.verify_gate))

    def submit(self, order):  # pragma: no cover - cannot run without VERIFY
        raise BrokerNotEnabled("{} live submit requires VERIFY".format(self.name))

    def cancel(self, broker_order_id):  # pragma: no cover
        raise BrokerNotEnabled("{} not enabled".format(self.name))

    def get_order(self, broker_order_id):  # pragma: no cover
        raise BrokerNotEnabled("{} not enabled".format(self.name))

    def positions(self):  # pragma: no cover
        raise BrokerNotEnabled("{} not enabled".format(self.name))

    def cash(self):  # pragma: no cover
        raise BrokerNotEnabled("{} not enabled".format(self.name))


class AlpacaBroker(_GatedLiveBroker):
    name = "alpaca"
    verify_gate = "alpaca"


class CCXTBroker(_GatedLiveBroker):
    name = "ccxt"
    verify_gate = "ccxt"


class RobinhoodBroker(_GatedLiveBroker):
    name = "robinhood"
    verify_gate = "robinhood"
    requires = ("trading_enabled", "live_trading_enabled", "robinhood_enabled")


# ── factory ───────────────────────────────────────────────────────────────────

_BROKERS = {
    "paper": PaperBroker,
    "alpaca": AlpacaBroker,
    "ccxt": CCXTBroker,
    "robinhood": RobinhoodBroker,
}


def get_broker(name: Optional[str] = None) -> BrokerClient:
    """Construct a broker. Defaults to the configured default_broker. Live
    venues raise BrokerNotEnabled unless fully gated+verified. When live
    trading is off, any non-paper request is forced to PaperBroker (§11.3.8)."""
    cfg = aj_config.get_config()
    name = (name or cfg.get("default_broker") or "paper").lower()
    cls = _BROKERS.get(name)
    if cls is None:
        raise BrokerError("unknown broker {}".format(name))
    # Force paper only for inherently-LIVE venues when live trading is off
    # (§11.3.8). A paper-mode custom/alternate broker is returned as requested.
    if issubclass(cls, _GatedLiveBroker) and not cfg.get("live_trading_enabled"):
        log.info("live disabled -> forcing PaperBroker (requested %s)", name)
        return PaperBroker()
    return cls()
