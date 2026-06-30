"""
synth_bayessmart.py — Bayesian Smart-Money Composite (self-improving).

A sibling of `smart_money.compute_score` that:
  1. Calls the existing Smart-Money scorer to obtain the static score and its
     7 component sub-scores (insider, institutional, earnings, options,
     momentum, sec, ml).
  2. For each component, queries `research_tracker.get_track_record(...)` for
     a per-component signal name (e.g. `smart_money_insider_activity`). If
     enough scored history exists (n >= MIN_N), the static component weight
     is Bayes-updated against a Beta(20, 20) prior so good signals get more
     weight and bad signals shrink.
  3. Recomputes a composite (`bayes_score`) using the posterior weights.
  4. Returns rich per-component breakdown + a `weight_shift_summary` of the
     top-3 biggest weight changes.

The module is intentionally read-only with respect to every other file —
it depends on `smart_money.compute_score` for the raw components and on
`research_tracker.get_track_record` for the dynamic weights. If either is
missing or any component is absent, the function degrades gracefully and
falls back to the static score.

Python 3.9 compatible. Cached via `cache_store.coalesce(("bayessmart", sym),
600, fn)` so the panel is cheap to re-open.
"""

from __future__ import annotations

import logging
import math
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("augur.bayessmart")

# ── Bayesian constants ─────────────────────────────────────────────────────
# Beta(α=20, β=20) — symmetric prior centred at 0.5 with sample-equivalent of
# 40 observations. Strong enough that 5 lucky calls don't dominate, weak
# enough that 100 real calls clearly move the posterior.
PRIOR_ALPHA = 20.0
PRIOR_BETA = 20.0
PRIOR_MEAN = PRIOR_ALPHA / (PRIOR_ALPHA + PRIOR_BETA)  # 0.5
# Minimum scored, directional calls before we trust hit-rate enough to
# reweight a component. Below this threshold the prior dominates and the
# multiplier is effectively 1.0 anyway, but we skip the math to avoid
# noise-tracking with a handful of points.
MIN_N = 20
# Exponent on the posterior_mean/0.5 ratio — controls how aggressively
# proven components are upweighted. 1.5 means a 60% hit-rate -> ~1.31x
# weight, 70% -> ~1.66x, 40% -> ~0.76x, 30% -> ~0.55x.
WEIGHT_EXPONENT = 1.5
# Component signal-name prefix in the tracker DB.
SIGNAL_PREFIX = "smart_money_"

# Static dimension weights — taken verbatim from `smart_money.py`'s scoring
# bands ("Insider Activity 0-20", etc). These sum to 100; we normalise to
# 1.0 internally.
_STATIC_RAW_WEIGHTS: Dict[str, float] = {
    "insider_activity": 20.0,
    "institutional":    15.0,
    "earnings_quality": 15.0,
    "options_pricing":  10.0,
    "momentum":         10.0,
    "sec_sentiment":    10.0,
    "ml_forecast":      20.0,
}
_STATIC_WEIGHTS: Dict[str, float] = {
    k: v / sum(_STATIC_RAW_WEIGHTS.values())
    for k, v in _STATIC_RAW_WEIGHTS.items()
}


# ── Lazy imports / safe wrappers ───────────────────────────────────────────

def _safe_compute_score(symbol: str) -> Dict[str, Any]:
    """Pull the raw Smart Money result. Never raises; returns {} on failure."""
    try:
        import smart_money  # type: ignore
    except Exception as e:
        log.debug("smart_money import failed: %s", e)
        return {}
    fn = getattr(smart_money, "compute_score", None) or getattr(
        smart_money, "compute_smart_money_score", None
    )
    if fn is None:
        log.warning("smart_money has no compute_score / compute_smart_money_score")
        return {}
    try:
        return fn(symbol) or {}
    except Exception as e:
        log.debug("smart_money.compute_score(%s) failed: %s", symbol, e)
        return {}


def _safe_track_record(signal_name: str) -> Dict[str, Any]:
    """Pull live track-record stats. Returns an empty dict on any failure."""
    try:
        import research_tracker  # type: ignore
    except Exception:
        return {}
    try:
        return research_tracker.get_track_record(signal_name) or {}
    except Exception as e:
        log.debug("research_tracker.get_track_record(%s) failed: %s", signal_name, e)
        return {}


# ── Bayesian update ────────────────────────────────────────────────────────

def _posterior_mean(n: int, hit_rate: float) -> Tuple[float, float, float]:
    """Return (alpha_post, beta_post, posterior_mean) for the Beta-binomial."""
    hit_rate = max(0.0, min(1.0, hit_rate))
    hits = int(round(hit_rate * n))
    misses = max(0, n - hits)
    alpha_post = PRIOR_ALPHA + hits
    beta_post = PRIOR_BETA + misses
    mean = alpha_post / (alpha_post + beta_post)
    return alpha_post, beta_post, mean


def _weight_multiplier(posterior_mean: float) -> float:
    """How much to scale a static weight given a Bayes-updated hit-rate.

    Multiplier = (posterior_mean / 0.5) ** WEIGHT_EXPONENT, clamped to
    [0.25, 3.0] so a pathological track record can't completely wipe out
    or hog a component.
    """
    if posterior_mean is None or posterior_mean <= 0:
        return 0.25
    raw = (posterior_mean / PRIOR_MEAN) ** WEIGHT_EXPONENT
    return max(0.25, min(3.0, raw))


# ── Component extraction ───────────────────────────────────────────────────

def _normalize_component(name: str, comp: Dict[str, Any]) -> float:
    """Map a raw component score onto [-1, +1].

    Each component in `smart_money` ships its max (e.g. 20 for insider).
    Score below max/2 -> negative, above -> positive, scaled symmetrically.
    """
    score = comp.get("score")
    max_ = comp.get("max") or 1
    if score is None or not max_:
        return 0.0
    try:
        s = float(score)
        m = float(max_)
    except (TypeError, ValueError):
        return 0.0
    if m <= 0:
        return 0.0
    centred = (s - (m / 2.0)) / (m / 2.0)  # [-1..+1]
    return max(-1.0, min(1.0, centred))


def _raw_value(comp: Dict[str, Any]) -> float:
    """Return the original 0..1 component "strength" (score / max)."""
    score = comp.get("score")
    max_ = comp.get("max")
    if score is None or not max_:
        return 0.0
    try:
        return max(0.0, min(1.0, float(score) / float(max_)))
    except (TypeError, ValueError):
        return 0.0


# ── Synthetic-history seeding (used by tests + INTEGRATE doc demo) ────────

def seed_synthetic_history(
    counts: Optional[Dict[str, Tuple[int, float]]] = None,
    horizon_days: int = 30,
) -> Dict[str, int]:
    """Insert pre-scored fake rows into `signal_forecasts` for every smart-money
    component so the Bayes weights actually have data to act on.

    `counts` maps component-name -> (n, hit_rate). Defaults to a balanced 30-call
    history where insider/ml outperform and earnings/sec underperform.

    NOTE: this writes directly to the SQLite DB exposed by `research_tracker`
    rather than going through `log_forecast`, because `log_forecast` records
    only un-scored calls. We need *scored* rows to populate `get_track_record`.
    Returns a {component: rows_inserted} summary.

    SAFETY GUARD: this fabricates *scored* track-record rows, which then drive
    the Bayes weights of the live panel. Running it against the production
    `wealth.db` permanently pollutes the real ledger. It is therefore refused
    unless ``AUGUR_ALLOW_SEED=1`` is set in the environment, AND — even with
    the flag off — the target DB path must contain 'test' or 'tmp'. With the
    flag on, any path is allowed (explicit opt-in). Returns ``{}`` when
    refused, never raising.
    """
    db_path = os.environ.get("AUGUR_DB_PATH", "wealth.db")
    allow_seed = os.environ.get("AUGUR_ALLOW_SEED") == "1"
    _path_lc = db_path.lower()
    _is_test_db = ("test" in _path_lc) or ("tmp" in _path_lc)
    if not allow_seed and not _is_test_db:
        log.warning(
            "seed_synthetic_history refused: would write to non-test DB %r without "
            "AUGUR_ALLOW_SEED=1. Set AUGUR_ALLOW_SEED=1 to override, or point "
            "AUGUR_DB_PATH at a path containing 'test'/'tmp'.", db_path,
        )
        return {}

    if counts is None:
        counts = {
            "insider_activity": (40, 0.65),   # strong — gets upweighted
            "institutional":    (35, 0.55),
            "earnings_quality": (40, 0.42),   # weak  — gets downweighted
            "options_pricing":  (30, 0.50),
            "momentum":         (40, 0.58),
            "sec_sentiment":    (30, 0.38),   # weak
            "ml_forecast":      (50, 0.62),
        }

    try:
        import research_tracker as rt  # type: ignore
        rt.init_tracker_db()
    except Exception as e:
        log.warning("seed_synthetic_history: research_tracker unavailable: %s", e)
        return {}

    inserted: Dict[str, int] = {}
    now = datetime.now(timezone.utc)
    issued_at = (now - timedelta(days=horizon_days + 1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    scored_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    try:
        conn = sqlite3.connect(db_path, timeout=10.0)
        conn.execute("PRAGMA busy_timeout=5000")
        for comp_name, (n, rate) in counts.items():
            sig = SIGNAL_PREFIX + comp_name
            hits = int(round(rate * n))
            # Wipe any prior synthetic rows for this signal so re-running is
            # idempotent. We *only* delete rows tagged synthetic in metadata.
            conn.execute(
                "DELETE FROM signal_forecasts WHERE signal_name = ? "
                "AND metadata LIKE '%\"synthetic\": true%'",
                (sig,),
            )
            rows = []
            for i in range(n):
                hit = 1 if i < hits else 0
                direction = "BUY"
                realized = 0.02 if hit else -0.015
                # The forecast we *issued* (predicted_magnitude / confidence) is
                # distinct from what later *realized*. Seed plausible issue-time
                # values rather than reusing `realized` for those columns.
                predicted_magnitude = 0.02
                confidence = 0.6
                rows.append((
                    sig, "SYNTH", issued_at, horizon_days,
                    direction, predicted_magnitude, confidence,
                    '{"synthetic": true}', scored_at, realized, hit,
                    100.0, 100.0 * (1 + realized),
                ))
            conn.executemany(
                """
                INSERT INTO signal_forecasts
                  (signal_name, symbol, issued_at, horizon_days,
                   predicted_direction, predicted_magnitude, confidence,
                   metadata, scored_at, realized_return, hit,
                   issue_price, score_price)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                rows,
            )
            inserted[comp_name] = len(rows)
        conn.commit()
        conn.close()
    except Exception as e:
        log.warning("seed_synthetic_history failed: %s", e)
        return inserted

    return inserted


# ── Per-signal logger (called after bayes_smart_money has produced a score) ─

def log_bayes_smart_money(symbol: str, result: Dict[str, Any]) -> Optional[int]:
    """Optional hook: log each *component* call into research_tracker so the
    track records that drive next iteration's weights grow over time.

    Records ONE row per non-NEUTRAL component using the component's normalised
    score as the direction (positive -> BUY, negative -> SELL). 30-day horizon
    to match the existing `log_smart_money` cadence.
    """
    try:
        import research_tracker as rt  # type: ignore
    except Exception:
        return None
    if not isinstance(result, dict) or result.get("error"):
        return None
    last_id = None
    for comp in result.get("components") or []:
        name = comp.get("name")
        norm = comp.get("normalized")
        if name is None or norm is None:
            continue
        if abs(norm) < 0.05:
            direction = "NEUTRAL"
        elif norm > 0:
            direction = "BUY"
        else:
            direction = "SELL"
        try:
            rid = rt.log_forecast(
                SIGNAL_PREFIX + name,
                symbol,
                30,
                direction,
                float(norm),
                float(abs(norm)),
                None,
                {"raw_value": comp.get("raw_value"), "source": "bayes_smart_money"},
            )
            if rid is not None:
                last_id = rid
        except Exception as e:
            log.debug("log_forecast for %s failed: %s", name, e)
    return last_id


# ── Main API ───────────────────────────────────────────────────────────────

def _compute(symbol: str) -> Dict[str, Any]:
    """Inner computation — gets cached by `bayes_smart_money`."""
    sm = _safe_compute_score(symbol)
    if not sm or sm.get("error") or sm.get("score") is None:
        return {
            "symbol": symbol.upper(),
            "error": sm.get("error", "Smart Money score unavailable"),
            "static_score": None,
            "bayes_score": None,
            "components": [],
            "weight_shift_summary": [],
            "as_of": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }

    static_score = float(sm.get("score") or 0)
    raw_components: Dict[str, Dict[str, Any]] = sm.get("components") or {}

    components_out: List[Dict[str, Any]] = []
    posterior_unnormalised: Dict[str, float] = {}

    # ── Stage 1: per-component raw extraction + posterior multipliers ────
    for name in _STATIC_WEIGHTS:
        comp = raw_components.get(name) or {}
        static_w = _STATIC_WEIGHTS[name]
        normalized = _normalize_component(name, comp)
        raw_val = _raw_value(comp)

        tr = _safe_track_record(SIGNAL_PREFIX + name)
        n_dir = tr.get("n_directional") or 0
        hit_rate = tr.get("hit_rate")

        if n_dir >= MIN_N and hit_rate is not None:
            _, _, mean = _posterior_mean(int(n_dir), float(hit_rate))
            multiplier = _weight_multiplier(mean)
        else:
            mean = PRIOR_MEAN
            multiplier = 1.0

        posterior_unnormalised[name] = static_w * multiplier

        components_out.append({
            "name": name,
            "label": comp.get("label", name.replace("_", " ").title()),
            "score": comp.get("score"),
            "max": comp.get("max"),
            "detail": comp.get("detail"),
            "raw_value": round(raw_val, 4),
            "normalized": round(normalized, 4),
            "static_weight": round(static_w, 4),
            "track_record": {
                "n": tr.get("n") or 0,
                "n_directional": n_dir,
                "hit_rate": hit_rate,
                "avg_return": tr.get("avg_return"),
                "last_100_hit_rate": tr.get("last_100_hit_rate"),
            },
            "posterior_mean": round(mean, 4),
            "weight_multiplier": round(multiplier, 4),
        })

    # ── Stage 2: renormalise posterior weights so they sum to 1.0 ───────
    total_post = sum(posterior_unnormalised.values()) or 1.0
    for c in components_out:
        post_w = posterior_unnormalised[c["name"]] / total_post
        c["posterior_weight"] = round(post_w, 4)
        c["contribution"] = round(post_w * c["normalized"], 4)

    # ── Stage 3: composite recomputation ────────────────────────────────
    # NOTE: `bayes_score` is a TRACK-RECORD-WEIGHTED COMPOSITE, not a posterior
    # probability. It re-weights each component's normalized [-1..+1] value by
    # the (Beta-Binomial-derived) posterior weight and maps back onto 0..100
    # (0 = -1, 50 = 0, 100 = +1) — same scale as the static score. The
    # Bayes-ness is in the WEIGHTS, not in the output being P(up). See
    # `posterior_p_up` below for an actual log-odds posterior.
    bayes_normalized = sum(c["posterior_weight"] * c["normalized"] for c in components_out)
    bayes_score = 50.0 + (bayes_normalized * 50.0)
    bayes_score = max(0.0, min(100.0, bayes_score))

    # ── Stage 3b: TRUE log-odds posterior P(up) ─────────────────────────
    # Combine the calibrated component signals in log-odds space (naive-Bayes
    # fusion). Each component carries a directional signal `normalized` ∈
    # [-1,+1] and a *calibrated reliability* `posterior_mean` ∈ (0,1) — the
    # Beta-Binomial estimate of how often that component is directionally
    # right. A component pointing up with reliability p contributes
    # +|normalized|·logit(p) to the up-log-odds; pointing down contributes the
    # negation. Summing logits and applying the logistic gives a genuine
    # probability (unlike `bayes_score`, which is a weighted average on a
    # bipolar scale). Components with no track record (posterior_mean == prior
    # 0.5) contribute logit(0.5)=0 → they don't move the posterior, which is
    # the correct "no calibrated evidence" behaviour.
    # CRITICAL: `posterior_mean` is an UNDIRECTED reliability — how often the
    # component is directionally *right* — not P(up). Treating logit(p) as the
    # signed up-log-odds is wrong: a reliable-but-bearish component (p high,
    # normalized<0) would still need its sign from `normalized`, and an
    # *anti*-reliable component (p<0.5) carries no usable directional evidence
    # here (we can't tell which way it's wrong without a per-direction
    # hit-rate). So: magnitude = |normalized|·logit(p) (reliability), SIGN =
    # sign(normalized) (the component's directional read). Components with
    # p<=0.5 (no calibrated reliability, or anti-reliable) contribute nothing.
    # If NO component carries calibrated directional evidence, we cannot emit a
    # genuine probability → drop posterior_p_up (None) rather than report a
    # wrong number.
    log_odds = 0.0
    n_reliable = 0
    for c in components_out:
        p = c["posterior_mean"]
        try:
            p = min(0.999, max(0.001, float(p)))
        except (TypeError, ValueError):
            continue
        if p <= 0.5:
            # No calibrated reliability (prior 0.5) or anti-reliable: undirected
            # hit-rate gives us no trustworthy directional contribution.
            continue
        try:
            norm = float(c["normalized"])
        except (TypeError, ValueError):
            continue
        if norm == 0.0:
            continue
        reliability = math.log(p / (1.0 - p))     # > 0 since p > 0.5
        sign = 1.0 if norm > 0 else -1.0
        log_odds += sign * abs(norm) * reliability
        n_reliable += 1

    if n_reliable == 0:
        posterior_p_up = None
    else:
        try:
            posterior_p_up = 1.0 / (1.0 + math.exp(-log_odds))
        except OverflowError:
            posterior_p_up = 0.0 if log_odds < 0 else 1.0
        posterior_p_up = max(0.0, min(1.0, posterior_p_up))

    # ── Stage 4: top-3 weight shifts ────────────────────────────────────
    shifts = sorted(
        [
            {
                "name": c["name"],
                "label": c["label"],
                "shift": round(c["posterior_weight"] - c["static_weight"], 4),
                "static_weight": c["static_weight"],
                "posterior_weight": c["posterior_weight"],
            }
            for c in components_out
        ],
        key=lambda x: abs(x["shift"]),
        reverse=True,
    )[:3]

    # ── Stage 5: bayes signal label (mirrors smart_money.py bands) ─────
    if bayes_score >= 75:
        bayes_signal = "STRONG BUY"
    elif bayes_score >= 60:
        bayes_signal = "BUY"
    elif bayes_score >= 45:
        bayes_signal = "NEUTRAL"
    elif bayes_score >= 30:
        bayes_signal = "CAUTION"
    else:
        bayes_signal = "AVOID"

    return {
        "symbol": symbol.upper(),
        "name": sm.get("name", symbol.upper()),
        "price": sm.get("price"),
        "static_score": round(static_score, 2),
        "static_signal": sm.get("signal"),
        "bayes_score": round(bayes_score, 2),
        # User-facing label clarifying what bayes_score actually is.
        "bayes_score_label": "track-record-weighted composite (0-100, not a probability)",
        "bayes_signal": bayes_signal,
        # True log-odds posterior P(up) from naive-Bayes fusion of the
        # calibrated component signals (this one IS a probability).
        "posterior_p_up": round(posterior_p_up, 4) if posterior_p_up is not None else None,
        "posterior_log_odds": round(log_odds, 4),
        "score_delta": round(bayes_score - static_score, 2),
        "components": components_out,
        "weight_shift_summary": shifts,
        "prior": {
            "alpha": PRIOR_ALPHA,
            "beta": PRIOR_BETA,
            "mean": PRIOR_MEAN,
            "min_n": MIN_N,
            "weight_exponent": WEIGHT_EXPONENT,
        },
        "as_of": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def bayes_smart_money(symbol: str) -> Dict[str, Any]:
    """Public entry point. Cached for 10 minutes via `cache_store.coalesce`.

    Returns:
        {
          "symbol": "AAPL",
          "static_score": 65.0,
          "bayes_score": 71.2,
          "components": [...],
          "weight_shift_summary": [...],
          "as_of": "...",
          ...
        }
    """
    sym = (symbol or "").upper().strip()
    if not sym:
        return {"error": "missing symbol"}

    # Cache the full result for 10 min. The underlying smart_money call is
    # already cached by smart_money itself for its component sources, but
    # we cache here too so the Bayes math + tracker lookups don't run on
    # every UI toggle.
    try:
        import cache_store  # type: ignore
        return cache_store.coalesce(("bayessmart", sym), 600, lambda: _compute(sym))
    except Exception:
        return _compute(sym)


# ── CLI / smoke-test ───────────────────────────────────────────────────────

if __name__ == "__main__":
    import json
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")

    args = sys.argv[1:]
    if args and args[0] == "seed":
        print(json.dumps(seed_synthetic_history(), indent=2))
        sys.exit(0)
    sym = args[0] if args else "AAPL"
    t0 = time.time()
    res = bayes_smart_money(sym)
    print(json.dumps(res, indent=2, default=str))
    print(f"elapsed: {(time.time() - t0) * 1000:.0f}ms")
