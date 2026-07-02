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

# Process-wide paper order book. get_broker() builds a fresh PaperBroker() on
# every call, so per-instance order state would be lost the moment the
# submitting instance is discarded (after execute_trade()'s finally-disconnect).
# A later reconcile/cancel against a new instance must still find prior paper
# orders — otherwise a parked order stays 'unknown' forever and a cancel of it
# returns 'unknown' instead of canceling. Keep it module-level so all
# PaperBroker instances in this process share one book.
_PAPER_ORDERS: Dict[str, Dict[str, Any]] = {}


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
    With `paper_partial_fills` on, a marketable order larger than the
    `paper_fill_liquidity_usd` budget fills only partially at submit and
    completes on a later get_order() poll (see submit()).

    Paper is its own source of truth, so reconciliation against it always
    matches — the recon machinery is still exercised for shape parity.
    """
    name = "paper"

    def __init__(self):
        super().__init__(mode="paper")
        # Share one process-wide order book across instances (see _PAPER_ORDERS).
        self._orders = _PAPER_ORDERS

    # quote lookup (price only); crypto maps to SYM-USD; options price off the
    # chain (PER-CONTRACT premium) so the paper book's qty×price math is correct.
    def _quote(self, symbol: str, asset_type: str = "") -> Optional[float]:
        try:
            if symbol.startswith("OPT:") or asset_type == "option":
                import aj_options
                m = aj_options.mark(symbol)
                return float(m) if isinstance(m, (int, float)) and m == m and m > 0 else None
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

    def _fees(self, notional: float, asset_type: str, qty: float = 0.0) -> float:
        cfg = aj_config.get_config()
        # Options: a per-CONTRACT commission (qty = contracts), not a bps of
        # notional — the standard options fee model.
        if asset_type == "option":
            per = max(0.0, float(cfg.get("option_fee_per_contract") or 0))
            return aj_db.money(abs(float(qty or 0)) * per)
        bps = cfg.get("crypto_fee_bps") if asset_type == "crypto" else cfg.get("fee_bps")
        # Clamp bps to >=0 so a misconfigured negative fee_bps cannot silently
        # zero out (or refund) fees — fail-safe toward charging, never free trades.
        bps = max(0.0, float(bps or 0))
        fee = max(float(cfg.get("min_fee_usd") or 0),
                  abs(notional) * bps / 1e4)
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
        # A limit order MUST carry a positive limit price. Without it the SELL
        # marketability test (quote >= 0) was always true, so a malformed limit
        # SELL filled like a market order. Reject it.
        if otype == "limit" and (limit_price is None or float(limit_price) <= 0):
            res = {"broker_order_id": boid, "state": "rejected", "filled_qty": 0.0,
                   "avg_fill_price": None, "fees_usd": 0.0, "fills": [],
                   "raw": {"reason": "limit order without a valid limit price"}}
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
            # resting limit order — persist the order params in raw so the
            # get_order() poll can re-check marketability and actually fill it
            # later (a resting paper limit that can never fill would strand
            # every limit_entry buy until TTL expiry).
            res = {"broker_order_id": boid, "state": "accepted", "filled_qty": 0.0,
                   "avg_fill_price": None, "fees_usd": 0.0, "fills": [],
                   "raw": {"resting": True, "quote": quote, "limit": limit_price,
                           "symbol": symbol, "side": side, "qty": qty,
                           "asset_type": asset_type}}
            self._orders[boid] = res
            return res

        # Round the PER-UNIT price to 8dp (matching _recompute_order), NOT to
        # cents: money() rounding zeroed sub-cent prices (SHIB/PEPE fill at
        # $0.00 => cost basis 0, infinite fake gain) and destroyed the modeled
        # adverse slippage on any low-priced asset. Cents apply to notional/fees.
        fill_price = round(float(fill_price), 8)

        # Liquidity-bounded PARTIAL FILLS (paper_partial_fills, default OFF —
        # OFF is byte-identical to the instant-full-fill behavior below). A
        # real venue never hands a large order the whole book in one print;
        # model that by capping the instant fill at what a nominal per-cycle
        # liquidity budget (paper_fill_liquidity_usd) can absorb. The cap is a
        # pure function of price/qty/config — DETERMINISTIC, no randomness.
        # The remainder rests as 'partially_filled' and completes on a later
        # get_order() poll (same adverse-slippage model, second fill appended).
        cfg = aj_config.get_config()
        if cfg.get("paper_partial_fills"):
            budget = float(cfg.get("paper_fill_liquidity_usd", 25000) or 0)
            if budget > 0 and fill_price * qty > budget:
                cap = budget / fill_price          # max qty this cycle absorbs
                if asset_type == "option":
                    # contracts are indivisible — floor, but never 0: a 0-qty
                    # "partial" would just be a resting order in disguise.
                    cap = max(1.0, float(int(cap)))
                fill_qty = min(qty, cap)
                if fill_qty < qty:
                    fees = self._fees(fill_price * fill_qty, asset_type, qty=fill_qty)
                    fill = {"qty": fill_qty, "price": fill_price, "fees_usd": fees,
                            "broker_fill_id": "pf_" + uuid.uuid4().hex[:12],
                            "filled_at": aj_db.utc_now_iso()}
                    res = {"broker_order_id": boid, "state": "partially_filled",
                           "filled_qty": fill_qty, "avg_fill_price": fill_price,
                           "fees_usd": fees, "fills": [fill],
                           # persist the order params so get_order() can fill
                           # the remainder at the then-current quote later —
                           # a limit remainder must ALSO stay inside its limit.
                           "raw": {"partial": True, "quote": quote,
                                   "adverse_bps": self._adverse_bps(),
                                   "symbol": symbol, "side": side, "qty": qty,
                                   "remaining": qty - fill_qty,
                                   "limit": (float(limit_price)
                                             if otype == "limit" else None),
                                   "asset_type": asset_type}}
                    self._orders[boid] = res
                    return res

        notional = fill_price * qty
        fees = self._fees(notional, asset_type, qty=qty)
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
        o = self._orders.get(broker_order_id)
        if o is None:
            # Paper orders live in process memory only: after a restart a
            # resting/parked paper id is unresolvable forever. There is no real
            # exposure behind a paper id, and any fill it DID produce was
            # recorded synchronously at submit — so report it terminally
            # expired rather than stranding it 'unknown' for every reconcile.
            if str(broker_order_id or "").startswith("paper_"):
                return {"broker_order_id": broker_order_id, "state": "expired"}
            return {"broker_order_id": broker_order_id, "state": "unknown"}
        # Re-check a resting limit against the CURRENT quote so paper resting
        # orders can fill when the market comes to them (mirrors a real venue).
        raw = o.get("raw") or {}
        if o.get("state") == "accepted" and raw.get("resting") and raw.get("symbol"):
            quote = self._quote(raw["symbol"], str(raw.get("asset_type") or "stock"))
            lp = float(raw.get("limit") or 0)
            side, qty = raw.get("side"), float(raw.get("qty") or 0)
            fill_price = None
            if quote is not None and lp > 0 and qty > 0:
                adverse = self._adverse_bps() / 1e4
                if side == "buy" and quote <= lp:
                    fill_price = min(lp, quote * (1 + adverse))
                elif side == "sell" and quote >= lp:
                    fill_price = max(lp, quote * (1 - adverse))
            if fill_price is not None:
                fill_price = round(float(fill_price), 8)
                fees = self._fees(fill_price * qty, str(raw.get("asset_type") or "stock"), qty=qty)
                o.update({"state": "filled", "filled_qty": qty,
                          "avg_fill_price": fill_price, "fees_usd": fees,
                          "fills": [{"qty": qty, "price": fill_price,
                                     "fees_usd": fees,
                                     "broker_fill_id": "pf_" + uuid.uuid4().hex[:12],
                                     "filled_at": aj_db.utc_now_iso()}]})
        # Complete a liquidity-capped PARTIAL fill (paper_partial_fills) on
        # poll: fill the remainder at the CURRENT quote with the same adverse-
        # slippage model, APPENDING a second fill. Aggregates (filled_qty /
        # avg_fill_price / fees_usd) are recomputed from the fills list so
        # they stay consistent — downstream _apply_broker_result /
        # _recompute_order trusts them verbatim. Not gated on the flag here:
        # raw['partial'] only exists if the flag was on at submit, and a
        # partial in flight must complete even if the flag is flipped off.
        elif (o.get("state") == "partially_filled" and raw.get("partial")
                and raw.get("symbol")):
            rem = float(raw.get("remaining") or 0)
            quote = self._quote(raw["symbol"], str(raw.get("asset_type") or "stock"))
            if rem > 0 and quote is not None:
                side = str(raw.get("side") or "")
                adverse = self._adverse_bps() / 1e4
                lp = raw.get("limit")
                fill_price = None
                if lp:
                    # parent was a LIMIT order — the remainder only completes
                    # while the market is still inside the limit (marketable).
                    lp = float(lp)
                    if side == "buy" and quote <= lp:
                        fill_price = min(lp, quote * (1 + adverse))
                    elif side == "sell" and quote >= lp:
                        fill_price = max(lp, quote * (1 - adverse))
                else:  # market remainder: current quote + adverse slippage
                    fill_price = (quote * (1 + adverse) if side == "buy"
                                  else quote * (1 - adverse))
            else:
                fill_price = None
            if fill_price is not None:
                fill_price = round(float(fill_price), 8)
                at = str(raw.get("asset_type") or "stock")
                fees2 = self._fees(fill_price * rem, at, qty=rem)
                fills = list(o.get("fills") or [])
                fills.append({"qty": rem, "price": fill_price, "fees_usd": fees2,
                              "broker_fill_id": "pf_" + uuid.uuid4().hex[:12],
                              "filled_at": aj_db.utc_now_iso()})
                tot_qty = sum(float(f["qty"]) for f in fills)
                avg = (sum(float(f["qty"]) * float(f["price"]) for f in fills)
                       / tot_qty) if tot_qty > 0 else None
                o.update({"state": "filled", "filled_qty": tot_qty,
                          "avg_fill_price": (round(avg, 8) if avg is not None
                                             else None),
                          "fees_usd": aj_db.money(
                              sum(float(f["fees_usd"]) for f in fills)),
                          "fills": fills})
                raw["remaining"] = 0.0
        return o

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
        # DERIVED available cash, not the raw aj_paper_cash setting: the static
        # setting is never debited by buys, so reporting it raw overstates
        # buying power once anything is invested. aj_alpha derives
        # base − open cost basis + realized P&L (net of fees), floored at 0;
        # with no base configured it returns 0.0 — same as the old raw read.
        try:
            import aj_alpha
            return aj_db.money(aj_alpha.available_paper_cash())
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
    "ccxt": CCXTBroker,
    "robinhood": RobinhoodBroker,
}


def _alpaca_cls():
    """Lazy — keeps the `requests`/HTTP surface out of aj_broker import."""
    import aj_alpaca
    return aj_alpaca.AlpacaBroker


def get_broker(name: Optional[str] = None) -> BrokerClient:
    """Construct a broker. Defaults to the configured default_broker. Live
    venues raise BrokerNotEnabled unless fully gated+verified. CCXT/Robinhood
    are forced to PaperBroker when live trading is off (§11.3.8). Alpaca
    self-gates (VERIFY-ALPACA + leased keys) and supports its own paper sub-mode
    when live is off — so it's constructed directly and fails closed if not set
    up."""
    cfg = aj_config.get_config()
    name = (name or cfg.get("default_broker") or "paper").lower()
    if name == "alpaca":
        # Uniform with ccxt/robinhood: when live trading is OFF, route to the
        # INTERNAL PaperBroker (the book reconcile treats as truth) rather than
        # Alpaca's external paper endpoint — otherwise paper fills land at
        # Alpaca while reconcile compares against the internal book (mismatch).
        # Alpaca engages only when live is enabled AND VERIFY-ALPACA passed.
        if not cfg.get("live_trading_enabled"):
            log.info("live disabled -> internal PaperBroker (requested alpaca)")
            return PaperBroker()
        return _alpaca_cls()()
    cls = _BROKERS.get(name)
    if cls is None:
        raise BrokerError("unknown broker {}".format(name))
    if issubclass(cls, _GatedLiveBroker) and not cfg.get("live_trading_enabled"):
        log.info("live disabled -> forcing PaperBroker (requested %s)", name)
        return PaperBroker()
    return cls()
