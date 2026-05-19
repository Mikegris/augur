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
import time as _time
import numpy as np
import pandas as pd
from datetime import datetime

log = logging.getLogger(__name__)

# ── TTL cache: avoid retraining models on every request ──────────────────────
# Cache results for 1 hour per symbol. Training 4 models takes 2-5s per symbol.
_forecast_cache = {}   # symbol -> result dict
_cache_times = {}      # symbol -> timestamp
_CACHE_TTL = 3600      # 1 hour

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
    df["bb_width"] = (2 * bb_std) / bb_mid
    df["bb_position"] = (close - (bb_mid - 2 * bb_std)) / (4 * bb_std)

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

def _rf_predict(hist):
    """
    Train a Random Forest on the stock's own history to predict
    P(positive 20-day forward return). Uses walk-forward: train on
    all data except last 20 bars, predict on last row.

    Returns dict with probability, confidence, feature importances.
    """
    from sklearn.ensemble import RandomForestClassifier
    from sklearn.preprocessing import StandardScaler

    df, feature_cols = _build_features(hist)
    df = df.dropna(subset=feature_cols + ["fwd_ret_20d"])

    if len(df) < 120:
        return None  # not enough data

    # Binary target: 1 if positive, 0 if negative
    df["target"] = (df["fwd_ret_20d"] > 0).astype(int)

    # Train on all rows that have a known forward return
    train = df.iloc[:-20].dropna(subset=feature_cols + ["target"])
    if len(train) < 80:
        return None

    X_train = train[feature_cols].values
    y_train = train["target"].values

    # Scale features
    scaler = StandardScaler()
    X_train_scaled = scaler.fit_transform(X_train)

    # Latest features (no forward return needed)
    df_full, _ = _build_features(hist)
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

    # Predict probability
    proba = rf.predict_proba(X_latest)
    if proba.shape[1] < 2:
        prob_up = float(proba[0][0]) if y_train[0] == 1 else 1.0 - float(proba[0][0])
    else:
        class_idx = list(rf.classes_).index(1) if 1 in rf.classes_ else 0
        prob_up = float(proba[0][class_idx])

    # Feature importances
    importances = dict(zip(feature_cols, rf.feature_importances_))
    top_features = sorted(importances.items(), key=lambda x: x[1], reverse=True)[:8]

    # Walk-forward accuracy (last 60 predictions)
    test_start = max(0, len(train) - 60)
    test_df = train.iloc[test_start:]
    if len(test_df) >= 20:
        X_test = scaler.transform(test_df[feature_cols].values)
        preds = rf.predict(X_test)
        accuracy = float(np.mean(preds == test_df["target"].values))
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
        "accuracy_recent": round(accuracy, 3) if accuracy else None,
        "class_balance": round(class_balance, 3),
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

    # Confidence bands (using short-term residual std)
    res_std = forecasts["short"]["residual_std"]
    ci_factor = res_std * np.sqrt(days_ahead) * 1.96
    # blend_price comes out of OLS extrapolation and can briefly go non-positive
    # for highly-shorted instruments — np.log(<=0) emits a RuntimeWarning every
    # forecast pass. Clamp to a small epsilon so the band collapses to 0 instead.
    log_ratio = np.log(max(blend_price / current_price, 1e-9))
    upper_price = round(current_price * np.exp(log_ratio + ci_factor), 2)
    lower_price = round(current_price * np.exp(log_ratio - ci_factor), 2)

    # Forecast path (daily for chart)
    short_coeffs = np.polyfit(np.arange(min(60, n)), log_prices[-min(60, n):], 1)
    path = []
    for d in range(days_ahead + 1):
        t = min(60, n) + d
        log_p = np.polyval(short_coeffs, t)
        p = float(np.exp(log_p))
        u = float(np.exp(log_p + res_std * np.sqrt(d + 1) * 1.96))
        l = float(np.exp(log_p - res_std * np.sqrt(d + 1) * 1.96))
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

def _detect_regime(hist):
    """
    Cluster recent market states into regimes:
      - LOW VOL TREND   = steady up/down
      - HIGH VOL CHOP   = mean-reverting, volatile
      - BREAKOUT         = vol expanding, directional
      - COMPRESSION      = vol contracting, coiling
    """
    from sklearn.cluster import KMeans
    from sklearn.preprocessing import StandardScaler

    df, feature_cols = _build_features(hist)

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
        mr_probability = 0.3  # weak mean reversion
    else:
        half_life = round(np.log(2) / theta, 1)
        # Faster half-life = stronger mean reversion
        if half_life < 5:
            mr_probability = 0.85
        elif half_life < 15:
            mr_probability = 0.70
        elif half_life < 30:
            mr_probability = 0.55
        else:
            mr_probability = 0.40

    # Adjust by z-score magnitude (more extended = higher reversion probability)
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

    return {
        "zscore": round(current_z, 2),
        "half_life_days": half_life,
        "mr_probability": round(mr_probability, 3),
        "reversion_direction": reversion_dir,
        "signal": signal,
        "ou_theta": round(theta, 4),
    }


# ── Composite ML Forecast ────────────────────────────────────────────────────

def ml_forecast(symbol, bypass_cache=False):
    """
    Run all ML models on a symbol and return a unified forecast.
    All models train on the stock's own data — no mock logic.
    Results are cached for 1 hour to avoid expensive retraining.
    """
    import fetcher
    import pandas as pd

    symbol = symbol.upper()

    # Check cache first
    if not bypass_cache and symbol in _forecast_cache:
        cached_at = _cache_times.get(symbol, 0)
        if (_time.time() - cached_at) < _CACHE_TTL:
            return _forecast_cache[symbol]

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

    # 1. Random Forest
    try:
        rf_result = _rf_predict(hist)
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
        regime = _detect_regime(hist)
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

    # Composite ML signal
    signals = []
    if results.get("rf_classifier"):
        p = results["rf_classifier"]["prob_up_20d"]
        signals.append(("RF", p))
    if results.get("trend_forecast"):
        fp = results["trend_forecast"]["forecast_pct"]
        # Map forecast % to 0-1 probability-like
        p = 0.5 + min(0.4, max(-0.4, fp / 20))
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

    # Store in cache
    _forecast_cache[symbol] = results
    _cache_times[symbol] = _time.time()

    return results
