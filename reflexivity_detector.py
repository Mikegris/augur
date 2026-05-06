"""
Reflexivity Detector — detects self-reinforcing feedback loops (Soros's
reflexivity) before they accelerate.

When a stock's price movement itself becomes the fundamental driver:
  - Rising stock improves borrowing -> funds growth -> drives stock higher
  - Falling stock triggers covenant violations -> forced selling -> drives lower

Detects 4 loop types and their proximity to trigger points:
  1. Equity Feedback Loop
  2. Dilution Spiral
  3. Index Inclusion/Exclusion
  4. Short Squeeze

Part of the AUGUR wealth intelligence platform.
Python 3.9 compatible (no match/case, no X | Y unions).
"""

import logging
import time
from datetime import datetime
from typing import Dict, List, Optional

import sec_edgar as edgar
import fetcher

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level cache
# ---------------------------------------------------------------------------
_cache = {}       # type: Dict[str, object]
_cache_ts = {}    # type: Dict[str, float]
_CACHE_TTL = 1800  # 30 minutes


def _get_cached(key):
    # type: (str) -> object
    if key in _cache and (time.time() - _cache_ts.get(key, 0)) < _CACHE_TTL:
        return _cache[key]
    return None


def _set_cached(key, value):
    # type: (str, object) -> None
    _cache[key] = value
    _cache_ts[key] = time.time()
    if len(_cache) > 200:
        cutoff = time.time() - _CACHE_TTL
        stale = [k for k, t in _cache_ts.items() if t < cutoff]
        for k in stale:
            _cache.pop(k, None)
            _cache_ts.pop(k, None)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _price_trend(price_data, days):
    # type: (list, int) -> float
    """Calculate linear regression slope over the last N trading days.

    Returns slope of best-fit line through closing prices.
    Positive = uptrend, negative = downtrend.
    """
    if not price_data or len(price_data) < 2:
        return 0.0
    subset = price_data[-days:] if len(price_data) >= days else price_data
    closes = []
    for bar in subset:
        c = bar.get("close")
        if c is not None:
            closes.append(float(c))
    n = len(closes)
    if n < 2:
        return 0.0
    # Simple linear regression: y = closes, x = 0..n-1
    sum_x = 0.0
    sum_y = 0.0
    sum_xy = 0.0
    sum_x2 = 0.0
    for i, y in enumerate(closes):
        x = float(i)
        sum_x += x
        sum_y += y
        sum_xy += x * y
        sum_x2 += x * x
    denom = n * sum_x2 - sum_x * sum_x
    if denom == 0:
        return 0.0
    slope = (n * sum_xy - sum_x * sum_y) / denom
    return slope


def _pct_change_over(price_data, days):
    # type: (list, int) -> Optional[float]
    """Return percentage change in closing price over last N trading days."""
    if not price_data or len(price_data) < 2:
        return None
    subset = price_data[-days:] if len(price_data) >= days else price_data
    first_close = None
    last_close = None
    for bar in subset:
        c = bar.get("close")
        if c is not None:
            if first_close is None:
                first_close = float(c)
            last_close = float(c)
    if first_close is None or last_close is None or first_close == 0:
        return None
    return ((last_close - first_close) / first_close) * 100.0


def _scan_filing_keywords(symbol, filings):
    # type: (str, list) -> list
    """Extract reflexivity-related keywords from filing text.

    Scans the first 2 filings to limit EDGAR API calls.
    Returns list of found keyword strings.
    """
    keywords_to_find = [
        "convertible", "conversion", "dilution", "anti-dilution",
        "warrant", "covenant", "debt covenant", "credit facility",
        "shelf registration", "at-the-market", "ATM offering",
    ]
    found = []
    cache_key = "filing_kw_{}".format(symbol.upper())
    cached = _get_cached(cache_key)
    if cached is not None:
        return cached

    if not filings:
        return found

    try:
        cik = edgar.get_cik(symbol)
    except Exception:
        logger.debug("Could not get CIK for %s, skipping filing scan", symbol)
        return found

    if not cik:
        return found

    scanned = 0
    for filing in filings:
        if scanned >= 2:
            break
        try:
            accession = filing.get("accession", "")
            primary_doc = filing.get("primary_document", "")
            form_type = filing.get("form_type", "")
            if not accession or not primary_doc:
                continue
            text = edgar.get_filing_text(cik, accession, primary_doc, form_type)
            if not text:
                continue
            text_lower = text.lower()
            for kw in keywords_to_find:
                if kw.lower() in text_lower and kw not in found:
                    found.append(kw)
            scanned += 1
        except Exception as e:
            logger.debug("Error scanning filing for %s: %s", symbol, e)
            continue

    _set_cached(cache_key, found)
    return found


def _make_loop_result(loop_type, detected=False, strength=0, direction="NEUTRAL",
                      proximity=0.0, timeline="N/A", detail="", metrics=None):
    # type: (str, bool, int, str, float, str, str, Optional[dict]) -> dict
    """Build a consistent loop result dict."""
    return {
        "type": loop_type,
        "detected": detected,
        "strength": min(max(strength, 0), 100),
        "direction": direction,
        "proximity_to_trigger_pct": round(proximity, 2),
        "acceleration_timeline": timeline,
        "detail": detail,
        "metrics": metrics or {},
    }


# ---------------------------------------------------------------------------
# Loop detectors
# ---------------------------------------------------------------------------

def _detect_equity_feedback(price_data, fundamentals):
    # type: (list, dict) -> dict
    """Equity Feedback Loop: rising stock -> better borrowing -> funds growth
    -> drives stock higher (virtuous), or the reverse (vicious)."""
    loop_type = "EQUITY_FEEDBACK"
    try:
        slope_63 = _price_trend(price_data, 63)
        pct_3m = _pct_change_over(price_data, 63)

        debt_equity = fundamentals.get("debt_equity")
        revenue_growth = fundamentals.get("revenue_growth")
        profit_margin = fundamentals.get("profit_margin")
        price = fundamentals.get("price")
        high_52w = fundamentals.get("fifty_two_week_high") or fundamentals.get("year_high")
        low_52w = fundamentals.get("fifty_two_week_low") or fundamentals.get("year_low")

        uptrend = slope_63 > 0
        low_debt = debt_equity is not None and debt_equity < 1.0
        growing = revenue_growth is not None and revenue_growth > 0
        profitable = profit_margin is not None and profit_margin > 0

        # Check for vicious reverse
        downtrend = slope_63 < 0
        high_debt = debt_equity is not None and debt_equity > 1.5
        shrinking = revenue_growth is not None and revenue_growth < 0

        metrics = {
            "slope_63d": round(slope_63, 4),
            "pct_change_3m": round(pct_3m, 2) if pct_3m is not None else None,
            "debt_equity": debt_equity,
            "revenue_growth": revenue_growth,
            "profit_margin": profit_margin,
        }

        # --- Virtuous path ---
        if uptrend and low_debt and growing:
            strength = 30
            if pct_3m is not None and pct_3m > 20:
                strength += 20
            if revenue_growth is not None and revenue_growth > 0.15:
                strength += 15
            if debt_equity is not None and debt_equity < 0.5:
                strength += 15
            if profit_margin is not None and profit_margin > 0.15:
                strength += 10
            # Near 52-week high?
            if price is not None and high_52w is not None and high_52w > 0:
                pct_from_high = ((high_52w - price) / high_52w) * 100
                if pct_from_high <= 5:
                    strength += 10
                metrics["pct_from_52w_high"] = round(pct_from_high, 2)

            proximity = 0.0
            if price is not None and high_52w is not None and high_52w > 0:
                proximity = max(0, 100 - ((high_52w - price) / high_52w) * 100)

            if strength > 70:
                timeline = "Active and accelerating"
            elif strength >= 40:
                timeline = "Building momentum, 1-3 months"
            else:
                timeline = "Early stage, 3-6 months"

            detail = ("Virtuous equity feedback detected: rising price (%.1f%% 3m) "
                      "with low leverage (D/E %.2f) and revenue growth (%.1f%%) "
                      "creates favorable borrowing conditions that fund further growth."
                      % (pct_3m or 0, debt_equity or 0,
                         (revenue_growth or 0) * 100))

            return _make_loop_result(loop_type, detected=True, strength=strength,
                                     direction="VIRTUOUS", proximity=proximity,
                                     timeline=timeline, detail=detail, metrics=metrics)

        # --- Vicious path ---
        if downtrend and high_debt and shrinking:
            strength = 30
            if pct_3m is not None and pct_3m < -20:
                strength += 20
            if revenue_growth is not None and revenue_growth < -0.15:
                strength += 15
            if debt_equity is not None and debt_equity > 2.0:
                strength += 15
            if profit_margin is not None and profit_margin < 0:
                strength += 10

            proximity = 0.0
            if price is not None and low_52w is not None and low_52w > 0:
                pct_from_low = ((price - low_52w) / low_52w) * 100
                proximity = max(0, 100 - pct_from_low)
                metrics["pct_from_52w_low"] = round(pct_from_low, 2)

            if strength > 70:
                timeline = "Active and accelerating"
            elif strength >= 40:
                timeline = "Deteriorating, 1-3 months"
            else:
                timeline = "Early warning, 3-6 months"

            detail = ("Vicious equity feedback detected: falling price (%.1f%% 3m) "
                      "with high leverage (D/E %.2f) and shrinking revenue (%.1f%%) "
                      "tightens borrowing conditions and accelerates decline."
                      % (pct_3m or 0, debt_equity or 0,
                         (revenue_growth or 0) * 100))

            return _make_loop_result(loop_type, detected=True, strength=strength,
                                     direction="VICIOUS", proximity=proximity,
                                     timeline=timeline, detail=detail, metrics=metrics)

        # Not detected
        return _make_loop_result(loop_type, detail="No equity feedback loop detected.",
                                 metrics=metrics)

    except Exception as e:
        logger.warning("Equity feedback detection error: %s", e)
        return _make_loop_result(loop_type, detail="Error during detection: {}".format(e))


def _detect_dilution_spiral(price_data, fundamentals, filing_keywords):
    # type: (list, dict, list) -> dict
    """Dilution Spiral: falling stock -> convertible conversion -> dilution
    -> more selling -> further decline."""
    loop_type = "DILUTION_SPIRAL"
    try:
        slope_63 = _price_trend(price_data, 63)
        pct_3m = _pct_change_over(price_data, 63)

        shares_out = fundamentals.get("shares_outstanding")
        float_shares = fundamentals.get("float_shares")

        downtrend = slope_63 < 0

        kw_lower = [kw.lower() for kw in filing_keywords]
        has_convertible = any("convertible" in kw or "conversion" in kw for kw in kw_lower)
        has_dilution = any("dilution" in kw or "anti-dilution" in kw for kw in kw_lower)
        has_warrant = any("warrant" in kw for kw in kw_lower)
        has_atm = any("at-the-market" in kw or "atm offering" in kw for kw in kw_lower)

        convertible_mentions = has_convertible or has_warrant or has_atm

        metrics = {
            "slope_63d": round(slope_63, 4),
            "pct_change_3m": round(pct_3m, 2) if pct_3m is not None else None,
            "shares_outstanding": shares_out,
            "float_shares": float_shares,
            "filing_keywords_found": filing_keywords,
        }

        if downtrend and convertible_mentions:
            strength = 25
            if pct_3m is not None and pct_3m < -30:
                strength += 25
            elif pct_3m is not None and pct_3m < -15:
                strength += 12
            if has_convertible:
                strength += 20
            if has_dilution:
                strength += 15
            if (shares_out is not None and float_shares is not None
                    and float_shares > 0 and shares_out > float_shares * 1.1):
                strength += 15
                metrics["shares_to_float_ratio"] = round(shares_out / float_shares, 3)

            # Proximity: steeper decline = closer to trigger
            proximity = 0.0
            if pct_3m is not None:
                proximity = min(100, abs(pct_3m) * 2)

            if strength > 70:
                timeline = "Active spiral, 2-4 weeks"
            elif strength >= 40:
                timeline = "Accelerating, 1-3 months"
            else:
                timeline = "Potential risk, 3-6 months"

            detail = ("Dilution spiral risk: price down %.1f%% over 3 months with "
                      "convertible/dilutive instruments detected in filings. "
                      "Continued decline may trigger conversions, increasing share "
                      "count and driving further selling."
                      % (pct_3m or 0,))

            return _make_loop_result(loop_type, detected=True, strength=strength,
                                     direction="VICIOUS", proximity=proximity,
                                     timeline=timeline, detail=detail, metrics=metrics)

        return _make_loop_result(loop_type, detail="No dilution spiral detected.",
                                 metrics=metrics)

    except Exception as e:
        logger.warning("Dilution spiral detection error: %s", e)
        return _make_loop_result(loop_type, detail="Error during detection: {}".format(e))


def _detect_index_inclusion(fundamentals):
    # type: (dict,) -> dict
    """Index Inclusion/Exclusion: approaching market cap thresholds triggers
    massive passive fund flows."""
    loop_type = "INDEX_INCLUSION"
    try:
        market_cap = fundamentals.get("market_cap")
        earnings_growth = fundamentals.get("earnings_growth")
        price = fundamentals.get("price")

        metrics = {
            "market_cap": market_cap,
            "earnings_growth": earnings_growth,
        }

        if market_cap is None or market_cap <= 0:
            return _make_loop_result(loop_type,
                                     detail="Market cap unavailable, cannot assess.",
                                     metrics=metrics)

        cap_b = market_cap / 1e9  # in billions

        # Thresholds (in billions)
        thresholds = [
            ("S&P 500 inclusion", 18.0, "VIRTUOUS",
             "Approaching S&P 500 inclusion threshold (~$18B). "
             "Inclusion triggers massive passive fund buying."),
            ("S&P 500 exclusion", 8.0, "VICIOUS",
             "Approaching S&P 500 exclusion threshold (~$8B). "
             "Exclusion triggers forced selling by index funds."),
            ("Russell 1000", 5.0, "VIRTUOUS",
             "Approaching Russell 1000 threshold (~$5B). "
             "Inclusion/exclusion triggers institutional rebalancing."),
            ("Russell 2000 inclusion", 0.3, "VIRTUOUS",
             "Approaching Russell 2000 inclusion threshold (~$300M). "
             "Inclusion triggers small-cap fund buying."),
        ]

        best_match = None
        best_strength = 0

        for name, threshold_b, base_direction, base_detail in thresholds:
            threshold_val = threshold_b * 1e9
            pct_distance = abs(market_cap - threshold_val) / threshold_val * 100

            # Determine direction based on which side we're on
            if name == "S&P 500 exclusion":
                # Vicious if falling toward exclusion from above
                if cap_b > threshold_b:
                    direction = "VICIOUS"
                else:
                    continue  # already below, not relevant
            elif name == "S&P 500 inclusion":
                if cap_b < threshold_b:
                    direction = "VIRTUOUS"  # approaching from below
                else:
                    continue  # already above
            elif name == "Russell 1000":
                if cap_b < threshold_b and cap_b > 3.0:
                    direction = "VIRTUOUS"
                elif cap_b > threshold_b and cap_b < 7.0:
                    direction = "VICIOUS"  # could drop below
                else:
                    continue
            elif name == "Russell 2000 inclusion":
                if cap_b < threshold_b and cap_b > 0.15:
                    direction = "VIRTUOUS"
                else:
                    continue
            else:
                direction = base_direction

            # Only detect if within 30%
            if pct_distance > 30:
                continue

            if pct_distance <= 5:
                strength = 90
            elif pct_distance <= 10:
                strength = 70
            elif pct_distance <= 20:
                strength = 50
            else:
                strength = 30

            # Bonus for positive earnings (S&P 500 requires it)
            if "S&P 500 inclusion" in name and earnings_growth is not None and earnings_growth > 0:
                strength = min(100, strength + 5)

            if strength > best_strength:
                best_strength = strength
                proximity = max(0, 100 - pct_distance)
                if strength >= 70:
                    timeline = "Next rebalance, 2-4 weeks"
                elif strength >= 50:
                    timeline = "1-2 quarters"
                else:
                    timeline = "2-4 quarters"

                detail = ("{} Market cap ${:.1f}B is {:.1f}% from ${:.0f}B threshold."
                          .format(base_detail, cap_b, pct_distance, threshold_b))

                best_match = _make_loop_result(
                    loop_type, detected=True, strength=strength,
                    direction=direction, proximity=proximity,
                    timeline=timeline, detail=detail,
                    metrics=dict(metrics, threshold_name=name,
                                 threshold_b=threshold_b,
                                 market_cap_b=round(cap_b, 2),
                                 pct_from_threshold=round(pct_distance, 2)),
                )

        if best_match is not None:
            return best_match

        return _make_loop_result(loop_type,
                                 detail="Not near any index inclusion/exclusion threshold.",
                                 metrics=dict(metrics, market_cap_b=round(cap_b, 2)))

    except Exception as e:
        logger.warning("Index inclusion detection error: %s", e)
        return _make_loop_result(loop_type, detail="Error during detection: {}".format(e))


def _detect_short_squeeze(price_data, fundamentals):
    # type: (list, dict) -> dict
    """Short Squeeze: high short interest + rising price -> shorts forced to
    cover -> more buying pressure."""
    loop_type = "SHORT_SQUEEZE"
    try:
        short_pct = fundamentals.get("short_percent_float")
        short_ratio = fundamentals.get("short_ratio")

        if short_pct is None:
            return _make_loop_result(loop_type,
                                     detail="Short interest data unavailable.",
                                     metrics={"short_percent_float": None})

        slope_20 = _price_trend(price_data, 20)
        pct_20d = _pct_change_over(price_data, 20)

        # Volume analysis
        vol_5d_avg = 0.0
        vol_20d_avg = 0.0
        if price_data and len(price_data) >= 20:
            recent_5 = price_data[-5:]
            recent_20 = price_data[-20:]
            vols_5 = [bar.get("volume", 0) for bar in recent_5 if bar.get("volume")]
            vols_20 = [bar.get("volume", 0) for bar in recent_20 if bar.get("volume")]
            if vols_5:
                vol_5d_avg = sum(vols_5) / len(vols_5)
            if vols_20:
                vol_20d_avg = sum(vols_20) / len(vols_20)

        vol_surge = (vol_5d_avg / vol_20d_avg) if vol_20d_avg > 0 else 1.0
        uptrend = slope_20 > 0
        high_short = short_pct > 0.15  # >15%

        metrics = {
            "short_percent_float": short_pct,
            "short_ratio": short_ratio,
            "slope_20d": round(slope_20, 4),
            "pct_change_20d": round(pct_20d, 2) if pct_20d is not None else None,
            "volume_surge_ratio": round(vol_surge, 2),
        }

        if high_short and uptrend:
            strength = 20
            if short_pct > 0.30:
                strength += 20
            elif short_pct > 0.20:
                strength += 15
            if short_ratio is not None and short_ratio > 5:
                strength += 15
            if pct_20d is not None and pct_20d > 10:
                strength += 15
            if vol_surge > 1.5:
                strength += 15

            # Proximity: combination of short% and price movement
            proximity = min(100, short_pct * 200 + (pct_20d or 0) * 1.5)
            proximity = max(0, proximity)

            if strength > 70:
                timeline = "Active squeeze, days to 2 weeks"
            elif strength >= 40:
                timeline = "Building pressure, 2-4 weeks"
            else:
                timeline = "Potential setup, 1-3 months"

            detail = ("Short squeeze conditions: {:.1f}% of float sold short "
                      "(days to cover: {:.1f}), price up {:.1f}% over 20 days "
                      "with volume surge ratio {:.1f}x. Rising price forces "
                      "short covering, amplifying upward pressure."
                      .format(short_pct * 100,
                              short_ratio if short_ratio is not None else 0,
                              pct_20d or 0,
                              vol_surge))

            return _make_loop_result(loop_type, detected=True, strength=strength,
                                     direction="VIRTUOUS", proximity=proximity,
                                     timeline=timeline, detail=detail, metrics=metrics)

        return _make_loop_result(loop_type,
                                 detail="No short squeeze conditions detected.",
                                 metrics=metrics)

    except Exception as e:
        logger.warning("Short squeeze detection error: %s", e)
        return _make_loop_result(loop_type, detail="Error during detection: {}".format(e))


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def detect_loops(symbol):
    # type: (str) -> dict
    """Detect self-reinforcing feedback loops for a given stock symbol.

    Runs all 4 loop detectors and aggregates results into a single report.
    """
    symbol = symbol.upper().strip()
    cache_key = "reflexivity_{}".format(symbol)
    cached = _get_cached(cache_key)
    if cached is not None:
        return cached

    try:
        # 1. Fetch data
        fundamentals = fetcher.get_fundamentals(symbol)
        if not fundamentals or "error" in fundamentals:
            return {"error": "Could not fetch fundamentals for {}".format(symbol)}

        price_data = fetcher.get_chart_data(symbol, period="1y", interval="1d")
        if not price_data:
            price_data = []

        quote = fetcher.get_quote(symbol)

        # Merge quote price into fundamentals if missing
        if fundamentals.get("price") is None and quote:
            fundamentals["price"] = quote.get("price")

        # 4. Fetch recent filings
        try:
            filings = edgar.get_recent_filings(symbol, forms=["10-K", "10-Q"], limit=3)
        except Exception as e:
            logger.debug("Could not fetch filings for %s: %s", symbol, e)
            filings = []

        # 5. Scan filings for keywords
        filing_keywords = _scan_filing_keywords(symbol, filings or [])

        # 6. Run all 4 detectors
        loops = [
            _detect_equity_feedback(price_data, fundamentals),
            _detect_dilution_spiral(price_data, fundamentals, filing_keywords),
            _detect_index_inclusion(fundamentals),
            _detect_short_squeeze(price_data, fundamentals),
        ]

        # 7. Aggregate
        detected_loops = [lp for lp in loops if lp.get("detected")]
        loop_count = len(detected_loops)
        max_strength = max((lp.get("strength", 0) for lp in loops), default=0)

        dominant_loop = None
        if detected_loops:
            dominant = max(detected_loops, key=lambda lp: lp.get("strength", 0))
            dominant_loop = dominant.get("type")

        if max_strength >= 70:
            overall_risk = "HIGH REFLEXIVITY"
        elif max_strength >= 40:
            overall_risk = "MODERATE"
        elif loop_count > 0:
            overall_risk = "LOW"
        else:
            overall_risk = "NONE DETECTED"

        # Fundamentals snapshot
        snap = {
            "price": fundamentals.get("price"),
            "market_cap": fundamentals.get("market_cap"),
            "debt_equity": fundamentals.get("debt_equity"),
            "short_pct": fundamentals.get("short_percent_float"),
            "shares_out": fundamentals.get("shares_outstanding"),
        }

        result = {
            "symbol": symbol,
            "active_loops": loops,
            "loop_count": loop_count,
            "max_strength": max_strength,
            "dominant_loop": dominant_loop,
            "overall_risk": overall_risk,
            "fundamentals_snapshot": snap,
            "timestamp": datetime.utcnow().isoformat() + "Z",
        }

        _set_cached(cache_key, result)
        return result

    except Exception as e:
        logger.error("Reflexivity detection failed for %s: %s", symbol, e)
        return {"error": str(e)}
