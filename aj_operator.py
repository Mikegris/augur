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
    # Priority-first capping: a plain alphabetical slice deterministically
    # blind-spots late-alphabet names FOREVER (their forecast-driven sells are
    # never evaluated). Allowlist picks and currently-held paper-book names
    # survive the cap first; the remainder fills alphabetically.
    held_syms: List[str] = []
    try:
        import aj_positions
        held_syms = sorted(
            s for s, p in (aj_positions.paper_book().get("positions") or {}).items()
            if float(p.get("qty") or 0) != 0 and not str(s).startswith("OPT:"))
    except Exception:
        log.debug("open-universe: paper book unavailable", exc_info=True)
    prio = list(dict.fromkeys([s for s in allow if s in universe] + held_syms))
    rest = sorted(universe - set(prio))
    return (prio + rest)[:cap]


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

    # advisory (default) and locked-coequal: the council TRIMS or BLOCKS, never
    # grows. Veto only an ACTIVELY-BEARISH call (UNDERWEIGHT/SELL both map to
    # Action.SELL). A neutral HOLD is "no strong view", NOT "don't buy" — proceed
    # at a reduced size (council_hold_size_factor) so the council sizes-down on
    # uncertainty instead of silently vetoing the whole strategy. Set the factor
    # to 0 to restore the legacy strict behavior (HOLD == veto).
    if dec.action is Action.SELL:
        return {"verdict": "veto", "factor": 0.0, "council": audit, "brief": brief}
    if dec.action is Action.HOLD:
        hf = max(0.0, min(1.0, float(cfg.get("council_hold_size_factor", 0.5) or 0.0)))
        if hf <= 0.0:
            return {"verdict": "veto", "factor": 0.0, "council": audit, "brief": brief}
        return {"verdict": "proceed", "factor": hf, "council": audit,
                "brief": brief + " (hold->trim)"}
    # bullish (BUY) agreement: shrink on low conviction (never grow here).
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


def _option_order(opt: Dict[str, Any], target_notional: float,
                  base_thesis: str, cap_notional: float = 0.0) -> Optional[Dict[str, Any]]:
    """Build {symbol, sizing, decision, instrument} for a long option from a
    target notional (premium-sized, whole contracts). None if unaffordable.

    When the target can't cover a single contract but `cap_notional` (the hard
    per-order cap) can, size UP to one contract so the sleeve engages instead of
    silently falling through — the risk gate still validates the result."""
    prem = float((opt or {}).get("premium_contract") or 0)
    n_ct = int(target_notional // prem) if prem > 0 else 0
    if opt and n_ct < 1 and prem > 0 and 0 < prem <= float(cap_notional or 0):
        n_ct = 1
    if not opt or n_ct < 1:
        return None
    t = opt["option_type"][0].upper()
    thesis = (str(base_thesis) + " | opt {} {:.0f}{} {} x{} @${:.0f}".format(
        opt["underlying"], opt["strike"], t, opt["expiry"], n_ct, prem))[:480]
    return {
        "symbol": opt["symbol"],
        "sizing": {"qty": n_ct, "price": prem, "notional": aj_db.money(n_ct * prem),
                   "order_type": "limit", "limit_price": prem},
        "decision": {"side": "buy", "thesis": thesis},
        "instrument": {"instrument_type": "option", "underlying": opt["underlying"],
                       "option_type": opt["option_type"], "strike": opt["strike"],
                       "expiry": opt["expiry"],
                       "contract_multiplier": opt["contract_multiplier"]},
    }


def _bearish_put_entry(cycle_id: str, symbol: str, fc: Dict[str, Any],
                       cfg: Dict[str, Any], held_qty: float) -> Optional[Dict[str, Any]]:
    """A bearish forecast on a not-held name becomes a long PUT (the agent's only
    bearish play — it never shorts). Returns the propose/execute result or None.
    Bypasses the council (its bull/bear framing doesn't map to put-buying)."""
    if not (cfg.get("trade_options") and cfg.get("option_trade_puts")):
        return None
    if aj_positions.infer_asset_type(symbol) == "crypto" or held_qty > 0:
        return None
    ens = (fc or {}).get("ensemble") or {}
    prob = ens.get("prob_up")
    if not isinstance(prob, (int, float)):
        return None
    sell_thr = float(cfg.get("sell_prob_threshold", 0.45))
    min_edge = float(cfg.get("min_edge_pct_pts", 0) or 0)
    raw_edge = ens.get("edge_pct_pts")
    edge_pts = abs(float(raw_edge)) if raw_edge is not None else abs((prob - 0.5) * 100)
    if prob > sell_thr or edge_pts < min_edge:
        return None
    try:
        import aj_options
        opt = aj_options.pick_contract(symbol, "buy", cfg, direction="bearish")
    except Exception:
        log.debug("bearish put pick failed for %s", symbol, exc_info=True)
        return None
    if not opt:
        return None
    max_notional = aj_db.money(cfg.get("max_order_notional_usd") or 0)
    target = aj_db.money(cfg.get("order_notional_target_usd") or 0) or aj_db.money(max_notional * 0.5)
    target = min(target, max_notional)
    oo = _option_order(opt, target, "rule: bearish prob_up={:.2f} -> long put".format(float(prob)),
                       cap_notional=max_notional)
    if not oo:
        return None
    out = _propose_and_execute(cycle_id, oo["symbol"], oo["decision"], oo["sizing"],
                               cfg, instrument=oo["instrument"])
    out["symbol"] = oo["symbol"]
    out["underlying"] = symbol
    return out


def _close_expired_options(cycle_id: str, cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Administratively close any held option whose expiry has passed: book a
    SELL fill at the expired mark (0 = worthless) so the FIFO book realizes the
    loss and the position is removed. Bypasses the broker marketability check
    (a $0 quote would otherwise reject)."""
    out: List[Dict[str, Any]] = []
    try:
        import aj_options
        book = aj_positions.paper_book().get("positions") or {}
        for sym, p in list(book.items()):
            if not (isinstance(sym, str) and sym.startswith("OPT:")):
                continue
            qty = float(p.get("qty") or 0)
            if qty <= 0:
                continue
            meta = aj_options.parse_symbol(sym)
            if not meta:
                continue
            dte = aj_options._days_to(meta["expiry"])
            if dte is None or dte >= 0:
                continue   # not expired
            # Book the close at INTRINSIC value, not a hardcoded 0: an ITM
            # contract is worth (spot - strike) x multiplier at expiry, and
            # booking it worthless realizes a fake 100% loss that corrupts
            # equity/drawdown stats and the trade labels the ML trains on.
            close_px = 0.0
            try:
                import aj_risk
                und = meta["underlying"]
                spot = aj_risk._marks([und]).get(und)
                if spot is not None and float(spot) > 0:
                    mult = float(meta.get("contract_multiplier")
                                 or aj_options._config_multiplier())
                    strike = float(meta["strike"])
                    if str(meta["option_type"]).lower().startswith("c"):
                        intrinsic = max(0.0, float(spot) - strike)
                    else:
                        intrinsic = max(0.0, strike - float(spot))
                    close_px = round(intrinsic * mult, 2)
            except Exception:
                log.debug("intrinsic pricing failed for %s; booking 0", sym,
                          exc_info=True)
            try:
                oid = aj_db.insert(
                    "aj_orders", proposal_id=0,
                    client_order_id="ajexp-{}-{}".format(sym, cycle_id),
                    broker="paper", mode="paper", symbol=sym, side="sell", qty=qty,
                    order_type="market", state="filled", filled_qty=qty,
                    avg_fill_price=close_px, fees_usd=0.0, created_at=aj_db.utc_now_iso(),
                    terminal_at=aj_db.utc_now_iso(), instrument_type="option",
                    underlying=meta["underlying"], option_type=meta["option_type"],
                    strike=meta["strike"], expiry=meta["expiry"])
                aj_db.insert("aj_fills", order_id=oid, qty=qty, price=close_px,
                             fees_usd=0.0, filled_at=aj_db.utc_now_iso())
                aj_db.audit("option_expiry_close",
                            {"symbol": sym, "qty": qty, "price": close_px},
                            cycle_id=cycle_id)
                out.append({"symbol": sym, "result": "expired_closed", "qty": qty})
            except Exception:
                log.exception("expiry close failed for %s", sym)
    except Exception:
        log.exception("close_expired_options failed (non-fatal)")
    return out


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
                # Net of the exit fill's fees (the booked realized P&L), so the
                # scorecard scores what was actually banked — not a gross
                # (mark-avg)×qty figure that overstates a marginal/round-trip-
                # fee-eaten close as a winner.
                ex = r.get("exec") or {}
                ex_qty = float(ex.get("filled_qty") or qty)
                ex_px = float(ex.get("avg_fill_price") or mark)
                fees = float(ex.get("fees_usd") or 0)
                realized = ((ex_px - avg) * ex_qty - fees) if avg > 0 else 0.0
                aj_alpha.scorecard_record(aj_alpha.pop_entry_conviction(sig["symbol"]), realized)
            except Exception:
                log.debug("scorecard exit record skipped", exc_info=True)
        out.append(r)

    # ── Batch 3 — supplementary exits: time-stop + profit-ladder (opt-in) ────
    # time-stop closes a stagnant thesis; the ladder scales out at gain rungs. A
    # per-symbol high-water mark stops a rung re-trimming every cycle; the mark
    # is cleared on a full exit so a re-opened position starts fresh. Fail-open.
    if cfg.get("time_stop_days", 0) or cfg.get("profit_ladder"):
        try:
            import aj_execution_alpha as _ea
            import aj_risk
            book = aj_positions.paper_book().get("positions") or {}
            exited = {r.get("symbol") for r in out if r.get("result") == "executed"
                      and float(r.get("exec", {}).get("qty", 0) or 0) >= 0}
            done_syms = {r.get("symbol") for r in out}   # any sell processed this cycle
            marks = aj_risk._marks([s for s in book if s not in done_syms])
            # Entry timestamps live in aj_position_state (stamped by
            # aj_rules.update_position_state), NOT in the paper book — without
            # this lookup pos.get("opened_at") is always None and the time-stop
            # silently never fires.
            opened_at_by_sym: Dict[str, Any] = {}
            try:
                opened_at_by_sym = {r["symbol"]: r.get("opened_at") for r in
                                    aj_db.query("SELECT symbol, opened_at FROM aj_position_state")}
            except Exception:
                log.debug("position-state opened_at lookup failed", exc_info=True)
            for sym, pos in list(book.items()):
                if sym in done_syms:
                    continue
                mk = marks.get(sym)
                qty = float(pos.get("qty") or 0)
                if mk is None or qty <= 0:
                    continue
                position = dict(pos)
                position["symbol"] = sym
                lkey = "__aj_ladder_" + sym
                # time-stop -> full exit
                ts = _ea.time_stop(position, cfg, mark=mk,
                                   opened_at=pos.get("opened_at") or opened_at_by_sym.get(sym))
                if ts.get("exit"):
                    dec = {"side": "sell", "thesis": "exit: " + str(ts.get("reason") or "time-stop"),
                           "edge_pts": 0.0}
                    sz = {"qty": qty, "price": mk, "notional": aj_db.money(qty * mk),
                          "order_type": "market", "limit_price": None}
                    r = _propose_and_execute(cycle_id, sym, dec, sz, cfg)
                    r["symbol"] = sym
                    r["exit_reason"] = "time_stop"
                    out.append(r)
                    # Clear the ladder mark only when the sell actually
                    # EXECUTED — clearing on a gate-blocked sell would let
                    # already-taken rungs re-trigger next cycle and double-trim.
                    if r.get("result") == "executed":
                        try:
                            aj_db.set_setting_raw(lkey, "")    # clear ladder on full exit
                        except Exception:
                            pass
                    continue
                # profit ladder -> trim only rungs ABOVE the high-water mark
                rungs = [x for x in _ea.profit_ladder(position, cfg, mark=mk) if x.get("triggered")]
                if rungs:
                    # Atomically read the high-water mark, pick the next fresh
                    # rung, and ADVANCE the mark under the single write lock with
                    # a compare-and-set. Two exit passes (or a re-entrant cycle)
                    # racing the old read-modify-write would each see the same
                    # stale mark and double-trim/double-sell the same rung; the
                    # CAS makes exactly one pass win and advance the mark.
                    import database as _db
                    rung = None
                    new_mark = None
                    prev_raw = ""
                    try:
                        with _db._write_lock:
                            conn = _db.get_conn()
                            row = conn.execute(
                                "SELECT value FROM settings WHERE key=?", (lkey,)).fetchone()
                            prev_raw = "" if not row else (row["value"] or "")
                            try:
                                done = float((row["value"] if row else 0) or 0)
                            except (TypeError, ValueError):
                                done = 0.0
                            fresh = [x for x in rungs if float(x.get("gain_pct") or 0) > done]
                            if fresh:
                                rung = fresh[-1]
                                new_mark = float(rung.get("gain_pct") or 0)
                                # CAS: only advance if the mark is still `done`.
                                cur = conn.execute(
                                    "UPDATE settings SET value=? WHERE key=? AND COALESCE(value,'')=?",
                                    (str(new_mark), lkey, "" if not row else (row["value"] or ""))
                                )
                                if cur.rowcount != 1:
                                    # No existing row (or it changed under us).
                                    ins = conn.execute(
                                        "INSERT OR IGNORE INTO settings (key, value) VALUES (?,?)",
                                        (lkey, str(new_mark)))
                                    if ins.rowcount != 1:
                                        rung = None   # lost the race; another pass took this rung
                                conn.commit()
                        try:
                            _db._invalidate_settings_cache()
                        except Exception:
                            pass
                    except Exception:
                        log.debug("profit-ladder CAS failed", exc_info=True)
                        rung = None
                    if rung is not None:
                        trim = max(0.0, min(1.0, float(rung.get("trim_frac") or 0)))
                        tqty = qty * trim
                        if tqty > 0:
                            dec = {"side": "sell", "edge_pts": 0.0,
                                   "thesis": "exit: profit-ladder {}%".format(rung.get("gain_pct"))}
                            sz = {"qty": tqty, "price": mk, "notional": aj_db.money(tqty * mk),
                                  "order_type": "market", "limit_price": None}
                            r = _propose_and_execute(cycle_id, sym, dec, sz, cfg)
                            r["symbol"] = sym
                            r["exit_reason"] = "profit_ladder"
                            out.append(r)
                            if r.get("result") != "executed" and new_mark is not None:
                                # The trim was blocked (halt/session/gate): roll
                                # the high-water mark back with a CAS so the rung
                                # isn't permanently consumed without a sell — it
                                # re-triggers on a later cycle when unblocked.
                                try:
                                    with _db._write_lock:
                                        conn = _db.get_conn()
                                        conn.execute(
                                            "UPDATE settings SET value=? WHERE key=? AND value=?",
                                            (prev_raw, lkey, str(new_mark)))
                                        conn.commit()
                                    try:
                                        _db._invalidate_settings_cache()
                                    except Exception:
                                        pass
                                except Exception:
                                    log.debug("ladder mark rollback failed", exc_info=True)
        except Exception:
            log.debug("supplementary exits skipped", exc_info=True)
    return out


# ── per-cycle observability snapshot (Insights visualizations) ─────────────────

def _persist_cycle_stats(cycle_id: str, summary: Dict[str, Any],
                         scan_edges: Dict[str, Dict[str, Any]]) -> None:
    """Write a decision-funnel tally + scan snapshot for this cycle. Pure
    observability — never read by the trading path. Caller wraps in try/except."""
    import json
    import collections
    import database as _db
    props = summary.get("proposals") or []
    counts = collections.Counter(str(p.get("result") or "?") for p in props)
    executed = int(counts.get("executed", 0))
    exits = len(summary.get("exits") or [])
    # scan snapshot: every name that produced a forecast this cycle, with its
    # edge/conviction and final disposition (for the selectivity scatter + funnel
    # reasons). Cap to keep the row small.
    rows = []
    for p in props:
        sym = p.get("symbol")
        se = scan_edges.get(sym) or {}
        rows.append({"symbol": sym, "result": p.get("result"),
                     "edge": se.get("edge"), "conviction": se.get("conviction"),
                     "prob_up": se.get("prob_up")})
    rows = rows[:300]
    with _db._write_lock:
        conn = _db.get_conn()
        conn.execute(
            "INSERT OR REPLACE INTO aj_cycle_stats "
            "(cycle_id, ts, mode, session, scanned, with_signal, executed, exits, "
            " result_json, scan_json) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (cycle_id, aj_db.utc_now_iso(), summary.get("mode"),
             summary.get("session"), len(props), len(scan_edges), executed, exits,
             json.dumps(dict(counts)), json.dumps(rows)))
        conn.commit()


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
    cycle_id: Optional[str] = None
    try:
        # open_cycle INSIDE the try/finally: if it raises (DB locked/disk full),
        # the finally below still releases the single-instance flock — otherwise
        # a long-lived process leaks the held lock and every later scheduled or
        # manual cycle reports "another operator cycle is running" until restart.
        cycle_id = aj_db.open_cycle(mode)
        summary["cycle_id"] = cycle_id
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
            # close any expired option positions (worthless) before exits/scan
            summary["expired_options"] = _close_expired_options(cycle_id, cfg)
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
        # Batch 2 — cross-sectional selection: narrow the scan to the top-N best
        # RELATIVE opportunities by realized forecast edge (not an absolute bar).
        # Forecasts are cached, so the per-symbol loop below reuses them. Exits
        # already ran above, so narrowing new-entry candidates is safe.
        fc_cache: Dict[str, Any] = {}
        if cfg.get("cross_sectional_selection"):
            try:
                import aj_select
                cands = []
                for s in scan:
                    fcx = _forecast(s, horizon)
                    if fcx and fcx.get("ensemble"):
                        cands.append((s, fcx))
                        # Reuse in the per-symbol loop: a second _forecast call
                        # would log a duplicate accountability-ledger row per
                        # selected name, double-weighting Brier/accuracy stats.
                        fc_cache[s] = fcx
                top = aj_select.select_top_n(cands, cfg)
                scan = [s for s, _ in top]
                # select_top_n keeps only BULLISH edges, but the scan drives
                # SELLS too (_judge's forecast sell + the bearish-put entry) —
                # held names whose forecast turned bearish must stay in the
                # loop or they can only ever exit via mechanical stops.
                held_scan = [s for s, q in held.items()
                             if float(q or 0) > 0 and not str(s).startswith("OPT:")
                             and s not in set(scan)]
                scan = list(dict.fromkeys(scan + held_scan))
                summary["cross_sectional"] = {"considered": len(cands),
                                              "selected": len(scan)}
            except Exception:
                log.debug("cross-sectional selection skipped", exc_info=True)
        # Batch 5 — portfolio construction: compute risk-aware target weights once
        # per cycle over the candidate set; used below to CAP each order's notional
        # (only ever reduces size). Empty map => fall back to per-name sizing.
        alloc_map = {}
        if cfg.get("portfolio_construction"):
            try:
                import aj_allocate
                alloc_map = aj_allocate.target_weights(list(scan), cfg) or {}
                summary["allocation"] = {"names": len(alloc_map),
                                         "method": cfg.get("alloc_method")}
            except Exception:
                log.debug("allocation plan skipped", exc_info=True)
        # Analyst-council advisory budget (Phase 3): consult the council for at
        # most council_topk BUY candidates per cycle (cost bound). No-op unless
        # council_active() (council_enabled + VERIFY-COUNCIL).
        council_budget = {"n": 0, "max": int(cfg.get("council_topk", 3) or 0)}
        scan_edges: Dict[str, Dict[str, Any]] = {}   # observability: edge/conviction per scanned name
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
                fc = fc_cache.get(symbol) or _forecast(symbol, horizon)
                if not fc or not fc.get("ensemble"):
                    summary["proposals"].append({"symbol": symbol, "result": "no_signal"})
                    continue
                _ens = fc.get("ensemble") or {}
                scan_edges[symbol] = {"edge": _ens.get("edge_pct_pts"),
                                      "conviction": _ens.get("conviction"),
                                      "prob_up": _ens.get("prob_up")}
                decision = _judge(symbol, fc, cfg, held.get(symbol, 0.0))
                if not decision:
                    # bearish + not-held + options on -> long PUT (no council)
                    bp = _bearish_put_entry(cycle_id, symbol, fc, cfg, held.get(symbol, 0.0))
                    if bp:
                        summary["proposals"].append(bp)
                        continue
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
                # ── Batch 3/5 — execution alpha + portfolio cap (BUY entries) ──
                # All opt-in + fail-open; they only ever BLOCK or SHRINK, never
                # create/boost, and never touch the fail-closed risk gate.
                if decision.get("side") == "buy":
                    try:
                        import aj_execution_alpha as _ea
                        # event blackout — don't initiate into earnings
                        eb = _ea.event_blackout(symbol, cfg)
                        if eb.get("blocked"):
                            summary["proposals"].append({"symbol": symbol,
                                "result": "event_blackout", "reason": eb.get("reason")})
                            continue
                        # cost gate — skip trades whose edge won't survive costs
                        _q = {"price": float(sizing.get("price") or 0)}
                        cg = _ea.cost_gate(float(decision.get("edge_pts") or 0), symbol, _q, cfg)
                        if not cg.get("ok"):
                            summary["proposals"].append({"symbol": symbol,
                                "result": "cost_blocked", "reason": cg.get("reason")})
                            continue
                        # limit/pullback entry — improve the fill vs market-on-cycle
                        ep = _ea.entry_price(symbol, "buy", _q, cfg)
                        if ep.get("order_type") == "limit" and ep.get("limit_price"):
                            sizing = dict(sizing)
                            sizing["order_type"] = "limit"
                            sizing["limit_price"] = ep["limit_price"]
                    except Exception:
                        log.debug("execution-alpha skipped", exc_info=True)
                    # portfolio-construction cap: shrink to the risk-aware target
                    if alloc_map.get(symbol):
                        try:
                            import aj_allocate
                            alloc_notional = aj_allocate.allocation_notional(
                                symbol, float(alloc_map[symbol]), cfg)
                            cur = float(sizing.get("notional") or 0)
                            price = float(sizing.get("price") or 0)
                            if alloc_notional > 0 and price > 0 and alloc_notional < cur:
                                sizing = dict(sizing)
                                sizing["qty"] = alloc_notional / price
                                sizing["notional"] = aj_db.money(alloc_notional)
                        except Exception:
                            log.debug("allocation cap skipped", exc_info=True)
                    # ── Meta-label gate (Phase 4): a calibrated P(profit|setup)
                    # model trained on the agent's OWN realized trades. Opt-in +
                    # double-gated (only a PROMOTED model that beat baseline OOS
                    # influences anything). It can only FILTER (skip a low-P trade)
                    # or SHRINK size (kelly-lite, never increases) — never bypasses
                    # the fail-closed risk gate. Fail-open.
                    if cfg.get("metalabel_enabled"):
                        try:
                            import aj_metalabel, aj_alpha as _aa
                            _reg = _aa.detect_regime()
                            _conv = {"low": 0.0, "medium": 0.5, "high": 1.0}.get(
                                str(decision.get("conviction") or "").lower(), 0.5)
                            feats = {"edge": float(decision.get("edge_pts") or 0.0),
                                     "conviction": _conv,
                                     "prob_up": float(decision.get("prob_up") or 0.5),
                                     "side_long": 1.0,
                                     "regime_bull": 1.0 if _reg == "bull" else 0.0,
                                     "regime_bear": 1.0 if _reg == "bear" else 0.0,
                                     "regime_chop": 1.0 if _reg == "chop" else 0.0}
                            p_profit = aj_metalabel.predict_proba(feats, cfg)
                            if p_profit is not None:
                                thr = float(cfg.get("metalabel_prob_threshold", 0.5) or 0.5)
                                if p_profit < thr:
                                    summary["proposals"].append(
                                        {"symbol": symbol, "result": "metalabel_skip",
                                         "p_profit": round(p_profit, 3)})
                                    continue
                                if cfg.get("metalabel_size_by_edge"):
                                    # bounded shrink only: full size at P>=0.65,
                                    # tapering to a 0.25 floor as P approaches the
                                    # threshold. Never increases size.
                                    mult = max(0.25, min(1.0, p_profit / 0.65))
                                    price = float(sizing.get("price") or 0)
                                    q = (sizing.get("qty") or 0.0) * mult
                                    sizing = dict(sizing)
                                    sizing["qty"] = q
                                    sizing["notional"] = aj_db.money(q * price)
                        except Exception:
                            log.debug("metalabel gate skipped", exc_info=True)
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
                        # The per-trade equity target is often smaller than a
                        # single contract (premium x100), which silently floored
                        # n_ct to 0 and forced the equity fallback every time —
                        # the options sleeve never engaged. When a contract is
                        # chosen but unaffordable at the equity size, size UP to
                        # exactly one contract, bounded by the hard per-order
                        # notional cap. The risk gate still validates the result
                        # (exposure/buying-power) and may block it; this only
                        # removes the silent 0-contract dead end.
                        if opt and n_ct < 1 and prem > 0:
                            cap = float(cfg.get("max_order_notional_usd") or 0)
                            if 0 < prem <= cap:
                                n_ct = 1
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

        # Meta-label learning loop (Phase 4): label any newly-closed trades and
        # retrain P(profit) when enough new labels accumulate. Cheap no-op unless
        # metalabel_enabled; maybe_retrain self-gates on metalabel_retrain_min_new.
        if cfg.get("metalabel_enabled"):
            try:
                import aj_metalabel
                summary["metalabel"] = aj_metalabel.maybe_retrain(cfg)
            except Exception:
                log.debug("metalabel retrain skipped", exc_info=True)

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

        # Persist the per-cycle decision funnel + scan snapshot (observability
        # only — the Insights visualizations read this; the trading path never
        # does). Fail-open: a stats write must never fail a cycle.
        try:
            _persist_cycle_stats(cycle_id, summary, scan_edges)
        except Exception:
            log.debug("cycle-stats persist skipped", exc_info=True)

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
        if cycle_id is not None:
            aj_db.close_cycle(cycle_id, "crashed")
        summary["ok"] = False
        summary["error"] = str(e)
        return summary
    finally:
        lock.release()
