"""
Options-implied risk-neutral density (RND) via Breeden-Litzenberger.

The full call-option chain at a single expiry encodes a forward-looking
*risk-neutral* probability distribution of the underlying's terminal price.
Breeden & Litzenberger (1978) showed that the RND is the second derivative
of the call price with respect to the strike, scaled by e^(rT):

    f(K) = e^(rT) · ∂²C/∂K²

where C(K) is the call price for strike K and T is time-to-expiry in years.

This module extracts that density from a yfinance option chain so the user
can read off statements like "the market is pricing a 28% chance NVDA closes
above $180 by next OPEX". Pairs naturally with model-based forecasts:
divergence between RND tails and a model's predictive distribution is a
tradable view.

API:
    risk_neutral_density(symbol, expiry=None, smooth=True) -> dict

Method:
    1. Pull the call chain for `expiry` (default: first expiry >7d out).
    2. Filter calls: drop zero-volume *and* zero-OI rows, drop crossed
       quotes (ask < bid), keep mid = (bid+ask)/2 (lastPrice as fallback).
    3. Smooth call prices via cubic-spline fit on (K, mid) — the second
       derivative is *very* noisy without smoothing.
    4. Compute density via centered finite difference on the smoothed curve.
    5. Floor negative values at 0; renormalize to integrate to ~1.
    6. CDF via cumulative trapezoidal integration.
    7. Tail probabilities at ±5/10/20% of spot; implied moments from PDF.

Cache: 30 minutes per (symbol, expiry). IV moves faster than fundamentals
but slower than spot.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timezone, date as _date
from typing import Optional

log = logging.getLogger("augur.rnd")

# Soft imports — module degrades gracefully if any of these are missing,
# returning an error envelope rather than throwing at import time.
try:
    import numpy as np
except Exception:  # pragma: no cover
    np = None  # type: ignore

try:
    from scipy.interpolate import CubicSpline
    from scipy.integrate import cumulative_trapezoid
    _SCIPY_OK = True
except Exception:  # pragma: no cover
    CubicSpline = None  # type: ignore
    cumulative_trapezoid = None  # type: ignore
    _SCIPY_OK = False

try:
    import fetcher
except Exception as _fe:  # pragma: no cover
    fetcher = None  # type: ignore
    log.warning("fetcher unavailable for research_iv_density: %s", _fe)

try:
    import cache_store
except Exception:  # pragma: no cover
    cache_store = None  # type: ignore


_CACHE_TTL = 1800  # 30 minutes
_DEFAULT_RISK_FREE = 0.045  # sensible fallback when ^TNX unavailable
_MIN_STRIKES = 8


# ─── Helpers ───────────────────────────────────────────────────────────────

def _today_utc() -> _date:
    return datetime.now(timezone.utc).date()


def _days_to(expiry: str) -> int:
    """Calendar days from today (UTC) to ISO-formatted expiry string."""
    try:
        exp = datetime.strptime(expiry, "%Y-%m-%d").date()
    except Exception:
        return -1
    return (exp - _today_utc()).days


def _risk_free_rate() -> float:
    """Annualized risk-free rate. Uses ^TNX (10Y treasury yield, in %) if
    available via fetcher.get_quote, else a 4.5% fallback. ^TNX is quoted
    as e.g. 4.32 meaning 4.32% — divide by 100."""
    if fetcher is None:
        return _DEFAULT_RISK_FREE
    try:
        q = fetcher.get_quote("^TNX")
        px = q.get("price") if isinstance(q, dict) else None
        if px and 0 < px < 25:
            return float(px) / 100.0
    except Exception:
        pass
    return _DEFAULT_RISK_FREE


def _spot(symbol: str) -> Optional[float]:
    if fetcher is None:
        return None
    try:
        q = fetcher.get_quote(symbol)
        if isinstance(q, dict):
            px = q.get("price")
            if px and px > 0:
                return float(px)
    except Exception:
        return None
    return None


def _choose_expiry(symbol: str, requested: Optional[str]) -> Optional[str]:
    """If `requested` is provided, return it as-is (validated downstream).
    Otherwise pick the first expiry with >7 days to expiry."""
    if requested:
        return requested
    if fetcher is None:
        return None
    try:
        dates = fetcher.get_options_dates(symbol) or []
    except Exception:
        dates = []
    for d in dates:
        if _days_to(d) > 7:
            return d
    # If nothing >7d, fall back to whatever's farthest out
    return dates[-1] if dates else None


def _clean_calls(calls: list) -> list:
    """Filter the raw call list to liquid rows with valid quotes. Returns a
    list of dicts {strike, mid, bid, ask, volume, oi}.

    Drop rules:
      - strike or bid/ask is None
      - volume == 0 AND open_interest == 0   (illiquid → stale prices)
      - ask < bid   (crossed quote — yfinance occasionally returns these)
      - mid <= 0    (degenerate)
    """
    cleaned: list[dict] = []
    for c in calls or []:
        k = c.get("strike")
        bid = c.get("bid")
        ask = c.get("ask")
        last = c.get("lastPrice")
        # yfinance frequently returns float('nan') (not None) for bid/ask/last
        # on illiquid strikes. Coerce non-finite values to None up front so
        # NaN can never reach the (bid+ask) arithmetic below and leak into mid.
        def _finite_or_none(v):
            try:
                f = float(v) if v is not None else None
            except (TypeError, ValueError):
                return None
            return f if (f is not None and math.isfinite(f)) else None
        bid = _finite_or_none(bid)
        ask = _finite_or_none(ask)
        last = _finite_or_none(last)
        # yfinance routinely returns NaN for volume/openInterest on illiquid
        # strikes. `x or 0` keeps NaN (NaN is truthy in Python), which later
        # crashes int(NaN) on the cleaned-dict build below. Coerce defensively.
        def _num_or_zero(v):
            try:
                f = float(v) if v is not None else 0.0
            except (TypeError, ValueError):
                return 0
            if not math.isfinite(f):
                return 0
            return int(f)
        vol = _num_or_zero(c.get("volume"))
        oi = _num_or_zero(c.get("openInterest"))
        if k is None:
            continue
        # Determine mid: prefer (bid+ask)/2 when both present and ask>=bid,
        # else fall back to lastPrice. If neither yields a positive number,
        # drop the row.
        mid: Optional[float] = None
        if bid is not None and ask is not None and ask >= bid and (bid + ask) > 0:
            mid = (float(bid) + float(ask)) / 2.0
        elif last is not None and last > 0:
            mid = float(last)
        if mid is None or mid <= 0:
            continue
        if vol <= 0 and oi <= 0:
            continue
        cleaned.append({
            "strike": float(k),
            "mid": float(mid),
            "bid": float(bid) if bid is not None else None,
            "ask": float(ask) if ask is not None else None,
            "volume": int(vol),
            "oi": int(oi),
        })
    # De-dup strikes (keep most liquid by volume+oi) and sort.
    by_strike: dict[float, dict] = {}
    for row in cleaned:
        k = row["strike"]
        if k not in by_strike or (row["volume"] + row["oi"]) > (by_strike[k]["volume"] + by_strike[k]["oi"]):
            by_strike[k] = row
    out = sorted(by_strike.values(), key=lambda r: r["strike"])
    return out


def _enforce_monotonic_decreasing(prices: list) -> list:
    """Call prices must be non-increasing in strike (no-arbitrage). Yahoo
    occasionally ships a stale mid that violates this. We enforce it with
    a single backward pass: any C(K_i) > C(K_{i-1}) gets clipped to
    C(K_{i-1}). This is a mild assumption that improves the second-
    derivative estimate dramatically."""
    if not prices:
        return prices
    out = list(prices)
    for i in range(1, len(out)):
        if out[i] > out[i - 1]:
            out[i] = out[i - 1]
    return out


def _pava_nondecreasing(y: "np.ndarray", w: "np.ndarray") -> "np.ndarray":
    """Pool-Adjacent-Violators (PAVA) weighted isotonic regression onto the
    NON-DECREASING cone. Pure-numpy, no extra dependency.

    Returns the least-squares non-decreasing fit ŷ minimising
    Σ w_i (y_i − ŷ_i)². Used to enforce convexity of the call-price curve by
    monotonising its discrete slopes.
    """
    n = len(y)
    if n == 0:
        return y
    # Each "block" tracks its weighted mean, total weight, and span.
    vals = list(map(float, y))
    wts = list(map(float, w))
    means = []
    weights = []
    counts = []
    for i in range(n):
        means.append(vals[i])
        weights.append(wts[i] if wts[i] > 0 else 1e-12)
        counts.append(1)
        # Merge backward while the previous block's mean exceeds this one.
        while len(means) > 1 and means[-2] > means[-1]:
            w2 = weights[-2] + weights[-1]
            m2 = (means[-2] * weights[-2] + means[-1] * weights[-1]) / w2
            c2 = counts[-2] + counts[-1]
            means.pop(); weights.pop(); counts.pop()
            means[-1] = m2; weights[-1] = w2; counts[-1] = c2
    out = np.empty(n, dtype=float)
    pos = 0
    for m, c in zip(means, counts):
        out[pos:pos + c] = m
        pos += c
    return out


def _enforce_convex_decreasing(grid: "np.ndarray", C: "np.ndarray") -> "np.ndarray":
    """Project a call-price curve C(K) onto the (non-increasing, CONVEX) cone.

    WHY (C): the only no-arbitrage condition that guarantees a VALID density
    is convexity of C(K) (since f(K)=e^{rT}·C''(K) ≥ 0). Enforcing mere
    monotonicity still lets local concavities through, which the second
    difference turns into negative densities that then get floored to 0 —
    biasing probability mass. Here we (1) make the discrete slopes
    s_i=ΔC/ΔK non-DECREASING via weighted isotonic regression (PAVA), which
    is exactly discrete convexity, then (2) re-integrate the corrected slopes
    back into a price curve, anchored at C[0]. The result has C''≥0 by
    construction, so far fewer floored-negative density regions remain.
    Fails OPEN to the input on any error.
    """
    try:
        n = len(grid)
        if n < 3:
            return C
        dK = np.diff(grid)
        if not np.all(dK > 0):
            return C
        slopes = np.diff(C) / dK                  # length n-1
        # Weight each slope by its interval length (proper L2 projection).
        fixed = _pava_nondecreasing(slopes, dK)   # non-decreasing slopes
        C_new = np.empty(n, dtype=float)
        C_new[0] = C[0]
        C_new[1:] = C[0] + np.cumsum(fixed * dK)
        if not np.all(np.isfinite(C_new)):
            return C
        return C_new
    except Exception as e:  # pragma: no cover - defensive
        log.debug("convexity projection failed, using monotonic-only C: %s", e)
        return C


def _make_uniform_grid(strikes: list, n: int = 200) -> "np.ndarray":
    """Evenly-spaced strike grid spanning [min, max], for finite-difference
    differentiation. ~200 points keeps the spacing fine enough that the
    finite-difference approximation is accurate yet coarse enough to not
    over-amplify spline wobble."""
    lo, hi = float(min(strikes)), float(max(strikes))
    return np.linspace(lo, hi, n)


# ─── Core: Breeden-Litzenberger ────────────────────────────────────────────

def _density_from_calls(strikes: list, mids: list, T: float, r: float,
                        smooth: bool) -> tuple:
    """Return (grid_K, density, cdf) given filtered call quotes.

    density(K) = e^(rT) · d²C/dK²  on a uniform grid.
    Negatives are floored at 0; the density is renormalized so the
    trapezoidal integral over [Kmin, Kmax] equals 1.
    """
    # Enforce non-increasing call prices (small no-arb correction).
    mids_clean = _enforce_monotonic_decreasing(mids)

    K_arr = np.array(strikes, dtype=float)
    C_arr = np.array(mids_clean, dtype=float)

    # Build the call-price function C(K). With smooth=True, fit a natural
    # cubic spline through (K, mid) — this dampens the per-strike quote
    # noise that otherwise dominates the second derivative.
    if smooth and _SCIPY_OK and len(K_arr) >= 4:
        try:
            cs = CubicSpline(K_arr, C_arr, bc_type="natural", extrapolate=False)
            grid = _make_uniform_grid(K_arr, n=200)
            C_grid = cs(grid)
        except Exception as e:
            log.warning("CubicSpline failed (%s) — falling back to raw grid", e)
            grid = K_arr.copy()
            C_grid = C_arr.copy()
    else:
        # Raw strikes — assume they're roughly evenly spaced (typical for
        # liquid US equity options: $1 / $2.5 / $5 increments).
        grid = K_arr.copy()
        C_grid = C_arr.copy()

    # Replace any NaN/inf the spline emitted (out-of-support, extrapolation).
    C_grid = np.where(np.isfinite(C_grid), C_grid, 0.0)

    # Enforce CONVEXITY of C(K) (C) — the true no-arb condition for a valid
    # density. This makes the second difference non-negative by construction,
    # so the Breeden-Litzenberger density has far fewer floored-negative
    # regions. Applied on top of the earlier monotonic-decreasing clamp.
    C_grid = _enforce_convex_decreasing(grid, C_grid)

    # Centered finite-difference second derivative.
    n = len(grid)
    d2 = np.zeros(n)
    diffs = np.diff(grid) if n > 1 else np.array([])
    uniform = bool(diffs.size and np.allclose(diffs, diffs[0]))
    if n >= 3 and uniform and diffs[0] > 0:
        # Uniform grid (smooth-spline path): exact constant-h second difference.
        # For interior points: d²C/dK² ≈ (C[i+1] - 2C[i] + C[i-1]) / h².
        h = float(diffs[0])
        d2[1:-1] = (C_grid[2:] - 2.0 * C_grid[1:-1] + C_grid[:-2]) / (h * h)
        # Endpoints — use one-sided (less accurate but avoids NaN gaps).
        d2[0] = d2[1]
        d2[-1] = d2[-2]
    elif n >= 3:
        # Non-uniform grid (raw-strike path: $1 near ATM, $5 in the wings) —
        # a single constant h gives a wrong d²C/dK². np.gradient applied twice
        # respects the actual per-point spacing.
        d2 = np.gradient(np.gradient(C_grid, grid), grid)

    # Apply Breeden-Litzenberger discount factor.
    density = np.exp(r * T) * d2

    # Floor negatives (numerical noise + no-arb violations).
    density = np.where(density < 0, 0.0, density)

    # Renormalize: ∫ f(K) dK = 1 over [Kmin, Kmax]. The chain doesn't span
    # the full real line so we only get the support that's actively traded,
    # but for liquid names that covers the bulk of probability mass.
    total = float(np.trapz(density, grid))
    if total > 1e-12:
        density = density / total

    # CDF via cumulative trapezoid.
    if _SCIPY_OK:
        cdf = cumulative_trapezoid(density, grid, initial=0.0)
    else:
        # Manual fallback if scipy.integrate is unavailable.
        cdf = np.zeros(n)
        for i in range(1, n):
            cdf[i] = cdf[i - 1] + 0.5 * (density[i] + density[i - 1]) * (grid[i] - grid[i - 1])

    # Clip CDF to [0, 1] (rounding can push the final value to 1.0000001).
    cdf = np.clip(cdf, 0.0, 1.0)

    return grid, density, cdf


def _prob_above(grid: "np.ndarray", cdf: "np.ndarray", x: float) -> float:
    """P[K_T > x] interpolated from the CDF. Returns 0 if x exceeds support
    (we observe no probability mass beyond the highest strike)."""
    if x <= float(grid[0]):
        return 1.0
    if x >= float(grid[-1]):
        return 0.0
    # np.interp linearly interpolates the CDF at x.
    cdf_x = float(np.interp(x, grid, cdf))
    return max(0.0, min(1.0, 1.0 - cdf_x))


def _prob_below(grid: "np.ndarray", cdf: "np.ndarray", x: float) -> float:
    return 1.0 - _prob_above(grid, cdf, x)


def _moments(grid: "np.ndarray", density: "np.ndarray") -> dict:
    """Mean / std / skew / excess kurtosis from a discrete PDF on a uniform
    (or near-uniform) grid. Uses trapezoidal weights."""
    # Build trapezoidal weights so ∫ f dK ≈ Σ w_i f_i.
    n = len(grid)
    w = np.zeros(n)
    if n >= 2:
        w[0] = (grid[1] - grid[0]) / 2.0
        w[-1] = (grid[-1] - grid[-2]) / 2.0
        if n >= 3:
            w[1:-1] = (grid[2:] - grid[:-2]) / 2.0
    p = density * w  # discrete probability masses
    p_sum = float(p.sum())
    if p_sum <= 1e-12:
        return {
            "mean_implied_in_support": None,
            "mean_implied": None,  # alias (Q9): kept for back-compat
            "std_implied": None,
            "skew_implied": None,
            "kurtosis_implied": None,
        }
    p = p / p_sum
    mean = float((grid * p).sum())
    var = float(((grid - mean) ** 2 * p).sum())
    std = math.sqrt(var) if var > 0 else 0.0
    if std > 1e-9:
        skew = float((((grid - mean) / std) ** 3 * p).sum())
        kurt = float((((grid - mean) / std) ** 4 * p).sum()) - 3.0  # excess kurtosis
    else:
        skew = 0.0
        kurt = 0.0
    # WHY (Q9): `mean` here is the expectation of K_T over the OBSERVED strike
    # grid only — the RND is truncated at the lowest/highest listed strikes, so
    # this is a conditional-on-support mean, NOT the true risk-neutral mean
    # (which would equal the forward if the support were complete). Reporting it
    # as `mean_implied` invited reading it as the RND mean / implied forward.
    # Name it `mean_implied_in_support`; keep `mean_implied` as an alias so
    # existing consumers and tests don't break.
    return {
        "mean_implied_in_support": round(mean, 4),
        "mean_implied": round(mean, 4),  # alias (Q9): truncated-support mean
        "std_implied": round(std, 4),
        "skew_implied": round(skew, 4),
        "kurtosis_implied": round(kurt, 4),
    }


# ─── Public API ────────────────────────────────────────────────────────────

def _error(symbol: str, msg: str, **extra) -> dict:
    out = {
        "symbol": symbol.upper() if symbol else "",
        "error": msg,
        "method": "breeden_litzenberger_finite_difference",
        "as_of": _today_utc().isoformat(),
    }
    out.update(extra)
    return out


def _compute(symbol: str, expiry: Optional[str], smooth: bool) -> dict:
    """Uncached worker. Wrapped by `risk_neutral_density`."""
    sym = (symbol or "").upper().strip()
    if not sym:
        return _error(sym, "Symbol is required")
    if np is None:
        return _error(sym, "numpy unavailable")
    if fetcher is None:
        return _error(sym, "fetcher module unavailable")

    chosen_expiry = _choose_expiry(sym, expiry)
    if not chosen_expiry:
        return _error(sym, "No option expiries available for symbol")

    dte = _days_to(chosen_expiry)
    if dte <= 0:
        return _error(sym, f"Expiry {chosen_expiry} is in the past", expiry=chosen_expiry)

    spot = _spot(sym)
    if spot is None or spot <= 0:
        return _error(sym, "Could not resolve spot price for symbol", expiry=chosen_expiry,
                      days_to_expiry=dte)

    try:
        chain = fetcher.get_option_chain(sym, chosen_expiry) or {}
    except Exception as e:
        return _error(sym, f"Failed to fetch option chain: {e}", expiry=chosen_expiry,
                      days_to_expiry=dte, spot=spot)

    calls = chain.get("calls") or []
    if not calls:
        return _error(sym, chain.get("error") or "No calls in chain",
                      expiry=chosen_expiry, days_to_expiry=dte, spot=spot)

    clean = _clean_calls(calls)
    if len(clean) < _MIN_STRIKES:
        return _error(sym,
                      f"Insufficient liquid call strikes ({len(clean)} < {_MIN_STRIKES})",
                      expiry=chosen_expiry, days_to_expiry=dte, spot=spot,
                      n_strikes=len(clean))

    # Window around spot: keep strikes within [0.5·spot, 2.0·spot]. Deep ITM
    # calls (K << spot) behave like K-linear (C ≈ S - K·e^(-rT)) so their
    # contribution to ∂²C/∂K² is essentially zero — but any quote noise out
    # there gets amplified by the second derivative and creates spurious
    # density spikes. Deep OTM (K >> 2·spot) is usually pennies-quoted and
    # similarly noisy. For liquid names the [0.5x, 2x] window captures all
    # meaningful probability mass; for tighter expiries it's even more
    # conservative than needed.
    windowed = [c for c in clean if 0.5 * spot <= c["strike"] <= 2.0 * spot]
    # Always prefer the windowed set when it has ANY strikes — keeping the full
    # chain merely because the window is thin would reintroduce exactly the
    # deep-ITM/OTM curvature noise the window exists to remove. Only fall back
    # to the full chain when the window is empty (chain never reaches near
    # spot). The min-strikes check below then correctly rejects a too-thin
    # window instead of silently scoring the noisy full chain.
    if windowed:
        clean = windowed
    if len(clean) < _MIN_STRIKES:
        return _error(sym,
                      f"Insufficient liquid call strikes in window ({len(clean)} < {_MIN_STRIKES})",
                      expiry=chosen_expiry, days_to_expiry=dte, spot=spot,
                      n_strikes=len(clean))

    # Drop deep-ITM calls with near-zero time value. For K << spot, the call
    # is essentially intrinsic: C(K) ≈ S - K·e^(-rT). The time-value (mid −
    # intrinsic) is dominated by bid-ask noise — micro-variations there are
    # what produced the spurious density peaks we saw at K = 0.85·spot in
    # early tests. Time-value < 0.5% of spot means the call is "fully ITM"
    # and contributes no information to the curvature. Keep OTM strikes
    # always (intrinsic = 0 for K > spot so this filter is a no-op there).
    T = max(dte, 1) / 365.0  # years; guard against zero-day expiries
    r = _risk_free_rate()
    tv_floor = max(spot * 0.005, 0.10)
    informative = []
    for c in clean:
        # Use DISCOUNTED strike for intrinsic (S − K·e^(-rT)), consistent with
        # the Breeden-Litzenberger discounting used elsewhere. Undiscounted
        # intrinsic understates intrinsic for ITM strikes, overstating time
        # value and letting low-information deep-ITM strikes slip past the filter.
        intrinsic = max(spot - c["strike"] * math.exp(-r * T), 0.0)
        time_val = c["mid"] - intrinsic
        # OTM: always keep. ITM: require non-trivial time value.
        if c["strike"] >= spot or time_val >= tv_floor:
            informative.append(c)
    if len(informative) >= _MIN_STRIKES:
        clean = informative

    # Drop sparse-spacing wings. The cubic spline through massively non-
    # uniform strike spacing (e.g. $5 ↑ $700 then $1 spacing through ATM)
    # produces artificial curvature at the spacing-transition boundaries —
    # which the second derivative then amplifies into bogus density spikes.
    # Heuristic: compute the median strike spacing across the chain; any
    # consecutive pair separated by more than 3× the median is a wing gap.
    # Trim to the contiguous run that contains spot.
    if len(clean) >= _MIN_STRIKES * 2:
        sorted_strikes = [c["strike"] for c in clean]
        gaps = [sorted_strikes[i + 1] - sorted_strikes[i] for i in range(len(sorted_strikes) - 1)]
        if gaps:
            median_gap = sorted(gaps)[len(gaps) // 2]
            threshold = max(median_gap * 3.0, 0.01)
            # Find the segment around spot: walk outwards, stopping at the
            # first gap larger than threshold.
            # Index of strike closest to spot.
            spot_idx = min(range(len(sorted_strikes)),
                           key=lambda i: abs(sorted_strikes[i] - spot))
            left = spot_idx
            while left > 0 and (sorted_strikes[left] - sorted_strikes[left - 1]) <= threshold:
                left -= 1
            right = spot_idx
            while right < len(sorted_strikes) - 1 and (sorted_strikes[right + 1] - sorted_strikes[right]) <= threshold:
                right += 1
            trimmed = clean[left:right + 1]
            if len(trimmed) >= _MIN_STRIKES:
                clean = trimmed

    strikes = [c["strike"] for c in clean]
    mids = [c["mid"] for c in clean]

    try:
        grid, density, cdf = _density_from_calls(strikes, mids, T=T, r=r, smooth=smooth)
    except Exception as e:
        return _error(sym, f"Density computation failed: {e}",
                      expiry=chosen_expiry, days_to_expiry=dte, spot=spot,
                      n_strikes=len(clean))

    if grid is None or len(grid) == 0:
        return _error(sym, "Empty density grid", expiry=chosen_expiry,
                      days_to_expiry=dte, spot=spot, n_strikes=len(clean))

    # Degenerate density guard: if the curvature is non-positive everywhere
    # (heavy no-arb violations flatten C after the monotonic clamp), the
    # density is all zeros and _density_from_calls leaves it un-normalized.
    # A flat/zero density yields a flat CDF and meaningless probabilities, so
    # surface an explicit error rather than emitting misleading numbers.
    if float(np.trapz(density, grid)) <= 1e-12:
        return _error(sym, "Degenerate density (no usable curvature)",
                      expiry=chosen_expiry, days_to_expiry=dte, spot=spot,
                      n_strikes=len(clean))

    # Tail probabilities relative to spot.
    probs = {
        "p_above_spot_5pct":  round(_prob_above(grid, cdf, spot * 1.05), 4),
        "p_above_spot_10pct": round(_prob_above(grid, cdf, spot * 1.10), 4),
        "p_above_spot_20pct": round(_prob_above(grid, cdf, spot * 1.20), 4),
        "p_below_spot_5pct":  round(_prob_below(grid, cdf, spot * 0.95), 4),
        "p_below_spot_10pct": round(_prob_below(grid, cdf, spot * 0.90), 4),
        "p_below_spot_20pct": round(_prob_below(grid, cdf, spot * 0.80), 4),
    }

    moments = _moments(grid, density)

    # Liquidity-quality flag: if median (volume + OI) across kept strikes is
    # tiny we warn the consumer that the RND may be noisy.
    liq = sorted(c["volume"] + c["oi"] for c in clean)
    median_liq = liq[len(liq) // 2] if liq else 0
    illiquid_flag = bool(median_liq < 50)

    # Diagnostic: integral of density over the support — should be ~1 after
    # renormalization. If it's far off the chain probably truncates the tails.
    integral = float(np.trapz(density, grid))

    return {
        "symbol": sym,
        "expiry": chosen_expiry,
        "days_to_expiry": dte,
        "spot": round(spot, 4),
        "risk_free_rate": round(r, 5),
        "n_strikes": len(clean),
        "strikes": [round(float(x), 4) for x in grid.tolist()],
        "density": [round(float(x), 8) for x in density.tolist()],
        "cdf": [round(float(x), 6) for x in cdf.tolist()],
        "probabilities": probs,
        "implied_moments": moments,
        "diagnostics": {
            "density_integral": round(integral, 4),
            "median_liquidity": int(median_liq),
            "illiquid_flag": illiquid_flag,
            "raw_strikes": [round(float(s), 2) for s in strikes],
            "raw_mids": [round(float(m), 4) for m in mids],
        },
        "method": "breeden_litzenberger_finite_difference",
        "as_of": _today_utc().isoformat(),
    }


def risk_neutral_density(symbol: str, expiry: Optional[str] = None,
                         smooth: bool = True) -> dict:
    """Compute the options-implied risk-neutral density for `symbol` at
    `expiry`.

    Args:
        symbol: Underlying ticker, e.g. "SPY".
        expiry: ISO date "YYYY-MM-DD". If None, picks the first expiry
                more than 7 days out.
        smooth: If True (default), fit a cubic spline to (K, mid) before
                differentiating. Strongly recommended — the raw second
                difference is too noisy to be useful.

    Returns: a dict with strikes / density / cdf / probabilities /
             implied_moments / diagnostics, or an error envelope.
    """
    if cache_store is None:
        return _compute(symbol, expiry, smooth)
    key = ("rnd", (symbol or "").upper(), expiry or "auto", "smooth" if smooth else "raw")
    try:
        return cache_store.coalesce(key, _CACHE_TTL, lambda: _compute(symbol, expiry, smooth))
    except Exception as e:
        log.warning("cache_store.coalesce failed (%s); computing uncached", e)
        return _compute(symbol, expiry, smooth)


# ─── CLI smoke-test ────────────────────────────────────────────────────────

if __name__ == "__main__":  # pragma: no cover
    import json
    import sys
    sym = sys.argv[1] if len(sys.argv) > 1 else "SPY"
    exp = sys.argv[2] if len(sys.argv) > 2 else None
    r = risk_neutral_density(sym, exp)
    if "error" in r:
        print(json.dumps(r, indent=2))
    else:
        # Compact summary — full density array is too long for terminal.
        summary = {k: v for k, v in r.items()
                   if k not in ("strikes", "density", "cdf", "diagnostics")}
        summary["density_integral"] = r["diagnostics"]["density_integral"]
        summary["median_liquidity"] = r["diagnostics"]["median_liquidity"]
        print(json.dumps(summary, indent=2))
