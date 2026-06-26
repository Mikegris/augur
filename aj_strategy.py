"""AJTA — order sizing & entry construction (enhancements ①②③④).

Pure decision helpers used by the operator:
  ① conviction-weighted sizing — scale the order toward the per-order cap in
     proportion to the forecast edge (stronger edge → bigger size).
  ② min-order-notional floor — skip dust orders below a configured minimum.
  ③ limit-order entries — place a passive limit at a configured bps offset
     through the quote instead of a market order.
Every result still passes the risk gate; this only shapes qty/price/type.
"""
from __future__ import annotations

import logging
import math
from typing import Any, Dict, Optional

import aj_db
import aj_positions

log = logging.getLogger("augur.aj_strategy")

# Edge (pct-points) at which conviction sizing reaches full size.
_FULL_SIZE_EDGE_PTS = 20.0
_MIN_SIZE_FACTOR = 0.25


def size_order(symbol: str, side: str, cfg: Dict[str, Any], held_qty: float,
               decision: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """Return {qty, price, notional, order_type, limit_price} or None to skip."""
    import aj_risk
    atype = aj_positions.infer_asset_type(symbol)
    price = aj_risk._order_price(symbol, "market", None, atype)
    # NaN/inf must fail closed: `NaN <= 0` is False, so a non-finite price would
    # slip through and propagate a NaN qty/notional into the order.
    if price is None or not math.isfinite(price) or price <= 0:
        return None

    max_notional = aj_db.money(cfg.get("max_order_notional_usd") or 0)
    target = aj_db.money(cfg.get("order_notional_target_usd") or 0)
    if target <= 0:
        target = aj_db.money(max_notional * 0.5)   # default: half the per-order cap

    # ① conviction sizing — scale target by edge strength
    if cfg.get("conviction_sizing") and decision:
        edge = abs(float(decision.get("edge_pts") or 0))
        factor = min(1.0, max(_MIN_SIZE_FACTOR, edge / _FULL_SIZE_EDGE_PTS))
        target = aj_db.money(target * factor)

    # 100x sizing intelligence (Kelly · vol-target · compounding · symbol-perf ·
    # drawdown throttle). A combined multiplier on the target; 0 vetoes the order
    # (chronic-loser / drawdown halt). No-op (1.0) when every 100x flag is off.
    try:
        import aj_alpha
        mult = aj_alpha.sizing_multiplier(symbol, side, cfg, decision)
        if mult <= 0:
            return None
        target = aj_db.money(target * mult)
    except Exception:
        log.debug("alpha sizing multiplier skipped", exc_info=True)

    target = min(target, max_notional)
    if target <= 0:
        return None

    if side == "sell":
        # A sell is an EXIT — close the full held position. The notional target
        # (half the per-order cap) is a sizing budget for opening exposure, not a
        # ceiling on how much risk we may unwind; capping here would under-sell an
        # exit and leave residual risk the sell signal meant to remove.
        qty = held_qty if held_qty > 0 else 0.0
    else:
        qty = target / price
    if qty <= 0:
        return None

    notional = aj_db.money(qty * price)
    # ② min-order-notional floor (skip dust)
    min_notional = aj_db.money(cfg.get("min_order_notional_usd") or 0)
    # Dust floor applies to ENTRIES only. A sell is an exit closing the full held
    # position; skipping it because the residual notional is below the floor would
    # leave unwanted risk on the book indefinitely (stop/take-profit/time-stop
    # could never close a small residual). Always let an exit through.
    if side != "sell" and min_notional > 0 and notional < min_notional:
        if max_notional < min_notional:
            # Contradictory config: the per-order cap is below the dust floor, so
            # EVERY buy is silently skipped. Surface it to the operator, not debug.
            log.warning("skip %s %s: max_order_notional %.2f < min_order_notional "
                        "%.2f — no order can ever pass the floor; check config",
                        side, symbol, max_notional, min_notional)
        else:
            log.debug("skip %s %s: notional %.2f below min %.2f", side, symbol, notional, min_notional)
        return None

    # ③ entry order type — passive limit at an offset, or market
    order_type = "market"
    limit_price = None
    if str(cfg.get("entry_order_type") or "market").lower() == "limit":
        order_type = "limit"
        off = float(cfg.get("entry_limit_offset_bps") or 0) / 1e4
        # buy passively BELOW the quote, sell passively ABOVE it
        limit_price = aj_db.money(price * (1 - off)) if side == "buy" \
            else aj_db.money(price * (1 + off))

    return {"qty": qty, "price": price, "notional": notional,
            "order_type": order_type, "limit_price": limit_price}
