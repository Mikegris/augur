"""
smart_money.py — Smart Money Convergence Score
Aggregates multiple institutional signals into a single conviction score (0-100).

Components:
  Insider Activity       0-20  (Form 4 from EDGAR)
  Institutional Moves    0-15  (13F from EDGAR)
  Earnings Quality       0-15  (yfinance fundamentals)
  Options Pricing        0-10  (IV vs HV implied move signal)
  Price Momentum         0-10  (yfinance 3M/6M/12M)
  SEC Filing Sentiment   0-10  (AI signal from edgar.py)
  ML Forecast            0-20  (RF classifier + trend + regime + mean reversion)
  ─────────────────────────────
  Total                  0-100
"""

import logging
import time
from datetime import datetime, timedelta, timezone

import yfinance as yf

import fetcher

try:
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover — Py3.9 without tzdata
    _ET = None


def _today_et() -> datetime:
    """Return now anchored to America/New_York (option/market wall clock).

    Falls back to a naive local `datetime.today()` when zoneinfo is unavailable
    so the module still imports — the underlying date-arithmetic is identical
    whenever the host is already in ET, and only drifts by ~1 day off-hours
    in foreign time zones."""
    if _ET is not None:
        return datetime.now(_ET).replace(tzinfo=None)
    return datetime.today()

log = logging.getLogger(__name__)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _safe_float(val, default=None):
    try:
        return float(val) if val is not None else default
    except (TypeError, ValueError):
        return default


def _score_clamp(val, lo=0, hi=None, max_score=0):
    """Clamp a numeric value into [0, max_score]."""
    if val is None:
        return 0
    return max(0, min(max_score, val))


# ── Component: Insider Activity (0-25) ────────────────────────────────────────

def _score_insiders(symbol, days=90):
    """
    Score insider trading from EDGAR Form 4 data.
    Heavy buying → 25, heavy selling → 0, neutral → 12
    """
    try:
        from sec_edgar import get_form4_transactions
        txns = get_form4_transactions(symbol)
    except Exception:
        return 12, []

    if not txns:
        return 12, []

    # Anchor cutoff to ET so SEC Form 4 dates (which are filed on US business
    # days) line up with the same wall-clock window the user sees.
    cutoff = _today_et() - timedelta(days=days)
    recent = []
    for t in txns:
        try:
            dt = datetime.strptime(t.get("date", ""), "%Y-%m-%d")
            if dt >= cutoff:
                recent.append(t)
        except ValueError:
            pass

    if not recent:
        return 12, []

    buys = sum(1 for t in recent if t.get("transaction_type", "").upper() in ("P", "A", "BUY", "PURCHASE"))
    sells = sum(1 for t in recent if t.get("transaction_type", "").upper() in ("S", "D", "SELL", "SALE"))
    total = buys + sells

    if total == 0:
        return 12, recent

    buy_ratio = buys / total
    # Scale: 100% buys → 25, 50/50 → 12, 100% sells → 0
    raw = buy_ratio * 25
    score = round(raw)

    # Boost for cluster buying (≥3 different insiders)
    buyers = set(t.get("insider_name", "") for t in recent if t.get("transaction_type", "").upper() in ("P", "A"))
    if len(buyers) >= 3:
        score = min(25, score + 3)

    return score, recent


# ── Component: Institutional Moves (0-20) ─────────────────────────────────────

def _score_institutional(info):
    """
    Score from yfinance info dict.
    Uses: institutionalOwnershipChange, shortRatio, shortPercentOfFloat
    """
    score = 10  # neutral baseline

    # Institutional ownership percent
    inst_pct = _safe_float(info.get("institutionPercentHeld") or info.get("heldPercentInstitutions"))
    if inst_pct is not None:
        if inst_pct > 0.80:
            score += 3
        elif inst_pct > 0.60:
            score += 1
        elif inst_pct < 0.30:
            score -= 2

    # Short interest as contra-indicator
    short_pct = _safe_float(info.get("shortPercentOfFloat"))
    if short_pct is not None:
        if short_pct < 0.03:
            score += 3
        elif short_pct < 0.08:
            score += 1
        elif short_pct > 0.20:
            score -= 3
        elif short_pct > 0.12:
            score -= 1

    # Short ratio (days to cover)
    short_ratio = _safe_float(info.get("shortRatio"))
    if short_ratio is not None:
        if short_ratio > 10:
            score -= 2
        elif short_ratio < 2:
            score += 2

    return max(0, min(20, score))


# ── Component: Earnings Quality (0-20) ────────────────────────────────────────

def _score_earnings_quality(info, ticker):
    """
    Score fundamental quality: EPS growth, revenue growth, margins, P/E.
    """
    score = 10  # neutral

    # EPS trend (trailing vs forward)
    eps_trailing = _safe_float(info.get("trailingEps"))
    eps_forward  = _safe_float(info.get("forwardEps"))
    if eps_trailing and eps_forward:
        if eps_forward > eps_trailing * 1.15:
            score += 4
        elif eps_forward > eps_trailing * 1.05:
            score += 2
        elif eps_forward < eps_trailing * 0.90:
            score -= 3

    # Profit margin
    margin = _safe_float(info.get("profitMargins"))
    if margin is not None:
        if margin > 0.25:
            score += 3
        elif margin > 0.15:
            score += 1
        elif margin < 0:
            score -= 4
        elif margin < 0.05:
            score -= 2

    # Revenue growth
    rev_growth = _safe_float(info.get("revenueGrowth"))
    if rev_growth is not None:
        if rev_growth > 0.20:
            score += 3
        elif rev_growth > 0.10:
            score += 1
        elif rev_growth < -0.05:
            score -= 2

    # P/E relative to sector (rough heuristic)
    pe = _safe_float(info.get("trailingPE"))
    fpe = _safe_float(info.get("forwardPE"))
    if pe and fpe:
        if fpe < pe * 0.85:
            score += 2  # PE contracting = earnings growth
        elif fpe > pe * 1.15:
            score -= 2

    return max(0, min(20, score))


# ── Component: Options Pricing Signal (0-15) ──────────────────────────────────

def _score_options(ticker, current_price, hist):
    """
    Score based on implied move vs historical volatility.
    Accepts a shared yf.Ticker instance and pre-fetched history to avoid
    redundant API calls.
    """
    try:
        exps = ticker.options
        if not exps:
            return 8

        # Find nearest expiry ~30-45 days out. Options expire at 16:00 ET on
        # the listed Friday, so anchor "today" to ET to avoid a 1-day drift
        # for callers running outside US time zones.
        today = _today_et()
        target = []
        for exp in exps:
            try:
                dt = datetime.strptime(exp, "%Y-%m-%d")
                delta = (dt - today).days
                if 20 <= delta <= 50:
                    target.append((delta, exp))
            except ValueError:
                pass

        if not target:
            if exps:
                target = [(0, exps[0])]
            else:
                return 8

        target.sort()
        _, exp_date = target[0]

        chain = ticker.option_chain(exp_date)
        calls = chain.calls
        puts  = chain.puts

        if calls.empty or puts.empty:
            return 8

        # ATM straddle
        atm_calls = calls.iloc[(calls["strike"] - current_price).abs().argsort()[:1]]
        atm_puts  = puts.iloc[(puts["strike"] - current_price).abs().argsort()[:1]]
        straddle_price = float(atm_calls["lastPrice"].iloc[0]) + float(atm_puts["lastPrice"].iloc[0])
        implied_move_pct = straddle_price / current_price * 100 if current_price else 0

        # Historical volatility (30-day) — use shared history
        if hist.empty or len(hist) < 20:
            return 8

        import numpy as np
        returns = hist["Close"].pct_change().dropna()
        hv_daily = returns.tail(30).std()
        hv_monthly = hv_daily * (21 ** 0.5) * 100

        # IV premium ratio
        if hv_monthly > 0:
            iv_premium = implied_move_pct / hv_monthly
        else:
            iv_premium = 1.0

        if iv_premium < 0.7:
            return 14
        elif iv_premium < 0.9:
            return 12
        elif iv_premium < 1.2:
            return 8
        elif iv_premium < 1.5:
            return 5
        else:
            return 2

    except Exception as e:
        log.debug("Options score error: %s", e)
        return 8


# ── Component: Price Momentum (0-10) ──────────────────────────────────────────

def _score_momentum(hist):
    """Multi-timeframe momentum score using shared history."""
    try:
        if hist.empty or len(hist) < 20:
            return 5

        current = float(hist["Close"].iloc[-1])
        score = 5

        periods = {
            "1m": 21,
            "3m": 63,
            "6m": 126,
            "12m": 252,
        }
        weights = {"1m": 0.4, "3m": 0.3, "6m": 0.2, "12m": 0.1}

        composite = 0.0
        for label, bars in periods.items():
            if len(hist) >= bars + 5:
                past_price = float(hist["Close"].iloc[-bars])
                ret = (current - past_price) / past_price
                composite += ret * weights[label]

        if composite > 0.30:
            score = 10
        elif composite > 0.15:
            score = 8
        elif composite > 0.05:
            score = 7
        elif composite > 0:
            score = 6
        elif composite > -0.10:
            score = 4
        elif composite > -0.25:
            score = 2
        else:
            score = 1

        ma50  = hist["Close"].tail(50).mean()
        ma200 = hist["Close"].tail(200).mean() if len(hist) >= 200 else None

        if current > ma50:
            score = min(10, score + 1)
        if ma200 and current > ma200:
            score = min(10, score + 1)

        return max(0, min(10, score))

    except Exception as e:
        log.debug("Momentum score error: %s", e)
        return 5


# ── Component: SEC Filing Sentiment (0-10) ────────────────────────────────────

def _score_sec_sentiment(symbol):
    """Score based on recent SEC 8-K / 10-Q AI sentiment."""
    try:
        from sec_edgar import get_recent_filings
        filings = get_recent_filings(symbol, limit=5)
    except Exception:
        return 5

    if not filings:
        return 5

    signal_map = {"BULLISH": 10, "POSITIVE": 8, "NEUTRAL": 5, "NEGATIVE": 2, "BEARISH": 0}
    scores = []
    for f in filings:
        sig = f.get("ai_signal", "NEUTRAL").upper()
        scores.append(signal_map.get(sig, 5))

    if not scores:
        return 5

    return round(sum(scores) / len(scores))


# ── Component: ML Forecast (0-20) ────────────────────────────────────────────

def _score_ml_forecast(symbol):
    """
    Score from ml_forecast composite signal.
    Maps RF probability, trend alignment, regime, and mean reversion into 0-20.
    Returns (score, detail_dict).
    """
    try:
        from ml_forecast import ml_forecast
        result = ml_forecast(symbol)
    except Exception as e:
        log.debug("ML forecast error for %s: %s", symbol, e)
        return 10, {"signal": "N/A", "detail": "ML unavailable"}

    score = 10  # neutral baseline

    # RF classifier probability (strongest signal — up to ±6 pts).
    # ml_forecast.ml_forecast() explicitly sets each component to None when its
    # sub-model fails (rf_classifier=None, trend_forecast=None, ...), so
    # `result.get(key, {})` would still return None on failure. Coerce to {}
    # via `or {}` so .get() doesn't blow up with AttributeError.
    rf = result.get("rf_classifier") or {}
    prob_up = rf.get("prob_up_20d", 0.5)
    if prob_up >= 0.70:
        score += 6
    elif prob_up >= 0.60:
        score += 4
    elif prob_up >= 0.55:
        score += 2
    elif prob_up <= 0.30:
        score -= 6
    elif prob_up <= 0.40:
        score -= 4
    elif prob_up <= 0.45:
        score -= 2

    # Trend alignment (±3 pts)
    trend = result.get("trend_forecast") or {}
    if trend.get("trends_aligned"):
        if trend.get("trend_short") == "UP":
            score += 3
        elif trend.get("trend_short") == "DOWN":
            score -= 3
    else:
        if trend.get("forecast_pct", 0) > 5:
            score += 1
        elif trend.get("forecast_pct", 0) < -5:
            score -= 1

    # Mean reversion (±2 pts)
    mr = result.get("mean_reversion") or {}
    mr_sig = mr.get("signal", "")
    if mr_sig == "OVERSOLD":
        score += 2
    elif mr_sig == "OVERBOUGHT":
        score -= 2

    # Regime context (±1 pt)
    regime = result.get("regime") or {}
    regime_label = regime.get("current_regime", "")
    if regime_label == "COMPRESSION":
        score += 1  # potential breakout setup
    elif regime_label == "HIGH VOL CHOP":
        score -= 1

    score = max(0, min(20, score))

    # composite_ml_score is explicitly set to None by ml_forecast.ml_forecast()
    # when every sub-model fails, so the default-via-`get` doesn't kick in.
    # Fall back to a neutral 0.5 via `or` to keep round() safe below.
    composite = result.get("composite_ml_score") or 0.5
    composite_signal = result.get("composite_signal", "HOLD")

    detail = {
        "signal": composite_signal,
        "composite_score": round(composite, 3),
        "rf_prob_up": round(prob_up, 3),
        "rf_signal": rf.get("signal", "N/A"),
        "rf_accuracy": rf.get("accuracy_recent", 0),
        "trend_direction": trend.get("trend_short", "N/A"),
        "forecast_pct": round(trend.get("forecast_pct", 0), 1),
        "regime": regime_label,
        "mr_signal": mr_sig,
        "mr_zscore": round(mr.get("zscore", 0), 2),
        "top_features": rf.get("top_features", [])[:5],
    }

    return score, detail


# ── Main scorer ────────────────────────────────────────────────────────────────

def compute_score(symbol):
    """
    Compute the Smart Money Convergence Score for a symbol.
    Returns a dict with total score and component breakdown.
    """
    symbol = symbol.upper()
    start = time.time()

    # Route price + history through `fetcher` so we benefit from the v0.1.6
    # Yahoo-direct fallback chain. Direct yf.Ticker.history calls die silently
    # whenever yfinance's crumb auth is rate-limited, even when the same data
    # is reachable via the v8 chart endpoint or already in `cache_store`. The
    # ticker object itself is kept around so `_score_options` can still call
    # ticker.option_chain() — options data has its own separate fallback story
    # handled inside `_score_options`.
    ticker = yf.Ticker(symbol)
    try:
        info = ticker.info or {}
    except Exception:
        info = {}

    import pandas as pd
    hist = pd.DataFrame()
    current_price = None

    try:
        bars = fetcher.get_chart_data(symbol, "1y", "1d")
        if bars:
            hist = pd.DataFrame(bars)
            hist.index = pd.to_datetime([b["time"] for b in bars], unit="s")
            hist = hist.rename(columns={
                "open": "Open", "high": "High", "low": "Low",
                "close": "Close", "volume": "Volume",
            })
            current_price = float(hist["Close"].iloc[-1])
    except Exception:
        pass

    if current_price is None:
        # Last-resort price via fetcher.get_quote (which has its own
        # Yahoo-direct + Finviz fallback chain).
        try:
            q = fetcher.get_quote(symbol)
            current_price = _safe_float(q.get("price"))
        except Exception:
            pass

    # If yfinance .info populated currentPrice but the fetch path didn't,
    # prefer the info value to avoid a needless "no data" bailout.
    if current_price is None:
        current_price = _safe_float(
            info.get("currentPrice") or info.get("regularMarketPrice") or
            info.get("navPrice")
        )

    if current_price is None:
        return {
            "symbol": symbol,
            "error": "No price data",
            "score": None,
        }

    # Compute each component — pass shared ticker/info/hist where possible
    insider_score, insider_txns = _score_insiders(symbol)
    institutional_score = _score_institutional(info)
    earnings_score = _score_earnings_quality(info, symbol)
    options_score = _score_options(ticker, current_price, hist)
    momentum_score = _score_momentum(hist)
    sec_score = _score_sec_sentiment(symbol)
    ml_score, ml_detail = _score_ml_forecast(symbol)

    # Scale legacy components to new maxes (20+15+15+10+10+10+20=100)
    insider_scaled = round(insider_score * 20 / 25)
    institutional_scaled = round(institutional_score * 15 / 20)
    earnings_scaled = round(earnings_score * 15 / 20)
    options_scaled = round(options_score * 10 / 15)
    # momentum and sec stay at 10

    total = (insider_scaled + institutional_scaled + earnings_scaled +
             options_scaled + momentum_score + sec_score + ml_score)
    total = max(0, min(100, total))

    # Overall signal
    if total >= 75:
        signal = "STRONG BUY"
        signal_color = "green"
    elif total >= 60:
        signal = "BUY"
        signal_color = "col-green"
    elif total >= 45:
        signal = "NEUTRAL"
        signal_color = "col-yellow"
    elif total >= 30:
        signal = "CAUTION"
        signal_color = "col-amber"
    else:
        signal = "AVOID"
        signal_color = "col-red"

    return {
        "symbol": symbol,
        "name": info.get("longName") or info.get("shortName") or symbol,
        "score": total,
        "signal": signal,
        "signal_color": signal_color,
        "price": current_price,
        "components": {
            "insider_activity": {
                "score": insider_scaled,
                "max": 20,
                "label": "Insider Activity",
                "detail": f"{len(insider_txns)} recent Form 4 txns",
            },
            "institutional": {
                "score": institutional_scaled,
                "max": 15,
                "label": "Institutional Flow",
                "detail": f"Short float: {_safe_float(info.get('shortPercentOfFloat'), 0)*100:.1f}%",
            },
            "earnings_quality": {
                "score": earnings_scaled,
                "max": 15,
                "label": "Earnings Quality",
                "detail": f"Fwd EPS: {_safe_float(info.get('forwardEps'))}, Margin: {(_safe_float(info.get('profitMargins'), 0)*100):.1f}%",
            },
            "options_pricing": {
                "score": options_scaled,
                "max": 10,
                "label": "Options Signal",
                "detail": "IV vs HV analysis",
            },
            "momentum": {
                "score": momentum_score,
                "max": 10,
                "label": "Price Momentum",
                "detail": "Multi-timeframe trend",
            },
            "sec_sentiment": {
                "score": sec_score,
                "max": 10,
                "label": "Filing Sentiment",
                "detail": "AI-analyzed SEC filings",
            },
            "ml_forecast": {
                "score": ml_score,
                "max": 20,
                "label": "ML Forecast",
                "detail": f"RF: {ml_detail.get('rf_signal','N/A')} | Trend: {ml_detail.get('trend_direction','N/A')} | Regime: {ml_detail.get('regime','N/A')}",
                "ml_detail": ml_detail,
            },
        },
        "computed_in_ms": round((time.time() - start) * 1000),
    }


def compute_scores_bulk(symbols):
    """Compute Smart Money scores for a list of symbols using parallel execution."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    results = []

    def _score_one(sym):
        try:
            result = compute_score(sym)
            if result.get("score") is not None:
                return result
        except Exception as e:
            log.warning("Score error for %s: %s", sym, e)
        return None

    # 3 workers — enough parallelism without hammering yfinance
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {pool.submit(_score_one, sym): sym for sym in symbols}
        for future in as_completed(futures):
            result = future.result()
            if result:
                results.append(result)

    results.sort(key=lambda x: x.get("score", 0), reverse=True)
    return results
