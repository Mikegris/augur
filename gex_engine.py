"""
gex_engine.py — Gamma Exposure (GEX) Flow Predictor for AUGUR

Models delta-hedging flows that options market makers must execute based on
current open interest across every strike. Shows mechanical buy/sell pressure,
gamma walls (support/resistance), gamma flip point, and dealer hedge estimates.
"""

import logging
import math
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional

try:
    from zoneinfo import ZoneInfo
    _NY_TZ = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover
    try:
        import pytz
        _NY_TZ = pytz.timezone("America/New_York")
    except Exception:
        _NY_TZ = None

import yfinance as yf

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Caching: delegate to cache_store so we get
#   - request coalescing (parallel callers for SPY don't all hit Yahoo)
#   - persistence across restarts (cold-start doesn't refetch every chain)
#   - upstream-failure suppression (don't cache a YFRateLimitError shell
#     for 5 minutes and serve blank panels)
# The old `_cache` / `_cache_ts` dicts were in-memory only, had no
# coalescing, and stored failure responses verbatim.
# ---------------------------------------------------------------------------
_CACHE_TTL = 300  # seconds — option chains move ~1/sec but UI polls aren't
                  # the bottleneck; we trade freshness for upstream relief.


# ---------------------------------------------------------------------------
# Math helpers
# ---------------------------------------------------------------------------

def _norm_cdf(x):
    """Abramowitz and Stegun approximation for the standard normal CDF."""
    a1, a2, a3, a4, a5 = (
        0.254829592, -0.284496736, 1.421413741, -1.453152027, 1.061405429
    )
    p = 0.3275911
    sign = 1 if x >= 0 else -1
    x = abs(x)
    t = 1.0 / (1.0 + p * x)
    y = 1.0 - (((((a5 * t + a4) * t) + a3) * t + a2) * t + a1) * t * math.exp(-x * x / 2.0)
    return 0.5 * (1.0 + sign * y)


def _norm_pdf(x):
    """Standard normal PDF."""
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def _bs_gamma_vec(spot, strikes, tte, ivs, risk_free, div_yield):
    """Vectorized Black-Scholes(-Merton) gamma for an array of strikes/IVs.

    Returns a numpy array of gammas (0.0 where inputs are invalid). Mirrors the
    scalar `_black_scholes_greeks` gamma exactly:
        gamma = e^{-q*T} * φ(d1) / (S * iv * sqrt(T))
        d1 = (ln(S/K) + (r - q + 0.5*iv^2)*T) / (iv*sqrt(T))
    """
    import numpy as np
    strikes = np.asarray(strikes, dtype=float)
    ivs = np.asarray(ivs, dtype=float)
    gamma = np.zeros_like(strikes, dtype=float)
    if tte <= 0 or spot <= 0:
        return gamma
    valid = (strikes > 0) & (ivs > 0)
    if not valid.any():
        return gamma
    k = strikes[valid]
    iv = ivs[valid]
    sqrt_t = math.sqrt(tte)
    d1 = (np.log(spot / k) + (risk_free - div_yield + 0.5 * iv * iv) * tte) / (iv * sqrt_t)
    disc_q = math.exp(-div_yield * tte)
    pdf = np.exp(-0.5 * d1 * d1) / math.sqrt(2.0 * math.pi)
    gamma[valid] = disc_q * pdf / (spot * iv * sqrt_t)
    return gamma


def _black_scholes_greeks(spot, strike, tte, iv, is_call=True, risk_free=0.05, div_yield=0.0):
    # type: (float, float, float, float, bool, float, float) -> dict
    """
    Compute Black-Scholes(-Merton) delta and gamma with continuous dividend yield.

    Parameters
    ----------
    spot : float      – current underlying price
    strike : float    – option strike price
    tte : float       – time to expiry in years (must be > 0)
    iv : float        – implied volatility (annualised, e.g. 0.30 = 30 %)
    is_call : bool    – True for calls, False for puts
    risk_free : float – risk-free rate (annualised, continuous)
    div_yield : float – continuous dividend yield q (annualised; default 0)

    Returns
    -------
    dict with keys "delta" and "gamma"
    """
    if tte <= 0 or iv <= 0 or spot <= 0 or strike <= 0:
        return {"delta": 0.0, "gamma": 0.0}

    try:
        sqrt_t = math.sqrt(tte)
        # Merton drift carries the dividend yield: (r - q + 0.5*iv^2).
        d1 = (math.log(spot / strike) + (risk_free - div_yield + 0.5 * iv * iv) * tte) / (iv * sqrt_t)
        # d2 = d1 - iv * sqrt_t  # not needed for gamma

        disc_q = math.exp(-div_yield * tte)
        # Dividend-discount the gamma and delta (exp(-q*tte)).
        gamma = disc_q * _norm_pdf(d1) / (spot * iv * sqrt_t)

        if is_call:
            delta = disc_q * _norm_cdf(d1)
        else:
            delta = disc_q * (_norm_cdf(d1) - 1.0)

        return {"delta": delta, "gamma": gamma}
    except (ValueError, ZeroDivisionError, OverflowError):
        return {"delta": 0.0, "gamma": 0.0}


# ---------------------------------------------------------------------------
# Risk-free rate — pull the 13-week T-bill (^IRX) once, cache it, fall back to
# a hardcoded 0.05 if the fetch fails. ^IRX quotes the annualized discount rate
# in PERCENT (e.g. 5.2 -> 0.052).
# ---------------------------------------------------------------------------
_RISK_FREE_FALLBACK = 0.05
_risk_free_cache = {"rate": None, "ts": 0.0}
_RISK_FREE_TTL = 6 * 3600  # 6 hours — the policy rate barely moves intraday
# When ^IRX can't be fetched (e.g. a Yahoo 429), we serve the 0.05 fallback —
# but pinning it for the full 6h means a transient blip suppresses the REAL
# rate for the rest of the window. Cache the fallback for only a few minutes
# so a recovered ^IRX is picked up promptly.
_RISK_FREE_FALLBACK_TTL = 300  # 5 minutes


def _get_risk_free_rate():
    # type: () -> float
    """Annualized risk-free rate from ^IRX, cached; falls back to 0.05.

    A real ^IRX read is cached for 6h; the fallback is cached for only a few
    minutes so a transient fetch failure doesn't lock in 5% for hours.
    """
    now = time.time()
    cached = _risk_free_cache.get("rate")
    ttl = _risk_free_cache.get("ttl", _RISK_FREE_TTL)
    if cached is not None and (now - _risk_free_cache.get("ts", 0.0)) < ttl:
        return cached
    rate = _RISK_FREE_FALLBACK
    got_real = False
    try:
        irx = yf.Ticker("^IRX")
        last = None
        try:
            last = irx.fast_info.get("lastPrice")
        except Exception:
            last = None
        if last is None:
            try:
                last = irx.info.get("regularMarketPrice")
            except Exception:
                last = None
        if last is not None:
            val = float(last) / 100.0
            # Sanity-bound to a plausible 0-25% band; ignore garbage.
            if 0.0 <= val <= 0.25:
                rate = val
                got_real = True
    except Exception as e:
        logger.debug("gex risk-free: ^IRX fetch failed, using fallback: %s", e)
    _risk_free_cache["rate"] = rate
    _risk_free_cache["ts"] = now
    # Long TTL only when we got a genuine reading; short TTL for the fallback.
    _risk_free_cache["ttl"] = _RISK_FREE_TTL if got_real else _RISK_FREE_FALLBACK_TTL
    return rate


def _get_dividend_yield(ticker):
    # type: (object) -> float
    """Continuous dividend yield q from ticker.info; default 0.0 on any issue."""
    try:
        info = ticker.info or {}
        dy = info.get("dividendYield")
        if dy is None:
            return 0.0
        dy = float(dy)
        if dy <= 0:
            return 0.0

        # yfinance returns `dividendYield` inconsistently — sometimes a fraction
        # (0.0044 == 0.44%), sometimes a percent (0.44 == 0.44% but indistinct
        # from a 44% fraction). The old "anything > 1 is a percent" rule
        # mis-read a legit 0.44 (=0.44% as percent) as a 44% fraction. Instead
        # of guessing from the magnitude, VALIDATE against an independent yield
        # estimate: trailingAnnualDividendYield (already a fraction) or
        # trailingAnnualDividendRate / price. Pick whichever interpretation of
        # `dy` (as-fraction vs as-percent) is closest to that reference.
        ref = None
        tady = info.get("trailingAnnualDividendYield")
        if tady is not None:
            try:
                tady = float(tady)
                if tady > 0:
                    ref = tady
            except (TypeError, ValueError):
                ref = None
        if ref is None:
            rate = info.get("trailingAnnualDividendRate") or info.get("dividendRate")
            price = (info.get("regularMarketPrice") or info.get("currentPrice")
                     or info.get("previousClose"))
            try:
                if rate and price and float(price) > 0:
                    ref = float(rate) / float(price)
            except (TypeError, ValueError):
                ref = None

        as_fraction = dy            # interpret dy directly as a fraction
        as_percent = dy / 100.0     # interpret dy as a percent number
        if ref is not None and ref > 0:
            # Choose the interpretation closest (in log space) to the reference.
            cand = min(
                (as_fraction, as_percent),
                key=lambda x: abs(math.log((x or 1e-9) / ref)),
            )
            dy = cand
        else:
            # No reference available — fall back to the magnitude heuristic.
            dy = as_percent if dy > 1.0 else as_fraction

        # Bound to a sane 0-20% band; ignore garbage.
        if 0.0 <= dy <= 0.20:
            return dy
        return 0.0
    except Exception:
        return 0.0


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def _format_gex_value(value):
    # type: (float) -> str
    """Format a GEX dollar value as e.g. '$2.3B', '-$450M', '$12K'."""
    neg = value < 0
    av = abs(value)
    if av >= 1e9:
        formatted = "${:.1f}B".format(av / 1e9)
    elif av >= 1e6:
        formatted = "${:.0f}M".format(av / 1e6)
    elif av >= 1e3:
        formatted = "${:.0f}K".format(av / 1e3)
    else:
        formatted = "${:.0f}".format(av)
    if neg:
        formatted = "-" + formatted
    return formatted


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_spot_price(ticker, symbol=None):
    """Retrieve the current spot price from a yfinance Ticker object, with
    a fetcher.get_quote fall back so a yfinance crumb-auth failure doesn't
    take the whole GEX panel down. The `symbol` arg is optional only for
    backward compat; callers should pass it so the fall back can fire."""
    try:
        price = ticker.fast_info.get("lastPrice")
        if price and price > 0:
            return float(price)
    except Exception as e:
        logger.debug("gex spot: fast_info failed: %s", e)
    try:
        price = ticker.info.get("regularMarketPrice")
        if price and price > 0:
            return float(price)
    except Exception as e:
        logger.debug("gex spot: ticker.info failed: %s", e)
    # Final fall back — fetcher.get_quote carries the Yahoo-direct + Finviz
    # fallback chain so we still recover spot when yfinance auth is broken.
    if symbol:
        try:
            import fetcher
            q = fetcher.get_quote(symbol) or {}
            p = q.get("price")
            if p and float(p) > 0:
                return float(p)
        except Exception as e:
            logger.debug("gex spot: fetcher.get_quote(%s) failed: %s", symbol, e)
    # Last-resort stale-tolerant read from cache_store. During a Yahoo
    # rate-limit window the live fetch above returns None, leaving gex
    # unable to compute even when a recent quote is sitting in cache.
    # A slightly-stale spot is fine for gex — gamma is positional, not
    # tick-sensitive — so we accept up to 1h-old prices to keep the
    # panel rendering instead of returning the "no spot" error envelope.
    if symbol:
        try:
            import cache_store
            stale = cache_store.cache_get_stale(("quote", symbol.upper()),
                                                max_age_seconds=3600)
            if stale and isinstance(stale, dict):
                p = stale.get("price")
                if p and float(p) > 0:
                    logger.info("gex spot: serving stale %s quote (%.2f) "
                                "due to live-fetch failure", symbol, float(p))
                    return float(p)
        except Exception as e:
            logger.debug("gex spot: cache_get_stale(%s) failed: %s", symbol, e)
    return None


def _tte_from_expiry(expiry_str):
    # type: (str) -> float
    """Return time-to-expiry in years from an expiry date string (YYYY-MM-DD).

    Options expire at 16:00 America/New_York on the expiry date; anchor to
    that wall-clock time and compare in UTC so 0/1DTE doesn't mis-price.
    """
    try:
        exp_date = datetime.strptime(expiry_str, "%Y-%m-%d")
        if _NY_TZ is not None:
            naive_1600 = exp_date.replace(hour=16, minute=0, second=0)
            # WHY (S6): pytz timezones must be attached via .localize(), NOT
            # via replace(tzinfo=...). replace() binds pytz's default LMT entry
            # (-04:56, the 1883 "local mean time" offset) instead of EST/EDT,
            # mis-pricing every 0/1DTE by ~4 minutes. zoneinfo (no .localize)
            # handles replace() correctly, so branch on capability.
            if hasattr(_NY_TZ, "localize"):
                exp_dt = _NY_TZ.localize(naive_1600)
            else:
                exp_dt = naive_1600.replace(tzinfo=_NY_TZ)
            now = datetime.now(timezone.utc)
        else:
            # Fallback if no tz library available: options expire at 16:00
            # America/New_York, which is ~20:00 UTC (EST) / ~21:00 UTC (EDT) —
            # NOT 16:00 UTC. Anchoring at 16:00 UTC under-counts ~4h of every
            # 0/1DTE's remaining time. Use 20:00 UTC as a close approximation of
            # NY close (within an hour year-round).
            exp_dt = exp_date.replace(hour=20, minute=0, second=0, tzinfo=timezone.utc)
            now = datetime.now(timezone.utc)
        diff = (exp_dt - now).total_seconds()
        if diff <= 0:
            return 0.0
        return diff / (365.25 * 86400)
    except Exception:
        return 0.0


def _compute_max_pain(strikes_data):
    # type: (dict) -> Optional[float]
    """
    Compute max-pain strike: the strike price at which total ITM dollar
    value of all open interest is minimised.

    strikes_data: {strike: {"call_oi": int, "put_oi": int}}
    """
    if not strikes_data:
        return None

    all_strikes = sorted(strikes_data.keys())
    if not all_strikes:
        return None

    best_strike = None
    best_pain = None

    for test_price in all_strikes:
        total_pain = 0.0
        for s, info in strikes_data.items():
            # Call pain: if test_price > strike, calls are ITM
            call_itm = max(test_price - s, 0.0) * info.get("call_oi", 0) * 100
            # Put pain: if test_price < strike, puts are ITM
            put_itm = max(s - test_price, 0.0) * info.get("put_oi", 0) * 100
            total_pain += call_itm + put_itm
        if best_pain is None or total_pain < best_pain:
            best_pain = total_pain
            best_strike = test_price

    return best_strike


# ---------------------------------------------------------------------------
# Main computation
# ---------------------------------------------------------------------------

def compute_gex(symbol):
    # type: (str) -> dict
    """
    Compute full Gamma Exposure profile for *symbol*.

    Returns a dict with net GEX, regime, flip price, per-strike breakdown,
    top gamma walls, dealer hedge estimates, max pain, and metadata.
    On any error returns ``{"error": str}``.
    """
    symbol = symbol.upper().strip()

    # Coalesce concurrent requests for the same symbol through cache_store.
    # Without this, N parallel /api/gex/SPY callers would each fire their
    # own option_chain() sweep against Yahoo — easy way to blow the rate
    # limit when the dashboard polls.
    try:
        import cache_store
        cache_key = ("gex", symbol)
        hit = cache_store.cache_get(cache_key, ttl=_CACHE_TTL)
        if hit is not None:
            return hit
    except Exception:
        cache_store = None  # fall through; we still compute, just no cache
        cache_key = None

    try:
        ticker = yf.Ticker(symbol)
    except Exception as e:
        logger.error("Failed to create Ticker for %s: %s", symbol, e)
        return {"error": str(e)}

    # Spot price (with fetcher fall back so a yfinance auth failure
    # doesn't blank the whole GEX response when the chain itself is fine).
    spot = _get_spot_price(ticker, symbol=symbol)
    if not spot:
        return {"error": "Could not retrieve spot price for {}".format(symbol)}

    # Pull rate/dividend inputs once for the whole chain (cached / fail-open).
    risk_free = _get_risk_free_rate()
    div_yield = _get_dividend_yield(ticker)

    # Available expirations
    try:
        all_dates = ticker.options
    except Exception as e:
        logger.error("Failed to get options dates for %s: %s", symbol, e)
        return {"error": str(e)}

    if not all_dates:
        return {"error": "No options data available for {}".format(symbol)}

    dates_to_scan = list(all_dates[:4])

    # Per-strike accumulators
    # {strike: {"call_gex": float, "put_gex": float, "call_oi": int, "put_oi": int}}
    strike_map = {}  # type: Dict[float, dict]
    total_call_oi = 0
    total_put_oi = 0
    expirations_scanned = 0

    for exp_date in dates_to_scan:
        tte = _tte_from_expiry(exp_date)
        if tte <= 0:
            continue

        try:
            chain = ticker.option_chain(exp_date)
        except Exception as e:
            logger.warning("Skipping expiry %s for %s: %s", exp_date, symbol, e)
            continue

        expirations_scanned += 1

        import numpy as np

        for side, df, is_call in [("call", chain.calls, True), ("put", chain.puts, False)]:
            if df is None or df.empty:
                continue

            # Vectorize the OI / IV / strike extraction + BS gamma. The old
            # df.iterrows() + per-row _black_scholes_greeks() walked every
            # contract in Python; coercing the three columns to numpy arrays and
            # computing gamma in one shot is materially faster on dense chains
            # (SPX/SPY can have 1000s of strikes across 4 expiries) while
            # producing identical numbers.
            try:
                strikes = np.asarray(df.get("strike"), dtype=float)
                ois = np.asarray(df.get("openInterest"), dtype=float)
                ivs = np.asarray(df.get("impliedVolatility"), dtype=float)
            except (TypeError, ValueError):
                continue

            # Treat NaNs/missing as 0 (mirrors the `or 0` scalar fallbacks).
            strikes = np.nan_to_num(strikes, nan=0.0)
            ois = np.nan_to_num(ois, nan=0.0)
            ivs = np.nan_to_num(ivs, nan=0.0)
            ois = ois.astype(np.int64)  # int() truncation, as before

            # Filter: oi >= 10 and strike > 0 (same as the scalar guard).
            keep = (ois >= 10) & (strikes > 0)
            if not keep.any():
                continue

            k_arr = strikes[keep]
            oi_arr = ois[keep]
            iv_arr = ivs[keep]

            # BS gamma for the whole side at once.
            gamma_arr = _bs_gamma_vec(spot, k_arr, tte, iv_arr, risk_free, div_yield)

            # Honor a precomputed `gamma` column when present and > 0 (overrides
            # the BS value per-row, exactly as the scalar path did).
            if "gamma" in df.columns:
                try:
                    g_col = np.asarray(df["gamma"], dtype=float)
                    g_col = np.nan_to_num(g_col[keep], nan=0.0)
                    use_col = g_col > 0
                    gamma_arr = np.where(use_col, g_col, gamma_arr)
                except (TypeError, ValueError):
                    pass

            # Drop non-positive gammas.
            pos = gamma_arr > 0
            if not pos.any():
                continue
            k_arr = k_arr[pos]
            oi_arr = oi_arr[pos]
            gamma_arr = gamma_arr[pos]

            # GEX = OI * gamma * 100 * spot^2 * 0.01
            gex_arr = oi_arr * gamma_arr * 100.0 * spot * spot * 0.01

            # Calls -> positive gamma (dealer short calls = long gamma)
            # Puts  -> negative gamma (dealer short puts  = short gamma)
            #
            # NOTE: this call-long / put-short dealer sign is the
            # INDUSTRY-STANDARD NAIVE PROXY — it assumes dealers are net
            # short every call and net short every put. It is an assumption,
            # NOT measured order flow (we have no per-trade buy/sell tape),
            # so the directional sign of the resulting GEX can be wrong for
            # any name where customer positioning differs from that default.
            if not is_call:
                gex_arr = -gex_arr

            for strike_f, oi_v, signed_gex in zip(
                k_arr.tolist(), oi_arr.tolist(), gex_arr.tolist()
            ):
                if strike_f not in strike_map:
                    strike_map[strike_f] = {
                        "call_gex": 0.0,
                        "put_gex": 0.0,
                        "call_oi": 0,
                        "put_oi": 0,
                    }
                if is_call:
                    strike_map[strike_f]["call_gex"] += signed_gex
                    strike_map[strike_f]["call_oi"] += oi_v
                    total_call_oi += oi_v
                else:
                    strike_map[strike_f]["put_gex"] += signed_gex
                    strike_map[strike_f]["put_oi"] += oi_v
                    total_put_oi += oi_v

    if not strike_map:
        return {"error": "No valid options data after filtering for {}".format(symbol)}

    # Build per-strike list sorted by strike
    sorted_strikes = sorted(strike_map.keys())
    gex_by_strike = []  # type: List[dict]
    for s in sorted_strikes:
        info = strike_map[s]
        gex_by_strike.append({
            "strike": s,
            "call_gex": round(info["call_gex"], 2),
            "put_gex": round(info["put_gex"], 2),
            "net_gex": round(info["call_gex"] + info["put_gex"], 2),
        })

    # Net GEX
    net_gex = sum(item["net_gex"] for item in gex_by_strike)

    # Gamma flip point: where cumulative GEX crosses zero (low -> high)
    gamma_flip_price = None
    cumulative = 0.0
    prev_cum = 0.0
    prev_strike = None
    for item in gex_by_strike:
        cumulative += item["net_gex"]
        if prev_strike is not None:
            # Check for sign change (the prev_cum != 0 guard wrongly skipped the
            # first interval; the sign-change test below already handles prev==0)
            if (prev_cum < 0 and cumulative >= 0) or (prev_cum > 0 and cumulative <= 0):
                # Linear interpolation between prev_strike and current strike
                denom = cumulative - prev_cum
                if denom != 0:
                    frac = -prev_cum / denom
                    gamma_flip_price = prev_strike + frac * (item["strike"] - prev_strike)
                else:
                    gamma_flip_price = item["strike"]
                break
        prev_cum = cumulative
        prev_strike = item["strike"]

    # Top gamma walls (top 10 by absolute net_gex)
    sorted_by_abs = sorted(gex_by_strike, key=lambda x: abs(x["net_gex"]), reverse=True)
    top_gamma_walls = []
    for item in sorted_by_abs[:10]:
        wall_type = "resistance" if item["strike"] >= spot else "support"
        top_gamma_walls.append({
            "strike": item["strike"],
            "net_gex": item["net_gex"],
            "type": wall_type,
        })
    top_gamma_walls.sort(key=lambda x: x["strike"])

    # Gamma regime
    # Use a threshold relative to the absolute average to define "near zero"
    abs_values = [abs(item["net_gex"]) for item in gex_by_strike]
    avg_abs = sum(abs_values) / len(abs_values) if abs_values else 1.0
    if abs(net_gex) < avg_abs * 0.1:
        gamma_regime = "NEUTRAL"
    elif net_gex > 0:
        gamma_regime = "LONG GAMMA"
    else:
        gamma_regime = "SHORT GAMMA"

    # Dealer hedge estimates at various moves
    # Estimate delta change = net_gamma * price_change * 100 shares per contract
    net_gamma_raw = sum(
        strike_map[s]["call_gex"] + strike_map[s]["put_gex"]
        for s in strike_map
    )  # this is the net gex, which is proportional to gamma * OI
    # Approximate total dealer gamma in shares:
    # net_gex = sum(OI * gamma * 100 * S^2 * 0.01)
    # so total_gamma_shares ~ net_gex / (S^2 * 0.01) * S  (rough)
    # Simpler: delta_shares ~ net_gex / S * pct_move / 0.01
    dealer_hedge_estimates = {}
    for pct in [1, -1, 2, -2, 5, -5]:
        label = "+{}%".format(pct) if pct > 0 else "{}%".format(pct)
        new_price = spot * (1.0 + pct / 100.0)
        price_change = new_price - spot
        # Dealer delta change in shares ~ net_gex / (spot * 0.01) * (price_change / spot)
        # Simplified: delta_shares = net_gex * pct / (spot * 100)
        # More precisely: if dealers hold gamma G, delta change = G * dS
        # net_gex = G * 100 * S^2 * 0.01  =>  G = net_gex / (100 * S^2 * 0.01)
        # delta_change_per_contract = G * dS => total shares = G * dS * 100 (contracts)
        # but G already embedded total OI, so:
        # delta_shares = (net_gex / (S^2 * 0.01)) * price_change
        if spot > 0:
            # WHY (S5): (net_gex/(S^2*0.01))*ΔS is the change in the dealer's
            # delta INVENTORY (+Γ·ΔS). The hedge TRADE that keeps them
            # delta-neutral is the OPPOSITE sign: -Γ·ΔS. With long gamma
            # (net_gex>0) and an up move, dealers must SELL into strength to
            # stay flat — that's the stabilizing flow GEX is meant to show.
            # The unnegated value implied dealers BUYING the rally, inverting
            # the entire long-gamma pinning narrative. Negate it.
            delta_shares = -(net_gex / (spot * spot * 0.01)) * price_change
        else:
            delta_shares = 0.0
        dealer_hedge_estimates[label] = {
            "price": round(new_price, 2),
            "delta_shares": round(delta_shares, 0),
        }

    # Max pain
    mp_data = {}
    for s in strike_map:
        mp_data[s] = {
            "call_oi": strike_map[s]["call_oi"],
            "put_oi": strike_map[s]["put_oi"],
        }
    max_pain = _compute_max_pain(mp_data)

    # Put/call ratio
    put_call_ratio = round(total_put_oi / total_call_oi, 3) if total_call_oi > 0 else 0.0

    result = {
        "symbol": symbol,
        "spot_price": round(spot, 2),
        "net_gex": round(net_gex, 2),
        "net_gex_formatted": _format_gex_value(net_gex),
        "gamma_regime": gamma_regime,
        "gamma_flip_price": round(gamma_flip_price, 2) if gamma_flip_price is not None else None,
        "gex_by_strike": gex_by_strike,
        "top_gamma_walls": top_gamma_walls,
        "dealer_hedge_estimates": dealer_hedge_estimates,
        "expirations_scanned": expirations_scanned,
        "total_call_oi": total_call_oi,
        "total_put_oi": total_put_oi,
        "put_call_oi_ratio": put_call_ratio,
        "max_pain": max_pain,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    # Persist via cache_store. cache_set() refuses to cache values that look
    # like upstream failures (empty/error envelopes), so a transient Yahoo
    # 429 doesn't get pinned for the full TTL window.
    if cache_store is not None and cache_key is not None:
        try:
            cache_store.cache_set(cache_key, result, ttl=_CACHE_TTL)
        except Exception as e:
            logger.debug("gex cache_set failed for %s: %s", symbol, e)

    return result


def get_gex_summary(symbol):
    # type: (str) -> dict
    """
    Lighter GEX summary: net_gex, regime, flip price, top 5 walls.
    Suitable for composite scoring dashboards.
    """
    full = compute_gex(symbol)
    if "error" in full:
        return full

    walls = full.get("top_gamma_walls", [])[:5]

    return {
        "symbol": full["symbol"],
        "spot_price": full["spot_price"],
        "net_gex": full["net_gex"],
        "net_gex_formatted": full["net_gex_formatted"],
        "gamma_regime": full["gamma_regime"],
        "gamma_flip_price": full["gamma_flip_price"],
        "top_gamma_walls": walls,
        "max_pain": full["max_pain"],
        "put_call_oi_ratio": full["put_call_oi_ratio"],
        "timestamp": full["timestamp"],
    }
