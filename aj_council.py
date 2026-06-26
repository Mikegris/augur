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
from aj_schemas import (AnalystReport, CouncilDecision, ResearchPlan, Rating,
                        rating_conviction)

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
        budget.n_calls += 1
        mt = deep_tok if tier == aj_routing.DEEP else quick_tok
        try:
            r = aj_routing.complete_tiered(
                prompt, tier=tier, system=system, role=role, max_tokens=mt,
                sensitivity=aj_routing.PUBLIC, cycle_id=cycle_id)
            if r:
                budget.cost_usd += float(r.get("cost_usd", 0.0) or 0.0)
                return r.get("text") if r.get("ok") else None
        except Exception:
            log.exception("council call failed role=%s", role)
        return None

    return call


# ── decision cache (cost guard) ───────────────────────────────────────────────

_cache: Dict[str, Any] = {}
_cache_lock = threading.Lock()


def _cache_key(symbol: str, cfg: Dict[str, Any]) -> str:
    analysts = ",".join(_selected_analysts(cfg))
    return "{}|{}".format(str(symbol).upper(), analysts)


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
    return reports


# avg score → rating thresholds (10 = most bullish).
def _score_to_rating(avg: float) -> Rating:
    if avg >= 7.0:
        return Rating.BUY
    if avg >= 5.75:
        return Rating.OVERWEIGHT
    if avg > 4.25:
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
    conf = sum(r.confidence for r in backed) / len(backed)
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
             cycle_id: Optional[str]) -> Optional[int]:
    run_id = None
    try:
        run_id = aj_db.insert(
            "aj_council_runs", ts=aj_db.utc_now_iso(), cycle_id=cycle_id,
            symbol=dec.symbol, policy=None, status=dec.status,
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
    except Exception:
        log.exception("council persist failed (non-fatal)")
    # Audit hash chain — independent of the queryable copy.
    try:
        aj_db.audit("council_run",
                    {"decision": dec.to_audit(),
                     "reports": [r.to_audit() for r in reports]},
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

    budget = Budget(int(cfg.get("council_max_calls_per_cycle", 40) or 0))
    the_call = call or _make_call(cfg, budget, cycle_id)
    t0 = time.time()
    reports = _run_analysts(symbol, cfg, the_call)
    rating, conf, thesis, dissent = _consensus(reports)

    backed = [r for r in reports if r.confidence > 0.0]
    if not reports:
        status = "error"
    elif budget.hit_cap or len(backed) < len(reports):
        status = "degraded"
    else:
        status = "ok"

    plan = ResearchPlan(recommendation=rating, rationale=thesis)
    dec = CouncilDecision.from_plan(
        symbol, plan, confidence=conf,
        dissent=dissent, status=status, cost_usd=budget.cost_usd,
        latency_ms=int((time.time() - t0) * 1000), n_calls=budget.n_calls)
    _persist(dec, reports, cycle_id)
    _cache_put(symbol, cfg, dec)
    return dec
