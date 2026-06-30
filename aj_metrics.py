"""AJTA — observability metrics (AJTA-SPEC-1.0 §21.2) derived from aj_* tables.

Pure reads; never mutates state. Feeds the status CLI, the /api/aj/metrics
route, and the alert thresholds (§21.3).
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List

import aj_db
import aj_config

log = logging.getLogger("augur.aj_metrics")


def _today() -> str:
    return aj_db.utc_now().strftime("%Y-%m-%d")


def _utc_day_bounds() -> tuple:
    """[start, end) ISO-UTC instants for the current UTC date, so a range scan
    can use a created_at/ts index instead of an un-indexable substr() filter.
    Matches the old `substr(col,1,10) = today` semantics (UTC calendar day)."""
    from datetime import timedelta
    start = aj_db.utc_now().replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    return (start.isoformat(), end.isoformat())


def cumulative_pnl(mode: str = "paper") -> Dict[str, Any]:
    import aj_positions
    book = aj_positions.paper_book(mode)
    # realized total + current unrealized mark of open positions
    unreal = 0.0
    try:
        import aj_risk
        positions = book["positions"]
        # Resolve each held symbol to its quote-convention symbol FIRST (crypto
        # 'BTC' -> 'BTC-USD') so a bare crypto ticker never picks up an equity
        # quote — mirroring compute_day_pnl.
        qsyms = {sym: aj_risk._quote_symbol(sym, p.get("asset_type") or "")
                 for sym, p in positions.items()}
        marks = aj_risk._marks(list(qsyms.values()))
        for sym, p in positions.items():
            mark = marks.get(qsyms[sym])
            if mark is None:
                continue
            try:
                unreal += (mark - float(p.get("avg_cost") or 0)) * float(p.get("qty") or 0)
            except Exception:
                log.debug("cumulative_pnl bad position %s", sym, exc_info=True)
                continue
    except Exception:
        log.debug("cumulative_pnl unrealized failed", exc_info=True)
    unreal = aj_db.money(unreal)
    realized = book.get("realized_total") or 0.0
    fees = book.get("fees_total") or 0.0
    return {"realized_total": realized,
            "unrealized_open": unreal,
            "total": aj_db.money(realized + unreal),
            "fees_total": fees}


def order_stats() -> Dict[str, Any]:
    rows = aj_db.query("SELECT state, COUNT(*) AS n FROM aj_orders GROUP BY state")
    by_state = {r["state"]: r["n"] for r in rows}
    total = sum(by_state.values())
    filled = by_state.get("filled", 0) + by_state.get("partially_filled", 0)
    # Denominator = orders that actually reached a working/fillable state.
    # Exclude "new" (never submitted) AND terminal non-fillable states
    # (rejected/canceled/expired): those can never appear in `filled`, so
    # counting them structurally deflates fill_rate. A normal burst of
    # gate/broker rejections must not drag fill_rate under the autonomy
    # health-check threshold and spuriously trip the kill switch.
    submitted = (total - by_state.get("new", 0)
                 - by_state.get("rejected", 0)
                 - by_state.get("canceled", 0)
                 - by_state.get("expired", 0))
    fill_rate = (filled / submitted) if submitted else None
    return {"by_state": by_state, "total": total,
            "fill_rate": round(fill_rate, 3) if fill_rate is not None else None,
            "open": sum(by_state.get(s, 0) for s in
                        ("new", "submitted", "accepted", "partially_filled", "unknown"))}


def gate_stats(day: bool = True) -> Dict[str, Any]:
    # Range scan on created_at (idx_aj_risk_events_created) instead of an
    # un-indexable substr() filter; same UTC-calendar-day semantics.
    if day:
        start, end = _utc_day_bounds()
        where = "WHERE created_at >= ? AND created_at < ?"
        params = (start, end)
    else:
        where = ""
        params = ()
    rows = aj_db.query(
        "SELECT decision, COUNT(*) AS n FROM aj_risk_events {} GROUP BY decision".format(where),
        params)
    return {r["decision"]: r["n"] for r in rows}


def recon_stats() -> Dict[str, Any]:
    rows = aj_db.query("SELECT status, COUNT(*) AS n FROM aj_recon GROUP BY status")
    return {r["status"]: r["n"] for r in rows}


def routing_stats(day: bool = True) -> Dict[str, Any]:
    # Range scan on ts (idx_aj_routing_ts) instead of an un-indexable substr().
    if day:
        start, end = _utc_day_bounds()
        where = "WHERE ts >= ? AND ts < ?"
        params = (start, end)
    else:
        where = ""
        params = ()
    rows = aj_db.query(
        "SELECT COUNT(*) AS calls, "
        "SUM(ok) AS oks, SUM(escalated) AS escalations, "
        "SUM(fallback_used) AS fallbacks, AVG(latency_ms) AS avg_latency_ms, "
        "SUM(cost_usd) AS cost_usd FROM aj_routing {}".format(where), params)
    r = rows[0] if rows else {}
    calls = r.get("calls") or 0
    return {"calls": calls, "ok_rate": round((r.get("oks") or 0) / calls, 3) if calls else None,
            "escalations": r.get("escalations") or 0,
            "fallbacks": r.get("fallbacks") or 0,
            "avg_latency_ms": int(r.get("avg_latency_ms") or 0),
            "cost_usd": aj_db.money(r.get("cost_usd") or 0)}


def alerts() -> List[Dict[str, str]]:
    """Evaluate §21.3 thresholds against current metrics. Returns a list of
    {level, message}. Critical items also indicate a halted/blocked posture."""
    out: List[Dict[str, str]] = []
    try:
        if aj_config.get_config().get("trading_enabled") is False:
            out.append({"level": "info", "message": "trading disabled (halted or never enabled)"})
        os_ = order_stats()
        if os_.get("fill_rate") is not None and os_["fill_rate"] < 0.5 and os_.get("total", 0) >= 4:
            out.append({"level": "warning", "message": "order fill-rate < 50%"})
        rc = recon_stats()
        if rc.get("divergence"):
            out.append({"level": "critical", "message": "{} reconciliation divergence(s)".format(rc["divergence"])})
        gs = gate_stats()
        if gs.get("halt"):
            out.append({"level": "critical", "message": "{} halt event(s) today".format(gs["halt"])})
        unknown = (os_.get("by_state") or {}).get("unknown", 0)   # reuse os_, no re-query
        if unknown:
            out.append({"level": "critical", "message": "{} order(s) in UNKNOWN state".format(unknown)})
    except Exception:
        log.exception("alerts evaluation failed")
    return out


def status() -> Dict[str, Any]:
    """One snapshot for the dashboard / `aj status`."""
    cfg = aj_config.get_config()
    import aj_risk
    out = {
        # AJTA agent version. v3.4 adds the Analyst Council advisory layer.
        "version": "3.12.1",
        "trading_enabled": cfg.get("trading_enabled"),
        "live_trading_enabled": cfg.get("live_trading_enabled"),
        "session": aj_db.market_session(),
        "halted": aj_risk.is_halted(),
        "config": {k: cfg.get(k) for k in (
            "symbol_allowlist", "max_order_notional_usd", "max_trades_per_day",
            "max_daily_loss_usd", "daily_loss_basis", "session_whitelist",
            "default_broker", "auto_approve_paper")},
        "day_pnl": None,
        "cumulative_pnl": cumulative_pnl(),
        "orders": order_stats(),
        "gate_today": gate_stats(),
        "recon": recon_stats(),
        "routing_today": routing_stats(),
        "alerts": alerts(),
        "audit_chain": aj_db.verify_audit_chain(quick=True),
    }
    try:
        out["day_pnl"] = aj_risk.compute_day_pnl()
    except Exception:
        log.debug("status day_pnl failed", exc_info=True)
    try:
        import aj_autonomy
        out["scheduler"] = aj_autonomy.scheduler_status(cfg)
    except Exception:
        log.debug("status scheduler failed", exc_info=True)
    # effectiveness layer: which alpha/selection/execution/allocation features
    # are live, plus the IC-promotion verdict for the orthogonal signals.
    try:
        eff = {k: cfg.get(k) for k in (
            "multi_factor_signals", "signal_ic_gate", "regime_conditional_weights",
            "cross_sectional_selection", "cross_sectional_top_n",
            "limit_entry", "cost_gate", "time_stop_days", "event_blackout_days",
            "profit_ladder", "portfolio_construction", "alloc_method")}
        if cfg.get("multi_factor_signals"):
            import aj_ic
            eff["signal_promotion"] = aj_ic.promotion_status(
                ["smart_money", "insider", "congress", "social"], cfg)
        eff["metalabel_enabled"] = bool(cfg.get("metalabel_enabled"))
        try:
            import aj_metalabel
            eff["metalabel"] = aj_metalabel.is_promoted(cfg)
        except Exception:
            log.debug("status metalabel failed", exc_info=True)
        out["effectiveness"] = eff
    except Exception:
        log.debug("status effectiveness failed", exc_info=True)
    return out
