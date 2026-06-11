"""
Alt-Data Revenue Nowcasting Engine for AUGUR.

Fuses 6 publicly available alternative data signals into a forward-looking
revenue estimate that predicts the probability of a company beating Wall Street
consensus earnings estimates. Outputs a nowcast score (0-100) representing
beat probability.
"""

import logging
import math
import time
from datetime import datetime
from typing import Dict, List, Optional

try:
    from zoneinfo import ZoneInfo  # Python 3.9+
    _ET = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover - fallback if tzdata missing
    _ET = None

import fetcher

logger = logging.getLogger(__name__)

# Module-level cache with TTL
_cache = {}        # type: Dict[str, object]
_cache_ts = {}     # type: Dict[str, float]
_CACHE_TTL = 3600  # seconds


def _cache_get(key):
    # type: (str) -> Optional[object]
    ts = _cache_ts.get(key)
    if ts is not None and (time.time() - ts) < _CACHE_TTL:
        return _cache.get(key)
    return None


def _cache_set(key, value):
    # type: (str, object) -> None
    _cache[key] = value
    _cache_ts[key] = time.time()


def _clamp(value, lo=0, hi=100):
    # type: (int, int, int) -> int
    if value < lo:
        return lo
    if value > hi:
        return hi
    return value


def _fmt_market_cap(mc):
    """Format market cap as $XB or $XM string."""
    if mc is None:
        return "N/A"
    try:
        mc = float(mc)
    except (TypeError, ValueError):
        return "N/A"
    if mc >= 1e9:
        return "${:.1f}B".format(mc / 1e9)
    if mc >= 1e6:
        return "${:.0f}M".format(mc / 1e6)
    return "${:,.0f}".format(mc)


# ---------------------------------------------------------------------------
# Signal scorers
# ---------------------------------------------------------------------------

def _score_revenue_momentum(fundamentals):
    # type: (dict) -> dict
    """Signal 1: Revenue Momentum (weight 30%)."""
    rev_growth = fundamentals.get("revenue_growth")
    earn_growth = fundamentals.get("earnings_growth")
    profit_margin = fundamentals.get("profit_margin")

    # Convert to percentages if they look like decimals (e.g. 0.25 -> 25)
    if rev_growth is not None:
        rev_growth = float(rev_growth)
        if -1 < rev_growth < 1 and rev_growth != 0:
            rev_growth = rev_growth * 100
    if earn_growth is not None:
        earn_growth = float(earn_growth)
        if -1 < earn_growth < 1 and earn_growth != 0:
            earn_growth = earn_growth * 100
    if profit_margin is not None:
        profit_margin = float(profit_margin)
        if -1 < profit_margin < 1 and profit_margin != 0:
            profit_margin = profit_margin * 100

    if rev_growth is None:
        return {"score": 50, "detail": "Revenue growth data unavailable", "weight": 0.30}

    if rev_growth > 30:
        score = 90
    elif rev_growth > 20:
        score = 75
    elif rev_growth > 10:
        score = 60
    elif rev_growth > 5:
        score = 50
    elif rev_growth > 0:
        score = 40
    else:
        score = 20

    if earn_growth is not None and rev_growth is not None and earn_growth > rev_growth:
        score += 10  # margin expansion
    if profit_margin is not None and profit_margin > 20:
        score += 5  # high quality revenue

    score = _clamp(score)

    rg_str = "{:.1f}".format(rev_growth) if rev_growth is not None else "N/A"
    eg_str = "{:.1f}".format(earn_growth) if earn_growth is not None else "N/A"
    detail = "Revenue growth: {}%, Earnings growth: {}%".format(rg_str, eg_str)

    return {"score": score, "detail": detail, "weight": 0.30}


def _score_volume_divergence(symbol):
    # type: (str) -> dict
    """Signal 2: Volume Trend Divergence (weight 20%)."""
    chart = fetcher.get_chart_data(symbol, period="3mo", interval="1d")
    if not chart or len(chart) < 40:
        return {"score": 50, "detail": "Insufficient chart data", "weight": 0.20}

    recent = chart[-20:]
    prior = chart[-40:-20]

    recent_close_avg = sum(d.get("close", 0) or 0 for d in recent) / len(recent)
    prior_close_avg = sum(d.get("close", 0) or 0 for d in prior) / len(prior)

    recent_vol_avg = sum(d.get("volume", 0) or 0 for d in recent) / len(recent)
    prior_vol_avg = sum(d.get("volume", 0) or 0 for d in prior) / len(prior)

    if prior_close_avg == 0 or prior_vol_avg == 0:
        return {"score": 50, "detail": "Insufficient price/volume data", "weight": 0.20}

    price_change_pct = (recent_close_avg - prior_close_avg) / prior_close_avg * 100
    vol_change_pct = (recent_vol_avg - prior_vol_avg) / prior_vol_avg * 100

    price_rising = price_change_pct > 1
    price_falling = price_change_pct < -1
    vol_rising = vol_change_pct > 10
    vol_falling = vol_change_pct < -10

    if vol_rising and (price_rising or not price_falling):
        score = 80  # accumulation
    elif vol_rising and price_falling:
        score = 25  # distribution
    elif vol_falling and price_rising:
        score = 40  # weak rally
    elif vol_falling and price_falling:
        score = 55  # capitulation fading
    else:
        score = 50  # normal

    vol_trend = "rising ({:+.0f}%)".format(vol_change_pct) if vol_rising else (
        "falling ({:+.0f}%)".format(vol_change_pct) if vol_falling else "flat"
    )
    price_trend = "rising ({:+.1f}%)".format(price_change_pct) if price_rising else (
        "falling ({:+.1f}%)".format(price_change_pct) if price_falling else "flat"
    )

    detail = "Volume {}, Price {}".format(vol_trend, price_trend)
    return {"score": score, "detail": detail, "weight": 0.20}


def _score_options_implied(symbol, current_price):
    # type: (str, float) -> dict
    """Signal 3: Options Implied Move (weight 15%)."""
    dates = fetcher.get_options_dates(symbol)
    if not dates:
        return {"score": 50, "detail": "No options data available", "weight": 0.15}

    nearest_date = dates[0]
    chain = fetcher.get_option_chain(symbol, nearest_date)
    if not chain:
        return {"score": 50, "detail": "Options chain unavailable", "weight": 0.15}

    calls = chain.get("calls", [])
    puts = chain.get("puts", [])

    if not calls or not puts or current_price is None or current_price <= 0:
        return {"score": 50, "detail": "Incomplete options data", "weight": 0.15}

    # Find ATM call and put
    def find_atm(contracts):
        best = None
        best_diff = float("inf")
        for c in contracts:
            strike = c.get("strike")
            if strike is None:
                continue
            diff = abs(float(strike) - current_price)
            if diff < best_diff:
                best_diff = diff
                best = c
        return best

    atm_call = find_atm(calls)
    atm_put = find_atm(puts)

    if atm_call is None or atm_put is None:
        return {"score": 50, "detail": "Could not find ATM options", "weight": 0.15}

    call_price = atm_call.get("lastPrice") or atm_call.get("bid") or 0
    put_price = atm_put.get("lastPrice") or atm_put.get("bid") or 0

    try:
        call_price = float(call_price)
        put_price = float(put_price)
    except (TypeError, ValueError):
        return {"score": 50, "detail": "Invalid option prices", "weight": 0.15}

    implied_move_pct = (call_price + put_price) / current_price * 100

    # Days to expiration — options trade ET, so use NY date for "today".
    try:
        today_et = datetime.now(_ET).date() if _ET else datetime.utcnow().date()
        expiry_dt = datetime.strptime(nearest_date[:10], "%Y-%m-%d").date()
        dte = max(1, (expiry_dt - today_et).days)
    except Exception:
        dte = 30  # safe fallback

    # Compute historical average move from ATR. ATR is a *daily* range while
    # the straddle premium is the implied move over the option's full life,
    # so scale daily ATR by sqrt(dte) to compare like-for-like.
    chart = fetcher.get_chart_data(symbol, period="3mo", interval="1d")
    indicators = fetcher.compute_indicators(chart) if chart else {}
    atr = indicators.get("ATR")
    if atr and current_price > 0:
        historical_avg_pct = float(atr) / current_price * 100 * math.sqrt(dte)
    else:
        historical_avg_pct = implied_move_pct  # fallback: treat as normal

    if historical_avg_pct > 0:
        ratio = implied_move_pct / historical_avg_pct
    else:
        ratio = 1.0

    if ratio > 1.5:
        score = 80
    elif ratio > 1.2:
        score = 65
    elif ratio > 0.8:
        score = 50
    else:
        score = 35

    detail = "Implied move: {:.1f}%, Historical avg: {:.1f}%".format(
        implied_move_pct, historical_avg_pct
    )
    return {"score": score, "detail": detail, "weight": 0.15}


def _score_analyst_velocity(fundamentals):
    # type: (dict) -> dict
    """Signal 4: Analyst Revision Velocity (weight 15%)."""
    target = fundamentals.get("analyst_target")
    price = fundamentals.get("price")
    rec = fundamentals.get("recommendation")
    num_analysts = fundamentals.get("num_analysts")

    if price is None or price <= 0:
        return {"score": 50, "detail": "Price data unavailable", "weight": 0.15}

    # Normalize recommendation
    rec_lower = str(rec).lower().strip() if rec else ""

    rec_scores = {
        "strongbuy": 85,
        "strong_buy": 85,
        "buy": 70,
        "overweight": 70,
        "outperform": 70,
        "hold": 45,
        "neutral": 45,
        "equal-weight": 45,
        "underweight": 30,
        "underperform": 30,
        "sell": 25,
        "strongsell": 10,
        "strong_sell": 10,
    }

    score = rec_scores.get(rec_lower, 50)

    # Target upside adjustment
    if target is not None:
        try:
            target = float(target)
            price = float(price)
            upside = (target - price) / price * 100
            if upside > 30:
                score += 10
            elif upside > 15:
                score += 5
            elif upside < 0:
                score -= 15
        except (TypeError, ValueError, ZeroDivisionError):
            upside = 0
    else:
        upside = 0
        target = 0

    score = _clamp(score)

    rec_display = rec if rec else "N/A"
    detail = "Consensus: {}, Target: ${:.0f} ({:+.1f}%)".format(rec_display, target, upside)
    return {"score": score, "detail": detail, "weight": 0.15}


def _score_sector_momentum(symbol, fundamentals):
    # type: (str, dict) -> dict
    """Signal 5: Sector Relative Momentum (weight 15%)."""
    sector = fundamentals.get("sector", "")

    # Get sector performance
    sector_perf = fetcher.get_sector_performance()
    sector_change = None
    if sector_perf and sector:
        sector_lower = sector.lower()
        for sp in sector_perf:
            sp_name = sp.get("name", "")
            if sp_name and sector_lower in sp_name.lower():
                sector_change = sp.get("change_pct")
                break

    # Get stock's 1-month return
    chart = fetcher.get_chart_data(symbol, period="1mo", interval="1d")
    if chart and len(chart) >= 2:
        first_close = chart[0].get("close") or 0
        last_close = chart[-1].get("close") or 0
        if first_close > 0:
            stock_return = (last_close - first_close) / first_close * 100
        else:
            stock_return = 0
    else:
        return {"score": 50, "detail": "Insufficient chart data for sector comparison", "weight": 0.15}

    if sector_change is None:
        sector_change = 0.0  # fallback

    try:
        sector_change = float(sector_change)
    except (TypeError, ValueError):
        sector_change = 0.0

    relative = stock_return - sector_change

    if relative > 10:
        score = 90
    elif relative > 5:
        score = 75
    elif relative > 2:
        score = 60
    elif relative > -2:
        score = 50
    elif relative > -5:
        score = 35
    else:
        score = 20

    detail = "Stock: {:+.1f}% vs Sector: {:+.1f}%".format(stock_return, sector_change)
    return {"score": score, "detail": detail, "weight": 0.15}


def _score_scale_trajectory(fundamentals):
    # type: (dict) -> dict
    """Signal 6: Scale Trajectory (weight 5%)."""
    employees = fundamentals.get("employees")
    market_cap = fundamentals.get("market_cap")
    rev_growth = fundamentals.get("revenue_growth")

    # Normalize revenue_growth to percentage
    if rev_growth is not None:
        rev_growth = float(rev_growth)
        if -1 < rev_growth < 1 and rev_growth != 0:
            rev_growth = rev_growth * 100

    try:
        employees = int(employees) if employees else 0
    except (TypeError, ValueError):
        employees = 0

    try:
        market_cap_val = float(market_cap) if market_cap else 0
    except (TypeError, ValueError):
        market_cap_val = 0

    rg = rev_growth if rev_growth is not None else 0

    if employees > 50000 and market_cap_val > 50e9:
        score = 70
    elif employees > 10000 and rg > 10:
        score = 75
    elif employees > 1000 and rg > 20:
        score = 80
    elif employees < 1000 and rg > 30:
        score = 65
    else:
        score = 50

    mc_str = _fmt_market_cap(market_cap_val)
    detail = "{:,} employees, {} market cap".format(employees, mc_str)
    return {"score": score, "detail": detail, "weight": 0.05}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def nowcast_revenue(symbol):
    # type: (str) -> dict
    """
    Main entry point. Score 6 signals and produce composite revenue nowcast.

    Returns a dict with nowcast_score (0-100), beat_probability, signals, etc.
    On error returns {"error": str}.
    """
    try:
        symbol = symbol.upper().strip()
        cache_key = "nowcast_{}".format(symbol)
        cached = _cache_get(cache_key)
        if cached is not None:
            return cached

        # Fetch fundamentals (shared across multiple signals)
        fundamentals = fetcher.get_fundamentals(symbol) or {}
        current_price = fundamentals.get("price")
        if current_price is not None:
            try:
                current_price = float(current_price)
            except (TypeError, ValueError):
                current_price = None

        # Score all 6 signals, each wrapped in try/except
        signals = {}

        try:
            signals["revenue_momentum"] = _score_revenue_momentum(fundamentals)
        except Exception as e:
            logger.warning("Signal 1 (revenue_momentum) failed for %s: %s", symbol, e)
            signals["revenue_momentum"] = {"score": 50, "detail": "data unavailable", "weight": 0.30}

        try:
            signals["volume_divergence"] = _score_volume_divergence(symbol)
        except Exception as e:
            logger.warning("Signal 2 (volume_divergence) failed for %s: %s", symbol, e)
            signals["volume_divergence"] = {"score": 50, "detail": "data unavailable", "weight": 0.20}

        try:
            signals["options_implied"] = _score_options_implied(symbol, current_price)
        except Exception as e:
            logger.warning("Signal 3 (options_implied) failed for %s: %s", symbol, e)
            signals["options_implied"] = {"score": 50, "detail": "data unavailable", "weight": 0.15}

        try:
            signals["analyst_velocity"] = _score_analyst_velocity(fundamentals)
        except Exception as e:
            logger.warning("Signal 4 (analyst_velocity) failed for %s: %s", symbol, e)
            signals["analyst_velocity"] = {"score": 50, "detail": "data unavailable", "weight": 0.15}

        try:
            signals["sector_momentum"] = _score_sector_momentum(symbol, fundamentals)
        except Exception as e:
            logger.warning("Signal 5 (sector_momentum) failed for %s: %s", symbol, e)
            signals["sector_momentum"] = {"score": 50, "detail": "data unavailable", "weight": 0.15}

        try:
            signals["scale_trajectory"] = _score_scale_trajectory(fundamentals)
        except Exception as e:
            logger.warning("Signal 6 (scale_trajectory) failed for %s: %s", symbol, e)
            signals["scale_trajectory"] = {"score": 50, "detail": "data unavailable", "weight": 0.05}

        # Composite score
        s1 = signals["revenue_momentum"]["score"]
        s2 = signals["volume_divergence"]["score"]
        s3 = signals["options_implied"]["score"]
        s4 = signals["analyst_velocity"]["score"]
        s5 = signals["sector_momentum"]["score"]
        s6 = signals["scale_trajectory"]["score"]

        nowcast = int(s1 * 0.30 + s2 * 0.20 + s3 * 0.15 + s4 * 0.15 + s5 * 0.15 + s6 * 0.05)
        nowcast = _clamp(nowcast)

        # Beat probability
        if nowcast > 70:
            beat_probability = "LIKELY BEAT"
        elif nowcast >= 45:
            beat_probability = "UNCERTAIN"
        else:
            beat_probability = "LIKELY MISS"

        # Estimated surprise magnitude
        estimated_surprise_pct = round((nowcast - 50) * 0.15, 2)

        # Confidence level
        all_scores = [s1, s2, s3, s4, s5, s6]
        data_available = sum(
            1 for sig in signals.values() if sig["detail"] != "data unavailable"
        )
        score_range = max(all_scores) - min(all_scores)

        if data_available >= 4 and score_range < 40:
            confidence = "HIGH"
        elif data_available >= 3:
            confidence = "MODERATE"
        else:
            confidence = "LOW"

        # Overall signal label
        if nowcast >= 75:
            signal_label = "STRONG BEAT SIGNAL"
        elif nowcast >= 60:
            signal_label = "POSITIVE LEAN"
        elif nowcast >= 45:
            signal_label = "MIXED"
        elif nowcast >= 30:
            signal_label = "NEGATIVE LEAN"
        else:
            signal_label = "MISS SIGNAL"

        result = {
            "symbol": symbol,
            "nowcast_score": nowcast,
            "beat_probability": beat_probability,
            "estimated_surprise_pct": estimated_surprise_pct,
            "confidence_level": confidence,
            "signals": signals,
            "fundamentals_snapshot": {
                "revenue_growth": fundamentals.get("revenue_growth"),
                "earnings_growth": fundamentals.get("earnings_growth"),
                "profit_margin": fundamentals.get("profit_margin"),
                "analyst_target": fundamentals.get("analyst_target"),
                "recommendation": fundamentals.get("recommendation"),
            },
            "signal": signal_label,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }

        _cache_set(cache_key, result)
        return result

    except Exception as e:
        logger.error("nowcast_revenue failed for %s: %s", symbol, e)
        return {"error": str(e)}


def nowcast_bulk(symbols):
    # type: (list) -> list
    """
    Score multiple symbols in parallel via safe_executor.parallel_map.

    Caps at 15 symbols. Returns list sorted by nowcast_score descending.
    On error returns [{"error": str}].
    """
    try:
        if not symbols:
            return []

        # Cap at 15 symbols
        symbols = [s.upper().strip() for s in symbols[:15]]

        # Bundle-state guard: route through safe_executor so a tripped
        # `concurrent.futures.thread._shutdown` flag falls back to serial
        # rather than 500-ing the bulk endpoint. nowcast_revenue() returns
        # error-dicts instead of raising, so parallel_map's None-on-raise
        # rule rarely trips here — but synthesize an error-dict for any
        # None slot defensively.
        import safe_executor
        raw = safe_executor.parallel_map(
            nowcast_revenue, symbols, max_workers=3,
            thread_name_prefix="alt-nowcast",
        )
        results = []
        for sym, r in zip(symbols, raw):
            if r is None:
                results.append({"symbol": sym, "error": "nowcast_revenue returned None"})
            else:
                results.append(r)

        # Filter out errors for sorting, but keep them in output
        valid = [r for r in results if "error" not in r]
        errors = [r for r in results if "error" in r]

        valid.sort(key=lambda x: x.get("nowcast_score", 0), reverse=True)

        return valid + errors

    except Exception as e:
        logger.error("nowcast_bulk failed: %s", e)
        return [{"error": str(e)}]
