"""aj_allocate.py — portfolio construction for the AJTA trading agent.

Sizes positions by *risk-aware allocation across the whole book*, not just
per-name in isolation. This is an OPTIONAL layer that sits beside the existing
per-name sizing path in aj_alpha: every public function FAILS OPEN to the SAFE
default ("no allocation opinion" == {} / 0.0) so that on any error the caller
keeps using its own per-name sizing untouched.

Surface:
  • target_weights(symbols, cfg)            -> {symbol: weight}  (Σ ≤ 1)
  • correlation_capped_weights(weights, cfg)-> {symbol: weight}  (renormalized)
  • allocation_notional(symbol, w, cfg)     -> float (USD order notional)
  • rebalance_plan(cfg)                      -> {sym: {current_w, target_w, drift}}

Allocation math is delegated to research_optimizer (risk_parity /
markowitz_optimize, both of which apply Ledoit-Wolf covariance shrinkage).
Sector data comes from fetcher.get_fundamentals(symbol)["sector"]. Correlation
reuses aj_alpha._correlation. Account equity comes from aj_alpha.account_equity.

GUARDRAILS: reads only, never trades; NEVER raises; weights are bounded and
renormalized; everything degrades to {} / 0.0.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

log = logging.getLogger("augur.aj_allocate")


# ── data wrappers (monkeypatchable in tests) ──────────────────────────────────

def _optimizer():
    """The research_optimizer module, or None if unavailable."""
    try:
        import research_optimizer
        return research_optimizer
    except Exception:
        log.debug("research_optimizer unavailable", exc_info=True)
        return None


def _sector_of(symbol: str) -> Optional[str]:
    """Fetched GICS-style sector for a symbol (fetcher.get_fundamentals), or
    None when unavailable. None makes the sector cap fail-open to no cap."""
    try:
        import fetcher
        fund = fetcher.get_fundamentals(symbol) or {}
        sec = fund.get("sector")
        sec = str(sec).strip() if sec is not None else ""
        return sec or None
    except Exception:
        log.debug("sector lookup failed for %s", symbol, exc_info=True)
        return None


def _closes(symbol: str, period: str = "6mo") -> List[float]:
    """Daily closes via aj_alpha's monkeypatchable wrapper."""
    try:
        import aj_alpha
        return list(aj_alpha._closes(symbol, period) or [])
    except Exception:
        return []


def _returns(closes: List[float]) -> List[float]:
    try:
        import aj_alpha
        return list(aj_alpha._returns(closes) or [])
    except Exception:
        out = []
        for i in range(1, len(closes)):
            if closes[i - 1]:
                out.append(closes[i] / closes[i - 1] - 1.0)
        return out


def _correlation(a: List[float], b: List[float]) -> Optional[float]:
    try:
        import aj_alpha
        return aj_alpha._correlation(a, b)
    except Exception:
        return None


import time as _time
import threading as _threading

_CYCLE_TTL_S = 90.0
_returns_lock = _threading.Lock()
_returns_memo: Dict[str, Any] = {}   # (symbol|period) -> (expires_at, returns)


def reset_cycle_cache() -> None:
    """Drop the per-cycle returns memo. Optional (the memo self-expires on a
    short TTL); call at a cycle boundary or in tests that re-patch _closes."""
    with _returns_lock:
        _returns_memo.clear()


def _cycle_returns(symbol: str, period: str = "6mo") -> List[float]:
    """Per-symbol daily returns over `period`, memoized per cycle (short TTL) so
    correlation_capped_weights doesn't refetch the same 6mo series on every call.
    Uses the LOCAL _closes/_returns wrappers (so module-level monkeypatching is
    honored) rather than aj_alpha's cached fn, which reads aj_alpha._closes — a
    DIFFERENT patch point. Fail-open: any error falls through to a fresh compute."""
    try:
        key = "{}|{}".format(str(symbol).upper(), period)
        now = _time.time()
        with _returns_lock:
            hit = _returns_memo.get(key)
            if hit and hit[0] > now:
                return hit[1]
        rets = _returns(_closes(symbol, period))
        if rets:
            with _returns_lock:
                _returns_memo[key] = (now + _CYCLE_TTL_S, rets)
        return rets
    except Exception:
        return _returns(_closes(symbol, period))


def _account_equity() -> float:
    try:
        import aj_alpha
        return float(aj_alpha.account_equity())
    except Exception:
        return 0.0


def _paper_book() -> Dict[str, Any]:
    try:
        import aj_positions
        return aj_positions.paper_book()
    except Exception:
        return {"positions": {}}


def _marks(symbols: List[str]) -> Dict[str, Optional[float]]:
    try:
        import aj_risk
        return aj_risk._marks(symbols) if symbols else {}
    except Exception:
        return {}


# ── small numeric helpers ─────────────────────────────────────────────────────

def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _renormalize(weights: Dict[str, float]) -> Dict[str, float]:
    """Drop non-positive/non-finite weights and scale the rest to sum to 1.
    Returns {} if nothing positive survives."""
    clean: Dict[str, float] = {}
    for k, v in weights.items():
        try:
            fv = float(v)
        except (TypeError, ValueError):
            continue
        if fv > 0 and fv == fv and fv not in (float("inf"), float("-inf")):
            clean[k] = fv
    s = sum(clean.values())
    if s <= 0:
        return {}
    return {k: v / s for k, v in clean.items()}


def _apply_position_cap(weights: Dict[str, float], cap: float) -> Dict[str, float]:
    """Iteratively cap each weight at `cap`, redistributing the excess
    proportionally onto names that still have headroom. The total is preserved
    at the (already-renormalized) input sum while there is headroom; when there
    is none (n*cap < 1) the excess is simply dropped, leaving a sum < 1 — which
    is the intended "≤ 1" behaviour (we never scale a capped name back ABOVE the
    cap, which a final renormalize would do). cap <= 0 or >= 1 → renorm-only."""
    if cap <= 0 or cap >= 1.0 or not weights:
        return _renormalize(weights)
    w = _renormalize(weights)
    if not w:
        return {}
    for _ in range(64):
        over = {k: v for k, v in w.items() if v > cap + 1e-12}
        if not over:
            break
        excess = sum(v - cap for v in over.values())
        for k in over:
            w[k] = cap
        room = {k: v for k, v in w.items() if v < cap - 1e-12}
        room_tot = sum(cap - v for v in room.values())
        if room_tot <= 1e-12:
            # No headroom anywhere: clip remaining names at the cap and drop the
            # excess (sum falls below 1, honouring the ≤ 1 contract).
            return {k: min(v, cap) for k, v in w.items()}
        # Cap how much we can actually absorb so we never lift a name past cap.
        absorbable = min(excess, room_tot)
        for k, v in room.items():
            w[k] = v + absorbable * ((cap - v) / room_tot)
    # Drop any residual non-positive entries but DO NOT renormalize up.
    return {k: v for k, v in w.items() if v > 0}


def _apply_sector_cap(weights: Dict[str, float], cap: float,
                      sectors: Dict[str, Optional[str]]) -> Dict[str, float]:
    """Cap aggregate weight per sector at `cap`. When a sector is over the cap,
    scale its members down proportionally and push the freed weight onto names
    in under-cap sectors (proportionally), then renormalize. Names with an
    unknown sector (None) are exempt from the cap. cap<=0 or >=1 → no-op."""
    if cap <= 0 or cap >= 1.0 or not weights:
        return _renormalize(weights)
    w = _renormalize(weights)
    if not w:
        return {}
    for _ in range(64):
        # aggregate by known sector
        sec_tot: Dict[str, float] = {}
        for k, v in w.items():
            sec = sectors.get(k)
            if sec is None:
                continue
            sec_tot[sec] = sec_tot.get(sec, 0.0) + v
        over = {s: t for s, t in sec_tot.items() if t > cap + 1e-12}
        if not over:
            break
        freed = 0.0
        for sec, tot in over.items():
            scale = cap / tot
            for k, v in list(w.items()):
                if sectors.get(k) == sec:
                    freed += v - v * scale
                    w[k] = v * scale

        # Redistribute freed weight onto recipient names whose SECTOR still has
        # headroom (cap − current sector total), so we never lift a recipient
        # sector past the cap — that would create a new violation and make the
        # loop oscillate. Unknown-sector names are exempt and absorb without
        # limit. Distribute per recipient sector up to that sector's headroom,
        # proportional to member weight; any freed weight that no sector can
        # absorb is dropped, leaving Σ < 1 (which the ≤ 1 contract allows).
        sec_now: Dict[Any, float] = {}
        sec_members: Dict[Any, List[str]] = {}
        for k, v in w.items():
            sec = sectors.get(k)
            if sec is not None and sec in over:
                continue  # don't feed weight back into an over-cap sector
            sec_now[sec] = sec_now.get(sec, 0.0) + v
            sec_members.setdefault(sec, []).append(k)
        # capacity each recipient sector can still take (∞ for unknown sector)
        capacity = {s: (float("inf") if s is None else max(0.0, cap - t))
                    for s, t in sec_now.items()}
        total_capacity = sum(c for c in capacity.values())
        if total_capacity <= 1e-12 or freed <= 1e-12:
            break  # nowhere to put the freed weight; leave sum < 1 (≤ 1 ok)
        placed = 0.0
        for sec, members in sec_members.items():
            cap_s = capacity[sec]
            member_tot = sum(w[k] for k in members)
            if cap_s == float("inf"):
                take = freed * (member_tot / sec_now[sec]) if sec_now[sec] else 0.0
            else:
                # share of freed proportional to this sector's spare capacity
                take = min(cap_s, freed * (cap_s / total_capacity))
            if member_tot <= 0 or take <= 0:
                continue
            for k in members:
                w[k] = w[k] + take * (w[k] / member_tot)
            placed += take
        if placed <= 1e-9:
            break  # couldn't place anything more; avoid oscillation
    # DO NOT renormalize up — that would scale a capped sector back over the
    # cap. Just drop non-positive residue; the sum is preserved or falls < 1.
    return {k: v for k, v in w.items() if v > 0}


# ── 1. target_weights ─────────────────────────────────────────────────────────

def target_weights(symbols: List[str], cfg: Dict[str, Any]) -> Dict[str, float]:
    """Risk-aware target weights for a candidate symbol list, summing to ≤ 1.

    Method from cfg["alloc_method"] (default "risk_parity"):
      • risk_parity → research_optimizer.risk_parity (equal risk contribution)
      • max_sharpe  → research_optimizer.markowitz_optimize(objective=max_sharpe,
                      with the max_position_weight passed as the per-name box)
      • equal       → 1/N

    Then applies cfg["max_position_weight"] (default 0.25) and a per-sector cap
    cfg["max_sector_weight"] (default 0.40). Fail-OPEN: returns {} on any
    failure so the caller falls back to its own per-name sizing.
    """
    try:
        syms = [str(s).upper() for s in (symbols or []) if s]
        # de-dup, preserve order
        seen: set = set()
        syms = [s for s in syms if not (s in seen or seen.add(s))]
        if not syms:
            return {}

        method = str(cfg.get("alloc_method", "risk_parity") or "risk_parity").lower()
        max_pos = float(cfg.get("max_position_weight", 0.25) or 0.0)
        max_sec = float(cfg.get("max_sector_weight", 0.40) or 0.0)

        raw: Dict[str, float] = {}
        if method == "equal":
            n = len(syms)
            raw = {s: 1.0 / n for s in syms}
        else:
            opt = _optimizer()
            if opt is None:
                return {}
            if method == "max_sharpe":
                cons: Dict[str, Any] = {"long_only": True}
                # Hand the per-name cap to the optimizer's box so the solution
                # itself respects it; we still re-cap below as a backstop.
                if 0.0 < max_pos < 1.0:
                    cons["max_weight"] = max_pos
                res = opt.markowitz_optimize(syms, objective="max_sharpe",
                                             constraints=cons)
            else:  # risk_parity (default) and any unknown method
                res = opt.risk_parity(syms)
            if not isinstance(res, dict) or res.get("error") or "weights" not in res:
                return {}
            weights = res.get("weights") or {}
            raw = {str(k).upper(): float(v) for k, v in weights.items()
                   if v is not None}

        capped = _apply_position_cap(raw, max_pos) if max_pos > 0 else _renormalize(raw)
        if not capped:
            return {}

        # per-sector cap — fail-open to no cap when sectors can't be resolved
        if 0.0 < max_sec < 1.0:
            sectors = {s: _sector_of(s) for s in capped}
            if any(v is not None for v in sectors.values()):
                capped = _apply_sector_cap(capped, max_sec, sectors)
                # Re-apply the position cap ONLY when it's a real cap (0<cap<1):
                # sector redistribution can lift a single name back over the
                # per-name cap. We must NOT call it with cap>=1 here, as that
                # path renormalizes up and would undo the sector cap we just set.
                if 0.0 < max_pos < 1.0:
                    capped = _apply_position_cap(capped, max_pos)

        # Round for cleanliness and drop non-positive residue. We deliberately
        # do NOT renormalize back up to 1 here: when a cap leaves the sum < 1
        # (n*cap < 1, or a sector pinned with no recipients) renormalizing would
        # push capped names back ABOVE their cap. The contract is Σ ≤ 1.
        capped = {k: round(v, 6) for k, v in capped.items() if v > 0}
        # Guard against floating-point overshoot just above 1.0 → scale to 1.
        s = sum(capped.values())
        if s > 1.0 + 1e-9:
            capped = {k: v / s for k, v in capped.items()}
        return capped or {}
    except Exception:
        log.exception("target_weights failed -> {} (caller keeps own sizing)")
        return {}


# ── 2. correlation_capped_weights ─────────────────────────────────────────────

def correlation_capped_weights(weights: Dict[str, float],
                               cfg: Dict[str, Any]) -> Dict[str, float]:
    """Down-weight names that sit inside a highly-correlated cluster so the book
    isn't secretly concentrated, then renormalize. Each name is throttled by a
    BOUNDED factor in [floor, 1.0] based on its average positive correlation to
    the others above a threshold. Gated by cfg["correlation_cap"] (default True
    within allocation). Fail-OPEN: returns the input (renormalized) on error or
    when disabled."""
    try:
        w = _renormalize(weights or {})
        if not w:
            return {}
        if not cfg.get("correlation_cap", True):
            return w
        if len(w) < 2:
            return w

        thr = _clamp(float(cfg.get("correlation_cap_threshold", 0.6) or 0.6), 0.0, 1.0)
        floor = _clamp(float(cfg.get("correlation_cap_floor", 0.5) or 0.5), 0.05, 1.0)

        syms = list(w.keys())
        # Reuse aj_alpha's per-cycle returns memo so each name's 6mo series is
        # fetched once per cycle and shared with the aj_alpha correlation gates,
        # rather than refetched on every correlation_capped_weights call.
        rets = {s: _cycle_returns(s, "6mo") for s in syms}

        factors: Dict[str, float] = {}
        for s in syms:
            corrs = []
            for o in syms:
                if o == s:
                    continue
                c = _correlation(rets.get(s) or [], rets.get(o) or [])
                if c is not None:
                    corrs.append(max(0.0, c))   # only positive corr adds risk
            if not corrs:
                factors[s] = 1.0
                continue
            avg_c = sum(corrs) / len(corrs)
            if avg_c <= thr:
                factors[s] = 1.0
            else:
                span = max(1e-6, 1.0 - thr)
                frac = _clamp((avg_c - thr) / span, 0.0, 1.0)
                factors[s] = _clamp(1.0 - frac * (1.0 - floor), floor, 1.0)

        adjusted = {s: w[s] * factors[s] for s in syms}
        return _renormalize(adjusted) or w
    except Exception:
        log.exception("correlation_capped_weights failed -> renormalized input")
        return _renormalize(weights or {})


# ── 3. allocation_notional ────────────────────────────────────────────────────

def allocation_notional(symbol: str, target_weight: float,
                        cfg: Dict[str, Any]) -> float:
    """Convert a target weight to an order notional in USD = account_equity ×
    target_weight, clamped to cfg["max_order_notional_usd"] when set. Returns
    0.0 on any error or a non-positive/invalid weight."""
    try:
        w = float(target_weight)
        if not (w > 0) or w != w or w in (float("inf"), float("-inf")):
            return 0.0
        w = min(w, 1.0)
        equity = _account_equity()
        if not (equity > 0):
            return 0.0
        notional = equity * w
        cap_raw = cfg.get("max_order_notional_usd")
        if cap_raw is not None:
            try:
                cap = float(cap_raw)
                if cap > 0:
                    notional = min(notional, cap)
            except (TypeError, ValueError):
                pass
        return round(max(0.0, notional), 2)
    except Exception:
        log.exception("allocation_notional failed -> 0.0")
        return 0.0


# ── 4. rebalance_plan ─────────────────────────────────────────────────────────

def rebalance_plan(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Compare current book weights (from the paper book + live marks) to
    target_weights(current symbols) for visibility / UI. Returns
    {symbol: {current_w, target_w, drift}}. Best-effort: never raises, returns
    {} on any failure."""
    try:
        positions = (_paper_book().get("positions") or {})
        syms = [str(s).upper() for s in positions.keys()]
        if not syms:
            return {}
        marks = _marks(syms)

        # current market-value weights
        mv: Dict[str, float] = {}
        for s in syms:
            p = positions.get(s) or {}
            mk = marks.get(s)
            px = mk if mk is not None else float(p.get("avg_cost") or 0)
            mv[s] = float(p.get("qty") or 0) * float(px or 0)
        tot = sum(v for v in mv.values() if v > 0)
        cur_w = {s: (v / tot if tot > 0 else 0.0) for s, v in mv.items()}

        tgt = target_weights(syms, cfg)  # {} on optimizer failure

        out: Dict[str, Any] = {}
        for s in syms:
            cw = round(cur_w.get(s, 0.0), 6)
            tw = round(float(tgt.get(s, 0.0)), 6)
            out[s] = {"current_w": cw, "target_w": tw,
                      "drift": round(tw - cw, 6)}
        return out
    except Exception:
        log.exception("rebalance_plan failed -> {}")
        return {}


__all__ = [
    "target_weights",
    "correlation_capped_weights",
    "allocation_notional",
    "rebalance_plan",
]
