"""AJTA — autonomous-operation layer (enhancements 21-25).

The machinery that lets the agent run itself: a market-hours scheduler, a
self-diagnostic health monitor that can pull its own kill switch, performance-
triggered risk-preset escalation, an end-of-day reflection journal, and a
pre-market opportunity briefing.

All opt-in and fail-safe: with the 100x flags off these functions are no-ops or
pure reads. Anything that could place trades still routes through the fail-closed
risk gate; the only autonomous *mutation* of control state is health_autohalt's
kill switch (a strictly risk-REDUCING action) and the opt-in preset escalation
(which never touches master switches or the allowlist).
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

import aj_db
import aj_config

log = logging.getLogger("augur.aj_autonomy")

_LAST_RUN_KEY = "__aj_last_auto_run"
_REFLECTION_KEY = "__aj_reflection"        # + _YYYY-MM-DD
_BRIEFING_KEY = "__aj_briefing"
_ESCALATION_KEY = "__aj_preset_level"


# ════════════════════════════════════════════════════════════════════════════
#  21 — CONTINUOUS AUTO-RUN SCHEDULER
# ════════════════════════════════════════════════════════════════════════════

def _last_auto_run():
    raw = aj_db.get_setting_raw(_LAST_RUN_KEY)
    return aj_db.parse_iso(raw) if raw else None


def minutes_since_last_run() -> Optional[float]:
    last = _last_auto_run()
    if not last:
        return None
    return (aj_db.utc_now() - last).total_seconds() / 60.0


def due_to_run(cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Whether an automatic cycle is due now: auto-run on, a tradable session,
    and at least `auto_run_interval_min` since the last automatic cycle."""
    cfg = cfg or aj_config.get_config()
    if not cfg.get("auto_run_enabled"):
        return {"due": False, "reason": "auto_run disabled"}
    session = aj_db.market_session()
    wl = cfg.get("session_whitelist")
    if wl is None:
        wl = ["regular"]
    if session not in wl:
        return {"due": False, "reason": "session {} not tradable".format(session)}
    interval = max(1, int(cfg.get("auto_run_interval_min") or 30))
    mins = minutes_since_last_run()
    if mins is not None and mins < interval:
        return {"due": False, "reason": "only {:.0f}m since last run (interval {}m)".format(mins, interval),
                "minutes_since": round(mins, 1)}
    return {"due": True, "reason": "due", "minutes_since": mins, "session": session}


def run_scheduled(cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Run one cycle iff due. Stamps the last-run time on success. This is the
    function an external timer/cron ticks; it never raises."""
    cfg = cfg or aj_config.get_config()
    gate = due_to_run(cfg)
    if not gate.get("due"):
        return {"ran": False, "reason": gate.get("reason")}
    try:
        # health gate first — never auto-run into a known-bad state (22)
        if cfg.get("health_autohalt"):
            hc = health_check(cfg)
            if hc.get("halted"):
                return {"ran": False, "reason": "health auto-halt: " + hc.get("summary", "")}
        import aj_operator
        # run_once handles post-cycle autonomy (escalation/reflection/briefing).
        result = aj_operator.run_once("paper")
        aj_db.set_setting_raw(_LAST_RUN_KEY, aj_db.utc_now_iso())
        return {"ran": True, "cycle": result.get("cycle_id"),
                "proposals": len(result.get("proposals") or []), "result": result}
    except Exception as e:
        log.exception("run_scheduled failed")
        return {"ran": False, "reason": "error: {}".format(str(e)[:120])}


# ════════════════════════════════════════════════════════════════════════════
#  22 — SELF-DIAGNOSTIC HEALTH MONITOR (can pull the kill switch)
# ════════════════════════════════════════════════════════════════════════════

def health_check(cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Inspect the agent's own metrics for anomalies. When health_autohalt is on
    and a CRITICAL issue is found, engage the kill switch (risk-reducing)."""
    cfg = cfg or aj_config.get_config()
    issues: List[Dict[str, str]] = []
    try:
        import aj_metrics
        orders = aj_metrics.order_stats() or {}
        recon = aj_metrics.recon_stats() or {}
        # fill-rate collapse (only meaningful with a few submissions)
        fr = orders.get("fill_rate")
        if fr is not None and orders.get("total", 0) >= 6 and fr < 0.4:
            issues.append({"level": "critical", "msg": "fill-rate {:.0%} collapsed".format(fr)})
        # unknown-state orders — broker truth lost
        by_state = orders.get("by_state") or {}
        if int(by_state.get("unknown", 0)) > 0:
            issues.append({"level": "critical", "msg": "{} unknown-state orders".format(by_state["unknown"])})
        # reconciliation divergence — paper book vs broker disagree
        if int(recon.get("divergence", 0)) > 0:
            issues.append({"level": "critical", "msg": "{} unresolved reconciliation divergences".format(recon["divergence"])})
        # audit chain broken — tamper / corruption
        chain = aj_db.verify_audit_chain()
        if not chain.get("ok"):
            issues.append({"level": "critical", "msg": "audit chain broken at {}".format(chain.get("broken_at"))})
    except Exception:
        log.exception("health_check metrics failed")
        issues.append({"level": "warning", "msg": "health metrics unavailable"})

    critical = [i for i in issues if i["level"] == "critical"]
    summary = "; ".join(i["msg"] for i in issues) or "healthy"
    out = {"ok": not critical, "issues": issues, "summary": summary, "halted": False}
    if critical and cfg.get("health_autohalt"):
        try:
            import aj_risk
            if not aj_risk.is_halted():
                aj_risk.kill_switch("health auto-halt: " + summary[:160])
                out["halted"] = True
        except Exception:
            log.exception("health auto-halt kill_switch failed")
    return out


# ════════════════════════════════════════════════════════════════════════════
#  23 — PERFORMANCE-TRIGGERED PRESET ESCALATION
# ════════════════════════════════════════════════════════════════════════════

_PRESET_LADDER = ["conservative", "moderate", "aggressive"]


def _current_level() -> int:
    raw = aj_db.get_setting_raw(_ESCALATION_KEY)
    try:
        return max(0, min(2, int(float(raw)))) if raw is not None else 1
    except (TypeError, ValueError):
        return 1


def maybe_escalate(cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Move the risk preset up the ladder on proven performance, down on a
    drawdown. Earns its way up: positive Sharpe-like + win-rate ≥ 55% over
    enough trades escalates; a deep drawdown de-escalates. Never touches master
    switches or the allowlist (apply_preset only changes risk/strategy knobs)."""
    cfg = cfg or aj_config.get_config()
    if not cfg.get("auto_preset_escalation"):
        return {"changed": False, "reason": "disabled"}
    try:
        import aj_analytics, aj_alpha
        stats = aj_analytics.trade_stats() or {}
        sharpe = (aj_analytics.sharpe_like() or {}).get("sharpe")
        dd = aj_alpha.current_drawdown_pct()
        n = int(stats.get("trades") or 0)
        wr = stats.get("win_rate")
        level = _current_level()
        new_level = level
        reason = "hold"

        # de-escalate first (safety): a >15% drawdown steps down
        if dd >= 15.0 and level > 0:
            new_level = level - 1
            reason = "drawdown {:.0f}% — step down".format(dd)
        # escalate: proven edge and not in a drawdown
        elif (n >= 10 and wr is not None and wr >= 0.55
              and sharpe is not None and sharpe > 0.5 and dd < 5.0 and level < 2):
            new_level = level + 1
            reason = "win-rate {:.0%}, sharpe {:.1f} — step up".format(wr, sharpe)

        if new_level == level:
            return {"changed": False, "level": _PRESET_LADDER[level], "reason": reason,
                    "metrics": {"trades": n, "win_rate": wr, "sharpe": sharpe, "drawdown_pct": round(dd, 1)}}
        aj_config.apply_preset(_PRESET_LADDER[new_level])
        aj_db.set_setting_raw(_ESCALATION_KEY, str(new_level))
        aj_db.audit("preset_escalation",
                    {"from": _PRESET_LADDER[level], "to": _PRESET_LADDER[new_level], "reason": reason})
        return {"changed": True, "from": _PRESET_LADDER[level], "to": _PRESET_LADDER[new_level], "reason": reason}
    except Exception as e:
        log.exception("maybe_escalate failed")
        return {"changed": False, "reason": "error: {}".format(str(e)[:100])}


# ════════════════════════════════════════════════════════════════════════════
#  24 — DAILY REFLECTION JOURNAL
# ════════════════════════════════════════════════════════════════════════════

def build_reflection() -> Dict[str, Any]:
    """Compose (without persisting) the day's self-review: P&L, trade quality,
    best/worst names, drawdown, and a one-line takeaway."""
    import aj_analytics, aj_risk, aj_alpha
    stats = aj_analytics.trade_stats() or {}
    attrib = aj_analytics.attribution() or []
    day = aj_risk.compute_day_pnl() or {}
    best = attrib[0] if attrib else None
    worst = attrib[-1] if len(attrib) > 1 and attrib[-1].get("total", 0) < 0 else None
    dd = aj_alpha.current_drawdown_pct()
    wr = stats.get("win_rate")
    # deterministic takeaway (LLM optional below)
    if wr is None:
        takeaway = "No closed trades yet — accumulating positions; watch unrealized risk."
    elif wr >= 0.55 and day.get("day_pnl", 0) >= 0:
        takeaway = "Edge is working — win-rate {:.0%}. Let winners run, keep sizing disciplined.".format(wr)
    elif dd >= 10:
        takeaway = "In a {:.0f}% drawdown — tighten entries, shrink size, protect capital.".format(dd)
    else:
        takeaway = "Mixed day — win-rate {:.0%}. Review weakest names and entry timing.".format(wr or 0)

    refl = {
        "date": aj_db.utc_now().strftime("%Y-%m-%d"),
        "day_pnl_usd": day.get("day_pnl"),
        "trades_closed": stats.get("trades"),
        "win_rate": wr,
        "profit_factor": stats.get("profit_factor"),
        "drawdown_pct": round(dd, 1),
        "best_name": ({"symbol": best.get("symbol"), "total": best.get("total")} if best else None),
        "worst_name": ({"symbol": worst.get("symbol"), "total": worst.get("total")} if worst else None),
        "takeaway": takeaway,
    }
    # optional LLM polish — best-effort, never blocks
    try:
        cfg = aj_config.get_config()
        if cfg.get("use_llm_synthesis"):
            import aj_routing
            r = aj_routing.complete(
                "In one sentence, give a trading coach's takeaway. Day P&L ${:.0f}, "
                "win-rate {}, drawdown {:.0f}%.".format(
                    float(day.get("day_pnl") or 0),
                    "{:.0%}".format(wr) if wr is not None else "n/a", dd),
                role="judge", sensitivity="public", quality_floor=1.0)
            if r.get("ok") and isinstance(r.get("text"), str):
                refl["takeaway"] = r["text"].strip()[:300]
    except Exception:
        log.debug("reflection LLM polish skipped", exc_info=True)
    return refl


def write_reflection() -> Dict[str, Any]:
    """Build today's reflection and persist it (one per day, overwrites)."""
    refl = build_reflection()
    try:
        key = "{}_{}".format(_REFLECTION_KEY, refl["date"])
        aj_db.set_setting_raw(key, json.dumps(refl))
        aj_db.audit("reflection", {"date": refl["date"], "takeaway": refl["takeaway"]})
    except Exception:
        log.exception("write_reflection persist failed")
    return refl


def get_reflection(date: Optional[str] = None) -> Optional[Dict[str, Any]]:
    date = date or aj_db.utc_now().strftime("%Y-%m-%d")
    raw = aj_db.get_setting_raw("{}_{}".format(_REFLECTION_KEY, date))
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


# ════════════════════════════════════════════════════════════════════════════
#  25 — PRE-MARKET OPPORTUNITY BRIEFING
# ════════════════════════════════════════════════════════════════════════════

def build_briefing(cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Rank the candidate universe by setup score and surface the top ideas with
    a quick read on each — the pre-market 'what to watch today' list."""
    cfg = cfg or aj_config.get_config()
    try:
        import aj_operator, aj_alpha
        universe = aj_operator._scan_universe() or list(cfg.get("symbol_allowlist") or [])
        k = max(1, int(cfg.get("opportunity_radar_top_k") or 5))
        scored = sorted(((s, aj_alpha._radar_score(s)) for s in universe),
                        key=lambda t: -t[1])[:k]
        ideas = []
        for sym, score in scored:
            closes = aj_alpha._closes(sym, "1y")
            rs = aj_alpha._pct_return(closes, 20)
            r = aj_alpha._rsi(closes, 14)
            ideas.append({"symbol": sym, "score": round(score, 1),
                          "rs_20d_pct": round(rs, 1) if rs is not None else None,
                          "rsi": round(r, 0) if r is not None else None})
        briefing = {
            "date": aj_db.utc_now().strftime("%Y-%m-%d"),
            "session": aj_db.market_session(),
            "regime": aj_alpha.detect_regime(),
            "universe_size": len(universe),
            "ideas": ideas,
        }
        return briefing
    except Exception as e:
        log.exception("build_briefing failed")
        return {"date": aj_db.utc_now().strftime("%Y-%m-%d"), "ideas": [],
                "error": str(e)[:120]}


def write_briefing(cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    b = build_briefing(cfg)
    try:
        aj_db.set_setting_raw(_BRIEFING_KEY, json.dumps(b))
    except Exception:
        log.exception("write_briefing persist failed")
    return b


def get_briefing() -> Optional[Dict[str, Any]]:
    raw = aj_db.get_setting_raw(_BRIEFING_KEY)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None
