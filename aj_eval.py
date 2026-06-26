"""AJTA — routing eval harness (§23.5) + retention pruning (§8).

§23.5: replay logged `aj_routing` outcomes to compare models per task class so
underperformers can be demoted before an adoption decision. §8: trading records
(orders/fills/risk/audit) are retained indefinitely; `aj_routing` and verbose
model logs MAY be pruned after N days.

Also documents the §23.3 backtest-methodology guard: a backtest is research only
and MUST NOT, by itself, enable live trading.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import aj_db

log = logging.getLogger("augur.aj_eval")


def model_leaderboard(since: Optional[str] = None) -> List[Dict[str, Any]]:
    """Per-model routing performance from aj_routing telemetry."""
    where = "WHERE ts >= ?" if since else ""
    params = (since,) if since else ()
    rows = aj_db.query(
        "SELECT chosen_model, COUNT(*) AS calls, SUM(ok) AS oks, "
        "SUM(CASE WHEN ok IS NOT NULL THEN 1 END) AS ok_rows, "
        "SUM(escalated) AS escalations, SUM(fallback_used) AS fallbacks, "
        "AVG(latency_ms) AS avg_latency_ms, SUM(cost_usd) AS cost_usd "
        "FROM aj_routing {} GROUP BY chosen_model ORDER BY calls DESC".format(where),
        params)
    out = []
    for r in rows:
        calls = r["calls"] or 0
        # ok_rate is over rows that actually recorded ok (0/1); NULL-ok telemetry
        # is excluded from the denominator so it can't deflate the rate.
        ok_rows = r["ok_rows"] or 0
        out.append({
            "model": r["chosen_model"],
            "calls": calls,
            "ok_rate": round((r["oks"] or 0) / ok_rows, 3) if ok_rows else None,
            "escalation_rate": round((r["escalations"] or 0) / calls, 3) if calls else None,
            "fallback_rate": round((r["fallbacks"] or 0) / calls, 3) if calls else None,
            "avg_latency_ms": int(r["avg_latency_ms"] or 0),
            "cost_usd": aj_db.money(r["cost_usd"] or 0),
        })
    return out


def compare_by_role(since: Optional[str] = None) -> Dict[str, List[Dict[str, Any]]]:
    """For each role (task class), each model's ok-rate — the input to an A/B
    adoption decision."""
    where = "WHERE ts >= ?" if since else ""
    params = (since,) if since else ()
    rows = aj_db.query(
        "SELECT role, chosen_model, COUNT(*) AS calls, SUM(ok) AS oks, "
        "AVG(latency_ms) AS lat FROM aj_routing {} "
        "GROUP BY role, chosen_model".format(where), params)
    by_role: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        role = r["role"] or "(none)"
        calls = r["calls"] or 0
        by_role.setdefault(role, []).append({
            "model": r["chosen_model"], "calls": calls,
            "ok_rate": round((r["oks"] or 0) / calls, 3) if calls else None,
            "avg_latency_ms": int(r["lat"] or 0)})
    for role in by_role:
        by_role[role].sort(key=lambda x: (-(x["ok_rate"] or 0), x["avg_latency_ms"]))
    return by_role


def recommend_demotions(since: Optional[str] = None, min_calls: int = 20,
                        ok_floor: float = 0.6) -> List[Dict[str, Any]]:
    """Models with enough volume but an ok-rate below the floor — candidates to
    demote in the capability registry. Advisory only; never auto-applied."""
    out = []
    for m in model_leaderboard(since):
        ok = m["ok_rate"]
        # explicit None check — a 0.0 ok-rate is the WORST case, not "missing".
        if m["calls"] >= min_calls and ok is not None and ok < ok_floor:
            out.append({"model": m["model"], "ok_rate": m["ok_rate"],
                        "calls": m["calls"],
                        "reason": "ok_rate {} < {} over {} calls".format(
                            m["ok_rate"], ok_floor, m["calls"])})
    return out


# ── retention (§8) ────────────────────────────────────────────────────────────

# Tables that MUST be retained indefinitely (trading records). Never pruned.
_PROTECTED = ("aj_orders", "aj_fills", "aj_risk_events", "aj_audit",
              "aj_proposals")


def prune_routing(days: int = 30) -> Dict[str, Any]:
    """Delete aj_routing rows older than `days`. Trading records are untouched.
    Returns {pruned}. days<=0 is a no-op (keep everything)."""
    if days <= 0:
        return {"pruned": 0, "note": "retention disabled"}
    cutoff = (aj_db.utc_now() - _timedelta(days)).replace(microsecond=0).isoformat()
    import database as db
    with db._write_lock:
        conn = db.get_conn()
        try:
            cur = conn.execute("DELETE FROM aj_routing WHERE ts < ?", (cutoff,))
            conn.commit()
            n = cur.rowcount
        except Exception:
            conn.rollback()
            raise
    log.info("aj_eval.prune_routing: removed %s rows older than %s", n, cutoff)
    return {"pruned": n, "cutoff": cutoff}


def _timedelta(days: int):
    from datetime import timedelta
    return timedelta(days=days)


# ── §23.3 backtest methodology guard ─────────────────────────────────────────

BACKTEST_REQUIREMENTS = (
    "point-in-time data (no look-ahead / survivorship bias)",
    "model fees + slippage (§12.5)",
    "walk-forward / out-of-sample splits (never in-sample tuning reported as performance)",
    "report distribution and drawdown, not just mean return",
)


def backtest_caveat() -> Dict[str, Any]:
    """A backtest result MUST NOT, by itself, enable live trading (§23.3).
    Surfaced wherever a backtest is presented to the operator."""
    return {"requirements": list(BACKTEST_REQUIREMENTS),
            "enables_live": False,
            "note": "Backtests are research only. Live requires the §23.4 "
                    "paper-validation gate on REALIZED paper results."}
