"""AJTA — Analyst Council orchestrator (merge plan §4.2, §5).

A compact, pure-Python orchestrator (NO LangGraph — a merge invariant for the
py2app bundle) that fans out the analyst team, synthesizes a CouncilDecision,
persists every artifact (queryable tables + the immutable audit hash chain), and
returns an ADVISORY decision.

Phase 1: analysts → weighted consensus → CouncilDecision (no debate yet; the
bull/bear + risk debates and the arbiter arrive in Phases 2 & 4 and refine the
synthesis without changing this module's contract).

Safety: the council never trades, sizes, or touches the risk gate. It is doubly
gated (council_enabled + VERIFY-COUNCIL) and cost-bounded (hard per-cycle LLM
call cap). On any error or budget exhaustion it degrades to a neutral/low-
conviction decision — the operator then proceeds rule-based (fail-closed).
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional

import aj_config
import aj_db
import aj_routing
import aj_analysts
import aj_debate
import aj_memory
from aj_schemas import (AnalystReport, CouncilDecision, ResearchPlan,
                        TraderProposal, Rating, rating_conviction, clamp,
                        extract_json, parse_rating)

log = logging.getLogger("augur.aj_council")


# ── per-cycle cost budget ─────────────────────────────────────────────────────

class Budget:
    """Hard cap on LLM calls per council scope (cost guard). Cost in USD is
    tracked best-effort (aj_routing telemetry is the authoritative ledger)."""
    def __init__(self, max_calls: int):
        self.max_calls = max(0, int(max_calls))
        self.n_calls = 0
        self.cost_usd = 0.0
        self.hit_cap = False

    def exhausted(self) -> bool:
        if self.n_calls >= self.max_calls:
            self.hit_cap = True
            return True
        return False


def _make_call(cfg: Dict[str, Any], budget: Budget,
               cycle_id: Optional[str]) -> aj_analysts.CallFn:
    deep_tok = int(cfg.get("council_deep_max_tokens", 1024) or 1024)
    quick_tok = int(cfg.get("council_quick_max_tokens", 512) or 512)

    def call(tier: str, role: str, system: str, prompt: str) -> Optional[str]:
        if budget.exhausted():
            return None
        mt = deep_tok if tier == aj_routing.DEEP else quick_tok
        try:
            r = aj_routing.complete_tiered(
                prompt, tier=tier, system=system, role=role, max_tokens=mt,
                sensitivity=aj_routing.PUBLIC, cycle_id=cycle_id)
            if r:
                # count only completed model calls toward the per-cycle cap so a
                # transport failure can't exhaust the budget (cost guard) and the
                # persisted n_calls matches the authoritative routing ledger.
                budget.n_calls += 1
                budget.cost_usd += float(r.get("cost_usd", 0.0) or 0.0)
                return r.get("text") if r.get("ok") else None
        except Exception:
            log.exception("council call failed role=%s", role)
        return None

    return call


# ── decision cache (cost guard) ───────────────────────────────────────────────

_cache: Dict[str, Any] = {}
_cache_lock = threading.Lock()
# In-flight registry (S004): key -> threading.Event. While one thread computes a
# decision for a (symbol+config) key, concurrent identical run() calls wait on
# the event and then read the freshly-cached result instead of recomputing (and
# re-spending). Guarded by _cache_lock; the Event is always set in a finally so
# an error never deadlocks the waiters.
_inflight: Dict[str, "threading.Event"] = {}


def _cache_key(symbol: str, cfg: Dict[str, Any]) -> str:
    """Cache key for a council decision. Includes the analyst roster AND every
    cfg knob that changes the advice (policy, buy/sell thresholds, debate-round
    counts, personas, and the current regime). Omitting these served stale advice
    for up to the cache TTL (6h) after a preset/regime change — a fresh preset or
    a flipped regime now produces a fresh key (cache miss) instead."""
    analysts = ",".join(_selected_analysts(cfg))
    knobs = [
        str(cfg.get("council_policy", "advisory")),
        str(cfg.get("buy_prob_threshold", "")),
        str(cfg.get("sell_prob_threshold", "")),
        str(cfg.get("min_edge_pct_pts", "")),
        str(int(cfg.get("max_research_rounds", 1) or 0)),
        str(int(cfg.get("max_risk_rounds", 1) or 0)),
        str(bool(cfg.get("personas_enabled", False))),
        _current_regime(),
    ]
    return "{}|{}|{}".format(str(symbol).upper(), analysts, ",".join(knobs))


def _current_regime() -> str:
    """Best-effort current market regime for the cache key. Fail-open to '?' so a
    regime-detection error never breaks caching (it just won't vary by regime)."""
    try:
        import aj_alpha
        return str(aj_alpha.detect_regime())
    except Exception:
        return "?"


def _cache_get(symbol: str, cfg: Dict[str, Any]) -> Optional[CouncilDecision]:
    ttl = int(cfg.get("council_cache_ttl_min", 360) or 0) * 60
    if ttl <= 0:
        return None
    k = _cache_key(symbol, cfg)
    with _cache_lock:
        hit = _cache.get(k)
        if hit and hit[0] > time.time():
            return hit[1]
        if hit:
            _cache.pop(k, None)
    return None


def _cache_put(symbol: str, cfg: Dict[str, Any], dec: CouncilDecision) -> None:
    ttl = int(cfg.get("council_cache_ttl_min", 360) or 0) * 60
    if ttl <= 0 or dec.status not in ("ok",):
        return  # never cache degraded/error/skipped decisions
    with _cache_lock:
        _cache[_cache_key(symbol, cfg)] = (time.time() + ttl, dec)


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()
        # Release any waiters so a clear_cache() between runs can't strand them.
        for ev in _inflight.values():
            ev.set()
        _inflight.clear()


# ── coequal track-record gate (Phase 5) ───────────────────────────────────────

def track_record(window: int = 100) -> Dict[str, Any]:
    """The council's realized alpha track record from aj_reflections (real
    outcomes, not a fabricated backtest). Returns {n, mean_alpha, win_rate}."""
    try:
        import database as db
        rows = db.get_conn().execute(
            "SELECT alpha_return FROM aj_reflections ORDER BY id DESC LIMIT ?",
            (max(1, int(window)),)).fetchall()
    except Exception:
        return {"n": 0, "mean_alpha": 0.0, "win_rate": 0.0}
    alphas = [float(r["alpha_return"]) for r in rows if r["alpha_return"] is not None]
    n = len(alphas)
    if n == 0:
        return {"n": 0, "mean_alpha": 0.0, "win_rate": 0.0}
    mean_alpha = sum(alphas) / n
    win_rate = sum(1 for a in alphas if a > 0) / n
    return {"n": n, "mean_alpha": mean_alpha, "win_rate": win_rate}


def coequal_unlocked(cfg: Optional[Dict[str, Any]] = None) -> bool:
    """True only when 'coequal' may BOOST size: policy is coequal, council is
    active, AND the realized alpha track record clears the configured bars
    (>= min_samples resolved reflections with mean alpha >= min_alpha). This is
    the gate that prevents an unproven council from sizing up. Fail-closed:
    any error or thin sample => locked (no boost)."""
    try:
        cfg = cfg or aj_config.get_config()
        if str(cfg.get("council_policy", "advisory")).lower() != "coequal":
            return False
        if not aj_config.council_active(cfg):
            return False
        tr = track_record()
        return (tr["n"] >= int(cfg.get("coequal_min_samples", 20) or 0)
                and tr["mean_alpha"] >= float(cfg.get("coequal_min_alpha", 0.0) or 0.0))
    except Exception:
        return False


# ── analyst selection + consensus ─────────────────────────────────────────────

def _selected_analysts(cfg: Dict[str, Any]) -> List[str]:
    out = []
    for name in ("fundamentals", "news", "sentiment", "technical"):
        if cfg.get("council_analyst_" + name, True):
            out.append(name)
    return out


def _run_analysts(symbol: str, cfg: Dict[str, Any],
                  call: aj_analysts.CallFn) -> List[AnalystReport]:
    reports: List[AnalystReport] = []
    for name in _selected_analysts(cfg):
        fn = aj_analysts.ANALYSTS.get(name)
        if not fn:
            continue
        try:
            reports.append(fn(symbol, call=call, cfg=cfg))
        except Exception:
            log.exception("analyst %s crashed", name)
    # Phase 6: optional investor-persona analysts (no-op unless personas_enabled)
    if cfg.get("personas_enabled"):
        try:
            import aj_personas
            for pname, pfn in aj_personas.persona_analysts(cfg).items():
                try:
                    reports.append(pfn(symbol, call=call, cfg=cfg))
                except Exception:
                    log.exception("persona %s crashed", pname)
        except Exception:
            log.debug("personas skipped", exc_info=True)
    return reports


# avg score → rating thresholds (10 = most bullish). The neutral HOLD band is
# centered SYMMETRICALLY on 5.0 (5.0 ± 0.75 => [4.25, 5.75]) with consistent
# half-open boundary comparisons (>=), so a score of exactly 5.0 is HOLD and the
# two boundaries (4.25, 5.75) are mirror images rather than landing in different
# ratings.
def _score_to_rating(avg: float) -> Rating:
    if avg >= 7.0:
        return Rating.BUY
    if avg >= 5.75:
        return Rating.OVERWEIGHT
    if avg >= 4.25:          # HOLD band: [4.25, 5.75), centered on 5.0
        return Rating.HOLD
    if avg >= 3.0:
        return Rating.UNDERWEIGHT
    return Rating.SELL


def _consensus(reports: List[AnalystReport]):
    """Confidence-weighted consensus → (rating, confidence, thesis, dissent)."""
    backed = [r for r in reports if r.confidence > 0.0]
    if not backed:
        return Rating.HOLD, 0.0, "no analyst evidence", ""
    wsum = sum(r.confidence for r in backed) or 1.0
    avg = sum(r.score * r.confidence for r in backed) / wsum
    # Conviction is CONFIDENCE-WEIGHTED (consistent with the score, which is also
    # confidence-weighted): each analyst's confidence is weighted by itself, so a
    # strong analyst dominates instead of being diluted by an unweighted mean of
    # weak ones. Equivalent to Σc² / Σc; reduces to the plain mean when all the
    # confidences are equal.
    conf = clamp(sum(r.confidence * r.confidence for r in backed) / wsum, 0.0, 1.0, 0.0)
    rating = _score_to_rating(avg)
    # thesis: top key point or narrative per analyst
    bits = []
    for r in backed:
        head = (r.key_points[0] if r.key_points else r.narrative[:120]).strip()
        if head:
            bits.append("{}: {}".format(r.analyst, head))
    thesis = " | ".join(bits)[:1000] or "consensus from {} analysts".format(len(backed))
    # dissent: wide score spread => analysts disagree
    scores = [r.score for r in backed]
    dissent = ""
    if scores and (max(scores) - min(scores)) >= 4.0:
        hi = max(backed, key=lambda r: r.score)
        lo = min(backed, key=lambda r: r.score)
        dissent = "{} bullish ({:.1f}) vs {} bearish ({:.1f})".format(
            hi.analyst, hi.score, lo.analyst, lo.score)
    return rating, conf, thesis, dissent


# ── persistence (queryable copy + audit hash chain) ───────────────────────────

def _persist(dec: CouncilDecision, reports: List[AnalystReport],
             turns: Optional[List[Dict[str, Any]]],
             cycle_id: Optional[str]) -> Optional[int]:
    turns = turns or []
    run_id = None
    try:
        run_id = aj_db.insert(
            "aj_council_runs", ts=aj_db.utc_now_iso(), cycle_id=cycle_id,
            symbol=dec.symbol,
            policy=str(aj_config.get_config().get("council_policy", "advisory")),
            status=dec.status,
            rating=dec.rating.value, action=dec.action.value,
            conviction=dec.conviction, thesis=dec.thesis,
            price_target=dec.price_target, time_horizon=dec.time_horizon,
            stop_hint=dec.stop_hint, dissent=dec.dissent, cost_usd=dec.cost_usd,
            latency_ms=dec.latency_ms, n_calls=dec.n_calls,
            decision_json=_json(dec.to_audit()))
        for r in reports:
            aj_db.insert("aj_analyst_reports", council_run_id=run_id,
                         ts=aj_db.utc_now_iso(), analyst=r.analyst, band=r.band,
                         score=r.score, confidence=r.confidence,
                         narrative=r.narrative[:2000], report_json=_json(r.to_audit()))
        for t in turns:
            aj_db.insert("aj_debate_turns", council_run_id=run_id,
                         ts=aj_db.utc_now_iso(), debate=t.get("debate", "research"),
                         role=t.get("role", "?"), round=int(t.get("round", 0)),
                         content=str(t.get("content", ""))[:4000])
    except Exception:
        log.exception("council persist failed (non-fatal)")
    # Audit hash chain — independent of the queryable copy.
    try:
        aj_db.audit("council_run",
                    {"decision": dec.to_audit(),
                     "reports": [r.to_audit() for r in reports],
                     "debate": turns},
                    cycle_id=cycle_id, ref_id=run_id, actor="council")
    except Exception:
        log.exception("council audit failed (non-fatal)")
    return run_id


def _json(obj: Any) -> str:
    import json
    try:
        return json.dumps(obj, default=str)
    except Exception:
        return "{}"


# ── public entry ──────────────────────────────────────────────────────────────

def run(symbol: str, cfg: Optional[Dict[str, Any]] = None,
        cycle_id: Optional[str] = None, force: bool = False,
        call: Optional[aj_analysts.CallFn] = None) -> CouncilDecision:
    """Run the analyst council for one symbol and return an advisory
    CouncilDecision. `force=True` runs even when council_active() is False but
    STILL requires the VERIFY-COUNCIL gate (so inspection isn't free to anyone).
    `call` overrides the completion function (tests inject a deterministic fake).
    """
    cfg = cfg or aj_config.get_config()
    symbol = str(symbol or "").upper()
    if not symbol:
        return CouncilDecision(symbol="", status="error", dissent="no symbol")

    # gating: normal path requires council_active; forced path still requires the
    # VERIFY gate (never let an un-acknowledged caller spend on the council).
    if call is None:
        if force:
            if not aj_config.council_verify_passed():
                return CouncilDecision(symbol=symbol, status="skipped",
                                       dissent="VERIFY-COUNCIL not passed")
        elif not aj_config.council_active(cfg):
            return CouncilDecision(symbol=symbol, status="skipped",
                                   dissent="council not active")

    cached = _cache_get(symbol, cfg)
    if cached is not None:
        return cached

    # In-flight de-dup (S004): coalesce concurrent identical run() calls so only
    # the first computes (and spends); the rest wait and read the cache. Only
    # meaningful when the cache is on — without a TTL there is nothing for a
    # waiter to read, so it falls through and computes independently.
    ttl_on = int(cfg.get("council_cache_ttl_min", 360) or 0) > 0
    key = _cache_key(symbol, cfg)
    my_event: Optional[threading.Event] = None
    if ttl_on:
        wait_on: Optional[threading.Event] = None
        with _cache_lock:
            existing = _inflight.get(key)
            if existing is not None:
                wait_on = existing
            else:
                my_event = threading.Event()
                _inflight[key] = my_event
        if wait_on is not None:
            # Another thread owns this key — wait for it, then read its result.
            # The wait must comfortably exceed a WORST-CASE full council run
            # (~12 sequential DEEP calls: analysts, debate rounds, manager,
            # trader, risk rounds, arbiter — minutes, not seconds); a short
            # timeout made waiters give up and recompute, doubling the LLM
            # spend this in-flight registry exists to prevent.
            wait_on.wait(timeout=600)
            cached = _cache_get(symbol, cfg)
            if cached is not None:
                return cached
            # Leader produced a non-cacheable (degraded/error) decision or timed
            # out; fall through and compute our own rather than block forever.

    try:
        return _compute(symbol, cfg, cycle_id, call)
    finally:
        if my_event is not None:
            # Always release waiters, even on error, so they never deadlock.
            with _cache_lock:
                _inflight.pop(key, None)
            my_event.set()


def _compute(symbol: str, cfg: Dict[str, Any], cycle_id: Optional[str],
             call: Optional[aj_analysts.CallFn]) -> CouncilDecision:
    """Compute one council decision (no gating/cache/de-dup — run() owns those)."""
    raw_max = cfg.get("council_max_calls_per_cycle", 40)
    budget = Budget(int(raw_max) if raw_max is not None else 40)
    the_call = call or _make_call(cfg, budget, cycle_id)
    t0 = time.time()
    reports = _run_analysts(symbol, cfg, the_call)
    c_rating, conf, c_thesis, dissent = _consensus(reports)

    # Phase 2: bull/bear debate → research manager refines the consensus.
    # Phase 4: trader → 3-way risk debate → arbiter (final risk-adjusted call).
    # The consensus plan is the fail-closed fallback at every layer — we never
    # fabricate a debate; any missing model/budget degrades gracefully.
    rounds = int(cfg.get("max_research_rounds", 1) or 0)
    risk_rounds = int(cfg.get("max_risk_rounds", 1) or 0)
    mem_text = _safe_recall(symbol)
    plan = ResearchPlan(recommendation=c_rating, rationale=c_thesis)
    research_turns: List[Dict[str, Any]] = []
    risk_turns: List[Dict[str, Any]] = []
    proposal: Optional[TraderProposal] = None
    final: Optional[CouncilDecision] = None
    debate_ran = False
    if rounds > 0 and reports and not budget.exhausted():
        research_turns = aj_debate.research_debate(symbol, reports, mem_text, the_call, rounds)
        dplan = aj_debate.research_manager(symbol, reports, research_turns, the_call)
        if dplan is not None:
            plan = dplan
            debate_ran = True
            if not budget.exhausted():
                proposal = aj_debate.trader(symbol, plan, the_call)
            if proposal is not None and risk_rounds > 0 and not budget.exhausted():
                # PRIVACY: portfolio holdings are PRIVATE; we deliberately pass
                # NO holdings into the PUBLIC-routed risk debate. The debate
                # reasons on the proposal itself. (Richer private context would
                # need PRIVATE/local-only routing — a future refinement.)
                risk_turns = aj_debate.risk_debate(symbol, proposal, "", the_call, risk_rounds)
            final = _arbiter(symbol, plan, proposal, risk_turns, mem_text, conf, the_call)

    turns = research_turns + risk_turns
    backed = [r for r in reports if r.confidence > 0.0]
    if not reports:
        status = "error"
    elif budget.hit_cap or not backed or (rounds > 0 and not debate_ran):
        status = "degraded"
    else:
        status = "ok"

    elapsed = int((time.time() - t0) * 1000)
    if final is not None:
        dec = final
        dec.status = status
        dec.cost_usd = budget.cost_usd
        dec.latency_ms = elapsed
        dec.n_calls = budget.n_calls
        # Merge the arbiter's dissent with the consensus analyst-spread dissent so
        # the analyst disagreement signal (e.g. "X bullish vs Y bearish") isn't
        # lost when the arbiter also returns its own dissent.
        if dissent and dissent != dec.dissent:
            dec.dissent = "; ".join(filter(None, [dec.dissent, dissent]))[:400]
    else:
        dec = CouncilDecision.from_plan(
            symbol, plan, proposal, confidence=conf,
            dissent=dissent, status=status, cost_usd=budget.cost_usd,
            latency_ms=elapsed, n_calls=budget.n_calls)
    run_id = _persist(dec, reports, turns, cycle_id)
    # Stamp the exact persisted run id so callers can fetch this run's artifacts
    # directly (covers both the arbiter `final` and the `from_plan` branches —
    # `dec` is whichever was assigned above) instead of re-querying "latest run
    # for symbol" (which races concurrent runs of the same symbol).
    if run_id is not None:
        dec.run_id = run_id
    _remember(dec)
    _cache_put(symbol, cfg, dec)
    return dec


def _safe_recall(symbol: str) -> str:
    try:
        return aj_memory.recall_text(symbol)
    except Exception:
        return ""


# ── arbiter (final risk-adjusted decision, Phase 4) ───────────────────────────

_ARBITER_SYS = (
    "You are the Portfolio Manager — the final arbiter. Weigh the research "
    "stance, the trader's proposal, the 3-way risk debate, and prior lessons "
    "about {symbol}. Render an INDEPENDENT judgment (you may diverge from any "
    "single voice). Respond with STRICT JSON: {{\"rating\":\"BUY|OVERWEIGHT|"
    "HOLD|UNDERWEIGHT|SELL\",\"action\":\"BUY|HOLD|SELL\",\"conviction\":<0-1>,"
    "\"thesis\":\"<=80 words\",\"dissent\":\"<=40 words or empty>\","
    "\"time_horizon\":\"<short|medium|long or null>\"}}.")


def _arbiter(symbol: str, plan: ResearchPlan, proposal: Optional[TraderProposal],
             risk_turns: List[Dict[str, Any]], memory_text: str, conf: float,
             call: aj_analysts.CallFn) -> Optional[CouncilDecision]:
    """Final risk-adjusted synthesis. Returns None when there's nothing to
    arbitrate or the model is unavailable (caller falls back to the plan)."""
    if call is None or (not risk_turns and proposal is None):
        return None
    transcript = "\n".join("{}: {}".format(str(t.get("role", "?")).upper(),
                                            t.get("content", ""))
                           for t in risk_turns) or "(no risk debate)"
    mem = ("\nPrior lessons:\n" + memory_text) if memory_text else ""
    prompt = ("Research stance:\n{plan}\n\nTrader proposal:\n{prop}\n\n"
              "Risk debate:\n{risk}{mem}\n\nFinal decision (JSON):").format(
        plan=plan.render(), prop=(proposal.render() if proposal else "(none)"),
        risk=transcript, mem=mem)
    text = call(aj_routing.DEEP, "council.arbiter",
                _ARBITER_SYS.format(symbol=symbol), prompt)
    if not text:
        return None
    d = extract_json(text)
    if not d:
        return None
    rating = parse_rating(d.get("rating") or d.get("recommendation"),
                          default=plan.recommendation)
    plan2 = ResearchPlan(recommendation=rating,
                         rationale=str(d.get("thesis") or plan.rationale))
    dec = CouncilDecision.from_plan(symbol, plan2, proposal, confidence=conf)
    # arbiter may override conviction explicitly; honor a valid action too
    if d.get("conviction") is not None:
        dec.conviction = clamp(d.get("conviction"), 0.0, 1.0, dec.conviction)
    dec.dissent = str(d.get("dissent") or "")
    if d.get("action"):
        from aj_schemas import parse_action, Action, _RATING_ACTION
        proposed = parse_action(d.get("action"), default=dec.action)
        implied = _RATING_ACTION.get(rating, Action.HOLD)
        # Reconcile: a BUY-while-bearish (or SELL-while-bullish) pair is incoherent
        # — downstream sizing reads rating-based signed_score() while the operator
        # reads action, so keep the rating-derived action and record the conflict.
        contradicts = ((proposed == Action.BUY and implied == Action.SELL) or
                       (proposed == Action.SELL and implied == Action.BUY))
        if contradicts:
            note = "arbiter action {} contradicts rating {} — kept {}".format(
                proposed.value, rating.value, implied.value)
            dec.dissent = "; ".join(filter(None, [dec.dissent, note]))[:400]
        else:
            dec.action = proposed
    if d.get("time_horizon") and str(d.get("time_horizon")).lower() != "null":
        dec.time_horizon = str(d.get("time_horizon"))
    return dec


# ── reflection (alpha-aware lessons, Phase 4) ─────────────────────────────────

_REFLECT_SYS = (
    "You are a trading coach. In 1-2 sentences, extract ONE actionable lesson "
    "from this outcome — what thesis element worked or failed, judged on alpha "
    "(return vs benchmark), not raw return. Be terse and concrete.")


def reflect(symbol: str, raw_return: float, benchmark_return: float,
            benchmark: str = "SPY", situation: str = "",
            call: Optional[aj_analysts.CallFn] = None) -> Dict[str, Any]:
    """Write an alpha-aware lesson for a resolved decision: persists to
    aj_reflections + the memory log (as a cross-symbol 'lesson') + audit. The
    lesson is injected into future council prompts. Model lesson is optional;
    falls back to a deterministic template. Never raises into the caller."""
    try:
        raw = float(raw_return)
        bench = float(benchmark_return)
    except (TypeError, ValueError):
        raw, bench = 0.0, 0.0
    alpha = raw - bench
    lesson = ""
    if call is not None:
        try:
            prompt = ("Symbol {s}: realized {r:+.1%}, benchmark {b} {bn:+.1%}, "
                      "alpha {a:+.1%}. Situation: {sit}\nLesson:").format(
                s=symbol, r=raw, b=benchmark, bn=bench, a=alpha, sit=situation[:300])
            text = call(aj_routing.QUICK, "council.reflect", _REFLECT_SYS, prompt)
            if text:
                lesson = str(text).strip()[:400]
        except Exception:
            log.debug("reflect model call failed", exc_info=True)
    if not lesson:
        verb = "outperformed" if alpha >= 0 else "underperformed"
        lesson = "{}: {:+.1%} vs {} ({:+.1%} alpha) — {}.".format(
            symbol, raw, benchmark, alpha, verb)
    import hashlib
    sit_hash = hashlib.sha256((situation or "").encode("utf-8")).hexdigest()[:16]
    try:
        aj_db.insert("aj_reflections", ts=aj_db.utc_now_iso(), symbol=str(symbol).upper(),
                     situation_hash=sit_hash, raw_return=raw, alpha_return=alpha,
                     benchmark=benchmark, lesson=lesson)
    except Exception:
        log.exception("reflect persist failed (non-fatal)")
    try:
        aj_memory.append(str(symbol).upper(), "lesson", lesson)
    except Exception:
        pass
    try:
        aj_db.audit("council_reflection",
                    {"symbol": symbol, "raw_return": raw, "alpha_return": alpha,
                     "benchmark": benchmark, "lesson": lesson}, actor="council")
    except Exception:
        pass
    return {"symbol": str(symbol).upper(), "raw_return": raw,
            "alpha_return": alpha, "benchmark": benchmark, "lesson": lesson}


def _invested_usd(symbol: str) -> float:
    """Total cost basis of BUY fills for a symbol (qty*price + fees). Best-effort.

    NOTE: this sums ALL-TIME buy fills and so double-counts basis for a symbol
    traded across multiple round-trips. Per-round-trip reflection uses
    `_closed_round_trip()` instead; `_invested_usd` is retained for callers/tests
    that want the lifetime figure."""
    try:
        import database as db
        rows = db.get_conn().execute(
            "SELECT f.qty q, f.price p, f.fees_usd fee FROM aj_fills f "
            "JOIN aj_orders o ON o.id=f.order_id "
            "WHERE o.symbol=? AND o.side='buy'", (str(symbol).upper(),)).fetchall()
        return sum(float(r["q"]) * float(r["p"]) + float(r["fee"] or 0) for r in rows)
    except Exception:
        return 0.0


def _closed_round_trip(symbol: str) -> Optional[Dict[str, Any]]:
    """Best-effort scope of the most-recently CLOSED round-trip for `symbol`.

    Walks all fills (buy + sell) in chronological order, tracking running shares,
    and isolates the LAST completed round-trip: the run of fills from the buy that
    re-opened a flat position up to the sell that returned it to flat. Returns
    {basis_usd, realized_usd, first_buy_ts, last_sell_ts} for that round-trip, or
    None when a sound per-round-trip basis can't be derived (no fills, still open,
    or no realized sells). Never raises — returns None on any error so the caller
    skips the reflection rather than emitting a wrong number.

    realized_usd is computed FIFO over the consumed buy lots, so it reflects this
    round-trip's P&L only (not lifetime). basis_usd is the cost of the buy shares
    consumed by this round-trip's sells (qty*price + pro-rated buy fees) — the
    denominator the per-round-trip return is measured against."""
    try:
        import database as db
        rows = db.get_conn().execute(
            "SELECT o.side side, f.qty q, f.price p, f.fees_usd fee, f.filled_at ts "
            "FROM aj_fills f JOIN aj_orders o ON o.id=f.order_id "
            "WHERE o.symbol=? ORDER BY f.filled_at ASC, f.id ASC",
            (str(symbol).upper(),)).fetchall()
    except Exception:
        return None
    EPS = 1e-9
    lots: List[Dict[str, Any]] = []   # open FIFO buy lots: {qty, unit_cost, ts}
    rt: Optional[Dict[str, Any]] = None  # the round-trip currently being built
    for r in rows:
        try:
            side = str(r["side"] or "").lower()
            qty = float(r["q"] or 0)
            price = float(r["p"] or 0)
            fee = float(r["fee"] or 0)
            ts = r["ts"]
        except Exception:
            return None
        if qty <= 0:
            continue
        if side == "buy":
            # Per-share cost includes pro-rated entry fees so basis is complete.
            unit_cost = price + (fee / qty if qty else 0.0)
            if not lots:
                # Re-opening a flat position starts a fresh round-trip.
                rt = {"basis_usd": 0.0, "realized_usd": 0.0,
                      "first_buy_ts": ts, "last_sell_ts": None}
            lots.append({"qty": qty, "unit_cost": unit_cost, "ts": ts})
        elif side == "sell":
            if rt is None or not lots:
                # A sell with no open lots (short/over-sell/unmatched) — can't
                # derive a sound basis; abort to avoid a wrong number.
                return None
            remaining = qty
            sell_unit = price - (fee / qty if qty else 0.0)  # net of exit fees
            while remaining > EPS and lots:
                lot = lots[0]
                take = min(remaining, lot["qty"])
                rt["basis_usd"] += take * lot["unit_cost"]
                rt["realized_usd"] += take * (sell_unit - lot["unit_cost"])
                lot["qty"] -= take
                remaining -= take
                if lot["qty"] <= EPS:
                    lots.pop(0)
            rt["last_sell_ts"] = ts
    # A clean closed round-trip: position is flat and we matched at least one sell.
    if lots:
        return None                       # still open — not a closed round-trip
    if rt is None or rt["last_sell_ts"] is None or rt["basis_usd"] <= 0:
        return None
    return rt


def _spy_return_since(ts_iso: str, end_iso: Optional[str] = None) -> float:
    """SPY fractional return over the benchmark window. Start = first bar
    at-or-after `ts_iso`; end = last bar at-or-before `end_iso` (defaults to the
    latest bar, i.e. "now") so the benchmark window matches the actual trade
    horizon (S007). Fail-open to 0."""
    try:
        import fetcher
        import aj_db
        dt = aj_db.parse_iso(ts_iso)
        # Size the fetch window from the trade's age — a fixed 6mo window gave
        # any older round-trip the oldest available bar as its "start",
        # understating the benchmark and overstating alpha.
        period = "6mo"
        if dt is not None:
            age_days = (aj_db.utc_now() - dt).days
            if age_days > 1500:
                period = "10y"
            elif age_days > 650:
                period = "5y"
            elif age_days > 300:
                period = "2y"
            elif age_days > 150:
                period = "1y"
        bars = fetcher.get_chart_data("SPY", period, "1d")
        if not isinstance(bars, list) or len(bars) < 2:
            return 0.0
        start = None
        for b in bars:
            bt = b.get("time")
            # bars carry unix seconds; compare to the decision epoch
            if dt is not None and bt is not None and bt >= dt.timestamp():
                start = b.get("close")
                break
        # No bar at-or-after the timestamp (fresh/clock-skewed ts) => no benchmark
        # window. Return 0.0 rather than falling back to the oldest 6-month bar,
        # which would fabricate a huge, wrong benchmark return and corrupt alpha.
        if start is None:
            return 0.0
        # Track the START bar's epoch so we can guarantee the END bar is at/after
        # it; an end before the start would otherwise fabricate a backwards return.
        start_ts_epoch = None
        for b in bars:
            bt = b.get("time")
            if dt is not None and bt is not None and bt >= dt.timestamp():
                start_ts_epoch = bt
                break
        end = bars[-1].get("close")
        # Cap the benchmark window at the holding horizon's end (last sell), so a
        # closed trade's alpha isn't measured against SPY's move AFTER the exit.
        # Only accept end bars AT OR AFTER the start bar — never before it.
        end_dt = aj_db.parse_iso(end_iso) if end_iso else None
        if end_dt is not None:
            cutoff = end_dt.timestamp()
            end = None
            for b in bars:
                bt = b.get("time")
                if bt is None:
                    continue
                if bt > cutoff:
                    break
                if start_ts_epoch is not None and bt < start_ts_epoch:
                    continue  # before the start bar — not a valid window end
                end = b.get("close")
            # No bar at/after start AND at/before cutoff => window collapses to a
            # single bar (or is inverted): the start IS the end => 0 return.
            if end is None:
                return 0.0
        if start and end and start > 0:
            return (float(end) - float(start)) / float(start)
    except Exception:
        pass
    return 0.0


def reflect_due(cfg: Optional[Dict[str, Any]] = None, max_per_call: int = 5,
                call: Optional[aj_analysts.CallFn] = None) -> List[Dict[str, Any]]:
    """Reflect on CLOSED council round-trips not yet reflected (alpha vs SPY),
    writing lessons into memory for future prompts. Gated behind the operator's
    daily_reflection flag (opt-in); bounded; dedup'd by council run id; never
    raises into the cycle."""
    cfg = cfg or aj_config.get_config()
    if not cfg.get("daily_reflection"):
        return []
    out: List[Dict[str, Any]] = []
    try:
        import database as db
        conn = db.get_conn()
        rows = conn.execute(
            "SELECT id, symbol, ts FROM aj_council_runs WHERE action='BUY' "
            "AND status IN ('ok','degraded') ORDER BY id DESC LIMIT 50").fetchall()
    except Exception:
        return out
    try:
        import aj_positions
        open_syms = set((aj_positions.paper_book().get("positions") or {}).keys())
    except Exception:
        open_syms = set()
    import hashlib
    for r in rows:
        if len(out) >= max_per_call:
            break
        sym, run_id = r["symbol"], r["id"]
        if sym in open_syms:
            continue                          # only reflect CLOSED round-trips
        # S006: return is realized P&L over the CLOSED round-trip's OWN cost
        # basis (not all-time buy fills, which double-count a re-traded symbol).
        rt = _closed_round_trip(sym)
        if not rt or rt.get("basis_usd", 0) <= 0:
            continue                          # no sound per-round-trip basis => skip
        # Dedup on the ROUND-TRIP identity, not the council run id: every later
        # BUY run of the same symbol (routine with the 6h decision cache, and
        # many runs never trade at all) re-reflected the same last-closed
        # round-trip, inflating track_record() past coequal_min_samples off a
        # single real outcome. The legacy run-keyed hash is also checked so
        # pre-existing reflections aren't duplicated once.
        situation = "rt:{}:{}:{}".format(sym, rt.get("first_buy_ts") or "",
                                         rt.get("last_sell_ts") or "")
        h = hashlib.sha256(situation.encode("utf-8")).hexdigest()[:16]
        legacy_h = hashlib.sha256("run:{}".format(run_id).encode("utf-8")).hexdigest()[:16]
        try:
            if conn.execute("SELECT 1 FROM aj_reflections WHERE situation_hash IN (?,?)",
                            (h, legacy_h)).fetchone():
                continue                      # this round-trip already reflected
        except Exception:
            continue
        raw = float(rt["realized_usd"]) / float(rt["basis_usd"])
        # S007: benchmark over the trade's actual holding window (first buy →
        # last sell), not run-ts→now. Fall back to run-ts→now if fills missing.
        start_ts = rt.get("first_buy_ts") or r["ts"]
        bench = _spy_return_since(start_ts, rt.get("last_sell_ts"))
        out.append(reflect(sym, raw, bench, situation=situation, call=call))
    return out


def _remember(dec: CouncilDecision) -> None:
    """Append a terse decision summary to the council memory log (fail-open)."""
    if dec.status not in ("ok", "degraded"):
        return
    try:
        aj_memory.append(dec.symbol, "decision",
                         "{r}/{a} conviction={c:.0%} — {t}".format(
                             r=dec.rating.value, a=dec.action.value,
                             c=dec.conviction, t=dec.thesis[:240]))
    except Exception:
        pass
