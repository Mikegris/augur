"""AJTA — realized-gain tax lots + estimated-tax view (pure read layer).

The agent's FIFO engine (aj_positions) already nets fills into realized P&L, but
it throws away the one thing a tax view needs: each closed lot's ACQUISITION
date, without which a gain can't be split short-term (held <= 1 year, taxed as
ordinary income) vs long-term (held > 1 year, preferential rate). This module
re-runs the same FIFO pass carrying `[qty, price, acquired_date]` per lot, so
every closing trade knows how long the shares it consumed were held.

Everything here is READ-ONLY (no writes, no network) and deterministic. It is an
ESTIMATE for planning, not tax advice: holding period uses a > 365 calendar-day
rule, the wash-sale flag is a heuristic, and the tax figure uses whatever flat
combined rates the operator configures (0 until set, so it never invents a
liability). Paper and live share the identical accounting.
"""
from __future__ import annotations

from collections import defaultdict, deque
from datetime import date, datetime
from typing import Any, Dict, List, Optional

import aj_db

_EPS = 1e-9
_LONG_TERM_DAYS = 365     # IRS: long-term is held MORE than one year


def _as_date(s: Any) -> Optional[date]:
    """Parse a fill's `filled_at` (ISO-ish UTC text) to a calendar date."""
    if not s:
        return None
    txt = str(s).strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(txt).date()
    except ValueError:
        pass
    try:
        return date.fromisoformat(txt[:10])
    except ValueError:
        return None


def _classify(open_d: Optional[date], close_d: Optional[date], is_short: bool):
    """(term, holding_days). Short sales are short-term regardless of duration
    (IRS §1233); missing dates fall back to short-term (the conservative side)."""
    if open_d is None or close_d is None:
        return "short", None
    held = (close_d - open_d).days
    if is_short:
        return "short", held
    return ("long" if held > _LONG_TERM_DAYS else "short"), held


def tax_lots(mode: str = "paper") -> List[Dict[str, Any]]:
    """One record per CLOSING trade with its cost basis, proceeds, realized gain
    (net of the prorated fee), holding period and short/long term. Mirrors
    aj_positions._realized_trades_compute exactly, but each lot also carries the
    date it was opened so the close can be classified. Open lots are not emitted.
    """
    lots: Dict[str, deque] = defaultdict(deque)     # SYM -> deque([qty, price, acq_date])
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
        cdate = _as_date(r.get("filled_at"))
        dq = lots[sym]
        closed_pl = 0.0
        closed_qty = 0.0
        cost_basis = 0.0
        proceeds = 0.0
        earliest_open: Optional[date] = None
        is_short = False
        if side == "buy":
            # A buy first COVERS open short lots (FIFO) — realizing a short-sale
            # gain — before any residual opens/extends a long lot.
            remaining = qty
            while remaining > _EPS and dq and dq[0][0] < 0:
                is_short = True
                lot = dq[0]
                take = min(remaining, -lot[0])
                closed_pl += (lot[1] - price) * take
                cost_basis += price * take          # buy-to-cover is the cost
                proceeds += lot[1] * take           # original short sale = proceeds
                closed_qty += take
                if earliest_open is None or (lot[2] and (earliest_open is None or lot[2] < earliest_open)):
                    earliest_open = lot[2]
                lot[0] += take
                remaining -= take
                if lot[0] >= -_EPS:
                    dq.popleft()
            if remaining > _EPS:
                dq.append([remaining, price, cdate])
        elif side == "sell":
            # A sell first consumes open LONG lots (FIFO), then opens/extends a
            # short lot with any residual.
            remaining = qty
            while remaining > _EPS and dq and dq[0][0] > 0:
                lot = dq[0]
                take = min(remaining, lot[0])
                closed_pl += (price - lot[1]) * take
                cost_basis += lot[1] * take
                proceeds += price * take
                closed_qty += take
                if lot[2] is not None and (earliest_open is None or lot[2] < earliest_open):
                    earliest_open = lot[2]
                lot[0] -= take
                remaining -= take
                if lot[0] <= _EPS:
                    dq.popleft()
            if remaining > _EPS:
                dq.append([-remaining, price, cdate])
        if closed_qty > _EPS:
            # Prorate the fill fee by the fraction of the fill that CLOSED a lot,
            # so a fill that both closes and opens books only the closing fee.
            closed_fee = fees * (closed_qty / qty) if qty > _EPS else fees
            net = aj_db.money(closed_pl - closed_fee)
            term, held = _classify(earliest_open, cdate, is_short)
            out.append({
                "symbol": sym,
                "side": side,
                "qty": round(closed_qty, 8),
                "open_date": earliest_open.isoformat() if earliest_open else None,
                "close_date": cdate.isoformat() if cdate else None,
                "holding_days": held,
                "term": term,
                "cost_basis": aj_db.money(cost_basis + closed_fee),
                "proceeds": aj_db.money(proceeds),
                "gain": net,
                "is_short_sale": is_short,
            })
    _flag_wash_sales(out, mode)
    return out


def _flag_wash_sales(closes: List[Dict[str, Any]], mode: str) -> None:
    """Heuristic wash-sale flag: a realized LOSS whose symbol was also BOUGHT
    within ±30 calendar days of the close date. Mutates each record in place,
    adding `wash_sale` (bool). Advisory only — real wash-sale accounting tracks
    replacement-share basis and 'substantially identical' securities, which we
    don't attempt; this just surfaces losses a human should review."""
    try:
        rows = aj_db.query(
            "SELECT o.symbol AS symbol, o.side AS side, f.filled_at AS filled_at "
            "FROM aj_fills f JOIN aj_orders o ON f.order_id = o.id "
            "WHERE o.mode = ? AND o.side = 'buy'", (mode,))
    except Exception:
        rows = []
    buys: Dict[str, List[date]] = defaultdict(list)
    for r in rows:
        d = _as_date(r.get("filled_at"))
        if d is not None:
            buys[(r.get("symbol") or "").upper()].append(d)
    for c in closes:
        c["wash_sale"] = False
        if c.get("gain", 0) >= 0:
            continue
        cd = _as_date(c.get("close_date"))
        if cd is None:
            continue
        for bd in buys.get(c["symbol"], ()):
            if bd != cd and abs((bd - cd).days) <= 30:
                c["wash_sale"] = True
                break


def _rates() -> Dict[str, float]:
    import aj_config
    cfg = aj_config.get_config()
    st = float(cfg.get("tax_short_term_rate") or 0.0)
    lt = float(cfg.get("tax_long_term_rate") or 0.0)
    return {"short_term": max(0.0, st), "long_term": max(0.0, lt)}


def _estimate_tax(net_st: float, net_lt: float, rates: Dict[str, float]) -> Dict[str, Any]:
    """Simplified capital-gains netting: losses offset same-term gains, then any
    residual loss offsets the other term (IRS ordering), remainder taxed at the
    term's flat rate. Net loss => $0 tax + a carryover figure. Not a substitute
    for a tax pro — no $3,000 ordinary-income offset, no income-bracket logic."""
    st, lt = net_st, net_lt
    # cross-offset a losing term against the other term's gain
    if st < 0 and lt > 0:
        applied = min(-st, lt)
        lt -= applied
        st += applied
    elif lt < 0 and st > 0:
        applied = min(-lt, st)
        st -= applied
        lt += applied
    tax_st = max(0.0, st) * rates["short_term"]
    tax_lt = max(0.0, lt) * rates["long_term"]
    carryover = min(0.0, st) + min(0.0, lt)     # <= 0
    return {
        "tax_short_term": aj_db.money(tax_st),
        "tax_long_term": aj_db.money(tax_lt),
        "tax_total": aj_db.money(tax_st + tax_lt),
        "loss_carryover": aj_db.money(carryover),
    }


def tax_summary(mode: str = "paper", year: Optional[int] = None) -> Dict[str, Any]:
    """Realized short/long-term gains, an estimated liability at the configured
    flat rates, and a per-tax-year breakdown. `year` filters closing trades to a
    single calendar year (default: all history rolled up plus per-year detail).
    """
    lots = tax_lots(mode)
    rates = _rates()

    def _year_of(rec: Dict[str, Any]) -> Optional[int]:
        cd = rec.get("close_date")
        try:
            return int(cd[:4]) if cd else None
        except (TypeError, ValueError):
            return None

    by_year: Dict[Optional[int], Dict[str, float]] = defaultdict(
        lambda: {"short_term": 0.0, "long_term": 0.0, "proceeds": 0.0,
                 "cost_basis": 0.0, "wins": 0, "losses": 0, "n": 0,
                 "wash_sale_losses": 0.0})
    for rec in lots:
        y = _year_of(rec)
        b = by_year[y]
        g = rec["gain"]
        if rec["term"] == "long":
            b["long_term"] += g
        else:
            b["short_term"] += g
        b["proceeds"] += rec["proceeds"]
        b["cost_basis"] += rec["cost_basis"]
        b["n"] += 1
        b["wins" if g > 0 else "losses"] += 1
        if rec.get("wash_sale") and g < 0:
            b["wash_sale_losses"] += g

    years = []
    for y in sorted((k for k in by_year if k is not None), reverse=True):
        b = by_year[y]
        est = _estimate_tax(b["short_term"], b["long_term"], rates)
        years.append({
            "year": y,
            "short_term_gain": aj_db.money(b["short_term"]),
            "long_term_gain": aj_db.money(b["long_term"]),
            "total_realized": aj_db.money(b["short_term"] + b["long_term"]),
            "proceeds": aj_db.money(b["proceeds"]),
            "cost_basis": aj_db.money(b["cost_basis"]),
            "trades": b["n"], "wins": b["wins"], "losses": b["losses"],
            "wash_sale_losses": aj_db.money(b["wash_sale_losses"]),
            **est,
        })

    if year is not None:
        sel = next((y for y in years if y["year"] == year), None)
        scope_st = sel["short_term_gain"] if sel else 0.0
        scope_lt = sel["long_term_gain"] if sel else 0.0
        scope_lots = [r for r in lots if _year_of(r) == year]
    else:
        scope_st = aj_db.money(sum(y["short_term_gain"] for y in years))
        scope_lt = aj_db.money(sum(y["long_term_gain"] for y in years))
        scope_lots = lots

    est = _estimate_tax(scope_st, scope_lt, rates)
    configured = rates["short_term"] > 0 or rates["long_term"] > 0
    return {
        "mode": mode,
        "scope_year": year,
        "short_term_gain": scope_st,
        "long_term_gain": scope_lt,
        "total_realized": aj_db.money(scope_st + scope_lt),
        "rates": rates,
        "rates_configured": configured,
        "estimate": est,
        "after_tax_realized": aj_db.money(scope_st + scope_lt - est["tax_total"]),
        "closed_lots": len(scope_lots),
        "wash_sale_flags": sum(1 for r in scope_lots if r.get("wash_sale")),
        "by_year": years,
        "note": ("Estimate only — not tax advice. Set tax_short_term_rate / "
                 "tax_long_term_rate in config to compute a liability."
                 if not configured else
                 "Estimate only — not tax advice. Flat-rate approximation; "
                 "excludes the $3k ordinary-income loss offset and bracket logic."),
    }


def realized_lots_csv(mode: str = "paper") -> str:
    """Form-8949-style CSV of every closed lot (description, dates, proceeds,
    basis, gain, term). Exportable from the UI/CLI for a tax preparer."""
    cols = ["symbol", "term", "open_date", "close_date", "holding_days",
            "qty", "proceeds", "cost_basis", "gain", "wash_sale"]
    lines = [",".join(cols)]
    for r in tax_lots(mode):
        lines.append(",".join(str(r.get(c, "")) for c in cols))
    return "\n".join(lines) + "\n"
