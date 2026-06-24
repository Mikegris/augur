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

# Minimal crypto inference for asset-type-sensitive rules/marks.
_CRYPTO_HINTS = {"BTC", "ETH", "SOL", "XRP", "DOGE", "ADA", "HBAR", "LTC",
                 "BCH", "AVAX", "DOT", "MATIC", "LINK", "UNI", "ATOM"}


def infer_asset_type(symbol: str) -> str:
    s = (symbol or "").upper()
    if s.endswith("-USD") or s in _CRYPTO_HINTS:
        return "crypto"
    return "stock"


def paper_book(mode: str = "paper") -> Dict[str, Any]:
    """Net aj_fills into FIFO positions + realized P&L.

    Returns {
      positions: {SYM: {qty, avg_cost, cost_basis, asset_type}},
      realized_total, realized_today, fees_total, fees_today
    }
    """
    today = aj_db.utc_now().strftime("%Y-%m-%d")
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
        is_today = str(r.get("filled_at") or "")[:10] == today
        realized = -fees                            # fees are realized cost
        fees_total += fees
        if is_today:
            fees_today += fees
        if side == "buy":
            lots[sym].append([qty, price])
        elif side == "sell":
            remaining = qty
            dq = lots[sym]
            while remaining > 1e-12 and dq:
                lot = dq[0]
                take = min(remaining, lot[0])
                realized += (price - lot[1]) * take
                lot[0] -= take
                remaining -= take
                if lot[0] <= 1e-12:
                    dq.popleft()
            if remaining > 1e-9:
                # sold more than held — treat the excess as a short opened at
                # this price (basis = price, zero immediate realized on the
                # excess). Conservative: record a negative lot.
                lots[sym].append([-remaining, price])
        realized_total += realized
        if is_today:
            realized_today += realized

    positions: Dict[str, Any] = {}
    for sym, dq in lots.items():
        tot_qty = sum(l[0] for l in dq)
        if abs(tot_qty) <= 1e-9:
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


def positions_list(mode: str = "paper") -> List[Dict[str, Any]]:
    book = paper_book(mode)
    out = []
    for sym, p in book["positions"].items():
        out.append({"symbol": sym, "qty": p["qty"], "avg_cost": p["avg_cost"],
                    "asset_type": p["asset_type"]})
    return out
