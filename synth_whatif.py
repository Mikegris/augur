"""
synth_whatif.py — Portfolio What-If synthesis engine for AUGUR.

Answers the question "what happens if I add (or remove, or resize) a
position in my portfolio?" by synthesizing the impact across five
independent research lenses and presenting the deltas in a single bundle:

  1. Factor exposure shifts (research_factors.portfolio_factor_exposure):
     how Fama-French + momentum loadings move when the proposed trade is
     applied. Useful for catching "I thought I was diversifying but I
     actually doubled my value tilt" mistakes.

  2. Historical stress-test losses (fetcher.get_portfolio_stress_test):
     re-prices the portfolio under 2008 / COVID / dot-com / rate-hike
     drawdowns and reports the per-scenario loss shift in absolute % terms.

  3. Monte Carlo NAV cones (research_montecarlo.simulate_portfolio):
     2000 paths × 365 trading days; reports terminal NAV percentile shifts
     (p05 / median / p95) and the change in probability-of-loss.

  4. Optimizer verdict (research_optimizer.markowitz_optimize, max_sharpe):
     compares the user's proposed candidate weight against the max-Sharpe
     optimal weight. A position more than 50% above or below the optimum
     gets a verdict of OVERSIZED / UNDERSIZED so the user knows whether
     they're betting on edge versus diversification.

  5. ML-forecast-weighted expected return (ml_forecast.ml_forecast):
     pulls the composite_ml_score for each holding, normalizes around 0.5,
     weights by market value, and reports the portfolio-level expected
     return shift. This is a fast proxy for "does the trade actually
     improve my ML edge?".

Each sub-call is wrapped in try/except — any module failure produces a
None value with a short explanation string, leaving the rest of the
bundle intact. The final synthesised dict is cached in cache_store for
15 minutes keyed on (holdings_hash, candidate_hash).

Python 3.9 compatible (no PEP 604 unions, no `match` statements).
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import logging
import math
import time
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("augur.synth.whatif")

# ─── optional integrations ───────────────────────────────────────────────────
# All best-effort. Missing modules fall through to per-section error strings
# so the engine can be exercised even with a partial app stack.
try:
    import cache_store  # type: ignore
except Exception:  # pragma: no cover
    cache_store = None  # type: ignore

try:
    import research_factors  # type: ignore
except Exception as _e:
    research_factors = None  # type: ignore
    log.warning("research_factors unavailable: %s", _e)

try:
    import research_montecarlo  # type: ignore
except Exception as _e:
    research_montecarlo = None  # type: ignore
    log.warning("research_montecarlo unavailable: %s", _e)

try:
    import research_optimizer  # type: ignore
except Exception as _e:
    research_optimizer = None  # type: ignore
    log.warning("research_optimizer unavailable: %s", _e)

try:
    import fetcher  # type: ignore
except Exception as _e:
    fetcher = None  # type: ignore
    log.warning("fetcher unavailable: %s", _e)

try:
    import ml_forecast  # type: ignore
except Exception as _e:
    ml_forecast = None  # type: ignore
    log.warning("ml_forecast unavailable: %s", _e)


_CACHE_TTL = 15 * 60  # 15 minutes — long enough to absorb panel flicker
                      # but short enough to feel "live" if the user is
                      # iterating on candidates.

_RF_RATE = 0.045      # match research_optimizer.DEFAULT_RF for sharpe deltas.
_DEFAULT_VOL_PCT = 0.0  # safe fallback when MC fails

# Map raw scenario keys from fetcher._SCENARIOS to short slugs in the response.
_SCENARIO_SLUGS = {
    "2008 Financial Crisis": "2008",
    "2020 COVID Crash": "covid",
    "2022 Rate Hike Bear": "rate_hike",
    "2000 Dot-com Bust": "dot_com",
    "Custom Scenario": "custom",
}


# ─── public API ──────────────────────────────────────────────────────────────

def whatif(
    current_holdings: List[Dict[str, Any]],
    candidate: Dict[str, Any],
) -> Dict[str, Any]:
    """Run all five lenses on (current vs proposed) and return synthesised deltas.

    Parameters
    ----------
    current_holdings : list of {"symbol": str, "market_value": number, ...}
        Optional extra keys (shares, avg_cost, asset_type) are passed through
        to stress-test, which uses them for sector/beta enrichment.
    candidate : {"symbol": str, "market_value": number, "action": str}
        action ∈ {"add", "remove", "resize_to"}. "add" increments the
        position; "remove" zeros it out (market_value is ignored); "resize_to"
        sets the position to exactly the supplied market value.

    Returns
    -------
    dict — see module-level docstring + INTEGRATE_SYNTH_WHATIF.md for the
    full shape. Sections that fail return None with a sibling "*_error"
    field; the rest of the bundle still populates.
    """
    current_clean = _clean_holdings(current_holdings)
    cand = _clean_candidate(candidate)
    if cand is None:
        return {
            "error": "invalid candidate (need symbol, market_value, action)",
            "as_of": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }

    proposed_clean = _apply_candidate(current_clean, cand)

    cache_key = _make_cache_key(current_clean, cand)

    def _do() -> Dict[str, Any]:
        return _whatif_uncached(current_clean, proposed_clean, cand)

    if cache_store is not None:
        try:
            return cache_store.coalesce(cache_key, _CACHE_TTL, _do)
        except Exception:
            log.exception("cache_store.coalesce failed; running uncached")
    return _do()


# ─── orchestrator ────────────────────────────────────────────────────────────

def _whatif_uncached(
    current: List[Dict[str, Any]],
    proposed: List[Dict[str, Any]],
    cand: Dict[str, Any],
) -> Dict[str, Any]:
    t0 = time.time()

    # ── Section 1 + 2: factor exposure + ML-weighted return for both books.
    cur_factor, cur_factor_err = _portfolio_factors(current)
    new_factor, new_factor_err = _portfolio_factors(proposed)

    cur_ml, cur_ml_err = _ml_weighted_return(current)
    new_ml, new_ml_err = _ml_weighted_return(proposed)

    # ── Section 3: stress test (real $ losses + % shifts).
    cur_stress, cur_stress_err = _stress_test(current)
    new_stress, new_stress_err = _stress_test(proposed)

    # ── Section 4: Monte Carlo. The fixed 2000 paths × 365 days is a deliberate
    # compromise — anything heavier and a single what-if would dominate request
    # latency, but 2000 is enough to give stable percentile estimates at the
    # outer p05/p95 cones.
    cur_mc, cur_mc_err = _monte_carlo(current)
    new_mc, new_mc_err = _monte_carlo(proposed)

    # ── Section 5: optimizer verdict on the candidate's symbol.
    opt_block, opt_err = _optimizer_verdict(current, cand, proposed)

    # Aggregate.
    cur_nav = sum(h["market_value"] for h in current)
    new_nav = sum(h["market_value"] for h in proposed)

    cur_view = _build_view(current, cur_factor, cur_stress, cur_mc, cur_nav)
    new_view = _build_view(proposed, new_factor, new_stress, new_mc, new_nav)

    # Sharpe ratio delta uses the optimizer's expected_return / expected_vol
    # convention (annualised, decimal). Falls back to None if either side is
    # missing.
    sharpe_delta: Optional[float] = None
    try:
        if cur_view["expected_return_pct"] is not None and cur_view["expected_vol_pct"]:
            cur_s = ((cur_view["expected_return_pct"] - _RF_RATE * 100.0)
                     / cur_view["expected_vol_pct"])
        else:
            cur_s = None
        if new_view["expected_return_pct"] is not None and new_view["expected_vol_pct"]:
            new_s = ((new_view["expected_return_pct"] - _RF_RATE * 100.0)
                     / new_view["expected_vol_pct"])
        else:
            new_s = None
        if cur_s is not None and new_s is not None:
            sharpe_delta = round(new_s - cur_s, 4)
    except Exception:
        sharpe_delta = None

    # Factor exposure shift: list of {factor, shift} sorted by abs shift desc.
    factor_shifts = _factor_shifts(cur_view["factor_exposure"],
                                   new_view["factor_exposure"])

    # Scenario loss shift: per-scenario percentage-point delta. Positive
    # means the proposed book loses MORE in that scenario (worse).
    scenario_shifts = _scenario_shifts(cur_view["scenario_losses"],
                                       new_view["scenario_losses"])

    # ML-weighted return delta.
    if cur_ml is not None and new_ml is not None:
        ml_delta = round(new_ml - cur_ml, 4)
    else:
        ml_delta = None

    # Monte Carlo terminal NAV percentile shifts.
    mc_delta = _monte_carlo_delta(cur_mc, new_mc)

    expected_return_delta = _safe_sub(
        new_view["expected_return_pct"], cur_view["expected_return_pct"]
    )
    expected_vol_delta = _safe_sub(
        new_view["expected_vol_pct"], cur_view["expected_vol_pct"]
    )
    hhi_delta = _safe_sub(
        new_view["concentration_hhi"], cur_view["concentration_hhi"]
    )

    delta = {
        "expected_return_pct": _round(expected_return_delta, 4),
        "expected_vol_pct": _round(expected_vol_delta, 4),
        "sharpe_ratio_delta": sharpe_delta,
        "concentration_hhi": _round(hhi_delta, 4),
        "factor_exposure_shifts": factor_shifts,
        "scenario_loss_shifts": scenario_shifts,
        "ml_forecast_weighted_return_delta_pct": ml_delta,
        "nav_delta": _round(new_nav - cur_nav, 2),
    }

    # Per-section error breadcrumbs so the UI can show which lens failed
    # without polluting the main delta dict.
    errors: Dict[str, str] = {}
    for k, v in [
        ("current_factors", cur_factor_err),
        ("proposed_factors", new_factor_err),
        ("current_stress", cur_stress_err),
        ("proposed_stress", new_stress_err),
        ("current_monte_carlo", cur_mc_err),
        ("proposed_monte_carlo", new_mc_err),
        ("current_ml_forecast", cur_ml_err),
        ("proposed_ml_forecast", new_ml_err),
        ("optimizer", opt_err),
    ]:
        if v:
            errors[k] = v

    elapsed_ms = int((time.time() - t0) * 1000)
    return {
        "candidate": cand,
        "current": cur_view,
        "proposed": new_view,
        "delta": delta,
        "optimizer_says": opt_block,
        "monte_carlo_delta": mc_delta,
        "errors": errors or None,
        "elapsed_ms": elapsed_ms,
        "as_of": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


# ─── view builder ────────────────────────────────────────────────────────────

def _build_view(
    holdings: List[Dict[str, Any]],
    factor_result: Optional[Dict[str, Any]],
    stress_result: Optional[Dict[str, Any]],
    mc_result: Optional[Dict[str, Any]],
    nav: float,
) -> Dict[str, Any]:
    """Synthesise the per-side block: NAV, factor exposure dict, expected
    return / vol from MC, scenario losses, HHI concentration, top-5 weights."""
    # Factor exposure dict {factor → beta}
    factor_exposure: Dict[str, float] = {}
    if factor_result and isinstance(factor_result, dict):
        exposures = factor_result.get("exposures") or {}
        for k, v in exposures.items():
            if isinstance(v, dict) and v.get("beta") is not None:
                factor_exposure[k] = float(v["beta"])
            elif v is not None:
                try:
                    factor_exposure[k] = float(v)
                except (TypeError, ValueError):
                    pass
        # Fallback to weighted_exposures if direct regression was empty.
        if not factor_exposure:
            w = factor_result.get("weighted_exposures") or {}
            wexp = w.get("exposures") or {}
            for k, v in wexp.items():
                try:
                    factor_exposure[k] = float(v)
                except (TypeError, ValueError):
                    pass

    # Expected return / vol from Monte Carlo terminal distribution.
    expected_return_pct: Optional[float] = None
    expected_vol_pct: Optional[float] = None
    if mc_result and isinstance(mc_result, dict):
        td = mc_result.get("terminal_distribution") or {}
        initial = mc_result.get("initial_nav") or nav
        mean = td.get("mean")
        std = td.get("std")
        if initial and mean is not None:
            expected_return_pct = round((float(mean) / float(initial) - 1.0) * 100.0, 4)
        if initial and std is not None:
            # Terminal stdev as % of initial NAV — approximates 1-year vol since
            # horizon is 365 days. Good enough as a Sharpe-ratio denominator.
            expected_vol_pct = round((float(std) / float(initial)) * 100.0, 4)

    # Scenario losses (% of current NAV) keyed by short slug.
    scenario_losses: Dict[str, Optional[float]] = {}
    if stress_result and isinstance(stress_result, dict):
        scenarios = stress_result.get("scenarios") or {}
        for full_name, slug in _SCENARIO_SLUGS.items():
            if slug == "custom":
                continue  # skip the user-defined "Custom Scenario"
            entry = scenarios.get(full_name)
            if entry and entry.get("total_loss_pct") is not None:
                scenario_losses[slug] = float(entry["total_loss_pct"])
            else:
                scenario_losses[slug] = None

    # Concentration: Herfindahl-Hirschman index of weights (0 = perfect spread,
    # 1 = single holding). Useful diversification proxy.
    weights = _weights(holdings)
    hhi = sum(w * w for w in weights.values()) if weights else 0.0
    top_5 = sorted(weights.items(), key=lambda kv: kv[1], reverse=True)[:5]
    top_5_list = [{"symbol": s, "weight": round(w, 6)} for s, w in top_5]

    return {
        "nav": round(nav, 2),
        "n_holdings": len(holdings),
        "factor_exposure": factor_exposure or None,
        "expected_return_pct": expected_return_pct,
        "expected_vol_pct": expected_vol_pct,
        "scenario_losses": scenario_losses or None,
        "concentration_hhi": round(hhi, 4) if weights else 0.0,
        "top_5_weights": top_5_list,
    }


# ─── per-section wrappers (all try/except) ───────────────────────────────────

def _portfolio_factors(
    holdings: List[Dict[str, Any]],
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    if not holdings:
        return None, "no holdings"
    if research_factors is None:
        return None, "research_factors module unavailable"
    try:
        # 3y window — same default that the existing factor route uses.
        result = research_factors.portfolio_factor_exposure(holdings, period_years=3)
        if isinstance(result, dict) and result.get("error"):
            return None, str(result["error"])
        return result, None
    except Exception as e:
        log.warning("portfolio_factor_exposure failed: %s", e)
        return None, "factor regression error: " + str(e)


def _stress_test(
    holdings: List[Dict[str, Any]],
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    if not holdings:
        return None, "no holdings"
    if fetcher is None or not hasattr(fetcher, "get_portfolio_stress_test"):
        return None, "fetcher.get_portfolio_stress_test unavailable"
    try:
        # get_portfolio_stress_test expects the keys it would get out of
        # db.get_portfolio + enrichment: symbol, shares, avg_cost,
        # market_value, asset_type. We synthesize the missing ones from
        # market_value so the API still works for ad-hoc holdings lists.
        enriched = []
        for h in holdings:
            row = dict(h)
            row.setdefault("asset_type", "stock")
            row.setdefault("shares", 1.0)
            row.setdefault("avg_cost", row.get("market_value", 0.0))
            enriched.append(row)
        return fetcher.get_portfolio_stress_test(enriched), None
    except Exception as e:
        log.warning("stress test failed: %s", e)
        return None, "stress test error: " + str(e)


def _monte_carlo(
    holdings: List[Dict[str, Any]],
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    if not holdings:
        return None, "no holdings"
    if research_montecarlo is None:
        return None, "research_montecarlo module unavailable"
    try:
        sim = research_montecarlo.simulate_portfolio(
            holdings, n_paths=2000, horizon_days=365,
            method="historical_bootstrap",
        )
        if not isinstance(sim, dict):
            return None, "MC returned non-dict"
        # An "all-zero" MC payload is a known degenerate failure mode — every
        # holding had no price history. Surface it as an error so the UI can
        # show "MC unavailable" rather than a misleading zero band.
        td = sim.get("terminal_distribution") or {}
        if not td or (td.get("mean", 0.0) == 0.0 and td.get("std", 0.0) == 0.0):
            return None, "MC produced empty terminal distribution"
        return sim, None
    except Exception as e:
        log.warning("monte carlo failed: %s", e)
        return None, "MC error: " + str(e)


def _ml_weighted_return(
    holdings: List[Dict[str, Any]],
) -> Tuple[Optional[float], Optional[str]]:
    """Pull ml_forecast.composite_ml_score (or rf_classifier.prob_up_20d) for
    each holding, map to an expected-return-like percentage, weight by market
    value. Returns the value in % (already annualised by the ml_forecast
    model's RF target horizon convention)."""
    if not holdings:
        return None, "no holdings"
    if ml_forecast is None or not hasattr(ml_forecast, "ml_forecast"):
        return None, "ml_forecast module unavailable"

    total_mv = sum(h["market_value"] for h in holdings)
    if total_mv <= 0:
        return None, "zero NAV"

    weighted = 0.0
    coverage = 0.0
    for h in holdings:
        sym = h["symbol"]
        w = h["market_value"] / total_mv
        try:
            res = ml_forecast.ml_forecast(sym)
        except Exception as e:
            log.debug("ml_forecast(%s) failed: %s", sym, e)
            continue
        if not isinstance(res, dict) or res.get("error"):
            continue
        # Prefer composite, fall back to RF classifier probability.
        score = res.get("composite_ml_score")
        if score is None:
            rf = res.get("rf_classifier") or {}
            score = rf.get("prob_up_20d") if isinstance(rf, dict) else None
        if score is None:
            continue
        # Map [0, 1] probability → expected annualised return. A coin-flip
        # (0.5) → 0% expected return, a max-confidence bull (1.0) → +20%,
        # max-confidence bear (0.0) → -20%. Linear so it's easy to reason
        # about. 20% matches the ~5-yr equity premium ceiling we expect any
        # individual position-level ML signal to actually produce.
        expected_pct = (float(score) - 0.5) * 2.0 * 20.0
        weighted += w * expected_pct
        coverage += w

    if coverage <= 0:
        return None, "no ML coverage"
    # Renormalize so missing holdings don't artificially shrink the value
    # toward zero — same convention the factor module uses.
    return round(weighted / coverage, 4), None


def _optimizer_verdict(
    current: List[Dict[str, Any]],
    cand: Dict[str, Any],
    proposed: List[Dict[str, Any]],
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Run max-Sharpe Markowitz on the union of current + candidate symbols
    and extract the optimal weight for the candidate. Verdict logic:
      |proposed - optimal| / max(optimal, 0.01) ≤ 0.5  → OK
      proposed > 1.5 * optimal                          → OVERSIZED
      proposed < 0.5 * optimal                          → UNDERSIZED
    Candidate symbols at 0% optimal weight are flagged AVOID."""
    if research_optimizer is None:
        return None, "research_optimizer module unavailable"
    if not cand or not cand.get("symbol"):
        return None, "no candidate"

    cand_sym = cand["symbol"].upper()
    symbols = list({h["symbol"] for h in current} | {cand_sym})
    if not symbols:
        return None, "no symbols"

    try:
        res = research_optimizer.markowitz_optimize(
            symbols, objective="max_sharpe",
            constraints={"long_only": True, "min_weight": 0.0, "max_weight": 1.0,
                         "period": "2y", "risk_free_rate": _RF_RATE},
        )
    except Exception as e:
        log.warning("markowitz_optimize failed: %s", e)
        return None, "optimizer error: " + str(e)

    if not isinstance(res, dict) or res.get("error"):
        msg = (res or {}).get("error", "optimizer returned no weights")
        return None, str(msg)

    weights = res.get("weights") or {}
    optimal = float(weights.get(cand_sym, 0.0))

    proposed_nav = sum(h["market_value"] for h in proposed) or 1.0
    cand_mv = next((h["market_value"] for h in proposed if h["symbol"] == cand_sym), 0.0)
    proposed_size = cand_mv / proposed_nav

    # Verdict band.
    if optimal <= 0.001 and proposed_size > 0.001:
        verdict = "AVOID"
    elif optimal <= 0.001 and proposed_size <= 0.001:
        verdict = "OK"  # both essentially zero
    else:
        ratio = proposed_size / optimal if optimal > 0 else float("inf")
        if 0.5 <= ratio <= 1.5:
            verdict = "OK"
        elif ratio > 1.5:
            verdict = "OVERSIZED"
        else:
            verdict = "UNDERSIZED"

    return {
        "candidate_symbol": cand_sym,
        "current_optimal_size_pct": round(optimal, 4),
        "your_proposed_size_pct": round(proposed_size, 4),
        "verdict": verdict,
        "optimizer_sharpe": res.get("sharpe"),
        "optimizer_expected_return_pct": res.get("expected_return_pct"),
        "optimizer_expected_vol_pct": res.get("expected_vol_pct"),
        "optimizer_converged": res.get("converged"),
    }, None


# ─── delta helpers ───────────────────────────────────────────────────────────

def _factor_shifts(
    cur_factors: Optional[Dict[str, float]],
    new_factors: Optional[Dict[str, float]],
) -> Optional[List[Dict[str, Any]]]:
    if not cur_factors or not new_factors:
        return None
    out: List[Dict[str, Any]] = []
    for k in set(cur_factors.keys()) | set(new_factors.keys()):
        c = float(cur_factors.get(k, 0.0))
        n = float(new_factors.get(k, 0.0))
        out.append({"factor": k, "current": round(c, 4), "proposed": round(n, 4),
                    "shift": round(n - c, 4)})
    # Sort by |shift| desc so the most-impactful factor is up top.
    out.sort(key=lambda d: abs(d["shift"]), reverse=True)
    return out


def _scenario_shifts(
    cur_losses: Optional[Dict[str, Optional[float]]],
    new_losses: Optional[Dict[str, Optional[float]]],
) -> Optional[Dict[str, Optional[float]]]:
    if not cur_losses or not new_losses:
        return None
    out: Dict[str, Optional[float]] = {}
    for k in set(cur_losses.keys()) | set(new_losses.keys()):
        c = cur_losses.get(k)
        n = new_losses.get(k)
        if c is None or n is None:
            out[k] = None
        else:
            # Positive shift = the proposed book loses MORE (worse).
            # Losses are already negative percentages, so subtract for delta.
            out[k] = round(float(n) - float(c), 4)
    return out


def _monte_carlo_delta(
    cur_mc: Optional[Dict[str, Any]],
    new_mc: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Terminal-NAV percentile shifts (in $) + change in probability-of-loss."""
    if not cur_mc or not new_mc:
        return {"p05_shift": None, "median_shift": None, "p95_shift": None,
                "prob_loss_delta_pct": None}
    cur_td = cur_mc.get("terminal_distribution") or {}
    new_td = new_mc.get("terminal_distribution") or {}
    p05_shift = _safe_sub(new_td.get("p05"), cur_td.get("p05"))
    median_shift = _safe_sub(new_td.get("p50"), cur_td.get("p50"))
    p95_shift = _safe_sub(new_td.get("p95"), cur_td.get("p95"))
    cur_summary = cur_mc.get("summary") or {}
    new_summary = new_mc.get("summary") or {}
    cur_pl = cur_summary.get("prob_loss")
    new_pl = new_summary.get("prob_loss")
    if cur_pl is None or new_pl is None:
        prob_loss_delta = None
    else:
        # Convention: positive = book is now MORE likely to end down. Expressed
        # in percentage points (i.e. raw probability * 100).
        prob_loss_delta = round((float(new_pl) - float(cur_pl)) * 100.0, 4)
    return {
        "p05_shift": _round(p05_shift, 2),
        "median_shift": _round(median_shift, 2),
        "p95_shift": _round(p95_shift, 2),
        "prob_loss_delta_pct": prob_loss_delta,
    }


# ─── lifecycle helpers ───────────────────────────────────────────────────────

def _clean_holdings(holdings: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if not isinstance(holdings, list):
        return out
    for h in holdings:
        if not isinstance(h, dict):
            continue
        sym = h.get("symbol") or h.get("ticker")
        if not sym:
            continue
        try:
            mv = float(h.get("market_value") or 0.0)
        except (TypeError, ValueError):
            continue
        if mv <= 0:
            continue
        row = {"symbol": str(sym).upper(), "market_value": mv}
        # Pass-through metadata that downstream calls might use.
        for k in ("shares", "avg_cost", "asset_type", "sector"):
            if k in h:
                row[k] = h[k]
        out.append(row)
    # Combine duplicate symbols defensively.
    combined: Dict[str, Dict[str, Any]] = {}
    for r in out:
        s = r["symbol"]
        if s in combined:
            combined[s]["market_value"] += r["market_value"]
        else:
            combined[s] = dict(r)
    return list(combined.values())


def _clean_candidate(candidate: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not isinstance(candidate, dict):
        return None
    sym = candidate.get("symbol") or candidate.get("ticker")
    if not sym:
        return None
    action = str(candidate.get("action") or "add").lower()
    if action not in ("add", "remove", "resize_to"):
        action = "add"
    try:
        mv = float(candidate.get("market_value") or 0.0)
    except (TypeError, ValueError):
        return None
    if action != "remove" and mv <= 0:
        return None
    return {"symbol": str(sym).upper(), "market_value": round(mv, 2), "action": action}


def _apply_candidate(
    current: List[Dict[str, Any]],
    cand: Dict[str, Any],
) -> List[Dict[str, Any]]:
    sym = cand["symbol"]
    action = cand["action"]
    mv = cand["market_value"]
    existing_idx = next((i for i, h in enumerate(current) if h["symbol"] == sym), None)
    proposed = [dict(h) for h in current]  # deep-ish copy
    if action == "add":
        if existing_idx is None:
            proposed.append({"symbol": sym, "market_value": mv})
        else:
            proposed[existing_idx]["market_value"] += mv
    elif action == "remove":
        if existing_idx is not None:
            proposed.pop(existing_idx)
    elif action == "resize_to":
        if existing_idx is None:
            if mv > 0:
                proposed.append({"symbol": sym, "market_value": mv})
        else:
            if mv > 0:
                proposed[existing_idx]["market_value"] = mv
            else:
                proposed.pop(existing_idx)
    return proposed


def _weights(holdings: List[Dict[str, Any]]) -> Dict[str, float]:
    total = sum(h["market_value"] for h in holdings)
    if total <= 0:
        return {}
    return {h["symbol"]: h["market_value"] / total for h in holdings}


def _make_cache_key(
    current: List[Dict[str, Any]], cand: Dict[str, Any],
) -> Tuple[str, str, str]:
    """Stable hash on holdings (symbol, rounded MV) and candidate triple."""
    h1 = hashlib.sha1()
    for h in sorted(current, key=lambda x: x["symbol"]):
        h1.update(("%s|%.2f;" % (h["symbol"], h["market_value"])).encode("utf-8"))
    h2 = hashlib.sha1()
    h2.update(("%s|%.2f|%s" % (cand["symbol"], cand["market_value"], cand["action"])).encode("utf-8"))
    return ("whatif", h1.hexdigest()[:16], h2.hexdigest()[:16])


def _safe_sub(a: Any, b: Any) -> Optional[float]:
    if a is None or b is None:
        return None
    try:
        return float(a) - float(b)
    except (TypeError, ValueError):
        return None


def _round(v: Optional[float], digits: int) -> Optional[float]:
    if v is None:
        return None
    try:
        if math.isnan(float(v)) or math.isinf(float(v)):
            return None
        return round(float(v), digits)
    except (TypeError, ValueError):
        return None


# ─── self-test (only when executed as a script) ──────────────────────────────

if __name__ == "__main__":  # pragma: no cover
    import json
    logging.basicConfig(level=logging.INFO)
    result = whatif(
        [{"symbol": "AAPL", "market_value": 50000},
         {"symbol": "SPY",  "market_value": 50000}],
        {"symbol": "TLT", "market_value": 20000, "action": "add"},
    )
    print(json.dumps(result, indent=2, default=str))


__all__ = ["whatif"]
