"""AJTA — exit rules, position-state tracking, extra gates, order TTL
(enhancements ④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭).

Per-cycle machinery layered on the core gate. Everything is opt-in (a 0/disabled
config value is a no-op), so the fail-closed core is unchanged. Exit sells and
the extra gates still pass through the risk gate / execution path.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import aj_db
import aj_config

log = logging.getLogger("augur.aj_rules")


# ── VIX (regime gate ⑭) ───────────────────────────────────────────────────────

def current_vix() -> Optional[float]:
    """Best-effort VIX level. None when undeterminable (the regime gate then
    does not block — it's a risk-off enhancement, not the core rail)."""
    try:
        import fetcher
        for sym in ("^VIX", "VIX", "VIXY"):
            q = fetcher.get_quote(sym) or {}
            p = q.get("price")
            if isinstance(p, (int, float)) and not isinstance(p, bool) and p > 0:
                return float(p)
    except Exception:
        log.debug("VIX lookup failed", exc_info=True)
    return None


# ── position state (peak/last marks, opened_at, last_exit_at) ─────────────────

def _marks_for(symbols: List[str]) -> Dict[str, Optional[float]]:
    import aj_risk
    return aj_risk._marks(symbols)


def update_position_state(book: Optional[Dict[str, Any]] = None) -> None:
    """Refresh aj_position_state for every open paper position: stamp opened_at
    on first sight, track peak_mark (for trailing stops) + last_mark, and record
    last_exit_at when a previously-open position is now flat."""
    import aj_positions
    if book is None:
        book = aj_positions.paper_book()
    positions = book.get("positions") or {}
    marks = _marks_for(list(positions.keys()))
    now = aj_db.utc_now_iso()

    existing = {r["symbol"]: r for r in aj_db.query("SELECT * FROM aj_position_state")}
    with db_lock():
        conn = _conn()
        for sym, p in positions.items():
            mark = marks.get(sym) or float(p.get("avg_cost") or 0)
            row = existing.get(sym)
            if row and row.get("opened_at"):
                peak = max(float(row.get("peak_mark") or 0), mark)
                conn.execute(
                    "UPDATE aj_position_state SET peak_mark=?, last_mark=?, updated_at=?, last_exit_at=NULL WHERE symbol=?",
                    (peak, mark, now, sym))
            else:
                conn.execute(
                    "INSERT OR REPLACE INTO aj_position_state "
                    "(symbol, opened_at, peak_mark, last_mark, updated_at, last_exit_at) "
                    "VALUES (?,?,?,?,?,NULL)", (sym, now, mark, mark, now))
        # positions that were open and are now flat -> record exit time
        for sym, row in existing.items():
            if sym not in positions and row.get("opened_at"):
                conn.execute(
                    "UPDATE aj_position_state SET opened_at=NULL, peak_mark=NULL, "
                    "last_mark=?, updated_at=?, last_exit_at=? WHERE symbol=?",
                    (row.get("last_mark"), now, now, sym))
        conn.commit()


def _conn():
    import database as db
    return db.get_conn()


def db_lock():
    import database as db
    return db._write_lock


def position_age_days(symbol: str) -> Optional[float]:
    rows = aj_db.query("SELECT opened_at FROM aj_position_state WHERE symbol=?", (symbol,))
    if not rows or not rows[0].get("opened_at"):
        return None
    opened = aj_db.parse_iso(rows[0]["opened_at"])
    if not opened:
        return None
    return round((aj_db.utc_now() - opened).total_seconds() / 86400.0, 2)


# ── re-entry cooldown ⑧ ───────────────────────────────────────────────────────

def in_cooldown(symbol: str, cfg: Optional[Dict[str, Any]] = None) -> bool:
    cfg = cfg or aj_config.get_config()
    mins = int(cfg.get("exit_cooldown_min") or 0)
    if mins <= 0:
        return False
    rows = aj_db.query("SELECT last_exit_at FROM aj_position_state WHERE symbol=?",
                       (symbol.upper(),))
    if not rows or not rows[0].get("last_exit_at"):
        return False
    last = aj_db.parse_iso(rows[0]["last_exit_at"])
    if not last:
        return False
    return (aj_db.utc_now() - last).total_seconds() < mins * 60


# ── exit signals ⑤⑥⑦ ─────────────────────────────────────────────────────────

def exit_signals(cfg: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """Sell signals for open LONG paper positions hitting take-profit, stop-loss,
    or a trailing stop. Returns [{symbol, qty, reason, mark}]."""
    cfg = cfg or aj_config.get_config()
    tp = float(cfg.get("take_profit_pct") or 0)
    sl = float(cfg.get("stop_loss_pct") or 0)
    tr = float(cfg.get("trailing_stop_pct") or 0)
    if tp <= 0 and sl <= 0 and tr <= 0:
        return []
    import aj_positions
    book = aj_positions.paper_book()
    positions = book.get("positions") or {}
    marks = _marks_for(list(positions.keys()))
    state = {r["symbol"]: r for r in aj_db.query("SELECT * FROM aj_position_state")}
    out: List[Dict[str, Any]] = []
    for sym, p in positions.items():
        qty = float(p.get("qty") or 0)
        if qty <= 0:                      # only long positions exit on TP/SL here
            continue
        avg = float(p.get("avg_cost") or 0)
        mark = marks.get(sym)
        if mark is None or avg <= 0:
            continue
        gain_pct = (mark - avg) / avg * 100.0
        reason = None
        if tp > 0 and gain_pct >= tp:
            reason = "take-profit +{:.1f}%".format(gain_pct)
        elif sl > 0 and gain_pct <= -sl:
            reason = "stop-loss {:.1f}%".format(gain_pct)
        elif tr > 0:
            peak = float((state.get(sym) or {}).get("peak_mark") or mark)
            if peak > 0 and (peak - mark) / peak * 100.0 >= tr:
                reason = "trailing-stop {:.1f}% off peak".format((peak - mark) / peak * 100.0)
        if reason:
            out.append({"symbol": sym, "qty": qty, "reason": reason, "mark": mark})
    return out


# ── order TTL ④ ───────────────────────────────────────────────────────────────

def expire_stale_orders(cfg: Optional[Dict[str, Any]] = None) -> int:
    """Cancel/expire resting limit orders older than order_ttl_cycles cycles.
    Age is measured in completed cycles since the order was created."""
    cfg = cfg or aj_config.get_config()
    ttl = int(cfg.get("order_ttl_cycles") or 0)
    if ttl <= 0:
        return 0
    # completed cycles since order creation, via aj_cycles timestamps
    open_orders = aj_db.query(
        "SELECT id, created_at FROM aj_orders WHERE state IN ('accepted','submitted')")
    if not open_orders:
        return 0
    n = 0
    import aj_execution
    for o in open_orders:
        created = aj_db.parse_iso(o.get("created_at"))
        if not created:
            continue
        cycles_since = aj_db.query(
            "SELECT COUNT(*) AS n FROM aj_cycles WHERE started_at > ? AND status IN ('completed','recovered')",
            (o["created_at"],))
        if cycles_since and int(cycles_since[0]["n"]) >= ttl:
            if aj_execution._set_state(o["id"], "expired"):
                n += 1
                aj_db.audit("order", {"order_id": o["id"], "expired": "TTL {} cycles".format(ttl)},
                            ref_id=o["id"])
    return n


# ── extra gates ⑨⑩⑪⑫⑬⑭ (called by aj_risk.evaluate) ─────────────────────────

def enhanced_gate(symbol: str, side: str, qty: float, price: float,
                  cfg: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """Return a block reason or None. All checks are no-ops when their config is
    0/disabled. Fail-closed: an unexpected error blocks (returns a reason)."""
    try:
        cfg = cfg or aj_config.get_config()
        symbol = symbol.upper()
        import aj_positions
        book = aj_positions.paper_book()
        positions = book.get("positions") or {}
        held = float((positions.get(symbol) or {}).get("qty") or 0)
        opening_new = side == "buy" and held <= 0

        # ⑫ time-of-day filter (regular session only)
        skip_open = int(cfg.get("trade_skip_open_min") or 0)
        skip_close = int(cfg.get("trade_skip_close_min") or 0)
        if (skip_open > 0 or skip_close > 0) and aj_db.market_session() == "regular":
            mins = _minutes_into_regular()
            if mins is not None:
                if mins < skip_open:
                    return "within first {} min of session".format(skip_open)
                if mins > (390 - skip_close):     # 390 = 6.5h regular session
                    return "within last {} min of session".format(skip_close)

        # ⑨ max open positions (only when opening a NEW name with a buy)
        cap = int(cfg.get("max_open_positions") or 0)
        if cap > 0 and opening_new and len(positions) >= cap:
            return "max open positions reached ({})".format(cap)

        # ⑪ per-symbol daily trade cap
        sym_cap = int(cfg.get("max_trades_per_symbol_per_day") or 0)
        if sym_cap > 0:
            today = aj_db.utc_now().strftime("%Y-%m-%d")
            rows = aj_db.query(
                "SELECT COUNT(*) AS n FROM aj_orders WHERE symbol=? "
                "AND substr(COALESCE(submitted_at, created_at),1,10)=? "
                "AND state NOT IN ('rejected','canceled')", (symbol, today))
            if rows and int(rows[0]["n"]) >= sym_cap:
                return "per-symbol daily trade cap ({}/{})".format(rows[0]["n"], sym_cap)

        # ⑩ per-symbol weight cap (projected book weight after a buy)
        wcap = float(cfg.get("max_symbol_weight_pct") or 0)
        if wcap > 0 and side == "buy":
            import aj_risk
            marks = aj_risk._marks(list(positions.keys()) + [symbol])
            total = 0.0
            sym_val = 0.0
            for s, p in positions.items():
                mv = float(p.get("qty") or 0) * (marks.get(s) or float(p.get("avg_cost") or 0))
                total += mv
                if s == symbol:
                    sym_val = mv
            add = qty * price
            total += add
            sym_val += add
            if total > 0 and (sym_val / total * 100.0) > wcap:
                return "{} would be {:.1f}% of book (cap {:g}%)".format(
                    symbol, sym_val / total * 100.0, wcap)

        # ⑬ slippage guard (estimated adverse bps for a market order)
        smax = float(cfg.get("max_slippage_bps") or 0)
        if smax > 0:
            est = float(cfg.get("paper_slippage_bps") or 0) + \
                0.5 * float(cfg.get("paper_spread_fraction") or 0) * 8  # nominal
            if str(cfg.get("entry_order_type") or "market").lower() == "market" and est > smax:
                return "estimated slippage {:.1f}bps over cap {:g}bps".format(est, smax)

        # ⑭ risk-off VIX regime (only blocks NEW buys)
        rvix = float(cfg.get("risk_off_vix") or 0)
        if rvix > 0 and opening_new:
            vix = current_vix()
            if vix is not None and vix > rvix:
                return "risk-off: VIX {:.1f} > {:g}".format(vix, rvix)
        return None
    except Exception as e:
        log.exception("enhanced_gate error -> block (fail-closed)")
        return "enhanced gate error (fail-closed): {}".format(str(e)[:80])


def _minutes_into_regular() -> Optional[int]:
    """Minutes since the 09:30 ET regular open, or None if undeterminable."""
    try:
        from zoneinfo import ZoneInfo
        ny = aj_db.utc_now().astimezone(ZoneInfo("America/New_York"))
        return (ny.hour * 60 + ny.minute) - (9 * 60 + 30)
    except Exception:
        return None
