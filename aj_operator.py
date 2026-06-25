"""AJTA — operator spine (AJTA-SPEC-1.0 §19). The triggered, non-24/7 cycle.

run_once(mode):
    acquire single-instance lock (§19.2)
    open cycle (running)
    recover any crashed prior cycle (§20.1) — reconcile BEFORE proposing
    scan -> forecast -> judge -> red-team -> size -> propose
    for each proposal: risk_gate -> (paper auto if allowed; LIVE NEVER auto)
                       -> execute -> fills
    reconcile -> score due forecasts
    close cycle (completed); release lock; RETURN (caller exits)

Decisioning is rule-based by default (fail-closed, fully offline). The LLM
ModelRouter is an optional thesis enhancer; if it can't run, the cycle falls
back to the rule-based output and never proposes on degraded quality (§20.4).
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import aj_db
import aj_config
import aj_risk
import aj_execution
import aj_positions

log = logging.getLogger("augur.aj_operator")


# ── scan ──────────────────────────────────────────────────────────────────────

def _scan_universe() -> List[str]:
    """The candidate universe for the cycle.

    Default (fail-closed): exactly the allowlist — nothing else can pass the
    gate. Open-universe mode (`allow_any_symbol`): a broader, BOUNDED set the
    agent may choose from — allowlist ∪ watchlist ∪ equity holdings ∪ a
    best-effort idea-pool feed — capped at `scan_universe_max` so a cycle never
    forecasts an unbounded list. The risk gate still binds every pick."""
    cfg = aj_config.get_config()
    allow = list(cfg.get("symbol_allowlist") or [])
    if not cfg.get("allow_any_symbol"):
        return allow

    universe = set(s.upper() for s in allow)
    try:
        import database as db
        universe.update(str(w.get("symbol") or "").upper()
                        for w in (db.get_watchlist() or []))
        universe.update(str(h.get("symbol") or "").upper()
                        for h in (db.get_portfolio() or [])
                        if (h.get("asset_type") or "") != "crypto")
    except Exception:
        log.debug("open-universe: watchlist/portfolio unavailable", exc_info=True)
    # best-effort idea feed — genuinely "any" picks beyond what's tracked
    try:
        import idea_pool_warmer
        for p in (idea_pool_warmer.list_warmed_symbols() or [])[:30]:
            sym = p.get("symbol") if isinstance(p, dict) else p
            if sym:
                universe.add(str(sym).upper())
    except Exception:
        log.debug("open-universe: idea pool unavailable", exc_info=True)

    universe.discard("")
    cap = int(cfg.get("scan_universe_max") or 25)
    return sorted(universe)[:cap]


# ── forecast ──────────────────────────────────────────────────────────────────

def _forecast(symbol: str, horizon: int) -> Optional[Dict[str, Any]]:
    try:
        import forecast_ensemble
        fc = forecast_ensemble.ensemble_forecast(symbol, horizon)
        # log to the accountability ledger (fire-and-forget)
        try:
            import forecast_accountability
            forecast_accountability.log_ensemble(symbol, horizon, fc)
        except Exception:
            log.debug("log_ensemble failed for %s", symbol, exc_info=True)
        return fc
    except Exception:
        log.exception("forecast failed for %s", symbol)
        return None


# ── judge + red-team (rule-based, optional LLM thesis) ───────────────────────

def _judge(symbol: str, fc: Dict[str, Any], cfg: Dict[str, Any],
           held_qty: float) -> Optional[Dict[str, Any]]:
    """Decide buy/sell/none for a symbol from its ensemble forecast. Returns a
    decision dict {side, prob_up, edge, conviction, thesis} or None to skip.
    Red-team veto folded in: an edge below the floor, or a low-consensus split,
    yields None (no trade)."""
    ens = (fc or {}).get("ensemble") or {}
    prob = ens.get("prob_up")
    if not isinstance(prob, (int, float)):
        return None
    edge_pts = abs(float(ens.get("edge_pct_pts") or (prob - 0.5) * 100))
    conviction = str(ens.get("conviction") or "").lower()

    # red-team: minimum edge + not a low-conviction coin-flip
    if edge_pts < float(cfg.get("min_edge_pct_pts") or 0):
        return None
    if conviction in ("low", "none", ""):
        # a split book where calibration shrank the edge to noise
        if edge_pts < 2 * float(cfg.get("min_edge_pct_pts") or 0):
            return None

    side = None
    if prob >= float(cfg.get("buy_prob_threshold") or 0.55):
        side = "buy"
    elif prob <= float(cfg.get("sell_prob_threshold") or 0.45) and held_qty > 0:
        side = "sell"
    if side is None:
        return None

    thesis = "rule: prob_up={:.2f} edge={:.1f}pp conviction={} -> {}".format(
        float(prob), edge_pts, conviction or "n/a", side)
    # optional LLM synthesis (best-effort, never blocks the rule decision)
    if cfg.get("use_llm_synthesis"):
        try:
            import aj_routing
            r = aj_routing.complete(
                "One sentence: is a {} of {} justified given prob_up {:.2f}, "
                "edge {:.1f}pp? Be terse.".format(side, symbol, float(prob), edge_pts),
                role="judge", sensitivity="public", quality_floor=1.0)
            if r.get("ok") and r.get("text"):
                thesis = (thesis + " | " + r["text"].strip())[:480]
        except Exception:
            log.debug("llm synthesis failed; keeping rule thesis", exc_info=True)

    return {"side": side, "prob_up": float(prob), "edge_pts": edge_pts,
            "conviction": conviction, "thesis": thesis,
            "forecast_id": (fc or {}).get("forecast_id")}


# ── size ──────────────────────────────────────────────────────────────────────

def _size(symbol: str, side: str, cfg: Dict[str, Any], held_qty: float) -> Optional[Dict[str, Any]]:
    price = aj_risk._order_price(symbol, "market", None)
    if price is None or price <= 0:
        return None
    max_notional = aj_db.money(cfg.get("max_order_notional_usd") or 0)
    target = aj_db.money(cfg.get("order_notional_target_usd") or 0)
    if target <= 0:
        target = aj_db.money(max_notional * 0.5)   # default: half the per-order cap
    target = min(target, max_notional)
    if target <= 0:
        return None
    if side == "sell":
        # never sell more than we hold (paper book)
        qty = min(held_qty, target / price) if held_qty > 0 else 0
    else:
        qty = target / price
    if qty <= 0:
        return None
    return {"qty": qty, "price": price, "notional": aj_db.money(qty * price)}


# ── propose + gate + execute ──────────────────────────────────────────────────

def _propose_and_execute(cycle_id: str, symbol: str, decision: Dict[str, Any],
                         sizing: Dict[str, Any], cfg: Dict[str, Any]) -> Dict[str, Any]:
    pid = aj_db.insert(
        "aj_proposals", created_at=aj_db.utc_now_iso(), cycle_id=cycle_id,
        symbol=symbol, side=decision["side"], qty=sizing["qty"],
        notional_usd=sizing["notional"], order_type="market", limit_price=None,
        thesis=decision.get("thesis"), forecast_id=decision.get("forecast_id"),
        status="proposed")
    aj_db.audit("proposal", {"proposal_id": pid, "symbol": symbol,
                             "side": decision["side"], "qty": sizing["qty"],
                             "thesis": decision.get("thesis")},
                cycle_id=cycle_id, ref_id=pid)

    proposal = {"id": pid, "symbol": symbol, "side": decision["side"],
                "qty": sizing["qty"], "order_type": "market", "limit_price": None}
    rd = aj_risk.evaluate(proposal)
    if rd.get("decision") != "pass":
        aj_db.update("aj_proposals", pid,
                     status=("blocked" if rd["decision"] != "halt" else "blocked"),
                     risk_reason=rd.get("reason"))
        return {"proposal_id": pid, "result": rd.get("decision"),
                "reason": rd.get("reason")}

    # passed. paper MAY auto-approve; live NEVER auto (human-in-the-loop §19).
    mode = rd.get("mode") or "paper"
    if mode == "live" or not cfg.get("auto_approve_paper"):
        aj_db.update("aj_proposals", pid, status="approved",
                     risk_reason="awaiting human approval" if mode == "live" else None)
        aj_db.audit("approval", {"proposal_id": pid, "mode": mode,
                                 "required": True, "decision": "pending"},
                    cycle_id=cycle_id, ref_id=pid)
        return {"proposal_id": pid, "result": "approved_pending", "mode": mode}

    aj_db.audit("approval", {"proposal_id": pid, "mode": mode,
                             "required": False, "decision": "auto"},
                cycle_id=cycle_id, ref_id=pid)
    ex = aj_execution.execute_trade(proposal, rd, cycle_id=cycle_id)
    return {"proposal_id": pid, "result": "executed", "exec": ex}


# ── the cycle ─────────────────────────────────────────────────────────────────

def run_once(mode: str = "paper") -> Dict[str, Any]:
    """One triggered operator cycle. Returns a summary; the process exits after
    (the CLI wrapper handles exit). Safe to call from a scheduler N×/day."""
    aj_db.aj_init()
    lock = aj_db.SingleInstanceLock("operator")
    if not lock.acquire():
        log.warning("another operator cycle is running — exiting")
        return {"ok": False, "reason": "another cycle is running"}

    summary: Dict[str, Any] = {"ok": True, "mode": mode, "proposals": [],
                               "session": None}
    cycle_id = aj_db.open_cycle(mode)
    summary["cycle_id"] = cycle_id
    try:
        session = aj_db.market_session()
        summary["session"] = session
        aj_db.audit("connect", {"cycle_id": cycle_id, "mode": mode,
                                "session": session}, cycle_id=cycle_id)

        # §20.1 — recover crashed prior cycles BEFORE proposing anything.
        rec = aj_execution.recover_crashed_cycles(current_cycle=cycle_id)
        summary["recovery"] = rec

        cfg = aj_config.get_config()
        horizon = int(cfg.get("forecast_horizon_days") or 20)
        book = aj_positions.paper_book()
        held = {s: p["qty"] for s, p in book["positions"].items()}

        # scan -> forecast -> judge -> size -> propose -> gate -> execute
        for symbol in _scan_universe():
            try:
                fc = _forecast(symbol, horizon)
                if not fc or not fc.get("ensemble"):
                    summary["proposals"].append({"symbol": symbol, "result": "no_signal"})
                    continue
                decision = _judge(symbol, fc, cfg, held.get(symbol, 0.0))
                if not decision:
                    summary["proposals"].append({"symbol": symbol, "result": "no_edge"})
                    continue
                sizing = _size(symbol, decision["side"], cfg, held.get(symbol, 0.0))
                if not sizing:
                    summary["proposals"].append({"symbol": symbol, "result": "unsizable"})
                    continue
                out = _propose_and_execute(cycle_id, symbol, decision, sizing, cfg)
                out["symbol"] = symbol
                summary["proposals"].append(out)
            except Exception:
                log.exception("symbol %s failed in cycle", symbol)
                summary["proposals"].append({"symbol": symbol, "result": "error"})

        # reconcile (paper self-truth) + score due forecasts + close
        summary["reconcile"] = aj_execution.reconcile(venue=cfg.get("default_broker"),
                                                      cycle_id=cycle_id)["status"]
        try:
            import research_tracker
            summary["scored"] = research_tracker.score_due_forecasts()
        except Exception:
            log.debug("score_due_forecasts failed", exc_info=True)
            summary["scored"] = None

        aj_db.close_cycle(cycle_id, "completed")
        aj_db.audit("disconnect", {"cycle_id": cycle_id, "status": "completed"},
                    cycle_id=cycle_id)
        # Optional observability export (§21.1) — fail-open, never blocks.
        try:
            import aj_langfuse
            aj_langfuse.emit_cycle_trace(summary)
        except Exception:
            log.debug("langfuse emit skipped", exc_info=True)
        return summary
    except Exception as e:
        log.exception("run_once failed")
        aj_db.close_cycle(cycle_id, "crashed")
        summary["ok"] = False
        summary["error"] = str(e)
        return summary
    finally:
        lock.release()
