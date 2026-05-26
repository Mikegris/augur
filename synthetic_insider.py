"""
Synthetic Insider Composite — AUGUR wealth intelligence

Combines 6 independently weak public signals into a composite that
approximates insider knowledge.  No single signal is actionable alone,
but when 3+ fire simultaneously it has historically preceded major moves
by 2-6 weeks.

Alert levels:  DORMANT -> AWAKENING -> CONVERGING -> CRITICAL
"""

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import fetcher
import sec_edgar as edgar
import congress
import smart_money
import ml_forecast

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level cache
# ---------------------------------------------------------------------------
_cache = {}
_cache_ts = {}
_CACHE_TTL = 1800  # seconds


def _cache_get(key):
    if key in _cache and (time.time() - _cache_ts.get(key, 0)) < _CACHE_TTL:
        return _cache[key]
    return None


def _cache_set(key, value):
    _cache[key] = value
    _cache_ts[key] = time.time()


def _clamp(value, lo=0, hi=100):
    return max(lo, min(hi, value))


# ---------------------------------------------------------------------------
# Channel 1: Options Flow Anomaly  (weight 20%)
# ---------------------------------------------------------------------------
def _score_options_flow(symbol):
    try:
        data = fetcher.get_unusual_options_flow(symbol)
        unusual = data.get("unusual", [])
        count = len(unusual)

        if count == 0:
            score = 10
        elif count <= 2:
            score = 25
        elif count <= 5:
            score = 45
        elif count <= 10:
            score = 65
        else:
            score = 85

        # Sentiment skew
        if unusual:
            bullish = sum(1 for u in unusual if u.get("sentiment", "").lower() == "bullish")
            bearish = sum(1 for u in unusual if u.get("sentiment", "").lower() == "bearish")
            total = len(unusual)
            if total > 0:
                if bullish / total > 0.70:
                    score += 10
                elif bearish / total > 0.70:
                    score -= 10

        score = _clamp(score)
        detail = "{} unusual contracts".format(count)
        return {"score": score, "detail": detail, "firing": score >= 60, "weight": 0.20}

    except Exception as e:
        logger.warning("Options flow scoring failed for %s: %s", symbol, e)
        return {"score": 50, "detail": "data unavailable", "firing": False, "weight": 0.20}


# ---------------------------------------------------------------------------
# Channel 2: Volume Anomaly  (weight 10%)
# ---------------------------------------------------------------------------
def _score_volume_anomaly(symbol):
    try:
        bars = fetcher.get_chart_data(symbol, period="1mo", interval="1d")
        if not bars or len(bars) < 5:
            return {"score": 50, "detail": "insufficient data", "firing": False, "weight": 0.10}

        volumes = [b["volume"] for b in bars if b.get("volume")]
        if len(volumes) < 5:
            return {"score": 50, "detail": "insufficient volume data", "firing": False, "weight": 0.10}

        avg_5 = sum(volumes[-5:]) / 5.0
        avg_20 = sum(volumes[-20:]) / max(len(volumes[-20:]), 1)

        if avg_20 == 0:
            ratio = 1.0
        else:
            ratio = avg_5 / avg_20

        if ratio > 2.5:
            score = 90
        elif ratio > 2.0:
            score = 75
        elif ratio > 1.5:
            score = 55
        elif ratio > 1.2:
            score = 35
        elif ratio >= 1.0:
            score = 20
        else:
            score = 10

        score = _clamp(score)
        detail = "5d/20d vol ratio {:.2f}".format(ratio)
        return {"score": score, "detail": detail, "firing": score >= 60, "weight": 0.10}

    except Exception as e:
        logger.warning("Volume anomaly scoring failed for %s: %s", symbol, e)
        return {"score": 50, "detail": "data unavailable", "firing": False, "weight": 0.10}


# ---------------------------------------------------------------------------
# Channel 3: Insider Form 4 Cluster  (weight 25%)
# ---------------------------------------------------------------------------
def _score_insider_cluster(symbol):
    try:
        txns = edgar.get_form4_transactions(symbol, limit=30)
        if not txns:
            return {"score": 10, "detail": "no recent Form 4 filings", "firing": False, "weight": 0.25}

        cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).date()
        recent_buys = []
        recent_sells = []

        for t in txns:
            tx_date_str = t.get("date", "")
            try:
                if isinstance(tx_date_str, str):
                    tx_date = datetime.strptime(tx_date_str[:10], "%Y-%m-%d").date()
                else:
                    tx_date = tx_date_str.date() if hasattr(tx_date_str, "date") else tx_date_str
            except (ValueError, TypeError):
                continue

            if tx_date < cutoff:
                continue

            tx_type = t.get("transaction_type", "").upper()
            if tx_type == "BUY":
                recent_buys.append(t)
            elif tx_type == "SELL":
                recent_sells.append(t)

        unique_buyers = set()
        has_csuite = False
        for b in recent_buys:
            name = b.get("insider_name", "unknown")
            unique_buyers.add(name)
            title = (b.get("title") or "").upper()
            if any(c in title for c in ("CEO", "CFO", "COO")):
                has_csuite = True

        n_buyers = len(unique_buyers)

        if n_buyers == 0 and recent_sells:
            score = 5
        elif n_buyers >= 4:
            score = 90
        elif n_buyers == 3:
            score = 75
        elif n_buyers == 2:
            score = 55
        elif n_buyers == 1:
            score = 30
        else:
            score = 10

        if has_csuite and n_buyers > 0:
            score += 5

        score = _clamp(score)
        detail = "{} insider buyer(s) in 30d".format(n_buyers)
        if has_csuite:
            detail += " (includes C-suite)"
        return {"score": score, "detail": detail, "firing": score >= 60, "weight": 0.25}

    except Exception as e:
        logger.warning("Insider cluster scoring failed for %s: %s", symbol, e)
        return {"score": 50, "detail": "data unavailable", "firing": False, "weight": 0.25}


# ---------------------------------------------------------------------------
# Channel 4: Congressional Buy Cluster  (weight 15%)
# ---------------------------------------------------------------------------
def _score_congress_cluster(symbol):
    try:
        trades = congress.get_trades_for_ticker(symbol, days=180)
        if not trades:
            return {"score": 10, "detail": "no congressional trades", "firing": False, "weight": 0.15}

        purchases = [t for t in trades if t.get("transaction_type", "") == "Purchase"]
        sales = [t for t in trades if "Sale" in t.get("transaction_type", "")]

        n_purchases = len(purchases)

        if n_purchases == 0 and sales:
            score = 5
        elif n_purchases >= 3:
            score = 80
        elif n_purchases == 2:
            score = 60
        elif n_purchases == 1:
            score = 35
        else:
            score = 10

        # Bonus for multiple unique members buying
        unique_members = set(t.get("member_name", "") for t in purchases)
        if len(unique_members) > 1:
            score += 10

        score = _clamp(score)
        detail = "{} congressional purchase(s), {} member(s)".format(n_purchases, len(unique_members))
        return {"score": score, "detail": detail, "firing": score >= 60, "weight": 0.15}

    except Exception as e:
        logger.warning("Congress cluster scoring failed for %s: %s", symbol, e)
        return {"score": 50, "detail": "data unavailable", "firing": False, "weight": 0.15}


# ---------------------------------------------------------------------------
# Channel 5: Institutional Signal  (weight 10%)
# ---------------------------------------------------------------------------
def _score_institutional_signal(symbol):
    try:
        data = smart_money.compute_score(symbol)
        components = data.get("components", {})
        inst = components.get("institutional", {})
        raw = inst.get("score", 0)
        max_val = inst.get("max", 15)
        if max_val == 0:
            max_val = 15

        score = int((raw / max_val) * 100)
        score = _clamp(score)
        detail = "institutional {}/{} ({})".format(raw, max_val, data.get("signal", "n/a"))
        return {"score": score, "detail": detail, "firing": score >= 60, "weight": 0.10}

    except Exception as e:
        logger.warning("Institutional signal scoring failed for %s: %s", symbol, e)
        return {"score": 50, "detail": "data unavailable", "firing": False, "weight": 0.10}


# ---------------------------------------------------------------------------
# Channel 6: Technical Regime Shift  (weight 20%)
# ---------------------------------------------------------------------------
def _score_technical_regime(symbol):
    try:
        data = ml_forecast.ml_forecast(symbol)
        ml_score = data.get("composite_ml_score", 0.5)
        score = int(ml_score * 100)

        regime_label = data.get("regime", {}).get("current_regime", "")
        if regime_label.upper() in ("COMPRESSION", "BREAKOUT"):
            score += 5

        z_score = data.get("mean_reversion", {}).get("zscore", 0.0)
        if z_score < -1.5:
            score += 5

        score = _clamp(score)
        detail = "ML {:.0f}%, regime={}, z={:.2f}".format(ml_score * 100, regime_label, z_score)
        return {"score": score, "detail": detail, "firing": score >= 60, "weight": 0.20}

    except Exception as e:
        logger.warning("Technical regime scoring failed for %s: %s", symbol, e)
        return {"score": 50, "detail": "data unavailable", "firing": False, "weight": 0.20}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def compute_composite(symbol):
    """
    Score 6 independent channels and combine into a single composite.

    Returns a dict with composite_score, alert_level, convergence_count,
    per-channel breakdowns, signal label, and timestamp.
    On error returns {"error": str}.
    """
    try:
        symbol = symbol.upper().strip()
        cache_key = "composite:{}".format(symbol)
        cached = _cache_get(cache_key)
        if cached is not None:
            return cached

        # Score all 6 channels in parallel
        with ThreadPoolExecutor(max_workers=6) as pool:
            fut_options = pool.submit(_score_options_flow, symbol)
            fut_volume = pool.submit(_score_volume_anomaly, symbol)
            fut_insider = pool.submit(_score_insider_cluster, symbol)
            fut_congress = pool.submit(_score_congress_cluster, symbol)
            fut_inst = pool.submit(_score_institutional_signal, symbol)
            fut_tech = pool.submit(_score_technical_regime, symbol)

        ch_options = fut_options.result()
        ch_volume = fut_volume.result()
        ch_insider = fut_insider.result()
        ch_congress = fut_congress.result()
        ch_inst = fut_inst.result()
        ch_tech = fut_tech.result()

        # Weighted composite
        composite = (
            ch_options["score"] * 0.20
            + ch_volume["score"] * 0.10
            + ch_insider["score"] * 0.25
            + ch_congress["score"] * 0.15
            + ch_inst["score"] * 0.10
            + ch_tech["score"] * 0.20
        )
        composite = _clamp(int(round(composite)))

        channels_list = [ch_options, ch_volume, ch_insider, ch_congress, ch_inst, ch_tech]
        convergence_count = sum(1 for ch in channels_list if ch["firing"])

        # Alert level
        if convergence_count <= 1:
            alert_level = "DORMANT"
        elif convergence_count == 2:
            alert_level = "AWAKENING"
        elif convergence_count <= 4:
            alert_level = "CONVERGING"
        else:
            alert_level = "CRITICAL"

        # Signal label
        if convergence_count >= 5:
            signal = "STRONG CONVERGENCE"
        elif convergence_count >= 3:
            signal = "BUILDING"
        elif convergence_count >= 2:
            signal = "MIXED"
        else:
            signal = "QUIET"

        result = {
            "symbol": symbol,
            "composite_score": composite,
            "alert_level": alert_level,
            "convergence_count": convergence_count,
            "channels": {
                "options_flow": ch_options,
                "volume_anomaly": ch_volume,
                "insider_cluster": ch_insider,
                "congress_cluster": ch_congress,
                "institutional_signal": ch_inst,
                "technical_regime": ch_tech,
            },
            "signal": signal,
            "timestamp": datetime.utcnow().isoformat() + "Z",
        }

        _cache_set(cache_key, result)
        logger.info(
            "Synthetic insider %s: composite=%d, alert=%s, convergence=%d",
            symbol, composite, alert_level, convergence_count,
        )
        return result

    except Exception as e:
        logger.error("compute_composite failed for %s: %s", symbol, e)
        return {"error": str(e)}


def scan_composite_bulk(symbols):
    """
    Score multiple symbols in parallel and return results sorted by
    composite_score descending.  Caps input at 15 symbols.

    Returns a list of result dicts.  On error returns {"error": str}.
    """
    try:
        if not symbols:
            return []

        symbols = [s.upper().strip() for s in symbols[:15]]

        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = {pool.submit(compute_composite, sym): sym for sym in symbols}

        results = []
        for fut in futures:
            res = fut.result()
            if "error" not in res:
                results.append(res)
            else:
                # Include errored symbols with minimal info so caller knows
                results.append({
                    "symbol": futures[fut],
                    "composite_score": 0,
                    "alert_level": "DORMANT",
                    "convergence_count": 0,
                    "error": res["error"],
                })

        results.sort(key=lambda r: r.get("composite_score", 0), reverse=True)
        return results

    except Exception as e:
        # Keep the return shape stable (list) so callers — which iterate
        # over the result — don't fail with "TypeError: 'dict' is not
        # iterable" when bulk scanning errors out. Surface the error as
        # a single-element list with the error metadata.
        logger.error("scan_composite_bulk failed: %s", e)
        return [{
            "symbol": "",
            "composite_score": 0,
            "alert_level": "DORMANT",
            "convergence_count": 0,
            "error": str(e),
        }]
