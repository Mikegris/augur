"""
Opportunity Scanner — Multi-signal investment opportunity discovery engine.

Scans across stocks, ETFs, crypto, and options to find early-stage
investment opportunities ranked by a composite score. Uses the user's
investment profile (risk tolerance, horizon, strategy, etc.) to
personalize scoring weights and universe selection.

Python 3.9 compatible.
"""

import json
import time
import logging
import hashlib
from typing import Optional, Dict, List, Any
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

import database as db
import fetcher

logger = logging.getLogger(__name__)

try:
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover
    _ET = None


def _now_et() -> datetime:
    """Naive ET 'now' — anchors window cutoffs to US market wall clock so
    SEC Form 4 dates, Congress filing dates, and earnings calendars don't
    drift by a day for callers outside the US time zone."""
    if _ET is not None:
        return datetime.now(_ET).replace(tzinfo=None)
    return datetime.now()

# ── Profile Defaults ─────────────────────────────────────────────────────────

PROFILE_DEFAULTS = {
    "risk_tolerance": "moderate",       # conservative, moderate, aggressive
    "horizon": "medium",                # short (<3mo), medium (3-12mo), long (1yr+)
    "asset_types": ["stocks", "etfs"],  # stocks, etfs, crypto, options
    "sectors": [],                      # empty = all sectors
    "budget_min": 0,                    # min price per share/unit
    "budget_max": 10000,                # max price per share/unit
    "strategy": "growth",              # growth, value, income, momentum
}

# ── Curated Universes ────────────────────────────────────────────────────────

UNIVERSE_GROWTH = [
    "NVDA", "AMD", "AVGO", "SHOP", "SQ", "DDOG", "SNOW", "CRWD", "NET",
    "PLTR", "MELI", "UBER", "ABNB", "DASH", "RBLX", "COIN", "SOFI",
    "SMCI", "ARM", "PANW", "ZS", "MDB", "ANET", "NOW", "ADBE",
    # Growth ETFs
    "QQQ", "VUG", "ARKK", "XLK", "IGV",
]

UNIVERSE_VALUE = [
    "BRK-B", "JPM", "BAC", "WFC", "JNJ", "PG", "KO", "PEP", "MRK",
    "CVX", "XOM", "LMT", "GD", "MMM", "IBM", "VZ", "T", "CSCO",
    "BMY", "AMGN", "GILD", "CI", "UNH",
    # Value ETFs
    "VTV", "SCHD", "IWD", "DVY",
]

UNIVERSE_INCOME = [
    "O", "SCHD", "VYM", "T", "VZ", "MO", "PM", "KO", "PEP", "XOM",
    "ABBV", "PFE", "BMY", "SPG", "AVB", "WPC", "STAG", "MAIN",
    "ARCC", "EPD", "ET",
    # Income ETFs
    "JEPI", "JEPQ", "HDV", "SPHD",
]

UNIVERSE_CRYPTO_IDS = {
    "BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana",
    "ADA": "cardano", "AVAX": "avalanche-2", "DOT": "polkadot",
    "MATIC": "polygon-ecosystem-token", "LINK": "chainlink", "UNI": "uniswap",
    "AAVE": "aave", "XRP": "ripple", "BNB": "binancecoin",
    "NEAR": "near", "SUI": "sui", "APT": "aptos",
    "DOGE": "dogecoin", "SHIB": "shiba-inu", "ARB": "arbitrum",
    "OP": "optimism", "FIL": "filecoin",
    # MATIC/SUI/UNI's -USD tickers fail on Yahoo Finance. fetcher.get_quote()
    # falls back to CoinGecko for any ticker in CRYPTO_FALLBACK_IDS, so the
    # full universe stays scannable even when Yahoo's coverage drops a coin.
}

SECTORS = [
    "Technology", "Healthcare", "Financials", "Energy",
    "Consumer Cyclical", "Industrials", "Communication Services",
    "Consumer Defensive", "Utilities", "Real Estate", "Basic Materials",
]

# ── Strategy Weight Profiles ─────────────────────────────────────────────────
# Weights for each signal dimension per strategy (must sum to ~100)

STRATEGY_WEIGHTS = {
    "growth": {
        "smart_money": 15, "ml_forecast": 20, "momentum": 20,
        "fundamentals": 15, "insider": 10, "congress": 5,
        "options_flow": 10, "dividend": 5,
    },
    "value": {
        "smart_money": 20, "ml_forecast": 15, "momentum": 5,
        "fundamentals": 25, "insider": 15, "congress": 5,
        "options_flow": 10, "dividend": 5,
    },
    "income": {
        "smart_money": 15, "ml_forecast": 10, "momentum": 5,
        "fundamentals": 15, "insider": 10, "congress": 5,
        "options_flow": 10, "dividend": 30,
    },
    "momentum": {
        "smart_money": 15, "ml_forecast": 25, "momentum": 25,
        "fundamentals": 5, "insider": 10, "congress": 5,
        "options_flow": 15, "dividend": 0,
    },
}

# ── Scan Cache ───────────────────────────────────────────────────────────────

_scan_cache = {}        # profile_hash -> results dict
_scan_cache_time = {}   # profile_hash -> timestamp
_SCAN_CACHE_TTL = 1800  # 30 minutes


# ══════════════════════════════════════════════════════════════════════════════
# PROFILE MANAGEMENT
# ══════════════════════════════════════════════════════════════════════════════

def get_scanner_profile():
    # type: () -> dict
    """Load scanner profile from settings, or return defaults."""
    settings = db.get_settings()
    raw = settings.get("scanner_profile", "")
    if raw:
        try:
            profile = json.loads(raw)
            # Merge with defaults for any missing keys
            merged = dict(PROFILE_DEFAULTS)
            merged.update(profile)
            return merged
        except (json.JSONDecodeError, TypeError):
            pass
    return dict(PROFILE_DEFAULTS)


def save_scanner_profile(profile):
    # type: (dict) -> None
    """Persist scanner profile to settings table."""
    merged = dict(PROFILE_DEFAULTS)
    merged.update(profile)
    # Validate
    merged["risk_tolerance"] = merged["risk_tolerance"] if merged["risk_tolerance"] in ("conservative", "moderate", "aggressive") else "moderate"
    merged["horizon"] = merged["horizon"] if merged["horizon"] in ("short", "medium", "long") else "medium"
    merged["strategy"] = merged["strategy"] if merged["strategy"] in ("growth", "value", "income", "momentum") else "growth"
    if not isinstance(merged["asset_types"], list):
        merged["asset_types"] = ["stocks", "etfs"]
    if not isinstance(merged["sectors"], list):
        merged["sectors"] = []
    merged["budget_min"] = max(0, float(merged.get("budget_min", 0)))
    merged["budget_max"] = max(merged["budget_min"], float(merged.get("budget_max", 10000)))
    db.set_setting("scanner_profile", json.dumps(merged))


def _profile_hash(profile):
    # type: (dict) -> str
    raw = json.dumps(profile, sort_keys=True)
    return hashlib.md5(raw.encode()).hexdigest()[:12]


# ══════════════════════════════════════════════════════════════════════════════
# UNIVERSE BUILDER
# ══════════════════════════════════════════════════════════════════════════════

def _build_universe(profile):
    # type: (dict) -> List[Dict[str, Any]]
    """Build candidate symbol list based on user profile.
    Returns list of dicts: {symbol, asset_class, source}"""
    candidates = []
    seen = set()
    strategy = profile.get("strategy", "growth")
    asset_types = profile.get("asset_types", ["stocks", "etfs"])
    sectors = [s.lower() for s in profile.get("sectors", [])]

    def _add(sym, asset_class, source):
        s = sym.upper()
        if s not in seen:
            seen.add(s)
            candidates.append({"symbol": s, "asset_class": asset_class, "source": source})

    # Strategy-based universe
    if "stocks" in asset_types or "etfs" in asset_types:
        if strategy == "growth":
            base = UNIVERSE_GROWTH
        elif strategy == "value":
            base = UNIVERSE_VALUE
        elif strategy == "income":
            base = UNIVERSE_INCOME
        else:
            base = []  # momentum is built dynamically

        for sym in base:
            _add(sym, "stock", f"universe_{strategy}")

    # Momentum: pull from top movers
    if strategy == "momentum" or "stocks" in asset_types:
        try:
            movers = fetcher.get_top_movers()
            for g in (movers.get("gainers", []) or [])[:15]:
                if g.get("symbol"):
                    _add(g["symbol"], "stock", "top_gainer")
        except Exception:
            pass

    # Add sector ETF leaders
    if "etfs" in asset_types:
        try:
            sector_data = fetcher.get_sector_performance()
            for s in (sector_data or [])[:8]:
                sym = s.get("symbol", "")
                if sym:
                    _add(sym, "etf", "sector_leader")
        except Exception:
            pass

    # Congressional trades — recent buys
    if "stocks" in asset_types:
        try:
            import congress as cong_mod
            summary = cong_mod.get_congress_summary(days=45, max_pdfs=30)
            for ts in (summary.get("ticker_summary", []) or [])[:10]:
                if ts.get("buys", 0) > ts.get("sells", 0):
                    _add(ts["ticker"], "stock", "congress_buy")
        except Exception:
            pass

    # User watchlist
    try:
        watchlist = db.get_watchlist()
        for w in watchlist:
            atype = w.get("asset_type", "stock")
            if atype == "crypto" and "crypto" not in asset_types:
                continue
            _add(w["symbol"], atype, "watchlist")
    except Exception:
        pass

    # Crypto
    if "crypto" in asset_types:
        for sym in list(UNIVERSE_CRYPTO_IDS.keys())[:15]:
            _add(sym, "crypto", "crypto_universe")

    # Budget filter — quick price check
    budget_min = profile.get("budget_min", 0)
    budget_max = profile.get("budget_max", 10000)
    if budget_min > 0 or budget_max < 10000:
        equity_syms = [c["symbol"] for c in candidates if c["asset_class"] != "crypto"]
        if equity_syms:
            try:
                prices = fetcher.get_quotes_batch(equity_syms[:50])
                filtered = []
                for c in candidates:
                    if c["asset_class"] == "crypto":
                        filtered.append(c)
                        continue
                    p = (prices.get(c["symbol"]) or {}).get("price")
                    if p is None:
                        filtered.append(c)  # keep if price unknown
                    elif budget_min <= p <= budget_max:
                        filtered.append(c)
                candidates = filtered
            except Exception:
                pass

    # Cap at 50 symbols
    return candidates[:50]


# ══════════════════════════════════════════════════════════════════════════════
# SIGNAL SCORING
# ══════════════════════════════════════════════════════════════════════════════

def _score_equity(symbol, profile, weights):
    # type: (str, dict, dict) -> Dict[str, Any]
    """Score a stock/ETF opportunity using multiple signals."""
    result = {
        "symbol": symbol,
        "scores": {},
        "details": {},
        "early_signals": [],
        "composite": 0,
    }

    # 1. Smart Money Score (0-100 from compute_score)
    sm_score = 0
    sm_data = {}
    try:
        import smart_money
        sm_data = smart_money.compute_score(symbol)
        if not sm_data.get("error"):
            sm_score = sm_data.get("score", 0)
            result["details"]["smart_money"] = {
                "score": sm_score,
                "signal": sm_data.get("signal", "N/A"),
                "name": sm_data.get("name", symbol),
                "price": sm_data.get("price"),
            }
        else:
            result["details"]["smart_money"] = {"score": 0, "signal": "N/A", "error": sm_data.get("error")}
    except Exception as e:
        logger.debug("Smart money error for %s: %s", symbol, e)
    result["scores"]["smart_money"] = sm_score

    # Extract sub-signals from smart money components for detail
    components = sm_data.get("components", {})

    # 2. ML Forecast
    # ml_forecast.ml_forecast() returns nested components — not the flat
    # keys we used to read here. Pull each value from its real nest;
    # missing/None components fall back to neutral defaults so partial
    # signal sets don't zero out the whole channel.
    ml_score = 0
    try:
        import ml_forecast as mlf
        ml_data = mlf.ml_forecast(symbol)
        if not ml_data.get("error"):
            rf = ml_data.get("rf_classifier") or {}
            trend = ml_data.get("trend_forecast") or {}
            regime_obj = ml_data.get("regime") or {}
            mr = ml_data.get("mean_reversion") or {}

            prob_up = rf.get("prob_up_20d", 0.5)
            ml_score = round(prob_up * 100)
            regime = regime_obj.get("current_regime", "UNKNOWN")
            mr_signal = mr.get("signal", "NEUTRAL")
            trend_dir = trend.get("trend_short", "NEUTRAL")
            forecast_pct = trend.get("forecast_pct", 0)

            result["details"]["ml_forecast"] = {
                "prob_up": round(prob_up, 3),
                "regime": regime,
                "trend": trend_dir,
                "forecast_pct": round(forecast_pct, 2),
                "mr_signal": mr_signal,
                "mr_zscore": round(mr.get("zscore", 0), 2),
            }

            # Early signal: compression regime (pre-breakout)
            if regime == "COMPRESSION":
                result["early_signals"].append("Pre-breakout compression regime")
            # Early signal: oversold + momentum turning
            if mr_signal == "OVERSOLD" and forecast_pct > 0:
                result["early_signals"].append("Oversold with positive trend forming")
    except Exception as e:
        logger.debug("ML forecast error for %s: %s", symbol, e)
    result["scores"]["ml_forecast"] = ml_score

    # 3. Momentum (from smart_money components or compute fresh)
    momentum_score = 0
    mom_comp = components.get("momentum", {})
    if mom_comp:
        momentum_score = round(mom_comp.get("score", 0) / max(mom_comp.get("max", 1), 1) * 100)
    result["scores"]["momentum"] = momentum_score

    # 4. Fundamentals
    fund_score = 0
    earnings_comp = components.get("earnings_quality", {})
    if earnings_comp:
        fund_score = round(earnings_comp.get("score", 0) / max(earnings_comp.get("max", 1), 1) * 100)
    # Enhance with fetcher fundamentals
    try:
        fdata = fetcher.get_fundamentals(symbol)
        if fdata and not fdata.get("error"):
            pe = fdata.get("pe_ratio")
            eg = fdata.get("earnings_growth")
            rg = fdata.get("revenue_growth")
            pm = fdata.get("profit_margin")
            result["details"]["fundamentals"] = {
                "pe_ratio": pe,
                "earnings_growth": eg,
                "revenue_growth": rg,
                "profit_margin": pm,
                "market_cap": fdata.get("market_cap"),
                "sector": fdata.get("sector", ""),
            }
            # Boost fund_score based on growth metrics
            if eg and eg > 0.15:
                fund_score = min(100, fund_score + 15)
            if rg and rg > 0.10:
                fund_score = min(100, fund_score + 10)
            if pm and pm > 0.20:
                fund_score = min(100, fund_score + 10)
    except Exception:
        pass
    result["scores"]["fundamentals"] = fund_score

    # 5. Insider Activity
    insider_score = 0
    insider_comp = components.get("insider_activity", {})
    if insider_comp:
        insider_score = round(insider_comp.get("score", 0) / max(insider_comp.get("max", 1), 1) * 100)
    # Check for cluster buying (early signal)
    try:
        import sec_edgar as edgar
        txns = edgar.get_form4_transactions(symbol, limit=15)
        cutoff_30d = (_now_et() - timedelta(days=30)).strftime("%Y-%m-%d")
        recent_buys = [t for t in txns if t.get("date", "") >= cutoff_30d and t.get("transaction_type") == "BUY"]
        recent_sells = [t for t in txns if t.get("date", "") >= cutoff_30d and t.get("transaction_type") == "SELL"]
        result["details"]["insider"] = {
            "buys_30d": len(recent_buys),
            "sells_30d": len(recent_sells),
            "net_buy_value": sum(t.get("value", 0) or 0 for t in recent_buys) - sum(t.get("value", 0) or 0 for t in recent_sells),
        }
        if len(recent_buys) >= 3:
            result["early_signals"].append(f"Insider cluster: {len(recent_buys)} buys in 30 days")
            insider_score = min(100, insider_score + 20)
    except Exception:
        pass
    result["scores"]["insider"] = insider_score

    # 6. Congressional Interest
    congress_score = 0
    try:
        import congress as cong_mod
        trades = cong_mod.get_trades_for_ticker(symbol, days=90)
        buys = [t for t in trades if t.get("txn_type_raw") in ("P", "PE")]
        sells = [t for t in trades if "S" in (t.get("txn_type_raw") or "")]
        if buys or sells:
            ratio = len(buys) / max(len(buys) + len(sells), 1)
            congress_score = round(ratio * 100)
            result["details"]["congress"] = {
                "buys": len(buys),
                "sells": len(sells),
                "members": len(set(t.get("member_name", "") for t in trades)),
            }
            # Early signal: recent congressional buy
            cutoff_30d_dt = _now_et() - timedelta(days=30)
            recent_cong = [t for t in buys if t.get("txn_date", "") and _parse_date_loose(t["txn_date"]) >= cutoff_30d_dt]
            if recent_cong:
                result["early_signals"].append(f"Congress: {len(recent_cong)} buy(s) in 30 days")
    except Exception:
        pass
    result["scores"]["congress"] = congress_score

    # 7. Options Flow
    options_score = 0
    try:
        flow = fetcher.get_unusual_options_flow(symbol)
        unusual = flow.get("unusual", [])
        if unusual:
            bulls = len([u for u in unusual if "BULL" in (u.get("sentiment") or "")])
            bears = len([u for u in unusual if "BEAR" in (u.get("sentiment") or "")])
            total = max(bulls + bears, 1)
            options_score = round(bulls / total * 100)
            result["details"]["options_flow"] = {
                "unusual_contracts": len(unusual),
                "bullish": bulls,
                "bearish": bears,
            }
            # Early signal: high vol/OI ratio calls
            big_calls = [u for u in unusual if u.get("side") == "CALL" and (u.get("vol_oi_ratio") or 0) > 5]
            if big_calls:
                result["early_signals"].append(f"Options: {len(big_calls)} contracts with vol/OI > 5x")
    except Exception:
        pass
    result["scores"]["options_flow"] = options_score

    # 8. Dividend Yield
    div_score = 0
    try:
        div_data = fetcher.get_dividend_data(symbol)
        div_yield = div_data.get("div_yield") or 0
        if div_yield > 0:
            # Scale: 0% = 0, 3% = 50, 6%+ = 100
            # div_yield is already in percent units (e.g. 1.8 == 1.8%)
            div_score = min(100, round(div_yield / 6.0 * 100))
            result["details"]["dividend"] = {
                "yield_pct": round(div_yield, 2),
                "annual_rate": div_data.get("div_rate"),
                "ex_date": div_data.get("ex_date"),
            }
    except Exception:
        pass
    result["scores"]["dividend"] = div_score

    # ── Compute weighted composite ──────────────────────────────────────────
    composite = 0.0
    for dim, weight in weights.items():
        raw = result["scores"].get(dim, 0)
        composite += (raw / 100.0) * weight
    result["composite"] = round(composite, 1)

    # ── Early-stage bonus ───────────────────────────────────────────────────
    bonus = len(result["early_signals"]) * 3  # 3 pts per early signal, max ~15
    result["early_bonus"] = min(bonus, 15)
    result["composite"] = min(100, round(result["composite"] + result["early_bonus"], 1))

    # ── Risk adjustment ─────────────────────────────────────────────────────
    risk = profile.get("risk_tolerance", "moderate")
    if risk == "conservative":
        # Penalize high-volatility / speculative
        if ml_score < 40:
            result["composite"] = max(0, result["composite"] - 5)
    elif risk == "aggressive":
        # Boost high-momentum + early signals
        if result["early_signals"]:
            result["composite"] = min(100, result["composite"] + 3)

    # ── Horizon adjustment ──────────────────────────────────────────────────
    horizon = profile.get("horizon", "medium")
    if horizon == "short":
        # Short-term: boost momentum, penalize low momentum
        if momentum_score > 60:
            result["composite"] = min(100, result["composite"] + 3)
    elif horizon == "long":
        # Long-term: boost fundamentals
        if fund_score > 60:
            result["composite"] = min(100, result["composite"] + 3)

    # Pull name/price from smart money data
    result["name"] = sm_data.get("name", symbol)
    result["price"] = sm_data.get("price")
    result["signal"] = _composite_to_signal(result["composite"])

    return result


def _score_crypto(symbol, profile, weights):
    # type: (str, dict, dict) -> Dict[str, Any]
    """Score a crypto opportunity — uses subset of signals."""
    result = {
        "symbol": symbol,
        "asset_class": "crypto",
        "scores": {},
        "details": {},
        "early_signals": [],
        "composite": 0,
    }

    coin_id = UNIVERSE_CRYPTO_IDS.get(symbol, symbol.lower())

    # Get crypto quote from CoinGecko
    try:
        cdata = fetcher.get_crypto_quote(coin_id)
        if cdata and not cdata.get("error"):
            result["name"] = cdata.get("name", symbol)
            result["price"] = cdata.get("price")
            result["details"]["crypto"] = {
                "market_cap": cdata.get("market_cap"),
                "volume_24h": cdata.get("volume_24h"),
                "change_24h": cdata.get("change_24h"),
                "change_7d": cdata.get("change_7d"),
                "change_30d": cdata.get("change_30d"),
                "rank": cdata.get("rank"),
            }

            # Momentum from price changes
            chg_7d = cdata.get("change_7d") or 0
            chg_30d = cdata.get("change_30d") or 0
            momentum_score = 50  # neutral baseline
            if chg_7d > 5:
                momentum_score += 20
            if chg_30d > 10:
                momentum_score += 15
            if chg_7d < -10:
                momentum_score -= 20
            momentum_score = max(0, min(100, momentum_score))
            result["scores"]["momentum"] = momentum_score

            # Volume spike detection
            vol = cdata.get("volume_24h") or 0
            mcap = cdata.get("market_cap")
            # mcap None/0 means we can't normalize — skip rather than fall back
            # to a fake "1" denominator that turns every quote into a huge ratio.
            vol_ratio = (vol / mcap) if (mcap and mcap > 0) else 0
            if vol_ratio > 0.15:
                result["early_signals"].append("High volume/mcap ratio — unusual activity")
        else:
            result["name"] = symbol
            result["scores"]["momentum"] = 50
    except Exception:
        result["name"] = symbol
        result["scores"]["momentum"] = 50

    # ML forecast via yfinance (BTC-USD, ETH-USD, etc.)
    ml_score = 50
    try:
        import ml_forecast as mlf
        yf_sym = symbol + "-USD"
        ml_data = mlf.ml_forecast(yf_sym)
        if not ml_data.get("error"):
            rf = ml_data.get("rf_classifier") or {}
            trend = ml_data.get("trend_forecast") or {}
            regime_obj = ml_data.get("regime") or {}
            prob_up = rf.get("prob_up_20d", 0.5)
            ml_score = round(prob_up * 100)
            regime = regime_obj.get("current_regime", "UNKNOWN")
            result["details"]["ml_forecast"] = {
                "prob_up": round(prob_up, 3),
                "regime": regime,
                "trend": trend.get("trend_short", "NEUTRAL"),
                "forecast_pct": round(trend.get("forecast_pct", 0), 2),
            }
            if regime == "COMPRESSION":
                result["early_signals"].append("Crypto compression regime — breakout potential")
    except Exception:
        pass
    result["scores"]["ml_forecast"] = ml_score

    # Set zero for inapplicable dimensions
    result["scores"]["smart_money"] = 50  # neutral
    result["scores"]["fundamentals"] = 50
    result["scores"]["insider"] = 50
    result["scores"]["congress"] = 50
    result["scores"]["options_flow"] = 50
    result["scores"]["dividend"] = 0

    # Weighted composite
    composite = 0.0
    for dim, weight in weights.items():
        raw = result["scores"].get(dim, 50)
        composite += (raw / 100.0) * weight
    result["composite"] = round(composite, 1)

    bonus = len(result["early_signals"]) * 3
    result["early_bonus"] = min(bonus, 15)
    result["composite"] = min(100, round(result["composite"] + result["early_bonus"], 1))
    result["signal"] = _composite_to_signal(result["composite"])

    return result


def _composite_to_signal(score):
    # type: (float) -> str
    if score >= 75:
        return "STRONG BUY"
    if score >= 60:
        return "BUY"
    if score >= 45:
        return "HOLD"
    if score >= 30:
        return "CAUTION"
    return "AVOID"


def _parse_date_loose(date_str):
    # type: (str) -> datetime
    """Parse date strings in various formats."""
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y"):
        try:
            return datetime.strptime(date_str, fmt)
        except (ValueError, TypeError):
            continue
    return datetime.min


# ══════════════════════════════════════════════════════════════════════════════
# MAIN SCAN ORCHESTRATOR
# ══════════════════════════════════════════════════════════════════════════════

def scan_opportunities(profile=None, force_refresh=False):
    # type: (Optional[dict], bool) -> dict
    """
    Run a full opportunity scan. Returns:
    {
        "opportunities": [...],
        "scan_meta": { universe_size, scanned, scan_time_ms, cached, profile_summary }
    }
    """
    if profile is None:
        profile = get_scanner_profile()

    # Check cache
    phash = _profile_hash(profile)
    if not force_refresh and phash in _scan_cache:
        cached_at = _scan_cache_time.get(phash, 0)
        if (time.time() - cached_at) < _SCAN_CACHE_TTL:
            result = _scan_cache[phash]
            result["scan_meta"]["cached"] = True
            return result

    t0 = time.time()
    strategy = profile.get("strategy", "growth")
    weights = STRATEGY_WEIGHTS.get(strategy, STRATEGY_WEIGHTS["growth"])

    # Build universe
    universe = _build_universe(profile)
    universe_size = len(universe)

    # Split into equity and crypto
    equity_candidates = [c for c in universe if c["asset_class"] != "crypto"]
    crypto_candidates = [c for c in universe if c["asset_class"] == "crypto"]

    opportunities = []

    # Score equities in parallel
    def _score_eq(candidate):
        try:
            result = _score_equity(candidate["symbol"], profile, weights)
            result["asset_class"] = candidate["asset_class"]
            result["source"] = candidate["source"]
            return result
        except Exception as e:
            logger.warning("Scan error for %s: %s", candidate["symbol"], e)
            return None

    def _score_cr(candidate):
        try:
            result = _score_crypto(candidate["symbol"], profile, weights)
            result["source"] = candidate["source"]
            return result
        except Exception as e:
            logger.warning("Crypto scan error for %s: %s", candidate["symbol"], e)
            return None

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {}
        for c in equity_candidates:
            futures[pool.submit(_score_eq, c)] = c
        for c in crypto_candidates:
            futures[pool.submit(_score_cr, c)] = c

        for future in as_completed(futures):
            result = future.result()
            if result and result.get("composite", 0) > 0:
                opportunities.append(result)

    # Sort by composite score descending
    opportunities.sort(key=lambda x: x.get("composite", 0), reverse=True)

    # Tag top picks
    for i, opp in enumerate(opportunities[:3]):
        opp["badge"] = "TOP PICK"
    for opp in opportunities:
        if opp.get("early_signals") and not opp.get("badge"):
            opp["badge"] = "EARLY SIGNAL"

    elapsed_ms = round((time.time() - t0) * 1000)

    result = {
        "opportunities": opportunities[:30],  # top 30
        "scan_meta": {
            "universe_size": universe_size,
            "scanned": len(opportunities),
            "scan_time_ms": elapsed_ms,
            "cached": False,
            "strategy": strategy,
            "risk": profile.get("risk_tolerance", "moderate"),
            "horizon": profile.get("horizon", "medium"),
            "asset_types": profile.get("asset_types", []),
            "profile_summary": f"{strategy.title()} | {profile.get('risk_tolerance', 'moderate').title()} risk | {profile.get('horizon', 'medium').title()} term",
        },
    }

    # Cache
    _scan_cache[phash] = result
    _scan_cache_time[phash] = time.time()

    # Persist top results to scanner_history so we can chart scores over time.
    try:
        from datetime import timezone as _tz
        scanned_at = datetime.now(_tz.utc).strftime("%Y-%m-%d %H:%M:%S")
        db.save_scan_history(result["opportunities"], phash, strategy, scanned_at)
    except Exception as e:
        logger.warning("Failed to persist scan history: %s", e)

    return result


def get_cached_scan():
    # type: () -> Optional[dict]
    """Return most recent cached scan if still fresh, else None."""
    profile = get_scanner_profile()
    phash = _profile_hash(profile)
    if phash in _scan_cache:
        cached_at = _scan_cache_time.get(phash, 0)
        if (time.time() - cached_at) < _SCAN_CACHE_TTL:
            result = _scan_cache[phash]
            result["scan_meta"]["cached"] = True
            return result
    return None
