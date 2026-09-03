"""
ml_forecast.py — Real ML-based predictive analytics for stock signals.

Models (all trained on the stock's own history — no mock data):
  1. Random Forest return classifier   — predicts P(positive 20d return)
  2. Linear trend extrapolation        — 30-day price forecast with CI
  3. K-Means regime detector           — volatility/momentum regime label
  4. Mean-reversion z-score            — P(mean reversion) via OU process
  5. Feature importance ranking        — which technicals matter most now
"""

import logging
import threading
import time as _time
import numpy as np
import pandas as pd
from datetime import datetime

log = logging.getLogger(__name__)


def _phi(x):
    """Standard-normal CDF Φ(x). Prefers scipy; falls back to math.erf so a
    missing scipy never breaks a forecast. Returns 0.5 on any non-finite input."""
    try:
        x = float(x)
        if not np.isfinite(x):
            return 0.5
    except (TypeError, ValueError):
        return 0.5
    try:
        from scipy.stats import norm
        return float(norm.cdf(x))
    except Exception:
        import math as _m
        return 0.5 * (1.0 + _m.erf(x / _m.sqrt(2.0)))


# ── TTL cache: avoid retraining models on every request ──────────────────────
# Cache results for 1 hour per symbol. Training 4 models takes 2-5s per symbol.
_forecast_cache = {}   # symbol -> result dict
_cache_times = {}      # symbol -> timestamp
_CACHE_TTL = 3600      # 1 hour
# ml_forecast runs from multiple Flask request threads and from
# forecast_ensemble's ThreadPoolExecutor concurrently. Without locking, a
# check-then-act on the cache lets many threads retrain the SAME symbol at
# once (2-5s each, thundering herd) and exposes partially-written dict state
# to readers. _cache_lock guards every read/write of the cache dicts;
# _symbol_locks gives each symbol its own lock so concurrent callers of one
# symbol coalesce onto a single in-flight training while DIFFERENT symbols
# still run in parallel.
_cache_lock = threading.Lock()
_symbol_locks = {}     # symbol -> threading.Lock


def _symbol_lock(symbol):
    """Return (creating if needed) the per-symbol single-flight lock."""
    with _cache_lock:
        lk = _symbol_locks.get(symbol)
        if lk is None:
            lk = threading.Lock()
            _symbol_locks[symbol] = lk
        return lk


def _cache_get(symbol):
    """Thread-safe fresh-cache lookup. Returns the cached result or None."""
    with _cache_lock:
        if symbol in _forecast_cache:
            cached_at = _cache_times.get(symbol, 0)
            if (_time.time() - cached_at) < _CACHE_TTL:
                return _forecast_cache[symbol]
    return None


# WHY (#174): freshness was only checked on READ and nothing ever evicted, so
# a full-universe scan (thousands of symbols) left every expired result dict
# (31-point path, importances, regime stats) plus a per-symbol Lock resident
# for the process lifetime of the long-running desktop app. Sweep expired
# entries on every cache WRITE (cheap: one dict pass per 2-5s training run)
# and cap total entries with an oldest-first fallback.
_CACHE_MAX_ENTRIES = 512


def _evict_expired_locked():
    """Evict expired cache entries + their locks. Caller holds _cache_lock."""
    now = _time.time()
    stale = [s for s, ts in _cache_times.items() if now - ts >= _CACHE_TTL]
    if len(_cache_times) - len(stale) > _CACHE_MAX_ENTRIES:
        # Still over cap after the TTL sweep — drop the oldest fresh entries.
        fresh = sorted((s for s in _cache_times if s not in set(stale)),
                       key=lambda s: _cache_times.get(s, 0))
        stale.extend(fresh[: len(fresh) - _CACHE_MAX_ENTRIES])
    for s in stale:
        _forecast_cache.pop(s, None)
        _cache_times.pop(s, None)
        # Only drop an idle lock — popping one a trainer currently holds would
        # let a second caller mint a fresh lock and retrain the same symbol
        # concurrently (the thundering herd _symbol_locks exists to prevent).
        lk = _symbol_locks.get(s)
        if lk is not None and not lk.locked():
            _symbol_locks.pop(s, None)

# ── Feature Engineering ───────────────────────────────────────────────────────

def _build_features(hist):
    """
    Build a feature matrix from OHLCV history.
    Returns (X dataframe, y series of 20-day forward returns, feature_names).
    """
    df = hist.copy()
    close = df["Close"]

    # Returns at multiple horizons
    df["ret_1d"]  = close.pct_change(1)
    df["ret_5d"]  = close.pct_change(5)
    df["ret_10d"] = close.pct_change(10)
    df["ret_20d"] = close.pct_change(20)

    # Volatility
    df["vol_5d"]  = df["ret_1d"].rolling(5).std()
    df["vol_10d"] = df["ret_1d"].rolling(10).std()
    df["vol_20d"] = df["ret_1d"].rolling(20).std()

    # Moving averages ratios
    df["ma_5"]  = close.rolling(5).mean()
    df["ma_10"] = close.rolling(10).mean()
    df["ma_20"] = close.rolling(20).mean()
    df["ma_50"] = close.rolling(50).mean()

    df["price_vs_ma5"]  = close / df["ma_5"] - 1
    df["price_vs_ma10"] = close / df["ma_10"] - 1
    df["price_vs_ma20"] = close / df["ma_20"] - 1
    df["price_vs_ma50"] = close / df["ma_50"] - 1

    # RSI (14-period)
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rs = gain / loss.replace(0, np.nan)
    df["rsi_14"] = 100 - (100 / (1 + rs))

    # MACD
    ema12 = close.ewm(span=12).mean()
    ema26 = close.ewm(span=26).mean()
    df["macd"] = ema12 - ema26
    df["macd_signal"] = df["macd"].ewm(span=9).mean()
    df["macd_hist"] = df["macd"] - df["macd_signal"]

    # Bollinger Band width and position
    bb_mid = close.rolling(20).mean()
    bb_std = close.rolling(20).std()
    # Guard the denominators: a perfectly flat 20-day window (halted / brand-new
    # listing) makes bb_std == 0 → inf/NaN that poisons the feature scaling.
    df["bb_width"] = (2 * bb_std) / bb_mid.replace(0, np.nan)
    df["bb_position"] = (close - (bb_mid - 2 * bb_std)) / (4 * bb_std).replace(0, np.nan)

    # Volume features
    if "Volume" in df.columns:
        df["vol_ratio"] = df["Volume"] / df["Volume"].rolling(20).mean()
        df["vol_trend"] = df["Volume"].rolling(5).mean() / df["Volume"].rolling(20).mean()
    else:
        df["vol_ratio"] = 1.0
        df["vol_trend"] = 1.0

    # ATR (Average True Range, normalized)
    if "High" in df.columns and "Low" in df.columns:
        tr = pd.concat([
            df["High"] - df["Low"],
            (df["High"] - close.shift()).abs(),
            (df["Low"] - close.shift()).abs(),
        ], axis=1).max(axis=1)
        df["atr_14"] = tr.rolling(14).mean() / close
    else:
        df["atr_14"] = df["vol_20d"]

    # Rate of change
    df["roc_5"]  = close / close.shift(5) - 1
    df["roc_10"] = close / close.shift(10) - 1
    df["roc_20"] = close / close.shift(20) - 1

    # Z-score (20-day)
    df["zscore_20"] = (close - df["ma_20"]) / bb_std.replace(0, np.nan)

    # Forward return (target)
    df["fwd_ret_20d"] = close.shift(-20) / close - 1

    feature_cols = [
        "ret_1d", "ret_5d", "ret_10d", "ret_20d",
        "vol_5d", "vol_10d", "vol_20d",
        "price_vs_ma5", "price_vs_ma10", "price_vs_ma20", "price_vs_ma50",
        "rsi_14", "macd_hist", "bb_width", "bb_position",
        "vol_ratio", "vol_trend", "atr_14",
        "roc_5", "roc_10", "roc_20", "zscore_20",
    ]

    return df, feature_cols


# ── Model 1: Random Forest Return Classifier ─────────────────────────────────

def _rf_predict(hist, features=None):
    """
    Train a Random Forest on the stock's own history to predict
    P(positive 20-day forward return). The reported `accuracy_recent`
    is a true out-of-sample score on the most-recent 60 labelled rows
    (re-fit on everything before that holdout).

    Returns dict with probability, confidence, feature importances.

    WHY (Q14): `features` may be a pre-built ``(df_full, feature_cols)`` tuple
    from ``_build_features(hist)``; when supplied we reuse it instead of
    rebuilding the 2y feature frame (this and ``_detect_regime`` both need it).
    Defaults to None so existing direct callers/tests keep working.
    """
    from sklearn.ensemble import RandomForestClassifier
    from sklearn.preprocessing import StandardScaler

    try:
        from sklearn.calibration import CalibratedClassifierCV
    except Exception:  # pragma: no cover - sklearn always present in venv
        CalibratedClassifierCV = None

    df_full, feature_cols = features if features is not None else _build_features(hist)
    df = df_full.dropna(subset=feature_cols + ["fwd_ret_20d"])

    if len(df) < 120:
        return None  # not enough data

    # Binary target: 1 if positive, 0 if negative
    df["target"] = (df["fwd_ret_20d"] > 0).astype(int)

    # `df` already excludes rows whose 20-day forward return is unknown.
    # Embargo the final 20 labelled rows as well: their label window is the
    # freshest data and sits closest to the prediction date, so dropping them
    # keeps the production training set a clean 20-bar margin behind today.
    train = df.iloc[:-20].dropna(subset=feature_cols + ["target"])
    if len(train) < 80:
        return None

    X_train = train[feature_cols].values
    y_train = train["target"].values

    # Scale features
    scaler = StandardScaler()
    X_train_scaled = scaler.fit_transform(X_train)

    # Latest features (no forward return needed). Reuse the feature frame
    # built above — the latest row is identical and the only reason it was
    # dropped from `df` is the missing fwd_ret_20d label, which we don't need
    # for prediction. Recomputing _build_features(hist) here just doubled the
    # feature-engineering cost on the hot path.
    latest = df_full[feature_cols].iloc[-1:]
    if latest.isnull().any(axis=1).iloc[0]:
        return None
    X_latest = scaler.transform(latest.values)

    # Train RF — n_jobs=1 forces single-process fitting. With n_jobs=-1 joblib
    # spawns a loky pool whose worker teardown spams stderr with
    # "resource_tracker died" warnings on every batch (~200/min during the
    # warm pass). The fit is fast enough on this dataset that the parallelism
    # isn't worth the noise.
    rf = RandomForestClassifier(
        n_estimators=200,
        max_depth=6,
        min_samples_leaf=10,
        random_state=42,
        n_jobs=1,
    )
    rf.fit(X_train_scaled, y_train)

    # Predict probability from the raw RF (the calibrated estimate below
    # overrides this when calibration succeeds; this is the fail-open value).
    proba = rf.predict_proba(X_latest)
    if proba.shape[1] < 2:
        # Single-class training set — predict_proba returns one column for
        # whatever the only class is. Inspect rf.classes_ (the sklearn-known
        # set of classes), NOT y_train[0]: y_train[0] is just the first
        # training sample, which happens to coincide with the unique class
        # only because there *is* only one — but reading classes_ is the
        # correct contract and removes the implicit assumption.
        only_class = rf.classes_[0]
        prob_up = float(proba[0][0]) if only_class == 1 else 1.0 - float(proba[0][0])
    else:
        class_idx = list(rf.classes_).index(1) if 1 in rf.classes_ else 0
        prob_up = float(proba[0][class_idx])

    # CALIBRATION (C): raw RF votes are notoriously uncalibrated (they cluster
    # away from 0/1). Wrap a fresh RF in CalibratedClassifierCV fit on a PURGED
    # holdout so the reported prob_up is an honest probability, not a tree-vote
    # fraction. The split mirrors the OOS accuracy block: fit the inner RF on
    # everything except the final ~60 labelled rows, then fit the calibration
    # map on that held-out tail (purging the 20-row label overlap between them).
    # isotonic needs ~50+ calibration points to be stable; below that use
    # sigmoid (Platt). Fail OPEN to the raw RF prob_up on any error.
    calibrated = False
    if CalibratedClassifierCV is not None and len(train) >= 80 + 20:
        try:
            cal_hold = train.iloc[-60:] if len(train) >= 60 else train.iloc[-20:]
            cal_fit = train.iloc[: len(train) - len(cal_hold)]
            # Purge the 20-row label-window overlap (their realized 20d outcome
            # lands inside the calibration holdout).
            cal_fit = cal_fit.iloc[:-20] if len(cal_fit) > 20 else cal_fit.iloc[:0]
            y_hold = cal_hold["target"].values
            if (len(cal_fit) >= 60 and len(cal_hold) >= 20
                    and len(np.unique(cal_fit["target"].values)) == 2
                    and len(np.unique(y_hold)) == 2):
                scaler_cal = StandardScaler()
                X_cal_fit = scaler_cal.fit_transform(cal_fit[feature_cols].values)
                rf_cal = RandomForestClassifier(
                    n_estimators=200, max_depth=6, min_samples_leaf=10,
                    random_state=42, n_jobs=1,
                )
                rf_cal.fit(X_cal_fit, cal_fit["target"].values)
                method = "isotonic" if len(cal_hold) >= 50 else "sigmoid"
                # Calibrate the already-fitted rf_cal on the holdout. Prefer the
                # modern FrozenEstimator wrapper (sklearn >= 1.6); fall back to
                # cv="prefit" on older sklearn (both with new keyword and old
                # positional base-estimator signatures).
                cc = None
                try:
                    from sklearn.frozen import FrozenEstimator
                    cc = CalibratedClassifierCV(
                        FrozenEstimator(rf_cal), method=method)
                except Exception:
                    try:
                        cc = CalibratedClassifierCV(rf_cal, method=method, cv="prefit")
                    except TypeError:  # older sklearn: positional base estimator
                        cc = CalibratedClassifierCV(
                            base_estimator=rf_cal, method=method, cv="prefit")
                cc.fit(scaler_cal.transform(cal_hold[feature_cols].values), y_hold)
                X_latest_cal = scaler_cal.transform(latest.values)
                cproba = cc.predict_proba(X_latest_cal)
                cls = list(cc.classes_)
                cidx = cls.index(1) if 1 in cls else 0
                prob_up = float(cproba[0][cidx])
                calibrated = True
        except Exception as e:
            log.debug("RF calibration failed, using raw prob: %s", e)

    # Feature importances
    importances = dict(zip(feature_cols, rf.feature_importances_))
    top_features = sorted(importances.items(), key=lambda x: x[1], reverse=True)[:8]

    # Out-of-sample accuracy on the most recent 60 labelled rows. The old
    # implementation re-predicted on the tail of `train` — but those rows
    # were already in the training set, so the score was in-sample and
    # tended to ~95-100% regardless of real predictive ability. Re-fit on
    # everything *before* the holdout, then predict on the held-out tail.
    if len(train) >= 80 + 20:
        holdout = train.iloc[-60:] if len(train) >= 60 else train.iloc[-20:]
        train_oos = train.iloc[: len(train) - len(holdout)]
        # WHY (Q1): labels are 20-day forward returns, so the last 20 rows of
        # train_oos have their realized outcome land *inside* the holdout window
        # — training on them leaks holdout information and inflates the OOS
        # score. Purge those overlapping rows before fitting the OOS model.
        train_oos = train_oos.iloc[:-20] if len(train_oos) > 20 else train_oos.iloc[:0]
        if len(train_oos) >= 60 and len(holdout) >= 20:
            scaler_oos = StandardScaler()
            X_tr_oos = scaler_oos.fit_transform(train_oos[feature_cols].values)
            rf_oos = RandomForestClassifier(
                n_estimators=200, max_depth=6, min_samples_leaf=10,
                random_state=42, n_jobs=1,
            )
            rf_oos.fit(X_tr_oos, train_oos["target"].values)
            X_te = scaler_oos.transform(holdout[feature_cols].values)
            preds = rf_oos.predict(X_te)
            accuracy = float(np.mean(preds == holdout["target"].values))
        else:
            accuracy = None
    else:
        accuracy = None

    # Training class balance
    class_balance = float(y_train.mean())

    return {
        "prob_up_20d": round(prob_up, 3),
        "prob_down_20d": round(1 - prob_up, 3),
        "signal": "BULLISH" if prob_up > 0.6 else ("BEARISH" if prob_up < 0.4 else "NEUTRAL"),
        "confidence": round(abs(prob_up - 0.5) * 200, 1),  # 0-100 scale
        "train_samples": len(train),
        # Use `is not None` instead of truthiness: 0.0 (a perfectly wrong
        # model) is a valid accuracy value but `if accuracy` would drop it.
        "accuracy_recent": round(accuracy, 3) if accuracy is not None else None,
        "class_balance": round(class_balance, 3),
        "calibrated": calibrated,
        "top_features": [{"name": n, "importance": round(v, 4)} for n, v in top_features],
    }


# ── Model 2: Linear Trend Forecast ───────────────────────────────────────────

def _trend_forecast(hist, days_ahead=30):
    """
    Fit OLS regression to log-price for 3 regimes (60d, 120d, 250d),
    blend forecasts, compute confidence intervals.
    """
    close = hist["Close"].dropna()
    # Drop any non-positive closes — they propagate -inf through np.log and
    # poison polyfit / sklearn matmul downstream (the source of all those
    # "divide by zero in log" and "invalid value in matmul" RuntimeWarnings).
    close = close[close > 0]
    if len(close) < 60:
        return None

    log_prices = np.log(close.values)
    n = len(log_prices)
    current_price = float(close.iloc[-1])
    if current_price <= 0:
        return None

    forecasts = {}
    for label, window in [("short", 60), ("mid", 120), ("long", 250)]:
        w = min(window, n)
        y = log_prices[-w:]
        x = np.arange(w)
        # OLS
        coeffs = np.polyfit(x, y, 1)
        slope, intercept = coeffs
        # Annualized trend
        ann_return = float((np.exp(slope * 252) - 1) * 100)
        # Forecast
        future_x = np.arange(w, w + days_ahead)
        future_log = np.polyval(coeffs, future_x)
        forecast_prices = np.exp(future_log)
        # Residual std for confidence interval
        fitted = np.polyval(coeffs, x)
        residual_std = float(np.std(y - fitted))

        forecasts[label] = {
            "slope_daily": float(slope),
            "ann_return_pct": round(ann_return, 1),
            "forecast_end": round(float(forecast_prices[-1]), 2),
            "forecast_pct": round((float(forecast_prices[-1]) / current_price - 1) * 100, 1),
            "residual_std": residual_std,
        }

    # Blended forecast (weight: short 50%, mid 30%, long 20%)
    blend_price = (
        forecasts["short"]["forecast_end"] * 0.5 +
        forecasts["mid"]["forecast_end"] * 0.3 +
        forecasts["long"]["forecast_end"] * 0.2
    )
    blend_pct = round((blend_price / current_price - 1) * 100, 1)

    # Confidence bands (using short-term residual std).
    # WHY (B4): the old band was res_std·√h·1.96 — it captured residual scatter
    # but IGNORED slope (parameter) uncertainty, so the cone was too narrow far
    # out. Use the proper OLS prediction-interval factor for a NEW observation
    # at x*: se = res_std·√(1 + 1/n + (x*-x̄)²/Σ(x-x̄)²). The short regression
    # ran on x = 0..w_s-1, so the forecast end sits at x* = (w_s-1)+days_ahead.
    res_std = forecasts["short"]["residual_std"]
    w_s = min(60, n)
    x_short = np.arange(w_s)
    x_mean = float(x_short.mean())
    sxx = float(np.sum((x_short - x_mean) ** 2))

    def _pi_se(steps_ahead):
        """Prediction-interval std for a point `steps_ahead` beyond the last
        observed bar (anchor at x=w_s-1). Falls back to the old √h scaling if
        the design is degenerate (sxx==0, e.g. a single point)."""
        if sxx <= 0 or w_s <= 1:
            return res_std * np.sqrt(max(steps_ahead, 0.0))
        x_star = (w_s - 1) + steps_ahead
        return res_std * np.sqrt(1.0 + 1.0 / w_s + (x_star - x_mean) ** 2 / sxx)

    ci_factor = _pi_se(days_ahead) * 1.96
    # blend_price comes out of OLS extrapolation and can briefly go non-positive
    # for highly-shorted instruments — np.log(<=0) emits a RuntimeWarning every
    # forecast pass. Clamp to a small epsilon so the band collapses to 0 instead.
    log_ratio = np.log(max(blend_price / current_price, 1e-9))
    upper_price = round(current_price * np.exp(log_ratio + ci_factor), 2)
    lower_price = round(current_price * np.exp(log_ratio - ci_factor), 2)

    # Forecast path (daily for chart). NOTE: built from the SHORT (60d) fit
    # only, so its day-30 endpoint is the short-window projection and can
    # differ from the blended headline `forecast_price` (short/mid/long mix)
    # shown beside it — the chart trades headline consistency for a path
    # that tracks the recent trend.
    w = min(60, n)
    short_coeffs = np.polyfit(np.arange(w), log_prices[-w:], 1)
    # WHY (#175): the fitted value at the anchor is NOT the actual last close
    # (any stock sitting off its regression line started the rendered path at
    # a visibly different price than the live quote). Offset the whole path in
    # log space so day 0 equals current_price exactly.
    _anchor_offset = float(np.log(current_price)
                           - np.polyval(short_coeffs, w - 1))
    path = []
    for d in range(days_ahead + 1):
        # WHY (Q12): the fit's x-axis runs 0..w-1, so the *last observed* day
        # sits at t=w-1, not t=w. Evaluating day0 at t=w over-extrapolated the
        # whole path by one step. Anchor day d at t=w-1+d. Likewise the band
        # must vanish at d=0 (no forecast error on the anchor).
        t = (w - 1) + d
        log_p = np.polyval(short_coeffs, t) + _anchor_offset
        p = float(np.exp(log_p))
        # Proper prediction-interval band (B4): includes slope uncertainty, so
        # the cone widens correctly with horizon instead of only as √d.
        # d=0 is the OBSERVED close, not a forecast — zero band by invariant
        # (#175: _pi_se's "1 +" new-observation term is nonzero even at the
        # anchor, which painted an uncertainty band on "today").
        band = _pi_se(d) * 1.96 if d > 0 else 0.0
        u = float(np.exp(log_p + band))
        l = float(np.exp(log_p - band))
        path.append({"day": d, "price": round(p, 2), "upper": round(u, 2), "lower": round(l, 2)})

    # Trend direction
    short_trend = "UP" if forecasts["short"]["slope_daily"] > 0 else "DOWN"
    mid_trend   = "UP" if forecasts["mid"]["slope_daily"] > 0 else "DOWN"
    aligned = short_trend == mid_trend

    return {
        "current_price": round(current_price, 2),
        "forecast_price": round(blend_price, 2),
        "forecast_pct": blend_pct,
        "upper_ci": upper_price,
        "lower_ci": lower_price,
        "days_ahead": days_ahead,
        "trend_short": short_trend,
        "trend_mid": mid_trend,
        "trends_aligned": aligned,
        "regimes": forecasts,
        "path": path,
    }


# ── Model 3: Regime Detection (K-Means) ──────────────────────────────────────

def _detect_regime(hist, features=None):
    """
    Cluster recent market states into regimes:
      - LOW VOL TREND   = steady up/down
      - HIGH VOL CHOP   = mean-reverting, volatile
      - BREAKOUT         = vol expanding, directional
      - COMPRESSION      = vol contracting, coiling

    WHY (Q14): accepts the same pre-built ``(df_full, feature_cols)`` tuple as
    ``_rf_predict`` so the 2y feature frame is built once per forecast, not
    twice. ``features=None`` keeps the standalone behaviour.
    """
    from sklearn.cluster import KMeans
    from sklearn.preprocessing import StandardScaler

    df, feature_cols = features if features is not None else _build_features(hist)

    regime_features = ["vol_20d", "bb_width", "rsi_14", "roc_20", "atr_14", "vol_ratio"]
    subset = df[regime_features].dropna()
    if len(subset) < 60:
        return None

    scaler = StandardScaler()
    X = scaler.fit_transform(subset.values)

    # 4 clusters
    km = KMeans(n_clusters=4, random_state=42, n_init=10)
    labels = km.fit_predict(X)
    subset = subset.copy()
    subset["cluster"] = labels

    # Characterize each cluster by its centroid
    centroids = scaler.inverse_transform(km.cluster_centers_)
    cluster_chars = {}
    for i in range(4):
        c = dict(zip(regime_features, centroids[i]))
        cluster_chars[i] = c

    # Label clusters semantically
    def _label_cluster(chars):
        vol = chars["vol_20d"]
        bb = chars["bb_width"]
        roc = chars["roc_20"]
        rsi = chars["rsi_14"]

        if bb < 0.06 and vol < 0.015:
            return "COMPRESSION"
        if vol > 0.025 and abs(roc) > 0.05:
            return "BREAKOUT"
        if vol > 0.02 and rsi > 40 and rsi < 60:
            return "HIGH VOL CHOP"
        return "LOW VOL TREND"

    cluster_labels = {i: _label_cluster(c) for i, c in cluster_chars.items()}

    # WHY (#162): two clusters can map to the SAME semantic label (common:
    # "LOW VOL TREND" is the default branch), and regime_stats below is keyed
    # by label — a duplicate silently overwrote the other cluster's stats, so
    # consumers (forecast_ensemble looks up regime_stats[current_regime] for
    # vol_20d) could pair the current regime with the WRONG cluster's vol.
    # Disambiguate duplicates ("LOW VOL TREND #2") and use the disambiguated
    # label everywhere (current_regime, distribution, stats) so the label →
    # stats lookup is always the current bar's own cluster.
    _label_seen: dict = {}
    for i in sorted(cluster_labels):
        base = cluster_labels[i]
        _label_seen[base] = _label_seen.get(base, 0) + 1
        if _label_seen[base] > 1:
            cluster_labels[i] = "%s #%d" % (base, _label_seen[base])

    # Current regime
    current_cluster = int(labels[-1])
    current_regime = cluster_labels[current_cluster]

    # Days in current regime
    streak = 1
    for j in range(len(labels) - 2, -1, -1):
        if labels[j] == current_cluster:
            streak += 1
        else:
            break

    # Regime history (last 60 days)
    recent_labels = labels[-60:]
    regime_dist = {}
    for lbl in recent_labels:
        name = cluster_labels[int(lbl)]
        regime_dist[name] = regime_dist.get(name, 0) + 1

    return {
        "current_regime": current_regime,
        "days_in_regime": streak,
        "regime_distribution_60d": regime_dist,
        "regime_stats": {
            cluster_labels[i]: {
                "vol_20d": round(c["vol_20d"] * 100, 2),
                "bb_width": round(c["bb_width"] * 100, 1),
                "rsi": round(c["rsi_14"], 1),
                "momentum": round(c["roc_20"] * 100, 1),
            }
            for i, c in cluster_chars.items()
        },
    }


# ── Model 4: Mean Reversion Signal ───────────────────────────────────────────

def _spread_stationarity(spread_vals):
    """Cheap stationarity check for the OU spread WITHOUT statsmodels (which is
    not bundled). Uses a variance-ratio statistic: for a stationary
    (mean-reverting) series, the variance of k-step changes grows SUBLINEARLY
    in k, so VR(k) = Var(Δ_k)/(k·Var(Δ_1)) < 1. For a random walk VR≈1; for a
    trending/non-stationary series VR>1.

    Returns (is_stationary: bool, vr: float|None). Fails OPEN to
    (True, None) when there isn't enough data to judge — i.e. we don't punish
    confidence on thin samples, matching the prior behavior.
    """
    try:
        x = np.asarray(spread_vals, dtype=float)
        x = x[np.isfinite(x)]
        if x.size < 40:
            return True, None
        d1 = np.diff(x)
        var1 = float(np.var(d1, ddof=1))
        if var1 <= 1e-12:
            return True, None
        k = 5
        dk = x[k:] - x[:-k]
        vark = float(np.var(dk, ddof=1))
        vr = vark / (k * var1)
        if not np.isfinite(vr):
            return True, None
        # VR comfortably below 1 ⇒ mean-reverting/stationary. Allow a margin.
        return (vr < 0.9), vr
    except Exception:
        return True, None


def _empirical_reversion_freq(close, ma, zscore, current_z, horizon=20):
    """Empirical P(reversion toward the MA within `horizon` days), conditional
    on the z-score bucket the stock is in RIGHT NOW, measured over the stock's
    OWN history (B/A: replaces the hardcoded half-life step-function).

    A "reversion" event = the |spread to MA| is smaller `horizon` bars later
    than it is today (price moved back toward its mean). We count that frequency
    among historical bars whose z-score fell in the same bucket as `current_z`.

    Returns (freq: float|None, n: int). freq is None when there aren't enough
    same-bucket observations to trust — the caller then falls back to the old
    half-life constants.
    """
    try:
        c = close.reindex(zscore.index)
        m = ma.reindex(zscore.index)
        z = zscore
        spread_abs = (c - m).abs()
        # Bucket by |z|: [0,0.5),[0.5,1),[1,1.5),[1.5,2),[2,inf)
        def _bucket(zv):
            a = abs(zv)
            if a < 0.5:
                return 0
            if a < 1.0:
                return 1
            if a < 1.5:
                return 2
            if a < 2.0:
                return 3
            return 4
        cur_b = _bucket(current_z)
        vals = z.values
        sa = spread_abs.values
        n_total = len(vals)
        hits = 0
        n = 0
        for i in range(n_total - horizon):
            if _bucket(vals[i]) != cur_b:
                continue
            if not (np.isfinite(sa[i]) and np.isfinite(sa[i + horizon])):
                continue
            n += 1
            if sa[i + horizon] < sa[i]:
                hits += 1
        if n < 12:
            return None, n
        return float(hits) / float(n), n
    except Exception:
        return None, 0


def _mean_reversion_signal(hist):
    """
    Compute mean-reversion probability using z-score and
    Ornstein-Uhlenbeck half-life estimation.
    """
    close = hist["Close"].dropna()
    if len(close) < 60:
        return None

    # Z-score vs 50-day mean
    ma50 = close.rolling(50).mean()
    std50 = close.rolling(50).std()
    zscore = ((close - ma50) / std50.replace(0, np.nan)).dropna()
    if len(zscore) < 20:
        return None

    current_z = float(zscore.iloc[-1])

    # OU half-life: regress delta(price) on price - mean
    spread = (close - ma50).dropna()
    if len(spread) < 30:
        return None

    # Stationarity guard (B3): the spread is regressed against a TRAILING mean,
    # which makes it mechanically mean-reverting; if it's actually
    # non-stationary, θ is untrustworthy. We do NOT hard-fail — we lower the
    # reported confidence below.
    is_stationary, var_ratio = _spread_stationarity(spread.values)

    spread_lag = spread.shift(1).dropna()
    spread_diff = spread.diff().dropna()
    common_idx = spread_lag.index.intersection(spread_diff.index)
    if len(common_idx) < 20:
        return None

    y = spread_diff.loc[common_idx].values
    x = spread_lag.loc[common_idx].values.reshape(-1, 1)

    from sklearn.linear_model import LinearRegression
    reg = LinearRegression(fit_intercept=True)
    reg.fit(x, y)
    theta = float(-reg.coef_[0])

    if theta <= 0:
        half_life = None
    else:
        half_life = round(np.log(2) / theta, 1)

    # A2: prefer an EMPIRICAL reversion frequency measured on this stock's own
    # history, conditional on the current z-score bucket. Fall back to the old
    # half-life step-function constants only when there isn't enough same-bucket
    # history to trust the empirical estimate.
    emp_freq, emp_n = _empirical_reversion_freq(close, ma50, zscore, current_z, horizon=20)
    mr_prob_source = "empirical"
    if emp_freq is not None:
        mr_probability = float(emp_freq)
    else:
        mr_prob_source = "half_life_prior"
        if theta <= 0:
            mr_probability = 0.3  # weak mean reversion
        elif half_life < 5:
            mr_probability = 0.85
        elif half_life < 15:
            mr_probability = 0.70
        elif half_life < 30:
            mr_probability = 0.55
        else:
            mr_probability = 0.40

        # Adjust by z-score magnitude (more extended = higher reversion
        # probability). The empirical estimate already conditions on the
        # z-bucket, so this hand-tuned bump applies only to the prior fallback.
        z_adj = min(0.15, abs(current_z) * 0.05)
        if abs(current_z) > 1.5:
            mr_probability = min(0.95, mr_probability + z_adj)

    # Direction of expected reversion
    if current_z > 0.5:
        reversion_dir = "DOWN"
        signal = "OVERBOUGHT"
    elif current_z < -0.5:
        reversion_dir = "UP"
        signal = "OVERSOLD"
    else:
        reversion_dir = "FLAT"
        signal = "FAIR VALUE"

    # Confidence (B3): when the spread looks non-stationary, θ/half-life are
    # untrustworthy, so we report LOWER confidence rather than hard-failing.
    # The empirical-frequency path is more robust to this, so its haircut is
    # gentler. base_conf scales with how far mr_probability sits from a coin flip.
    base_conf = abs(float(mr_probability) - 0.5) * 2.0
    if is_stationary:
        mr_confidence = base_conf
    else:
        mr_confidence = base_conf * (0.6 if mr_prob_source == "empirical" else 0.4)
    mr_confidence = round(float(min(0.95, max(0.0, mr_confidence))), 3)

    return {
        "zscore": round(current_z, 2),
        "half_life_days": half_life,
        "mr_probability": round(mr_probability, 3),
        "reversion_direction": reversion_dir,
        "signal": signal,
        "ou_theta": round(theta, 4),
        # ADD-only diagnostic fields (existing keys untouched).
        "mr_prob_source": mr_prob_source,
        "mr_samples": emp_n,
        "spread_stationary": bool(is_stationary),
        "variance_ratio": round(float(var_ratio), 3) if var_ratio is not None else None,
        "mr_confidence": mr_confidence,
    }


# ── Composite ML Forecast ────────────────────────────────────────────────────

def ml_forecast(symbol, bypass_cache=False):
    """
    Run all ML models on a symbol and return a unified forecast.
    All models train on the stock's own data — no mock logic.
    Results are cached for 1 hour to avoid expensive retraining.
    """
    symbol = symbol.upper()

    # Check cache first (thread-safe).
    if not bypass_cache:
        cached = _cache_get(symbol)
        if cached is not None:
            return cached

    # Single-flight: serialize concurrent callers of THIS symbol so only one
    # thread trains while the rest wait and then pick up the fresh cache.
    # Different symbols still train in parallel (per-symbol lock).
    lock = _symbol_lock(symbol)
    with lock:
        if not bypass_cache:
            cached = _cache_get(symbol)
            if cached is not None:
                return cached
        return _ml_forecast_compute(symbol)


def _ml_forecast_compute(symbol):
    """Actually fetch history, train models, cache and return the forecast.
    Called only while holding the per-symbol single-flight lock."""
    import fetcher
    import pandas as pd

    import time
    t0 = time.time()

    # Route through fetcher.get_chart_data so we inherit the Yahoo direct-chart
    # fallback when yfinance's crumb auth breaks.
    try:
        bars = fetcher.get_chart_data(symbol, period="2y", interval="1d")
        if not bars:
            return {"symbol": symbol, "error": "Failed to fetch history"}
        hist = pd.DataFrame(bars)
        hist["Date"] = pd.to_datetime(hist["time"], unit="s")
        hist = hist.set_index("Date")
        hist = hist.rename(columns={
            "open": "Open", "high": "High", "low": "Low",
            "close": "Close", "volume": "Volume",
        })
    except Exception as e:
        return {"symbol": symbol, "error": f"Failed to fetch history: {e}"}

    if hist.empty or len(hist) < 100:
        return {"symbol": symbol, "error": "Insufficient history (need 100+ days)"}

    results = {"symbol": symbol}

    # WHY (Q14): _rf_predict and _detect_regime each used to call
    # _build_features(hist) independently, engineering the full 2y feature
    # matrix TWICE per forecast. Build it once here and pass it into both.
    # Fail-open: if feature engineering throws, fall back to None so each
    # model rebuilds on its own (prior behaviour) rather than crashing.
    try:
        _features = _build_features(hist)
    except Exception as e:
        log.warning("feature build failed for %s: %s", symbol, e)
        _features = None

    # 1. Random Forest
    try:
        rf_result = _rf_predict(hist, features=_features)
        results["rf_classifier"] = rf_result
    except Exception as e:
        log.warning("RF error for %s: %s", symbol, e)
        results["rf_classifier"] = None

    # 2. Trend Forecast
    try:
        trend = _trend_forecast(hist, days_ahead=30)
        results["trend_forecast"] = trend
    except Exception as e:
        log.warning("Trend error for %s: %s", symbol, e)
        results["trend_forecast"] = None

    # 3. Regime Detection
    try:
        regime = _detect_regime(hist, features=_features)
        results["regime"] = regime
    except Exception as e:
        log.warning("Regime error for %s: %s", symbol, e)
        results["regime"] = None

    # 4. Mean Reversion
    try:
        mr = _mean_reversion_signal(hist)
        results["mean_reversion"] = mr
    except Exception as e:
        log.warning("MR error for %s: %s", symbol, e)
        results["mean_reversion"] = None

    # Dispersion for the principled trend→prob map.
    #
    # WHY (Q13): the trend vote's expected move (forecast_pct) comes from a
    # BLEND of slope fits over 60/120/250-day windows, but the old map divided
    # it by a 60-day realized vol. A drift estimated partly from 250 days of
    # data paired with a 60-day dispersion is a units mismatch: it understates
    # the uncertainty around a slow long-window drift and inflates |prob-0.5|.
    # Use the trend regression's OWN residual scatter, blended with the SAME
    # 0.5/0.3/0.2 weights the price forecast uses, so the dispersion is fit-
    # consistent. `residual_std` is the daily log-space std around each slope
    # fit. Fall back to the 60d realized vol, then to None.
    _daily_vol = None
    _tf = results.get("trend_forecast")
    if _tf and isinstance(_tf.get("regimes"), dict):
        try:
            reg = _tf["regimes"]
            blend = 0.0
            wsum = 0.0
            for label, w in (("short", 0.5), ("mid", 0.3), ("long", 0.2)):
                rs = reg.get(label, {}).get("residual_std")
                if rs is not None and np.isfinite(rs) and rs > 0:
                    blend += w * float(rs)
                    wsum += w
            if wsum > 0:
                _daily_vol = blend / wsum
        except Exception:
            _daily_vol = None
    if _daily_vol is None:
        try:
            _v = float(hist["Close"].pct_change().dropna().iloc[-60:].std())
            if np.isfinite(_v) and _v > 0:
                _daily_vol = _v
        except Exception:
            _daily_vol = None

    # Composite ML signal
    signals = []
    if results.get("rf_classifier"):
        p = results["rf_classifier"]["prob_up_20d"]
        signals.append(("RF", p))
    if results.get("trend_forecast"):
        fp = results["trend_forecast"]["forecast_pct"]
        # forecast_pct is a 30-day projection but the RF/MR votes are 20-day;
        # rescale to a 20d-equivalent (fp*20/30) before mapping to a probability
        # so the trend vote isn't over-weighted vs the other channels.
        fp20 = fp * 20.0 / 30.0
        # A3: principled trend-%→probability via the normal CDF of the expected
        # return scaled by its horizon dispersion: P(up)=Φ(E[r]/(σ_daily·√h)).
        # Falls back to the old linear map only when realized vol is unavailable.
        if _daily_vol is not None:
            sigma_h = _daily_vol * np.sqrt(20.0)
            p = _phi((fp20 / 100.0) / sigma_h) if sigma_h > 0 else 0.5
            p = float(min(0.95, max(0.05, p)))
        else:
            p = 0.5 + min(0.4, max(-0.4, fp20 / 20))
        signals.append(("Trend", p))
    if results.get("mean_reversion"):
        mr = results["mean_reversion"]
        if mr["reversion_direction"] == "UP":
            p = 0.5 + mr["mr_probability"] * 0.3
        elif mr["reversion_direction"] == "DOWN":
            p = 0.5 - mr["mr_probability"] * 0.3
        else:
            p = 0.5
        signals.append(("MR", p))

    if signals:
        # Weighted average (RF gets highest weight as it's the real ML model)
        weights = {"RF": 0.50, "Trend": 0.30, "MR": 0.20}
        total_w = sum(weights.get(s[0], 0.2) for s in signals)
        composite = sum(s[1] * weights.get(s[0], 0.2) for s in signals) / total_w
        results["composite_ml_score"] = round(composite, 3)
        results["composite_signal"] = (
            "STRONG BUY" if composite > 0.70 else
            "BUY" if composite > 0.58 else
            "NEUTRAL" if composite > 0.42 else
            "SELL" if composite > 0.30 else
            "STRONG SELL"
        )
    else:
        results["composite_ml_score"] = None
        results["composite_signal"] = "INSUFFICIENT DATA"

    results["computed_in_ms"] = round((time.time() - t0) * 1000)

    # Store in cache (thread-safe; readers see a fully-built result).
    with _cache_lock:
        _forecast_cache[symbol] = results
        _cache_times[symbol] = _time.time()
        _evict_expired_locked()

    return results
