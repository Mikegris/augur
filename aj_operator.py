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
    """The candidate universe for the cycle, per `universe_mode`:

      'market_screen' (DEFAULT) — sweep the FULL investable population (SEC
          equities + top crypto) via aj_universe, screened to a shortlist. No
          allowlist; portfolio is NOT a seed. Always also includes the operator's
          explicit allowlist picks.
      'open'      — allowlist ∪ watchlist ∪ portfolio ∪ idea-pool (legacy),
          now INCLUDING crypto holdings, capped at scan_universe_max.
      'allowlist' — exactly the allowlist (fail-closed).

    The risk gate still binds every pick (aj_config.is_open_universe agrees with
    this on whether off-allowlist buys are permitted)."""
    cfg = aj_config.get_config()
    mode = str(cfg.get("universe_mode") or "market_screen").lower()
    allow = [str(s).upper() for s in (cfg.get("symbol_allowlist") or [])]

    if mode == "market_screen":
        try:
            import aj_universe
            shortlist = aj_universe.screen(cfg)
            out = list(dict.fromkeys(allow + shortlist))   # allowlist first, deduped
            if out:
                return out
            log.warning("market_screen returned empty; falling back to allowlist")
        except Exception:
            log.exception("market_screen failed; falling back to allowlist")
        return allow

    # 'allowlist' mode (and the legacy fail-closed default) — allowlist only.
    if mode == "allowlist" and not cfg.get("allow_any_symbol"):
        return allow

    # 'open' mode (or legacy allow_any_symbol): allowlist ∪ watchlist ∪ portfolio
    # ∪ idea-pool. Crypto holdings are now INCLUDED (crypto trading enabled).
    universe = set(allow)
    try:
        import database as db
        universe.update(str(w.get("symbol") or "").upper()
                        for w in (db.get_watchlist() or []))
        universe.update(str(h.get("symbol") or "").upper()
                        for h in (db.get_portfolio() or []))
    except Exception:
        log.debug("open-universe: watchlist/portfolio unavailable", exc_info=True)
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
    # Use the calibrated edge when present; only fall back to raw (prob-0.5)
    # when it's genuinely ABSENT (None) — `or` wrongly treated a legitimately
    # zero calibrated edge as missing and substituted the raw value, letting a
    # deliberately-shrunk edge clear the red-team floor on raw prob alone.
    raw_edge = ens.get("edge_pct_pts")
    edge_pts = abs(float(raw_edge)) if raw_edge is not None else abs((prob - 0.5) * 100)
    conviction = str(ens.get("conviction") or "").lower()
    min_edge = float(cfg.get("min_edge_pct_pts", 0) or 0)

    # red-team: minimum edge + not a low-conviction coin-flip
    if edge_pts < min_edge:
        return None
    if conviction in ("low", "none", ""):
        # a split book where calibration shrank the edge to noise
        if edge_pts < 2 * min_edge:
            return None

    # Explicit defaults (not `or`) so a legitimately-configured threshold of 0
    # isn't silently overridden back to 0.55/0.45.
    buy_thr = float(cfg.get("buy_prob_threshold", 0.55))
    sell_thr = float(cfg.get("sell_prob_threshold", 0.45))
    side = None
    if prob >= buy_thr:
        side = "buy"
    elif prob <= sell_thr and held_qty > 0:
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


# ── analyst council (advisory) ────────────────────────────────────────────────

def _council_advise(symbol: str, decision: Dict[str, Any], cfg: Dict[str, Any],
                    cycle_id: str, budget: Dict[str, int]) -> Dict[str, Any]:
    """Consult the Analyst Council for a BUY candidate (merge plan Phase 3).

    HARD SAFETY CONTRACT — the council is ADVISORY:
      * Only ever consulted for a 'buy' decision the rule engine already made.
        Sells / exits are NEVER gated by the council (we never block a
        risk-reducing close).
      * It can VETO the buy or SHRINK its size. It can NEVER create a buy nor
        increase size beyond what the rule engine + caps allow (coequal sizing
        is gated to Phase 5 and still bounded by the risk gate).
      * On not-active / over-budget / error / skipped → PROCEED rule-based
        (fail-open to today's behavior). The fail-closed risk gate
        (aj_risk.evaluate) remains the sole execution authority either way.

    Returns {verdict:'proceed'|'veto', factor:float(0..1], council:dict|None,
    brief:str}.
    """
    proceed = {"verdict": "proceed", "factor": 1.0, "council": None, "brief": ""}
    if (decision or {}).get("side") != "buy":
        return proceed
    try:
        if not aj_config.council_active(cfg):
            return proceed
    except Exception:
        return proceed
    if budget.get("max", 0) <= 0:
        return {**proceed, "brief": "council topk=0 (disabled)"}
    if budget.get("n", 0) >= budget.get("max", 0):
        return {**proceed, "brief": "council topk reached"}
    budget["n"] = budget.get("n", 0) + 1
    try:
        import aj_council
        from aj_schemas import Action
        dec = aj_council.run(symbol, cfg=cfg, cycle_id=cycle_id)
    except Exception:
        log.exception("council advise failed; proceeding rule-based")
        return {**proceed, "brief": "council error"}
    if dec.status in ("skipped", "error"):
        return {**proceed, "brief": "council " + dec.status}

    brief = "{}/{} conv={:.0%}".format(dec.rating.value, dec.action.value, dec.conviction)
    audit = dec.to_audit()
    policy = str(cfg.get("council_policy", "advisory")).lower()

    # confirm: an entry REQUIRES the council to also say BUY.
    if policy == "confirm":
        if dec.action is Action.BUY:
            return {"verdict": "proceed", "factor": 1.0, "council": audit, "brief": brief}
        return {"verdict": "veto", "factor": 0.0, "council": audit, "brief": brief}

    # advisory (default) and locked-coequal: veto a non-BUY council call; on
    # agreement, SHRINK size on low conviction (never grow). Only equal-or-more
    # conservative.
    if dec.action in (Action.SELL, Action.HOLD):
        return {"verdict": "veto", "factor": 0.0, "council": audit, "brief": brief}
    factor = max(0.5, min(1.0, 0.5 + 0.5 * float(dec.conviction or 0.0)))

    # coequal (Phase 5): once the realized track record unlocks it, a high-
    # conviction agreement may BOOST size up to coequal_max_boost — but ONLY
    # within the risk gate's notional cap (the run loop clips to the cap, never
    # bypasses it). Locked coequal behaves exactly like advisory above.
    if policy == "coequal":
        try:
            import aj_council as _c
            if _c.coequal_unlocked(cfg):
                max_boost = max(0.0, float(cfg.get("coequal_max_boost", 0.5) or 0.0))
                factor = 1.0 + max_boost * float(dec.conviction or 0.0)
        except Exception:
            pass
    return {"verdict": "proceed", "factor": factor, "council": audit, "brief": brief}


# ── size ──────────────────────────────────────────────────────────────────────

def _size(symbol: str, side: str, cfg: Dict[str, Any], held_qty: float,
          decision: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    # Conviction sizing, min-notional floor, and limit-vs-market entry all live
    # in aj_strategy (enhancements ①②③).
    import aj_strategy
    return aj_strategy.size_order(symbol, side, cfg, held_qty, decision)


# ── propose + gate + execute ──────────────────────────────────────────────────

def _propose_and_execute(cycle_id: str, symbol: str, decision: Dict[str, Any],
                         sizing: Dict[str, Any], cfg: Dict[str, Any],
                         instrument: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    order_type = sizing.get("order_type") or "market"
    limit_price = sizing.get("limit_price")
    # Normalize the sizing dict defensively: a sizer that omits 'notional'
    # (returning only 'qty') must not raise KeyError mid-insert and lose the
    # real cause behind a generic per-symbol failure.
    qty = sizing.get("qty") or 0.0
    notional = sizing.get("notional")
    if notional is None:
        notional = aj_db.money(qty * float(sizing.get("price") or 0))
    ins = dict(created_at=aj_db.utc_now_iso(), cycle_id=cycle_id,
               symbol=symbol, side=decision["side"], qty=qty,
               notional_usd=notional, order_type=order_type, limit_price=limit_price,
               thesis=decision.get("thesis"), forecast_id=decision.get("forecast_id"),
               status="proposed")
    if instrument:    # options carry extra columns (v4 schema)
        ins.update(instrument_type=instrument.get("instrument_type", "option"),
                   underlying=instrument.get("underlying"),
                   option_type=instrument.get("option_type"),
                   strike=instrument.get("strike"), expiry=instrument.get("expiry"),
                   contract_multiplier=instrument.get("contract_multiplier", 100))
    pid = aj_db.insert("aj_proposals", **ins)
    aj_db.audit("proposal", {"proposal_id": pid, "symbol": symbol,
                             "side": decision["side"], "qty": qty,
                             "instrument": (instrument or {}).get("instrument_type", "stock"),
                             "thesis": decision.get("thesis")},
                cycle_id=cycle_id, ref_id=pid)

    proposal = {"id": pid, "symbol": symbol, "side": decision["side"],
                "qty": qty, "order_type": order_type, "limit_price": limit_price}
    if instrument:
        proposal["instrument_type"] = instrument.get("instrument_type", "option")
        proposal["asset_type"] = "option"
    rd = aj_risk.evaluate(proposal)
    if rd.get("decision") != "pass":
        # The proposal CHECK constraint allows only proposed/blocked/approved/
        # rejected/executed/expired, so both block and halt map to 'blocked';
        # the distinction (and the halt) is carried in risk_reason + result.
        reason = rd.get("reason")
        if rd.get("decision") == "halt":
            reason = "HALTED: " + str(reason or "daily-loss breach")
        aj_db.update("aj_proposals", pid, status="blocked", risk_reason=reason)
        return {"proposal_id": pid, "result": rd.get("decision"), "reason": reason}

    # passed. paper MAY auto-approve; live NEVER auto (human-in-the-loop §19).
    mode = rd.get("mode") or "paper"
    if mode == "live" or not cfg.get("auto_approve_paper"):
        aj_db.update("aj_proposals", pid, status="approved",
                     risk_reason="awaiting human approval" if mode == "live" else None)
        aj_db.audit("approval", {"proposal_id": pid, "mode": mode,
                                 "required": True, "decision": "pending"},
                    cycle_id=cycle_id, ref_id=pid)
        return {"proposal_id": pid, "result": "approved_pending", "mode": mode}

    # ⑮ dry-run: proposal passed the gate but we never execute (preview mode).
    if cfg.get("dry_run"):
        aj_db.update("aj_proposals", pid, status="approved", risk_reason="dry-run (not executed)")
        aj_db.audit("approval", {"proposal_id": pid, "mode": mode, "dry_run": True},
                    cycle_id=cycle_id, ref_id=pid)
        return {"proposal_id": pid, "result": "dry_run", "would_execute": True}

    aj_db.audit("approval", {"proposal_id": pid, "mode": mode,
                             "required": False, "decision": "auto"},
                cycle_id=cycle_id, ref_id=pid)
    ex = aj_execution.execute_trade(proposal, rd, cycle_id=cycle_id)
    # ㉕ fill notification (best-effort)
    if ex.get("filled_qty"):
        try:
            import aj_analytics
            aj_analytics.notify_fill(symbol, decision["side"], ex["filled_qty"],
                                     ex.get("avg_fill_price") or sizing.get("price"))
        except Exception:
            pass
    return {"proposal_id": pid, "result": "executed", "exec": ex}


def _process_exits(cycle_id: str, cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Execute take-profit / stop-loss / trailing-stop sells (⑤⑥⑦) before the
    scan. Each exit sell still passes the risk gate (held-sells are permitted
    past the allowlist)."""
    import aj_rules
    # snapshot avg costs before the sells so realized P&L can be scored (19)
    pre_book = aj_positions.paper_book().get("positions") or {} if cfg.get("signal_scorecard") else {}
    out: List[Dict[str, Any]] = []
    for sig in aj_rules.exit_signals(cfg):
        decision = {"side": "sell", "thesis": "exit: " + sig["reason"], "edge_pts": 0.0}
        mark = float(sig.get("mark") or 0)
        qty = float(sig.get("qty") or 0)
        if mark <= 0 or qty <= 0:
            continue
        sizing = {"qty": qty, "price": mark, "notional": aj_db.money(qty * mark),
                  "order_type": "market", "limit_price": None}
        r = _propose_and_execute(cycle_id, sig["symbol"], decision, sizing, cfg)
        r["symbol"] = sig["symbol"]
        r["exit_reason"] = sig["reason"]
        # 19: score the realized close under its entry conviction
        if cfg.get("signal_scorecard") and r.get("result") == "executed":
            try:
                import aj_alpha
                avg = float((pre_book.get(sig["symbol"]) or {}).get("avg_cost") or 0)
                realized = (mark - avg) * qty if avg > 0 else 0.0
                aj_alpha.scorecard_record(aj_alpha.pop_entry_conviction(sig["symbol"]), realized)
            except Exception:
                log.debug("scorecard exit record skipped", exc_info=True)
        out.append(r)
    return out


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
        # 100x adaptive brain: tune thresholds to recent hit-rate (16) + the
        # detected market regime (17). No-op when both flags are off.
        try:
            import aj_alpha
            cfg = aj_alpha.effective_config(cfg)
        except Exception:
            log.debug("effective_config skipped", exc_info=True)
        horizon = int(cfg.get("forecast_horizon_days") or 20)

        # Enhancement housekeeping BEFORE proposing:
        try:
            import aj_rules
            aj_rules.update_position_state()            # peak/last marks, aging
            summary["expired_orders"] = aj_rules.expire_stale_orders(cfg)  # ④ TTL
            # ⑤⑥⑦ exit rules — close paper longs hitting TP/SL/trailing first
            summary["exits"] = _process_exits(cycle_id, cfg)
        except Exception:
            log.exception("enhancement housekeeping failed (non-fatal)")
            summary["exits"] = []
        # don't re-open a name we just exited in the same cycle (avoid churn)
        _exited = {e.get("symbol") for e in (summary.get("exits") or [])
                   if e.get("result") == "executed"}

        book = aj_positions.paper_book()
        held = {s: p.get("qty", 0.0) for s, p in (book.get("positions") or {}).items()}

        # scan -> forecast -> judge -> size -> propose -> gate -> execute.
        # 100x opportunity radar (20): rank the universe and trade only the
        # top-K best setups. No-op (full universe) when the flag is off.
        scan = _scan_universe()
        try:
            import aj_alpha
            scan = aj_alpha.rank_universe(scan, cfg)
        except Exception:
            log.debug("rank_universe skipped", exc_info=True)
        # Analyst-council advisory budget (Phase 3): consult the council for at
        # most council_topk BUY candidates per cycle (cost bound). No-op unless
        # council_active() (council_enabled + VERIFY-COUNCIL).
        council_budget = {"n": 0, "max": int(cfg.get("council_topk", 3) or 0)}
        for symbol in scan:
            try:
                if symbol in _exited:
                    summary["proposals"].append({"symbol": symbol, "result": "just_exited"})
                    continue
                # ⑧ skip a symbol in re-entry cooldown after a recent exit
                try:
                    import aj_rules as _r
                    if _r.in_cooldown(symbol, cfg):
                        summary["proposals"].append({"symbol": symbol, "result": "cooldown"})
                        continue
                except Exception:
                    pass
                fc = _forecast(symbol, horizon)
                if not fc or not fc.get("ensemble"):
                    summary["proposals"].append({"symbol": symbol, "result": "no_signal"})
                    continue
                decision = _judge(symbol, fc, cfg, held.get(symbol, 0.0))
                if not decision:
                    summary["proposals"].append({"symbol": symbol, "result": "no_edge"})
                    continue
                # Analyst-council advisory pass (BUY candidates only). Can veto
                # or shrink; never creates/boosts; never gates a sell.
                advise = _council_advise(symbol, decision, cfg, cycle_id, council_budget)
                if advise["verdict"] == "veto":
                    aj_db.audit("council_veto", {"symbol": symbol,
                                                 "council": advise.get("council")},
                                cycle_id=cycle_id)
                    summary["proposals"].append({"symbol": symbol,
                                                 "result": "council_veto",
                                                 "council": advise["brief"]})
                    continue
                if advise.get("council"):
                    decision = dict(decision)
                    decision["thesis"] = (str(decision.get("thesis", "")) +
                                          " | council:" + advise["brief"])[:480]
                sizing = _size(symbol, decision["side"], cfg, held.get(symbol, 0.0), decision)
                if not sizing:
                    summary["proposals"].append({"symbol": symbol, "result": "unsizable"})
                    continue
                # council size adjustment. factor<1 shrinks (advisory); factor>1
                # boosts (unlocked coequal) but is CLIPPED to the operator's
                # per-order notional cap — never exceeding the risk gate's limit.
                factor = float(advise.get("factor", 1.0) or 1.0)
                if factor > 0.0 and factor != 1.0:
                    sizing = dict(sizing)
                    price = float(sizing.get("price") or 0)
                    new_qty = (sizing.get("qty") or 0.0) * factor
                    if factor > 1.0 and price > 0:
                        cap = float(cfg.get("max_order_notional_usd", 0) or 0)
                        if cap > 0 and new_qty * price > cap:
                            new_qty = cap / price          # clip to the cap
                    sizing["qty"] = new_qty
                    sizing["notional"] = aj_db.money(new_qty * price)
                # Options sleeve: a BUY signal becomes a long CALL on the
                # underlying, sized by premium from the (conviction+council-
                # adjusted) target notional. Falls back to the equity buy when
                # options are off / no chain / can't afford one contract.
                p_symbol, p_decision, p_sizing, instrument = symbol, decision, sizing, None
                if cfg.get("trade_options") and decision.get("side") == "buy" \
                        and not aj_positions.infer_asset_type(symbol) == "crypto":
                    try:
                        import aj_options
                        opt = aj_options.pick_contract(symbol, "buy", cfg)
                        prem = float((opt or {}).get("premium_contract") or 0)
                        target = float(sizing.get("notional") or 0)
                        n_ct = int(target // prem) if prem > 0 else 0
                        if opt and n_ct >= 1:
                            p_symbol = opt["symbol"]
                            p_sizing = {"qty": n_ct, "price": prem,
                                        "notional": aj_db.money(n_ct * prem),
                                        "order_type": "limit", "limit_price": prem}
                            p_decision = dict(decision)
                            p_decision["thesis"] = (str(decision.get("thesis", "")) +
                                " | opt {} {:.0f}C {} x{} @${:.0f}".format(
                                    opt["underlying"], opt["strike"], opt["expiry"],
                                    n_ct, prem))[:480]
                            instrument = {"instrument_type": "option",
                                          "underlying": opt["underlying"],
                                          "option_type": opt["option_type"],
                                          "strike": opt["strike"], "expiry": opt["expiry"],
                                          "contract_multiplier": opt["contract_multiplier"]}
                    except Exception:
                        log.exception("option routing failed; equity fallback")
                out = _propose_and_execute(cycle_id, p_symbol, p_decision, p_sizing,
                                           cfg, instrument=instrument)
                out["symbol"] = p_symbol
                # 19: remember the conviction a long was opened on, to score the
                # eventual close under the right bucket.
                if cfg.get("signal_scorecard") and out.get("result") == "executed" \
                        and decision["side"] == "buy":
                    try:
                        import aj_alpha
                        aj_alpha.note_entry_conviction(symbol, decision.get("conviction"))
                    except Exception:
                        log.debug("scorecard note skipped", exc_info=True)
                summary["proposals"].append(out)
            except Exception:
                log.exception("symbol %s failed in cycle", symbol)
                summary["proposals"].append({"symbol": symbol, "result": "error"})

        # reconcile (paper self-truth) + score due forecasts + close.
        # Non-fatal: a reconcile failure (e.g. an unverified live venue raising)
        # must not mark an otherwise-successful paper cycle as crashed.
        try:
            summary["reconcile"] = aj_execution.reconcile(
                venue=cfg.get("default_broker"), cycle_id=cycle_id)["status"]
        except Exception:
            log.exception("reconcile failed (non-fatal)")
            summary["reconcile"] = "error"
        try:
            import research_tracker
            summary["scored"] = research_tracker.score_due_forecasts()
        except Exception:
            log.debug("score_due_forecasts failed", exc_info=True)
            summary["scored"] = None

        # Enhancement analytics: snapshot equity (⑯), refresh position state,
        # and log the cycle (㉒) — all best-effort, never fatal.
        try:
            import aj_rules, aj_analytics
            aj_rules.update_position_state()
            summary["equity"] = aj_analytics.snapshot_equity()
            aj_analytics.log_cycle(summary)
        except Exception:
            log.debug("cycle analytics failed", exc_info=True)

        # 100x autonomy (opt-in, fail-safe): performance-triggered preset
        # escalation (23), end-of-day reflection (24), pre-market briefing (25).
        try:
            import aj_autonomy
            if cfg.get("auto_preset_escalation"):
                summary["escalation"] = aj_autonomy.maybe_escalate(cfg)
            if cfg.get("daily_reflection"):
                summary["reflection"] = aj_autonomy.write_reflection()
            if cfg.get("premarket_briefing") and summary.get("session") == "premarket":
                summary["briefing"] = aj_autonomy.write_briefing(cfg)
        except Exception:
            log.debug("post-cycle autonomy skipped", exc_info=True)

        # Council reflection loop (Phase 4): alpha-aware lessons on closed
        # council round-trips, fed back into future council prompts. Gated by
        # daily_reflection (opt-in), bounded, dedup'd; never breaks the cycle.
        try:
            import aj_council
            summary["council_reflections"] = aj_council.reflect_due(cfg)
        except Exception:
            log.debug("council reflect_due skipped", exc_info=True)

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
