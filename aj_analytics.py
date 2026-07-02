"""AJTA — analytics & accountability (enhancements ⑯⑰⑱⑲⑳㉑㉒㉔㉕).

Daily equity curve, per-symbol P&L attribution, win-rate / profit-factor,
Sharpe-like ratio, position aging, backtest-lite forecast replay, cycle-history
log, CSV trade-journal export, and fill notifications. Pure reads + append-only
snapshots; never touches the gate/execution path.
"""
from __future__ import annotations

import io
import csv
import logging
import math
import subprocess
from typing import Any, Dict, List, Optional

import aj_db

log = logging.getLogger("augur.aj_analytics")


# ── ⑯ equity curve ────────────────────────────────────────────────────────────

def snapshot_equity() -> Dict[str, Any]:
    """Append today's paper equity (realized + unrealized) to aj_equity.

    The `date` key is the AMERICA/NEW_YORK trading date, NOT the UTC date: an
    evening cycle (20:00–24:00 ET) is still "today" on the exchange clock but
    already tomorrow in UTC, which shifted the aj_benchmark join (index closes
    are keyed by ET date) by a whole day. `ts` stays UTC for audit ordering;
    rows stamped before this fix keep their stored dates (backward compat —
    only NEW snapshots are keyed in ET)."""
    import aj_risk
    import aj_metrics
    pnl = aj_risk.compute_day_pnl()
    cum = aj_metrics.cumulative_pnl()
    now = aj_db.utc_now()
    try:
        from zoneinfo import ZoneInfo
        date_str = now.astimezone(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
    except Exception:  # zoneinfo/tzdata missing → degrade to the old UTC date
        date_str = now.strftime("%Y-%m-%d")
    row = {"ts": aj_db.utc_now_iso(), "date": date_str,
           "realized_usd": cum.get("realized_total", 0.0),
           "unrealized_usd": cum.get("unrealized_open", 0.0),
           "equity_usd": cum.get("total", 0.0),
           "day_pnl_usd": pnl.get("day_pnl", 0.0)}
    aj_db.insert("aj_equity", **row)
    return row


def equity_curve(days: int = 90) -> List[Dict[str, Any]]:
    """Latest equity per date (one point/day)."""
    base = ("SELECT date, equity_usd, day_pnl_usd, realized_usd, unrealized_usd "
            "FROM aj_equity WHERE id IN (SELECT MAX(id) FROM aj_equity GROUP BY date) ")
    if days:
        # Push the row limit into SQL: take the most-recent `days` dates, then
        # reverse to chronological order — instead of loading the entire history
        # and slicing the tail in Python.
        rows = aj_db.query(base + "ORDER BY date DESC LIMIT ?", (days,))
        return list(reversed(rows))
    return aj_db.query(base + "ORDER BY date ASC")


# ── ⑰ per-symbol P&L attribution ──────────────────────────────────────────────

def attribution() -> List[Dict[str, Any]]:
    """Realized (FIFO) + current unrealized P&L per symbol."""
    import aj_positions
    import aj_risk
    realized = aj_positions.realized_by_symbol("paper")
    book = aj_positions.paper_book()
    positions = book.get("positions") or {}
    # Resolve each held symbol to its quote-convention symbol FIRST (crypto
    # 'BTC' -> 'BTC-USD') so a bare crypto ticker never picks up an equity quote.
    qsyms = {sym: aj_risk._quote_symbol(sym, p.get("asset_type") or "")
             for sym, p in positions.items()}
    marks = aj_risk._marks(list(qsyms.values()))
    out: Dict[str, Dict[str, Any]] = {}
    for sym, r in realized.items():
        out.setdefault(sym, {"symbol": sym, "realized": 0.0, "unrealized": 0.0, "qty": 0.0})
        out[sym]["realized"] = r
    for sym, p in positions.items():
        qty = float(p.get("qty") or 0)
        avg = float(p.get("avg_cost") or 0)
        mark = marks.get(qsyms[sym])
        unreal = aj_db.money((mark - avg) * qty) if mark is not None else 0.0
        d = out.setdefault(sym, {"symbol": sym, "realized": 0.0, "unrealized": 0.0, "qty": 0.0})
        d["unrealized"] = unreal
        d["qty"] = qty
    for d in out.values():
        d["total"] = aj_db.money(d["realized"] + d["unrealized"])
    return sorted(out.values(), key=lambda x: -x["total"])


# ── ⑱ win-rate / profit factor ────────────────────────────────────────────────

def trade_stats() -> Dict[str, Any]:
    import aj_positions
    trades = aj_positions.realized_trades("paper")
    n = len(trades)
    wins = [t for t in trades if (t.get("realized") or 0) > 0]
    losses = [t for t in trades if (t.get("realized") or 0) < 0]
    gross_win = aj_db.money(sum(t.get("realized") or 0 for t in wins))
    gross_loss = aj_db.money(-sum(t.get("realized") or 0 for t in losses))
    return {
        "trades": n,
        "wins": len(wins), "losses": len(losses),
        "win_rate": round(len(wins) / n, 3) if n else None,
        "avg_win": aj_db.money(gross_win / len(wins)) if wins else 0.0,
        "avg_loss": aj_db.money(gross_loss / len(losses)) if losses else 0.0,
        "gross_profit": gross_win, "gross_loss": gross_loss,
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else None,
        "net": aj_db.money(gross_win - gross_loss),
    }


# ── ⑲ Sharpe-like on the equity curve ────────────────────────────────────────

def sharpe_like(days: int = 90) -> Dict[str, Any]:
    """Annualized Sharpe-like ratio of daily equity *changes* (P&L), since paper
    has no funded base to compute % returns against. Needs >= 3 days."""
    curve = equity_curve(days)
    pnls = [float(r.get("day_pnl_usd") or 0) for r in curve]
    if len(pnls) < 3:
        return {"sharpe": None, "days": len(pnls), "note": "need >= 3 days"}
    mean = sum(pnls) / len(pnls)
    var = sum((x - mean) ** 2 for x in pnls) / (len(pnls) - 1)
    sd = math.sqrt(var)
    if sd == 0:
        return {"sharpe": None, "days": len(pnls), "note": "zero variance"}
    return {"sharpe": round((mean / sd) * math.sqrt(252), 2),
            "days": len(pnls), "mean_day_pnl": aj_db.money(mean),
            "stdev_day_pnl": aj_db.money(sd)}


# ── ⑳ position aging ──────────────────────────────────────────────────────────

def position_aging() -> List[Dict[str, Any]]:
    import aj_positions
    import aj_rules
    book = aj_positions.paper_book()
    out = []
    for sym in (book.get("positions") or {}):
        out.append({"symbol": sym, "age_days": aj_rules.position_age_days(sym)})
    return sorted(out, key=lambda x: (x["age_days"] is None, -(x["age_days"] or 0)))


# ── ㉑ backtest-lite (forecast replay) ────────────────────────────────────────

def backtest_lite() -> Dict[str, Any]:
    """Hit-rate of the agent's logged ensemble forecasts vs realized direction,
    from research_tracker's scored signal_forecasts. Research only — never
    enables live (§23.3)."""
    try:
        import research_tracker
        rows = research_tracker.get_scored_rows() if hasattr(research_tracker, "get_scored_rows") else []
    except Exception:
        rows = []
    ens = [r for r in rows if str(r.get("signal_name", "")).startswith("ens:")]
    scored = [r for r in ens if r.get("hit") is not None]
    hits = sum(1 for r in scored if r.get("hit"))
    out = {"scored": len(scored), "hits": hits,
           "hit_rate": round(hits / len(scored), 3) if scored else None,
           "note": "backtest-lite: forecast hit-rate; does NOT enable live (§23.3)"}
    return out


# ── ㉒ cycle-history log ───────────────────────────────────────────────────────

def log_cycle(summary: Dict[str, Any]) -> None:
    try:
        props = summary.get("proposals") or []
        executed = sum(1 for p in props if isinstance(p, dict) and p.get("result") == "executed")
        import json as _json
        aj_db.insert("aj_cycle_log", cycle_id=summary.get("cycle_id"),
                     ts=aj_db.utc_now_iso(), mode=summary.get("mode"),
                     session=summary.get("session"), n_proposals=len(props),
                     n_executed=executed,
                     summary_json=_json.dumps(
                         {"reconcile": summary.get("reconcile"),
                          "recovery": (summary.get("recovery") or {}).get("crashed"),
                          "results": [p.get("result") for p in props if isinstance(p, dict)]},
                         default=str))
    except Exception:
        log.debug("log_cycle failed", exc_info=True)


def cycle_history(limit: int = 50) -> List[Dict[str, Any]]:
    return aj_db.query("SELECT * FROM aj_cycle_log ORDER BY id DESC LIMIT ?", (limit,))


# ── ㉔ trade-journal CSV export ────────────────────────────────────────────────

def journal_csv() -> str:
    rows = aj_db.query(
        "SELECT f.filled_at, o.symbol, o.side, f.qty, f.price, f.fees_usd, "
        "o.mode, o.broker, o.client_order_id "
        "FROM aj_fills f JOIN aj_orders o ON f.order_id = o.id "
        "ORDER BY f.filled_at ASC, f.id ASC")
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["filled_at", "symbol", "side", "qty", "price", "fees_usd",
                "notional", "mode", "broker", "client_order_id"])
    for r in rows:
        notional = aj_db.money(float(r.get("qty") or 0) * float(r.get("price") or 0))
        w.writerow([r.get("filled_at"), r.get("symbol"), r.get("side"),
                    r.get("qty"), r.get("price"), r.get("fees_usd"), notional,
                    r.get("mode"), r.get("broker"), r.get("client_order_id")])
    return buf.getvalue()


# ── ㉕ fill notifications ──────────────────────────────────────────────────────

def notify_fill(symbol: str, side: str, qty: float, price: float) -> None:
    """Best-effort macOS notification on a fill. No-op off macOS / on error."""
    try:
        import aj_config
        if not aj_config.get_config().get("notify_fills"):
            return
        msg = "{} {:g} {} @ ${:,.2f}".format(side.upper(), qty, symbol, price)
        # Escape AppleScript string metacharacters so a malformed symbol/side
        # cannot break out of the string literal and inject osascript.
        msg = msg.replace("\\", "\\\\").replace('"', '\\"')
        subprocess.run(
            ["osascript", "-e",
             'display notification "{}" with title "AUGUR paper fill"'.format(msg)],
            check=False, capture_output=True, timeout=5)
    except Exception:
        log.debug("notify_fill failed", exc_info=True)


def positions_detail() -> Dict[str, Any]:
    """The agent's current PAPER positions, enriched per stock: quantity, avg
    cost, current mark, market value, unrealized P&L (+%), book weight %, and
    holding age. This is the agent's portfolio view (separate from your real
    holdings)."""
    import aj_positions
    import aj_risk
    import aj_rules
    book = aj_positions.paper_book()
    positions = book.get("positions") or {}
    # Resolve each held symbol to its quote-convention symbol FIRST (crypto
    # 'BTC' -> 'BTC-USD') so a bare crypto ticker never picks up an equity quote.
    qsyms = {sym: aj_risk._quote_symbol(sym, p.get("asset_type") or "")
             for sym, p in positions.items()}
    marks = aj_risk._marks(list(qsyms.values()))
    rows = []
    total_mv = 0.0
    gross_mv = 0.0
    total_unreal = 0.0
    for sym, p in positions.items():
        qty = float(p.get("qty") or 0)
        avg = float(p.get("avg_cost") or 0)
        mark = marks.get(qsyms[sym])
        if mark is None:
            mark = avg
        mv = qty * mark
        total_mv += mv
        gross_mv += abs(mv)
        unreal = (mark - avg) * qty
        total_unreal += unreal
        rows.append({
            "symbol": sym, "qty": round(qty, 4), "avg_cost": round(avg, 2),
            "mark": round(mark, 2), "market_value": aj_db.money(mv),
            "unrealized": aj_db.money(unreal),
            # % off the position's GROSS cost basis, sign-carried by the P&L
            # itself: (mark-avg)/avg alone is INVERTED for a short (qty < 0),
            # where a falling mark is a gain. unreal already embeds sign(qty).
            "unrealized_pct": (round(unreal / (abs(avg) * abs(qty)) * 100, 2)
                               if (avg > 0 and qty) else 0.0),
            "age_days": aj_rules.position_age_days(sym),
            "asset_type": p.get("asset_type") or "stock"})
    for r in rows:
        # Weight by gross exposure (sum of |market value|) so short legs and
        # net-short books still produce meaningful, non-zero concentration.
        r["weight_pct"] = round(abs(r["market_value"]) / gross_mv * 100, 1) if gross_mv > 0 else 0.0
    rows.sort(key=lambda x: -abs(x["market_value"]))
    return {"positions": rows, "count": len(rows),
            "total_market_value": aj_db.money(total_mv),
            "total_unrealized": aj_db.money(total_unreal)}


def summary() -> Dict[str, Any]:
    """One bundle for the analytics route / CLI / UI."""
    return {"positions": positions_detail(),
            "equity_curve": equity_curve(60),
            "attribution": attribution(),
            "trade_stats": trade_stats(),
            "sharpe": sharpe_like(),
            "aging": position_aging(),
            "backtest_lite": backtest_lite(),
            "cycle_history": cycle_history(20)}
