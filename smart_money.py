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

import copy
import logging
import threading
import time
from datetime import datetime, timedelta, timezone

import yfinance as yf

import fetcher
import safe_executor

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


# ── compute_score result memo ─────────────────────────────────────────────────
# synth_cluster, synth_divmap (which evaluates TWO smart-money pairs per
# symbol), opportunity_scanner, synthetic_insider and jarvis_tools all call
# compute_score for overlapping symbols inside the same warmer cycle.  Every
# input the score is built from is already cached upstream for >= 30 minutes
# (ml_forecast 1h, EDGAR via cache_store, chart data 12h, narrative 30min), so
# a short memo here collapses pure duplicate work without changing freshness.
# In-flight coalescing means concurrent scans share ONE computation per symbol
# instead of hammering Yahoo with identical .info/.option_chain calls.
_SCORE_TTL_S = 600.0       # positive results
_SCORE_ERR_TTL_S = 120.0   # "No price data" results — retry sooner
_score_cache = {}          # symbol -> (timestamp, result dict)
_score_lock = threading.Lock()
_score_inflight = {}       # symbol -> threading.Event


def _score_cache_get(symbol):
    """Return a defensive copy of a fresh cached result, else None."""
    with _score_lock:
        hit = _score_cache.get(symbol)
        if hit is None:
            return None
        ts, res = hit
        ttl = _SCORE_TTL_S if res.get("score") is not None else _SCORE_ERR_TTL_S
        if (time.time() - ts) >= ttl:
            return None
        return copy.deepcopy(res)


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

    # sec_edgar normalizes open-market actions to "BUY"/"SELL" and tags
    # everything else "OTHER:<code>", so match only the normalized values
    # (the old "P"/"A"/"S"/"D"/"PURCHASE"/"SALE" branches were dead, and
    # counting "A" awards as buys would have been wrong if they ever fired).
    buys = sum(1 for t in recent if t.get("transaction_type", "").upper() == "BUY")
    sells = sum(1 for t in recent if t.get("transaction_type", "").upper() == "SELL")
    total = buys + sells

    if total == 0:
        return 12, recent

    buy_ratio = buys / total
    # Scale: 100% buys → 25, 50/50 → 12, 100% sells → 0
    raw = buy_ratio * 25
    score = round(raw)

    # Boost for cluster buying (≥3 different insiders). sec_edgar normalizes
    # open-market purchases to transaction_type "BUY" (grants/awards become
    # "OTHER:A"), so match "BUY" — the old ("P","A") check never matched the
    # normalized values and the boost silently never fired.
    buyers = set(t.get("insider_name", "") for t in recent if t.get("transaction_type", "").upper() == "BUY")
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
    if pe and fpe and pe > 0 and fpe > 0:  # both must be positive — a negative
        if fpe < pe * 0.85:                 # forwardPE (loss forecast) isn't "PE contracting"
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
                # No expiry in the preferred 20-50d window — fall back to the
                # nearest expiry but compute its REAL calendar DTE. Using a
                # placeholder 0 (→ trading_dte 1) scaled HV to a single day
                # while the straddle's implied move spanned the real (often
                # multi-week) expiry, inflating the IV/HV ratio severalfold
                # and pinning thin-options names to a max-bearish score.
                try:
                    _exp_d = datetime.strptime(exps[0], "%Y-%m-%d").date()
                    _dte = max(1, (_exp_d - _today_et().date()).days)
                except Exception:
                    _dte = 21  # neutral-ish default rather than 1
                target = [(_dte, exps[0])]
            else:
                return 8

        target.sort()
        chosen_dte, exp_date = target[0]
        # chosen_dte is calendar days; convert to trading days (~ x * 5/7).
        trading_dte = max(1, int(round(chosen_dte * 5.0 / 7.0)))

        chain = ticker.option_chain(exp_date)
        calls = chain.calls
        puts  = chain.puts

        if calls.empty or puts.empty:
            return 8

        # ATM straddle — use bid/ask MID (not stale lastPrice); fall back to
        # lastPrice only when a leg has no live quotes.
        def _leg_mid(df):
            bid = ask = None
            try:
                if "bid" in df.columns:
                    bid = float(df["bid"].iloc[0])
                if "ask" in df.columns:
                    ask = float(df["ask"].iloc[0])
            except (TypeError, ValueError):
                bid = ask = None
            if bid and ask and bid > 0 and ask > 0:
                return (bid + ask) / 2.0
            return float(df["lastPrice"].iloc[0])

        atm_calls = calls.iloc[(calls["strike"] - current_price).abs().argsort()[:1]]
        atm_puts  = puts.iloc[(puts["strike"] - current_price).abs().argsort()[:1]]
        straddle_price = _leg_mid(atm_calls) + _leg_mid(atm_puts)
        raw_implied_move_pct = straddle_price / current_price * 100 if current_price else 0
        # The ATM straddle prices roughly a 1.25-sigma move, while HV below is a
        # 1-sigma quantity. Divide the straddle-implied move by ~1.25 so that a
        # fairly-priced chain (IV == HV) yields iv_premium ~= 1.0 instead of a
        # spurious ~1.25 premium on every name.
        implied_move_pct = raw_implied_move_pct / 1.25

        # Historical volatility (30-day) — use shared history
        if hist.empty or len(hist) < 20:
            return 8

        import numpy as np
        returns = hist["Close"].pct_change().dropna()
        hv_daily = returns.tail(30).std()
        # Scale daily HV out to the chosen expiry's trading-day horizon so the
        # ratio is apples-to-apples with the straddle's `dte`-period implied
        # move. The previous fixed sqrt(21) assumed every expiry was exactly
        # 21 trading days out and silently mis-scaled by 15-50% for any
        # expiry outside that window.
        hv_horizon = hv_daily * (trading_dte ** 0.5) * 100

        # IV premium ratio
        if hv_horizon > 0:
            iv_premium = implied_move_pct / hv_horizon
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

        # Weighted average of horizons that actually have enough history.
        # The previous code accumulated the weighted sum but didn't renormalize
        # by the fired weights, so a 100-bar history (only 1m + 3m available)
        # was scored against thresholds calibrated for the full weight sum of
        # 1.0 — a real 10% 3m gain registered as 0.03, dropping into "score 6"
        # instead of the intended "score 7+" bucket.
        composite_num = 0.0
        composite_den = 0.0
        for label, bars in periods.items():
            if len(hist) >= bars + 5:
                past_price = float(hist["Close"].iloc[-bars])
                if past_price <= 0:
                    continue
                ret = (current - past_price) / past_price
                composite_num += ret * weights[label]
                composite_den += weights[label]
        composite = (composite_num / composite_den) if composite_den > 0 else 0.0

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

        # `tail(50).mean()` silently returned a 25-bar mean when called with
        # 25 bars of history, mislabelled as "ma50" — so the comparison was
        # essentially "current > recent-mean", a trivially true tautology for
        # any up-trending stock. Only compute MAs when enough bars exist.
        ma50  = hist["Close"].tail(50).mean() if len(hist) >= 50 else None
        ma200 = hist["Close"].tail(200).mean() if len(hist) >= 200 else None

        if ma50 is not None and current > ma50:
            score = min(10, score + 1)
        if ma200 is not None and current > ma200:
            score = min(10, score + 1)

        return max(0, min(10, score))

    except Exception as e:
        log.debug("Momentum score error: %s", e)
        return 5


# ── Component: SEC Filing Sentiment (0-10) ────────────────────────────────────

# 8-K item codes mapped to a sentiment signal. get_recent_filings() returns
# the raw item codes (e.g. "2.02,9.01") but never an 'ai_signal', so derive
# the signal from the disclosed item codes — the codes are themselves the SEC's
# structured classification of the event.
_SEC_ITEM_SIGNAL = {
    "1.01": "POSITIVE",   # Entry into Material Agreement
    "1.02": "NEGATIVE",   # Termination of Material Agreement
    "1.03": "BEARISH",    # Bankruptcy or Receivership
    "2.01": "POSITIVE",   # Completion of Acquisition
    # 2.02 (earnings) is directionally neutral on its own — leave NEUTRAL
    "2.03": "NEGATIVE",   # Creation of Direct Financial Obligation
    "2.05": "NEGATIVE",   # Departure of Directors / Officers
    "2.06": "BEARISH",    # Material Impairments
    "3.01": "BEARISH",    # Notice of Delisting
    "4.01": "NEGATIVE",   # Changes in Certifying Accountant
    "5.01": "NEGATIVE",   # Changes in Control
    # 5.02 / 5.03 / 7.01 / 8.01 / 9.01 carry no inherent direction
}


def _score_sec_sentiment(symbol):
    """Score based on recent SEC 8-K item codes (0-10).

    get_recent_filings() returns the SEC's structured 8-K item codes; we map
    those to a directional signal rather than relying on a never-populated
    'ai_signal' key (which would pin every symbol to a constant 5)."""
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
        codes = [c.strip() for c in str(f.get("items", "")).split(",") if c.strip()]
        # Pick the most extreme (furthest-from-neutral) signal among this
        # filing's item codes so a single bankruptcy/impairment isn't diluted
        # by neutral co-filed items.
        filing_score = None
        for c in codes:
            sig = _SEC_ITEM_SIGNAL.get(c)
            if sig is None:
                continue
            s = signal_map[sig]
            if filing_score is None or abs(s - 5) > abs(filing_score - 5):
                filing_score = s
        scores.append(filing_score if filing_score is not None else 5)

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

    Memoized for _SCORE_TTL_S with in-flight coalescing: concurrent callers
    (cluster scan, divergence map, opportunity scanner, synthetic insider)
    asking for the same symbol share one computation instead of each paying
    the full upstream fan-out. Returns are deep copies so a caller mutating
    its result can't pollute the cache.
    """
    symbol = symbol.upper()

    cached = _score_cache_get(symbol)
    if cached is not None:
        return cached

    # Coalesce concurrent computations of the same symbol.
    with _score_lock:
        ev = _score_inflight.get(symbol)
        owner = ev is None
        if owner:
            ev = threading.Event()
            _score_inflight[symbol] = ev

    if not owner:
        # Another thread is computing this symbol right now — wait for it
        # (bounded so a hung upstream can't wedge us), then re-check the
        # cache. On a miss we fall through and compute directly.
        ev.wait(timeout=45.0)
        cached = _score_cache_get(symbol)
        if cached is not None:
            return cached

    try:
        result = _compute_score_uncached(symbol)
        # WHY (S9): publish to _score_cache BEFORE ev.set(). The old order set
        # the event first, so a waiter woken by ev.set() could re-check the
        # cache before this thread had written it, miss, and redundantly
        # recompute the same symbol — defeating the coalescing. Writing the
        # cache first guarantees every waiter that observes the set event also
        # observes the result.
        if owner:
            with _score_lock:
                _score_cache[symbol] = (time.time(), copy.deepcopy(result))
    finally:
        if owner:
            with _score_lock:
                _score_inflight.pop(symbol, None)
            ev.set()

    if not owner:
        # Non-owner that fell through to a direct compute (timeout/miss): still
        # populate the cache for subsequent callers.
        with _score_lock:
            _score_cache[symbol] = (time.time(), copy.deepcopy(result))
    return result


def _compute_score_uncached(symbol):
    """Do the actual upstream fan-out + scoring for compute_score()."""
    start = time.time()

    # Route price + history through `fetcher` so we benefit from the v0.1.6
    # Yahoo-direct fallback chain. Direct yf.Ticker.history calls die silently
    # whenever yfinance's crumb auth is rate-limited, even when the same data
    # is reachable via the v8 chart endpoint or already in `cache_store`. The
    # ticker object itself is kept around so `_score_options` can still call
    # ticker.option_chain() — options data has its own separate fallback story
    # handled inside `_score_options`.
    ticker = yf.Ticker(symbol)

    import pandas as pd

    def _fetch_info():
        try:
            return ticker.info or {}
        except Exception:
            return {}

    def _fetch_hist():
        try:
            bars = fetcher.get_chart_data(symbol, "1y", "1d")
        except Exception:
            return pd.DataFrame()
        if not bars:
            return pd.DataFrame()
        h = pd.DataFrame(bars)
        h.index = pd.to_datetime([b["time"] for b in bars], unit="s")
        return h.rename(columns={
            "open": "Open", "high": "High", "low": "Low",
            "close": "Close", "volume": "Volume",
        })

    # `.info` is an uncached Yahoo HTTP round-trip every call; fetch it and
    # the price history concurrently instead of back-to-back.
    fetched = safe_executor.parallel_map(
        lambda fn: fn(), [_fetch_info, _fetch_hist], max_workers=2,
        thread_name_prefix="sm-fetch",
    )
    info = fetched[0] if fetched[0] is not None else {}
    hist = fetched[1] if fetched[1] is not None else pd.DataFrame()

    current_price = None
    try:
        if not hist.empty:
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

    # Compute each component — pass shared ticker/info/hist where possible.
    # The four network-bound scorers (EDGAR Form 4, options chain, SEC filing
    # sentiment, ML forecast) are independent of each other; run them on
    # safe_executor's daemon threads so the wall clock is the slowest one,
    # not their sum. A None slot (work fn raised) falls back to the same
    # neutral default the scorer itself returns on failure.
    component_fns = [
        lambda: _score_insiders(symbol),
        lambda: _score_options(ticker, current_price, hist),
        lambda: _score_sec_sentiment(symbol),
        lambda: _score_ml_forecast(symbol),
    ]
    part = safe_executor.parallel_map(
        lambda fn: fn(), component_fns, max_workers=4,
        thread_name_prefix="sm-parts",
    )
    # Guard arity, not just None: a malformed (non-2-tuple) slot degrades to
    # the neutral default instead of raising ValueError and aborting the whole
    # score, preserving the per-component fail-soft design.
    _p0 = part[0]
    insider_score, insider_txns = _p0 if isinstance(_p0, tuple) and len(_p0) == 2 else (12, [])
    options_score = part[1] if part[1] is not None else 8
    sec_score = part[2] if part[2] is not None else 5
    _p3 = part[3]
    ml_score, ml_detail = _p3 if isinstance(_p3, tuple) and len(_p3) == 2 else (
        10, {"signal": "N/A", "detail": "ML unavailable"})

    # CPU-only scorers over already-fetched data — no benefit from threads.
    institutional_score = _score_institutional(info)
    earnings_score = _score_earnings_quality(info, symbol)
    momentum_score = _score_momentum(hist)

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
                "detail": "8-K item-code analysis (no AI/LLM)",
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
    """Compute Smart Money scores for a list of symbols using parallel execution.

    Routes through safe_executor.parallel_map so the call survives the
    bundle-state where Python's `concurrent.futures.thread._shutdown` global
    has flipped (surfaces as `RuntimeError: cannot schedule new futures
    after interpreter shutdown`). In that state we silently fall back to
    sequential execution rather than returning HTTP 500.
    """
    import safe_executor

    def _score_one(sym):
        try:
            result = compute_score(sym)
            if result.get("score") is not None:
                return result
        except Exception as e:
            log.warning("Score error for %s: %s", sym, e)
        return None

    raw = safe_executor.parallel_map(
        _score_one, symbols, max_workers=3, thread_name_prefix="sm-bulk",
    )
    results = [r for r in raw if r]
    results.sort(key=lambda x: x.get("score", 0), reverse=True)
    return results
