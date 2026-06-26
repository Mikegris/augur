"""AJTA — paper position book (FIFO) derived from aj_fills.

ADR-001 (safety deviation from spec §4 literal wording): the spec models a
Position as the AUGUR `portfolio` row. This app is a LIVE personal portfolio
tracker the user actively relies on, so applying *paper* fills to the real
`portfolio` table would corrupt real holdings. Instead the agent keeps a
self-contained PAPER BOOK derived from `aj_fills` (mode='paper'); the real
portfolio is never mutated by paper trading. Day-P&L FIFO basis (§11.2) is
taken from this paper book. A future live venue can opt to reflect into the
real portfolio behind its own switch.

Pure FIFO lot accounting; no network, no writes.
"""
from __future__ import annotations

import logging
from collections import defaultdict, deque
from typing import Any, Dict, List, Optional

import aj_db

log = logging.getLogger("augur.aj_positions")

# One consistent quantity epsilon — the old code mixed 1e-12 and 1e-9, so a
# residual in (1e-12, 1e-9] was neither matched nor shorted nor flagged: it
# silently vanished.
_EPS = 1e-9

# Minimal crypto inference for asset-type-sensitive rules/marks.
_CRYPTO_HINTS = {"BTC", "ETH", "SOL", "XRP", "DOGE", "ADA", "HBAR", "LTC",
                 "BCH", "AVAX", "DOT", "MATIC", "LINK", "UNI", "ATOM"}


def infer_asset_type(symbol: str) -> str:
    if isinstance(symbol, str) and symbol.startswith("OPT:"):
        return "option"
    s = (symbol or "").upper()
    if s.endswith("-USD") or s in _CRYPTO_HINTS:
        return "crypto"
    return "stock"


def _et_date(dt) -> Optional[str]:
    """ET (America/New_York) calendar date for a tz-aware/naive datetime.

    Realized-today must share the ET trading-day boundary used by
    aj_risk._session_date / the session-open unrealized baseline; slicing the
    raw UTC ISO string instead rolls 'today' over at 00:00 UTC (~7-8pm ET),
    splitting one ET session across two buckets. Falls back to UTC date."""
    if dt is None:
        return None
    try:
        from zoneinfo import ZoneInfo
        return dt.astimezone(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
    except Exception:
        try:
            return dt.strftime("%Y-%m-%d")
        except Exception:
            return None


def paper_book(mode: str = "paper") -> Dict[str, Any]:
    """Net aj_fills into FIFO positions + realized P&L.

    Returns {
      positions: {SYM: {qty, avg_cost, cost_basis, asset_type}},
      realized_total, realized_today, fees_total, fees_today
    }
    """
    today = _et_date(aj_db.utc_now())
    lots: Dict[str, deque] = defaultdict(deque)   # SYM -> deque([qty, price])
    realized_total = 0.0
    realized_today = 0.0
    fees_total = 0.0
    fees_today = 0.0
    try:
        rows = aj_db.query(
            "SELECT o.symbol AS symbol, o.side AS side, f.qty AS qty, "
            "f.price AS price, f.fees_usd AS fees, f.filled_at AS filled_at "
            "FROM aj_fills f JOIN aj_orders o ON f.order_id = o.id "
            "WHERE o.mode = ? ORDER BY f.filled_at ASC, f.id ASC", (mode,))
    except Exception:
        log.exception("paper_book query failed")
        rows = []

    for r in rows:
        sym = (r.get("symbol") or "").upper()
        side = (r.get("side") or "").lower()
        qty = float(r.get("qty") or 0)
        price = float(r.get("price") or 0)
        fees = float(r.get("fees") or 0)
        is_today = _et_date(aj_db.parse_iso(r.get("filled_at"))) == today
        realized = -fees                            # fees are realized cost
        fees_total += fees
        if is_today:
            fees_today += fees
        dq = lots[sym]
        if side == "buy":
            remaining = qty
            # A buy first COVERS any open short lots (FIFO), realizing
            # (short_price - cover_price) per share, THEN opens/extends a long.
            # The old code appended every buy as a long even when a short was
            # open — the cover P&L was never booked and the position silently
            # netted to flat with zero realized.
            while remaining > _EPS and dq and dq[0][0] < 0:
                lot = dq[0]                       # lot[0] is negative (short)
                take = min(remaining, -lot[0])
                realized += (lot[1] - price) * take
                lot[0] += take
                remaining -= take
                if lot[0] >= -_EPS:
                    dq.popleft()
            if remaining > _EPS:
                dq.append([remaining, price])
        elif side == "sell":
            remaining = qty
            # A sell first consumes open LONG lots (FIFO), then opens/extends a
            # short with any excess.
            while remaining > _EPS and dq and dq[0][0] > 0:
                lot = dq[0]
                take = min(remaining, lot[0])
                realized += (price - lot[1]) * take
                lot[0] -= take
                remaining -= take
                if lot[0] <= _EPS:
                    dq.popleft()
            if remaining > _EPS:
                dq.append([-remaining, price])
        realized_total += realized
        if is_today:
            realized_today += realized

    positions: Dict[str, Any] = {}
    for sym, dq in lots.items():
        tot_qty = sum(l[0] for l in dq)
        if abs(tot_qty) <= _EPS:
            continue
        cost = sum(l[0] * l[1] for l in dq)
        positions[sym] = {
            "qty": tot_qty,
            "avg_cost": (cost / tot_qty) if tot_qty else 0.0,
            "cost_basis": cost,
            "asset_type": infer_asset_type(sym),
        }
    return {"positions": positions,
            "realized_total": aj_db.money(realized_total),
            "realized_today": aj_db.money(realized_today),
            "fees_total": aj_db.money(fees_total),
            "fees_today": aj_db.money(fees_today)}


def realized_trades(mode: str = "paper") -> List[Dict[str, Any]]:
    """Replay fills FIFO and emit ONE record per closing trade — a sell that
    consumed long lots, or a buy that covered shorts — with its realized P&L
    (incl fees). Open lots are not emitted. Feeds win-rate + per-symbol P&L.
    Separate pass from paper_book() to keep that hot path untouched."""
    from collections import defaultdict, deque
    lots: Dict[str, deque] = defaultdict(deque)
    out: List[Dict[str, Any]] = []
    try:
        rows = aj_db.query(
            "SELECT o.symbol AS symbol, o.side AS side, f.qty AS qty, "
            "f.price AS price, f.fees_usd AS fees, f.filled_at AS filled_at "
            "FROM aj_fills f JOIN aj_orders o ON f.order_id = o.id "
            "WHERE o.mode = ? ORDER BY f.filled_at ASC, f.id ASC", (mode,))
    except Exception:
        return out
    for r in rows:
        sym = (r.get("symbol") or "").upper()
        side = (r.get("side") or "").lower()
        qty = float(r.get("qty") or 0)
        price = float(r.get("price") or 0)
        fees = float(r.get("fees") or 0)
        dq = lots[sym]
        closed_pl = 0.0
        closed_qty = 0.0
        if side == "buy":
            remaining = qty
            while remaining > _EPS and dq and dq[0][0] < 0:
                lot = dq[0]
                take = min(remaining, -lot[0])
                closed_pl += (lot[1] - price) * take
                closed_qty += take
                lot[0] += take
                remaining -= take
                if lot[0] >= -_EPS:
                    dq.popleft()
            if remaining > _EPS:
                dq.append([remaining, price])
        elif side == "sell":
            remaining = qty
            while remaining > _EPS and dq and dq[0][0] > 0:
                lot = dq[0]
                take = min(remaining, lot[0])
                closed_pl += (price - lot[1]) * take
                closed_qty += take
                lot[0] -= take
                remaining -= take
                if lot[0] <= _EPS:
                    dq.popleft()
            if remaining > _EPS:
                dq.append([-remaining, price])
        if closed_qty > _EPS:
            # Pro-rate the fill fee by the share that actually CLOSED, so a fill
            # that both closes a lot and opens a new one books only the closing
            # portion's fee against realized (the rest belongs to the new lot).
            closed_fee = fees * (closed_qty / qty) if qty > _EPS else fees
            net = aj_db.money(closed_pl - closed_fee)
            out.append({"symbol": sym, "side": side, "qty": closed_qty,
                        "realized": net, "price": price,
                        "filled_at": r.get("filled_at"), "win": net > 0})
    return out


def realized_by_symbol(mode: str = "paper") -> Dict[str, float]:
    agg: Dict[str, float] = {}
    for t in realized_trades(mode):
        agg[t["symbol"]] = aj_db.money(agg.get(t["symbol"], 0.0) + t["realized"])
    return agg


def positions_list(mode: str = "paper") -> List[Dict[str, Any]]:
    book = paper_book(mode)
    out = []
    for sym, p in book["positions"].items():
        out.append({"symbol": sym, "qty": p["qty"], "avg_cost": p["avg_cost"],
                    "asset_type": p["asset_type"]})
    return out
