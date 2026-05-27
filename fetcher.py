"""
Data fetching layer — yfinance for equities/ETFs, CoinGecko for crypto.
All functions return plain dicts/lists for easy JSON serialisation.
"""

import yfinance as yf
import requests
import pandas as pd
import numpy as np
from datetime import datetime, timedelta, timezone
import time
import threading

try:
    from zoneinfo import ZoneInfo
    _NY_TZ = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover
    try:
        import pytz
        _NY_TZ = pytz.timezone("America/New_York")
    except Exception:
        _NY_TZ = None

COINGECKO_BASE = "https://api.coingecko.com/api/v3"
HEADERS = {"User-Agent": "WealthTracker/1.0 (personal)"}

# Yahoo Finance periodically drops or rate-limits crypto `<SYM>-USD` tickers.
# When a quote fetch fails (delisted / 401 Invalid Crumb / 429) for any
# ticker matching `<SYM>-USD` where SYM is in this dict, fall back to
# CoinGecko's quote endpoint by the mapped coin ID. Keep in sync with
# opportunity_scanner.UNIVERSE_CRYPTO_IDS.
CRYPTO_FALLBACK_IDS = {
    "BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana",
    "ADA": "cardano", "AVAX": "avalanche-2", "DOT": "polkadot",
    "MATIC": "polygon-ecosystem-token",  # ex-matic-network, rebranded to POL
    "LINK": "chainlink", "UNI": "uniswap",
    "AAVE": "aave", "XRP": "ripple", "BNB": "binancecoin",
    "NEAR": "near", "SUI": "sui", "APT": "aptos",
    "DOGE": "dogecoin", "SHIB": "shiba-inu", "ARB": "arbitrum",
    "OP": "optimism", "FIL": "filecoin",
}


def _coin_id_for_yf_symbol(symbol: str):
    """Return CoinGecko coin ID for a yfinance-style symbol like 'BTC-USD',
    or None if it isn't a known crypto ticker."""
    s = symbol.upper()
    if not s.endswith("-USD"):
        return None
    return CRYPTO_FALLBACK_IDS.get(s[:-4])


def _coingecko_simple_batch(coin_ids):
    """Fetch many coin quotes in a single HTTP call. Returns a dict keyed by
    CoinGecko coin ID. Uses the /simple/price endpoint — far cheaper than
    /coins/{id} (which gets rate-limited at the free tier after ~30/min)."""
    if not coin_ids:
        return {}
    try:
        return _cg_get("/simple/price", {
            "ids": ",".join(sorted(set(coin_ids))),
            "vs_currencies": "usd",
            "include_24hr_change": "true",
            "include_24hr_vol": "true",
            "include_market_cap": "true",
        })
    except Exception:
        return {}


def _coingecko_quote(coin_id: str, yf_symbol: str) -> dict:
    """Single-coin quote shaped to match yfinance's output. Hits the cheap
    /simple/price endpoint to stay under CoinGecko's free-tier rate limit."""
    batch = _coingecko_simple_batch([coin_id])
    d = batch.get(coin_id)
    if not d or "usd" not in d:
        return {"symbol": yf_symbol.upper(), "error": "coingecko returned no data"}
    price = d.get("usd")
    change_pct = d.get("usd_24h_change")
    change = (price * change_pct / 100) if (price and change_pct is not None) else None
    prev = (price - change) if (price and change is not None) else None
    return {
        "symbol": yf_symbol.upper(),
        "price": price,
        "prev_close": round(prev, 6) if prev is not None else None,
        "change": round(change, 6) if change is not None else None,
        "change_pct": round(change_pct, 4) if change_pct is not None else None,
        "volume": d.get("usd_24h_vol"),
        "market_cap": d.get("usd_market_cap"),
        "currency": "USD",
        "exchange": "coingecko",
        "source": "coingecko",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def _is_failed_crypto_quote(q: dict) -> bool:
    """True if a quote dict looks like a yfinance failure we can recover
    from via CoinGecko (no price, error string, or rate-limited)."""
    if not isinstance(q, dict):
        return True
    if "error" in q:
        return True
    return q.get("price") is None


# ─── TTL Cache ───────────────────────────────────────────────────────────────
# Delegates to cache_store, which adds SQLite write-through (so the cache
# survives app restarts and we don't cold-start hammer Yahoo on every launch)
# and request coalescing (so simultaneous requests for the same key don't
# fire duplicate upstream calls). The thin _cached / _set_cache shims below
# keep the rest of fetcher.py unchanged.

import cache_store

def _cached(key, ttl=60):
    return cache_store.cache_get(key, ttl)

def _set_cache(key, value, ttl=60):
    cache_store.cache_set(key, value, ttl)

# ─── Helpers ──────────────────────────────────────────────────────────────────

def _safe(val):
    """Convert numpy / NaN values to Python scalars."""
    if val is None:
        return None
    try:
        if pd.isna(val):
            return None
    except Exception:
        pass
    if isinstance(val, (np.integer,)):
        return int(val)
    if isinstance(val, (np.floating,)):
        return float(val)
    return val


# ─── Yahoo direct-API fallback ────────────────────────────────────────────────
# yfinance 1.2.0 depends on fc.yahoo.com to mint a "crumb" cookie. That host
# started returning 404 in 2026 — yfinance interprets the failure as
# YFRateLimitError and returns "Too Many Requests" for every call, even
# though Yahoo's underlying chart endpoint still serves data fine without
# any auth. We bypass the broken crumb flow by hitting v8/finance/chart
# directly. Result is shaped to match get_quote()'s output.
# Bumping yfinance to 1.2.1+ would fix this but requires Python 3.10
# (curl_cffi>=0.15 dependency); we're pinned to 3.9 for Xcode CLT compat.

_YAHOO_CHART_BASE = "https://query1.finance.yahoo.com/v8/finance/chart"
_YAHOO_DIRECT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json",
}


def _yahoo_chart_direct(symbol: str) -> dict:
    """Fetch a quote via Yahoo's chart endpoint, bypassing yfinance. Returns
    a dict in get_quote()'s shape, or {'error': ...} on failure."""
    sym = symbol.upper()
    try:
        r = requests.get(
            f"{_YAHOO_CHART_BASE}/{sym}",
            params={"interval": "1d", "range": "5d"},
            headers=_YAHOO_DIRECT_HEADERS,
            timeout=8,
        )
        r.raise_for_status()
        payload = r.json()
    except Exception as e:
        return {"symbol": sym, "error": f"yahoo-direct: {e}"}

    chart = (payload or {}).get("chart") or {}
    if chart.get("error"):
        return {"symbol": sym, "error": str(chart["error"])}
    results = chart.get("result") or []
    if not results:
        return {"symbol": sym, "error": "yahoo-direct: empty result"}
    meta = results[0].get("meta") or {}

    price = meta.get("regularMarketPrice")
    prev = meta.get("chartPreviousClose") or meta.get("previousClose")
    chg = round(price - prev, 4) if (price is not None and prev) else None
    chg_pct = round((chg / prev) * 100, 4) if (chg is not None and prev) else None

    return {
        "symbol": sym,
        "price": price,
        "prev_close": prev,
        "change": chg,
        "change_pct": chg_pct,
        "volume": meta.get("regularMarketVolume"),
        "market_cap": None,  # not exposed by chart endpoint
        "currency": meta.get("currency", "USD"),
        "exchange": meta.get("exchangeName", ""),
        "day_high": meta.get("regularMarketDayHigh"),
        "day_low": meta.get("regularMarketDayLow"),
        "fifty_two_week_high": meta.get("fiftyTwoWeekHigh"),
        "fifty_two_week_low": meta.get("fiftyTwoWeekLow"),
        "source": "yahoo-direct",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# When Yahoo's API is down, indices/futures map to liquid ETF proxies that
# Finviz tracks. The proxy's price isn't identical (small tracking error /
# leverage / scale) but the change% is close, which is what the UI relies on.
_INDEX_TO_FINVIZ_PROXY = {
    "^GSPC": "SPY",       # S&P 500 → SPY
    "^NDX": "QQQ",        # NASDAQ 100 → QQQ
    "^DJI": "DIA",        # DOW → DIA
    "^RUT": "IWM",        # Russell 2000 → IWM
    "^VIX": "VIXY",       # VIX → VIXY (rough; volatility ETN)
    "^TNX": "IEF",        # 10Y yield → IEF (7-10Y treasury ETF)
    "GC=F":  "GLD",       # Gold futures → GLD
    "CL=F":  "USO",       # WTI futures → USO
    "DX-Y.NYB": "UUP",    # DXY → UUP
    "^FTSE": "EWU",       # FTSE 100 → EWU (UK ETF)
    "^N225": "EWJ",       # Nikkei → EWJ (Japan ETF)
}


def _finviz_quote_fallback(symbol: str) -> dict:
    """Per-ticker quote via finvizfinance — last resort when Yahoo is hard
    rate-limiting us. Slower (HTML scrape) but unrelated to Yahoo's quota.
    For indices/futures, we substitute a liquid ETF proxy and preserve the
    original symbol in the response."""
    sym = symbol.upper()
    finviz_sym = sym
    proxy_used = None
    if sym in _INDEX_TO_FINVIZ_PROXY:
        finviz_sym = _INDEX_TO_FINVIZ_PROXY[sym]
        proxy_used = finviz_sym
    elif sym.startswith("^") or "=" in sym or "." in sym.replace("-", ""):
        return {"symbol": sym, "error": "finviz: unsupported ticker shape"}
    try:
        from finvizfinance.quote import finvizfinance as _Q
        fund = _Q(finviz_sym).ticker_fundament() or {}
    except Exception as e:
        return {"symbol": sym, "error": f"finviz: {e}"}

    def _num(v):
        if v is None: return None
        s = str(v).strip().rstrip("%").replace(",", "")
        if s in ("", "-", "—"): return None
        # Finviz uses suffixes K/M/B/T for compact numbers
        mult = 1
        if s and s[-1] in "KMBT":
            mult = {"K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}[s[-1]]
            s = s[:-1]
        try:
            return float(s) * mult
        except ValueError:
            return None

    price = _num(fund.get("Price"))
    if price is None:
        return {"symbol": sym, "error": "finviz: no Price field"}
    prev = _num(fund.get("Prev Close"))
    chg_pct = _num(fund.get("Change"))
    chg = round(price - prev, 4) if (price is not None and prev) else None
    return {
        "symbol": sym,
        "price": price,
        "prev_close": prev,
        "change": chg,
        "change_pct": chg_pct,
        "volume": _num(fund.get("Volume")),
        "market_cap": _num(fund.get("Market Cap")),
        "currency": "USD",
        "exchange": fund.get("Exchange", ""),
        "day_high": None,
        "day_low": None,
        "fifty_two_week_high": _num((fund.get("52W High") or "").split()[0] if fund.get("52W High") else None),
        "fifty_two_week_low":  _num((fund.get("52W Low")  or "").split()[0] if fund.get("52W Low")  else None),
        "source": f"finviz-via-{proxy_used}" if proxy_used else "finviz",
        "proxy_symbol": proxy_used,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def _yahoo_chart_history(symbol: str, period: str = "6mo", interval: str = "1d") -> list:
    """OHLCV list for charting via Yahoo's v8/finance/chart endpoint. Bypasses
    yfinance entirely. Used as a fallback for get_chart_data and _get_returns_df
    when yfinance trips its broken crumb auth and returns rate-limit errors."""
    sym = symbol.upper()
    # Map period+interval to Yahoo's range/interval params. We support the
    # combinations app.py actually uses; others fall through to defaults.
    range_map = {"1d":"1d","5d":"5d","1mo":"1mo","3mo":"3mo","6mo":"6mo",
                 "1y":"1y","2y":"2y","5y":"5y","10y":"10y","ytd":"ytd","max":"max"}
    yh_range = range_map.get(period, "6mo")
    try:
        r = requests.get(
            f"{_YAHOO_CHART_BASE}/{sym}",
            params={"interval": interval, "range": yh_range,
                    "includePrePost": "false", "events": "div,split"},
            headers=_YAHOO_DIRECT_HEADERS,
            timeout=10,
        )
        r.raise_for_status()
        payload = r.json()
    except Exception:
        return []
    chart = (payload or {}).get("chart") or {}
    if chart.get("error"):
        return []
    results = chart.get("result") or []
    if not results:
        return []
    res0 = results[0]
    timestamps = res0.get("timestamp") or []
    ind = (res0.get("indicators") or {}).get("quote") or [{}]
    q = ind[0] if ind else {}
    opens  = q.get("open") or []
    highs  = q.get("high") or []
    lows   = q.get("low") or []
    closes = q.get("close") or []
    vols   = q.get("volume") or []
    out = []
    for i, ts in enumerate(timestamps):
        c = closes[i] if i < len(closes) else None
        if c is None:
            continue  # Yahoo includes null bars for non-trading windows
        out.append({
            "time": int(ts),
            "open":  round(float(opens[i]),  4) if i < len(opens)  and opens[i]  is not None else c,
            "high":  round(float(highs[i]),  4) if i < len(highs)  and highs[i]  is not None else c,
            "low":   round(float(lows[i]),   4) if i < len(lows)   and lows[i]   is not None else c,
            "close": round(float(c), 4),
            "volume": int(vols[i]) if i < len(vols) and vols[i] is not None else 0,
        })
    return out


def _finviz_fundamentals_fallback(symbol: str) -> dict:
    """Map finvizfinance.ticker_fundament() into get_fundamentals()'s shape.
    Finviz has nearly every metric we report (PE, PEG, margins, sector,
    industry, employees, beta, 52w, target price, dividend) — coverage gaps
    are filled with None rather than crashing."""
    sym = symbol.upper()
    try:
        from finvizfinance.quote import finvizfinance as _Q
        fund = _Q(sym).ticker_fundament() or {}
    except Exception as e:
        return {"symbol": sym, "error": f"finviz fundamentals: {e}"}

    def _num(v):
        if v is None: return None
        s = str(v).strip().replace(",", "")
        # strip trailing % so percentage fields become plain floats
        had_pct = s.endswith("%")
        if had_pct: s = s[:-1]
        if s in ("", "-", "—"): return None
        mult = 1
        if s and s[-1] in "KMBT":
            mult = {"K":1e3,"M":1e6,"B":1e9,"T":1e12}[s[-1]]
            s = s[:-1]
        try:
            n = float(s) * mult
            return n / 100 if had_pct else n
        except ValueError:
            return None

    # Some fields combine value + percent in a single string (e.g. "1.05 (0.35%)")
    def _first_num(v):
        if v is None: return None
        head = str(v).split()[0] if str(v).strip() else ""
        return _num(head)

    target = _num(fund.get("Target Price"))
    result = {
        "symbol": sym,
        "name": fund.get("Company", ""),
        "sector": fund.get("Sector", ""),
        "industry": fund.get("Industry", ""),
        "description": "",
        "website": "",
        "country": fund.get("Country", ""),
        "employees": int(_num(fund.get("Employees"))) if _num(fund.get("Employees")) else None,
        "market_cap": _num(fund.get("Market Cap")),
        "enterprise_value": _num(fund.get("Enterprise Value")),
        "pe_ratio":   _num(fund.get("P/E")),
        "forward_pe": _num(fund.get("Forward P/E")),
        "peg_ratio":  _num(fund.get("PEG")),
        "ps_ratio":   _num(fund.get("P/S")),
        "pb_ratio":   _num(fund.get("P/B")),
        "ev_ebitda":  _num(fund.get("EV/EBITDA")),
        "debt_equity":   _num(fund.get("Debt/Eq")),
        "current_ratio": _num(fund.get("Current Ratio")),
        "return_on_equity": _num(fund.get("ROE")),
        "return_on_assets": _num(fund.get("ROA")),
        "profit_margin":   _num(fund.get("Profit Margin")),
        "gross_margin":    _num(fund.get("Gross Margin")),
        "operating_margin":_num(fund.get("Oper. Margin")),
        "revenue":         _num(fund.get("Sales")),
        "revenue_growth":  _num(fund.get("Sales Q/Q")),
        "earnings_growth": _num(fund.get("EPS Q/Q")),
        "free_cashflow":   None,
        "dividend_yield":  _num(fund.get("Dividend %")) or _first_num(fund.get("Dividend TTM")),
        "dividend_rate":   _first_num(fund.get("Dividend Est.")),
        "payout_ratio":    _num(fund.get("Payout")),
        "beta":            _num(fund.get("Beta")),
        "shares_outstanding": _num(fund.get("Shs Outstand")),
        "float_shares":       _num(fund.get("Shs Float")),
        "short_ratio":        _num(fund.get("Short Ratio")),
        "short_percent_float":_num(fund.get("Short Float")),
        "52w_high":   _first_num(fund.get("52W High")),
        "52w_low":    _first_num(fund.get("52W Low")),
        "50d_avg":    None,
        "200d_avg":   None,
        "analyst_target": target,
        "analyst_low":    None,
        "analyst_high":   None,
        "recommendation": fund.get("Recom", ""),
        "num_analysts":   None,
        "source": "finviz",
    }
    return result


def _is_rate_limit_error(exc) -> bool:
    """yfinance 1.2.0 raises YFRateLimitError as the user-facing error when
    its crumb flow fails — match on the class name (the type is module-private)
    plus the string for older paths."""
    if exc is None:
        return False
    name = type(exc).__name__
    return name == "YFRateLimitError" or "rate limit" in str(exc).lower() or "too many requests" in str(exc).lower()


# ─── Equity / ETF ─────────────────────────────────────────────────────────────

def _try_crypto_fallback(symbol, ttl=30):
    """If symbol maps to a known coin, fetch its CoinGecko quote and cache.
    Returns a quote dict, or None if the symbol isn't a known crypto."""
    coin_id = _coin_id_for_yf_symbol(symbol)
    if not coin_id:
        return None
    cg_quote = _coingecko_quote(coin_id, symbol)
    if "error" not in cg_quote:
        _set_cache(("quote", symbol.upper()), cg_quote, ttl=ttl)
    return cg_quote


def get_quote(symbol: str) -> dict:
    ck = ("quote", symbol.upper())
    hit = _cached(ck, ttl=30)
    if hit is not None:
        return hit

    # For known crypto symbols, prefer CoinGecko over yfinance. Yahoo's
    # crypto data has been silently shipping wildly-wrong prices for some
    # tickers (UNI returned $0.0002 instead of $3.74, SUI returned $0.0003
    # instead of $1.19). CoinGecko is the canonical source for these.
    # 120s TTL keeps API pressure low — CoinGecko's free tier is ~30/min.
    coin_id = _coin_id_for_yf_symbol(symbol)
    if coin_id:
        cg_quote = _coingecko_quote(coin_id, symbol)
        if "error" not in cg_quote and cg_quote.get("price"):
            _set_cache(ck, cg_quote, ttl=120)
            return cg_quote
        # Fall through to yfinance if CoinGecko hiccups (rate-limit, etc.)

    try:
        t = yf.Ticker(symbol)
        info = t.fast_info
        hist = t.history(period="2d", interval="1d")

        price = _safe(info.last_price) or _safe(info.regular_market_price)
        prev_close = _safe(info.previous_close)

        change = None
        change_pct = None
        if price and prev_close:
            change = round(price - prev_close, 4)
            change_pct = round((change / prev_close) * 100, 4)

        result = {
            "symbol": symbol.upper(),
            "price": price,
            "prev_close": prev_close,
            "change": change,
            "change_pct": change_pct,
            "volume": _safe(getattr(info, "three_month_average_volume", None)),
            "market_cap": _safe(getattr(info, "market_cap", None)),
            "currency": getattr(info, "currency", "USD"),
            "exchange": getattr(info, "exchange", ""),
            "day_high": _safe(getattr(info, "day_high", None)),
            "day_low": _safe(getattr(info, "day_low", None)),
            "fifty_two_week_high": _safe(getattr(info, "year_high", None)),
            "fifty_two_week_low": _safe(getattr(info, "year_low", None)),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        # yfinance returned a quote but no price → fallback chain.
        if result.get("price") is None:
            for fb in (_yahoo_chart_direct, _try_crypto_fallback, _finviz_quote_fallback):
                fbq = fb(symbol) if fb is not _try_crypto_fallback else fb(symbol)
                if fbq and "error" not in fbq and fbq.get("price") is not None:
                    _set_cache(ck, fbq, ttl=30)
                    return fbq
        _set_cache(ck, result, ttl=30)
        return result
    except Exception as e:
        # yfinance 1.2.0 trips YFRateLimitError when fc.yahoo.com (crumb host)
        # 404s. Fallback chain: Yahoo direct → crypto-via-CoinGecko → Finviz.
        for fb in (_yahoo_chart_direct, _try_crypto_fallback, _finviz_quote_fallback):
            fbq = fb(symbol)
            if fbq and "error" not in fbq and fbq.get("price") is not None:
                _set_cache(ck, fbq, ttl=30)
                return fbq
        return {"symbol": symbol.upper(), "error": str(e)}


def get_quotes_batch(symbols: list) -> dict:
    results = {}
    # yfinance batch download for speed
    try:
        tickers = yf.Tickers(" ".join(symbols))
        for sym in symbols:
            try:
                t = tickers.tickers.get(sym.upper()) or yf.Ticker(sym)
                info = t.fast_info
                price = _safe(info.last_price) or _safe(info.regular_market_price)
                prev = _safe(info.previous_close)
                if price is None:
                    raise RuntimeError("no price from yfinance")
                chg = round(price - prev, 4) if price and prev else None
                chg_pct = round((chg / prev) * 100, 4) if chg and prev else None
                results[sym.upper()] = {
                    "symbol": sym.upper(),
                    "price": price,
                    "prev_close": prev,
                    "change": chg,
                    "change_pct": chg_pct,
                    "day_high": _safe(getattr(info, "day_high", None)),
                    "day_low": _safe(getattr(info, "day_low", None)),
                    "market_cap": _safe(getattr(info, "market_cap", None)),
                }
            except Exception as e:
                # Mirror get_quote's fallback chain so batch users aren't
                # left with errors when only yfinance is broken.
                got = None
                for fb in (_yahoo_chart_direct, _finviz_quote_fallback):
                    fbq = fb(sym)
                    if fbq and "error" not in fbq and fbq.get("price") is not None:
                        got = fbq
                        break
                results[sym.upper()] = got or {"symbol": sym.upper(), "error": str(e)}
    except Exception as e:
        # Fallback: individual fetches
        for sym in symbols:
            results[sym.upper()] = get_quote(sym)

    # CoinGecko sweep — collect every crypto in the batch (failed OR
    # known-untrustworthy on Yahoo) and batch-fetch them in one /simple/price
    # call. This is much faster than per-coin requests and avoids hitting
    # CoinGecko's free-tier rate limit when scanning the full crypto universe.
    crypto_to_resolve = {}  # cg_id -> yf_symbol
    for sym in symbols:
        cg_id = _coin_id_for_yf_symbol(sym)
        if not cg_id:
            continue
        q = results.get(sym.upper())
        # Prefer CoinGecko for every known crypto — Yahoo has been silently
        # serving wrong prices for some (see _coingecko_quote for context).
        crypto_to_resolve[cg_id] = sym.upper()

    if crypto_to_resolve:
        # Check cache first — if we have a recent CoinGecko quote for this
        # ticker, use it directly without hitting the API. Keeps us well
        # under the free-tier rate limit when the UI polls every few seconds.
        still_need = {}
        for cg_id, yf_sym in crypto_to_resolve.items():
            cached = _cached(("quote", yf_sym), ttl=120)  # 2-min crypto cache
            if cached and cached.get("source") == "coingecko":
                results[yf_sym] = cached
            else:
                still_need[cg_id] = yf_sym
        if still_need:
            batch = _coingecko_simple_batch(list(still_need.keys()))
            for cg_id, yf_sym in still_need.items():
                d = batch.get(cg_id)
                if not d or "usd" not in d:
                    continue
                price = d["usd"]
                change_pct = d.get("usd_24h_change")
                change = (price * change_pct / 100) if change_pct is not None else None
                prev = (price - change) if change is not None else None
                quote = {
                    "symbol": yf_sym,
                    "price": price,
                    "prev_close": round(prev, 6) if prev is not None else None,
                    "change": round(change, 6) if change is not None else None,
                    "change_pct": round(change_pct, 4) if change_pct is not None else None,
                    "day_high": None,
                    "day_low": None,
                    "market_cap": d.get("usd_market_cap"),
                    "source": "coingecko",
                }
                results[yf_sym] = quote
                _set_cache(("quote", yf_sym), quote, ttl=120)
    return results


def get_fundamentals(symbol: str) -> dict:
    ck = ("fundamentals", symbol.upper())
    # Fundamentals (PE / sector / margins / employees / etc.) move slowly —
    # caching for 24h means we hit Yahoo/Finviz at most once per ticker per
    # day, which keeps us well under their rate limits.
    hit = _cached(ck, ttl=86400)
    if hit is not None:
        return hit
    try:
        t = yf.Ticker(symbol)
        info = t.info
        result = {
            "symbol": symbol.upper(),
            "name": info.get("longName") or info.get("shortName", ""),
            "sector": info.get("sector", ""),
            "industry": info.get("industry", ""),
            "description": info.get("longBusinessSummary", ""),
            "website": info.get("website", ""),
            "country": info.get("country", ""),
            "employees": _safe(info.get("fullTimeEmployees")),
            "market_cap": _safe(info.get("marketCap")),
            "enterprise_value": _safe(info.get("enterpriseValue")),
            "pe_ratio": _safe(info.get("trailingPE")),
            "forward_pe": _safe(info.get("forwardPE")),
            "peg_ratio": _safe(info.get("pegRatio")),
            "ps_ratio": _safe(info.get("priceToSalesTrailing12Months")),
            "pb_ratio": _safe(info.get("priceToBook")),
            "ev_ebitda": _safe(info.get("enterpriseToEbitda")),
            "debt_equity": _safe(info.get("debtToEquity")),
            "current_ratio": _safe(info.get("currentRatio")),
            "return_on_equity": _safe(info.get("returnOnEquity")),
            "return_on_assets": _safe(info.get("returnOnAssets")),
            "profit_margin": _safe(info.get("profitMargins")),
            "gross_margin": _safe(info.get("grossMargins")),
            "operating_margin": _safe(info.get("operatingMargins")),
            "revenue": _safe(info.get("totalRevenue")),
            "revenue_growth": _safe(info.get("revenueGrowth")),
            "earnings_growth": _safe(info.get("earningsGrowth")),
            "free_cashflow": _safe(info.get("freeCashflow")),
            "dividend_yield": _safe(info.get("dividendYield")),
            "dividend_rate": _safe(info.get("dividendRate")),
            "payout_ratio": _safe(info.get("payoutRatio")),
            "beta": _safe(info.get("beta")),
            "shares_outstanding": _safe(info.get("sharesOutstanding")),
            "float_shares": _safe(info.get("floatShares")),
            "short_ratio": _safe(info.get("shortRatio")),
            "short_percent_float": _safe(info.get("shortPercentOfFloat")),
            "52w_high": _safe(info.get("fiftyTwoWeekHigh")),
            "52w_low": _safe(info.get("fiftyTwoWeekLow")),
            "50d_avg": _safe(info.get("fiftyDayAverage")),
            "200d_avg": _safe(info.get("twoHundredDayAverage")),
            "analyst_target": _safe(info.get("targetMeanPrice")),
            "analyst_low": _safe(info.get("targetLowPrice")),
            "analyst_high": _safe(info.get("targetHighPrice")),
            "recommendation": info.get("recommendationKey", ""),
            "num_analysts": _safe(info.get("numberOfAnalystOpinions")),
        }
        # When yfinance's crumb auth is broken, `t.info` silently falls back
        # to a thin payload — we still get price-derived fields (50d_avg,
        # market_cap, PE from fast_info) but the .info-only modules
        # (description, sector, industry, employees, beta, country, margins,
        # analyst targets) all come back empty. Detect that signature by
        # counting how many .info-only slots are present, and if they're
        # mostly missing, fill the gaps from Finviz instead of caching a
        # half-empty result for 24 hours.
        _info_only = (
            "sector", "industry", "country", "description", "employees",
            "beta", "profit_margin", "operating_margin", "return_on_equity",
        )
        present = sum(1 for k in _info_only if result.get(k) not in (None, ""))
        if present <= 2:
            fb = _finviz_fundamentals_fallback(symbol)
            if "error" not in fb:
                # Prefer yfinance values where it has them (the price-derived
                # fields are more accurate); fill remaining gaps from Finviz.
                for k, v in fb.items():
                    if result.get(k) in (None, "") and v not in (None, ""):
                        result[k] = v
                result["source"] = "yfinance+finviz"
        _set_cache(ck, result, ttl=86400)
        return result
    except Exception as e:
        # Yahoo blew up — try Finviz before giving up.
        fb = _finviz_fundamentals_fallback(symbol)
        if "error" not in fb:
            _set_cache(ck, fb, ttl=86400)
            return fb
        return {"symbol": symbol.upper(), "error": str(e)}


_TICKER_RE = __import__("re").compile(r"^[A-Z0-9][A-Z0-9.\-]{0,9}$")

def is_valid_ticker(symbol) -> bool:
    """Reject symbols that aren't plain ticker shapes (prevents SQL/yfinance abuse)."""
    if not isinstance(symbol, str):
        return False
    return bool(_TICKER_RE.match(symbol.strip().upper()))


def get_chart_data(symbol: str, period: str = "6mo", interval: str = "1d") -> list:
    """Returns OHLCV list suitable for Lightweight Charts."""
    ck = ("chart", symbol.upper(), period, interval)
    # Intraday intervals refresh fast; daily and above are cached aggressively
    # since they only get one new bar at most per day.
    ttl = 600 if interval in ("1m","5m","15m","30m","1h","60m","90m") else 12 * 3600
    hit = _cached(ck, ttl=ttl)
    if hit is not None:
        return hit

    def _fetch():
        try:
            t = yf.Ticker(symbol)
            hist = t.history(period=period, interval=interval, auto_adjust=True)
            if hist.empty:
                return _yahoo_chart_history(symbol, period, interval)
            result = []
            for ts, row in hist.iterrows():
                ts_unix = int(ts.timestamp())
                result.append({
                    "time": ts_unix,
                    "open": round(float(row["Open"]), 4),
                    "high": round(float(row["High"]), 4),
                    "low": round(float(row["Low"]), 4),
                    "close": round(float(row["Close"]), 4),
                    "volume": int(row["Volume"]) if not pd.isna(row["Volume"]) else 0,
                })
            return result
        except Exception:
            return _yahoo_chart_history(symbol, period, interval)

    result = cache_store.coalesce(ck, ttl, _fetch)
    return result or []


def _finviz_news_fallback(symbol: str, limit: int = 15) -> list:
    """Per-ticker news via Finviz HTML scrape — works when Yahoo's news
    endpoint is rate-limited."""
    try:
        from finvizfinance.quote import finvizfinance as _Q
        df = _Q(symbol).ticker_news()
    except Exception:
        return []
    if df is None or df.empty:
        return []
    out = []
    for _, r in df.head(limit).iterrows():
        out.append({
            "title":    r.get("Title") or "",
            "summary":  "",
            "source":   r.get("Source") or "",
            "url":      r.get("Link") or "",
            "published": str(r.get("Date", "")),
        })
    return out


def get_news(symbol: str, limit: int = 15) -> list:
    ck = ("news", symbol.upper(), limit)
    hit = _cached(ck, ttl=900)  # 15 min — news doesn't change minute-to-minute
    if hit is not None:
        return hit
    try:
        t = yf.Ticker(symbol)
        news = t.news or []
        result = []
        for item in news[:limit]:
            content = item.get("content", {})
            title = content.get("title", item.get("title", ""))
            summary = content.get("summary", "")
            provider = content.get("provider", {})
            pub_date = content.get("pubDate", "")
            url = content.get("canonicalUrl", {}).get("url", "")
            if not url:
                url = item.get("link", "")
            result.append({
                "title": title,
                "summary": summary,
                "source": provider.get("displayName", "") if isinstance(provider, dict) else "",
                "url": url,
                "published": pub_date,
            })
        if not result:
            result = _finviz_news_fallback(symbol, limit)
        if result:
            _set_cache(ck, result, ttl=900)
        return result
    except Exception:
        fb = _finviz_news_fallback(symbol, limit)
        if fb:
            _set_cache(ck, fb, ttl=900)
        return fb


def _yahoo_search_direct(query: str, limit: int = 12) -> list:
    """Yahoo's /v1/finance/search endpoint is keyless and doesn't go through
    the broken crumb flow that breaks yf.Search. Public, no auth."""
    try:
        r = requests.get(
            "https://query1.finance.yahoo.com/v1/finance/search",
            params={"q": query, "quotesCount": limit, "newsCount": 0},
            headers=_YAHOO_DIRECT_HEADERS,
            timeout=8,
        )
        r.raise_for_status()
        data = r.json() or {}
    except Exception:
        return []
    out = []
    for q in (data.get("quotes") or [])[:limit]:
        sym = q.get("symbol", "")
        if not sym:
            continue
        out.append({
            "symbol": sym,
            "name": q.get("longname") or q.get("shortname") or "",
            "exchange": q.get("exchDisp") or q.get("exchange") or "",
            "type": q.get("quoteType") or "",
            "sector": q.get("sector") or "",
        })
    return out


def search_symbol(query: str) -> list:
    """Use yfinance search with Yahoo's direct /finance/search as a fallback."""
    ck = ("search", query.lower())
    hit = _cached(ck, ttl=86400)  # search results barely change day-to-day
    if hit is not None:
        return hit
    try:
        results = yf.Search(query, max_results=12)
        quotes = results.quotes or []
        out = []
        for q in quotes:
            out.append({
                "symbol": q.get("symbol", ""),
                "name": q.get("longname") or q.get("shortname", ""),
                "exchange": q.get("exchDisp", ""),
                "type": q.get("quoteType", ""),
                "sector": q.get("sector", ""),
            })
        if not out:
            out = _yahoo_search_direct(query)
        if out:
            _set_cache(ck, out, ttl=86400)
        return out
    except Exception:
        out = _yahoo_search_direct(query)
        if out:
            _set_cache(ck, out, ttl=86400)
        return out


def get_market_indices() -> list:
    ck = ("market_indices",)
    hit = _cached(ck, ttl=60)
    if hit is not None:
        return hit
    symbols = {
        "^GSPC": "S&P 500",
        "^NDX": "NASDAQ 100",
        "^DJI": "DOW JONES",
        "^RUT": "RUSSELL 2000",
        "^VIX": "VIX",
        "^TNX": "10Y YIELD",
        "GC=F": "GOLD",
        "CL=F": "OIL (WTI)",
        "DX-Y.NYB": "USD INDEX",
        "^FTSE": "FTSE 100",
        "^N225": "NIKKEI 225",
    }
    results = []
    try:
        batch = get_quotes_batch(list(symbols.keys()))
        for sym, label in symbols.items():
            q = batch.get(sym.upper(), {})
            q["label"] = label
            q["symbol"] = sym
            results.append(q)
    except Exception:
        pass
    _set_cache(ck, results, ttl=60)
    return results


def get_sector_performance() -> list:
    ck = ("sector_perf",)
    hit = _cached(ck, ttl=60)
    if hit is not None:
        return hit
    sector_etfs = {
        "XLK": "Technology",
        "XLF": "Financials",
        "XLV": "Healthcare",
        "XLE": "Energy",
        "XLI": "Industrials",
        "XLC": "Comm Services",
        "XLY": "Consumer Disc",
        "XLP": "Consumer Staples",
        "XLRE": "Real Estate",
        "XLB": "Materials",
        "XLU": "Utilities",
    }
    results = []
    try:
        batch = get_quotes_batch(list(sector_etfs.keys()))
        for sym, sector in sector_etfs.items():
            q = batch.get(sym, {})
            results.append({
                "symbol": sym,
                "sector": sector,
                "change_pct": q.get("change_pct"),
                "price": q.get("price"),
            })
        results.sort(key=lambda x: (x["change_pct"] or -999), reverse=True)
    except Exception:
        pass
    _set_cache(ck, results, ttl=60)
    return results


def get_top_movers() -> dict:
    """Return movers from a curated universe of ~36 large-caps.

    NOTE: This is NOT a market-wide screen. We fetch quotes for a fixed list
    of liquid large-cap names and re-sort by intraday change_pct within that
    set. Anything outside the curated universe will never appear here.
    """
    large_caps = [
        "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "BRK-B",
        "JPM", "V", "MA", "UNH", "XOM", "JNJ", "PG", "HD", "LLY", "AVGO",
        "MRK", "ABBV", "KO", "PEP", "COST", "ADBE", "CRM", "AMD", "NFLX",
        "INTC", "CSCO", "MCD", "WMT", "DIS", "BA", "GS", "MS", "GE", "CAT",
    ]
    try:
        batch = get_quotes_batch(large_caps)
        ranked = []
        for sym, q in batch.items():
            if q.get("change_pct") is not None:
                ranked.append(q)
        ranked.sort(key=lambda x: x.get("change_pct", 0), reverse=True)
        return {
            "gainers": ranked[:10],
            "losers": ranked[-10:][::-1],
        }
    except Exception:
        return {"gainers": [], "losers": []}


# ─── Crypto ───────────────────────────────────────────────────────────────────

# CoinGecko's free tier is ~30 requests/minute. Without caching every UI poll
# (crypto market, global, charts, per-coin extras) hits the upstream fresh,
# easily blowing the budget and triggering 429s that cascade into wrong-price
# fallbacks via yfinance. _cg_get adds a thin in-process TTL cache keyed on
# the full URL + sorted params, plus a global cool-down when CoinGecko 429s
# us so concurrent callers don't pile on while we're already throttled.

_CG_CACHE = {}                  # (path, frozen_params) -> (expiry_ts, value)
_CG_CACHE_LOCK = threading.Lock()
_CG_RATE_LIMIT_UNTIL = [0.0]    # mutable epoch — when we may resume calling

# Per-path TTLs (seconds). Crypto trades 24/7 but the free-tier budget is
# the binding constraint here, not freshness. /simple/price is what the
# quote sweep uses, so keep it short.
_CG_TTL_DEFAULTS = {
    "/simple/price": 60,
    "/coins/markets": 60,
    "/global": 300,
}


def _cg_default_ttl(path: str) -> int:
    for prefix, ttl in _CG_TTL_DEFAULTS.items():
        if path.startswith(prefix):
            return ttl
    # /coins/{id}, /coins/{id}/market_chart, etc. — coin-level data is fine
    # to cache a couple of minutes.
    return 120


def _cg_get(path: str, params: dict = None, ttl: int = None):
    """GET against CoinGecko with TTL caching + global 429 cool-down.

    ttl=None picks a per-path default. ttl=0 disables caching."""
    key = (path, tuple(sorted((params or {}).items())))
    now = time.time()
    cache_ttl = _cg_default_ttl(path) if ttl is None else ttl
    if cache_ttl > 0:
        with _CG_CACHE_LOCK:
            hit = _CG_CACHE.get(key)
            if hit and hit[0] > now:
                return hit[1]
    # If we're inside a 429 cool-down, fail fast — burning more requests
    # only deepens the throttle. Callers all wrap _cg_get in try/except and
    # gracefully return [] / {}, so raising here is safe.
    if _CG_RATE_LIMIT_UNTIL[0] > now:
        raise RuntimeError("coingecko rate-limited; cool-down active")
    url = f"{COINGECKO_BASE}{path}"
    resp = requests.get(url, params=params, headers=HEADERS, timeout=15)
    if resp.status_code == 429:
        # Respect Retry-After if present, otherwise back off 30s (CoinGecko's
        # free-tier window is a rolling minute).
        ra = resp.headers.get("Retry-After")
        try:
            backoff = float(ra) if ra else 30.0
        except (TypeError, ValueError):
            backoff = 30.0
        _CG_RATE_LIMIT_UNTIL[0] = time.time() + min(max(backoff, 5.0), 120.0)
        resp.raise_for_status()
    resp.raise_for_status()
    data = resp.json()
    if cache_ttl > 0:
        with _CG_CACHE_LOCK:
            _CG_CACHE[key] = (now + cache_ttl, data)
            # Bounded — prune oldest if we ever exceed 256 entries.
            if len(_CG_CACHE) > 256:
                oldest = sorted(_CG_CACHE.items(), key=lambda kv: kv[1][0])[:64]
                for k, _ in oldest:
                    _CG_CACHE.pop(k, None)
    return data


def get_crypto_market(limit: int = 50) -> list:
    try:
        data = _cg_get("/coins/markets", {
            "vs_currency": "usd",
            "order": "market_cap_desc",
            "per_page": limit,
            "page": 1,
            "sparkline": "true",
            "price_change_percentage": "1h,24h,7d",
        })
        result = []
        for c in data:
            result.append({
                "id": c["id"],
                "symbol": c["symbol"].upper(),
                "name": c["name"],
                "price": c["current_price"],
                "market_cap": c["market_cap"],
                "market_cap_rank": c["market_cap_rank"],
                "volume_24h": c["total_volume"],
                "change_1h": c.get("price_change_percentage_1h_in_currency"),
                "change_24h": c.get("price_change_percentage_24h"),
                "change_7d": c.get("price_change_percentage_7d_in_currency"),
                "ath": c.get("ath"),
                "ath_change_pct": c.get("ath_change_percentage"),
                "circulating_supply": c.get("circulating_supply"),
                "total_supply": c.get("total_supply"),
                "sparkline": c.get("sparkline_in_7d", {}).get("price", []),
                "image": c.get("image", ""),
            })
        return result
    except Exception as e:
        return []


def get_crypto_chart(coin_id: str, days: int = 30) -> list:
    try:
        data = _cg_get(f"/coins/{coin_id}/market_chart", {
            "vs_currency": "usd",
            "days": days,
        })
        prices = data.get("prices", [])
        volumes = {int(v[0]): v[1] for v in data.get("total_volumes", [])}
        result = []
        for ts_ms, price in prices:
            ts_s = int(ts_ms / 1000)
            result.append({
                "time": ts_s,
                "value": price,
                "volume": volumes.get(int(ts_ms), 0),
            })
        return result
    except Exception:
        return []


def get_crypto_global() -> dict:
    try:
        data = _cg_get("/global")
        d = data.get("data", {})
        return {
            "total_market_cap_usd": d.get("total_market_cap", {}).get("usd"),
            "total_volume_usd": d.get("total_volume", {}).get("usd"),
            "btc_dominance": d.get("market_cap_percentage", {}).get("btc"),
            "eth_dominance": d.get("market_cap_percentage", {}).get("eth"),
            "active_coins": d.get("active_cryptocurrencies"),
            "markets": d.get("markets"),
            "market_cap_change_24h": d.get("market_cap_change_percentage_24h_usd"),
        }
    except Exception:
        return {}


def get_crypto_quote(coin_id: str) -> dict:
    """Single coin detailed data."""
    try:
        data = _cg_get(f"/coins/{coin_id}", {
            "localization": "false",
            "tickers": "false",
            "market_data": "true",
            "community_data": "false",
            "developer_data": "false",
        })
        md = data.get("market_data", {})
        return {
            "id": data["id"],
            "symbol": data["symbol"].upper(),
            "name": data["name"],
            "description": data.get("description", {}).get("en", "")[:500],
            "price": md.get("current_price", {}).get("usd"),
            "market_cap": md.get("market_cap", {}).get("usd"),
            "volume_24h": md.get("total_volume", {}).get("usd"),
            "change_24h": md.get("price_change_percentage_24h"),
            "change_7d": md.get("price_change_percentage_7d"),
            "change_30d": md.get("price_change_percentage_30d"),
            "change_1y": md.get("price_change_percentage_1y"),
            "ath": md.get("ath", {}).get("usd"),
            "ath_change_pct": md.get("ath_change_percentage", {}).get("usd"),
            "atl": md.get("atl", {}).get("usd"),
            "circulating_supply": md.get("circulating_supply"),
            "total_supply": md.get("total_supply"),
            "max_supply": md.get("max_supply"),
            "market_cap_rank": data.get("market_cap_rank"),
        }
    except Exception as e:
        return {"id": coin_id, "error": str(e)}


def get_crypto_extras(coin_id: str) -> dict:
    """
    Pull CoinGecko's community + developer + scoring data for a coin.
    Used by the idea generator to surface on-chain-ish signals (no paid APIs).
    """
    if not coin_id:
        return {}
    cache_key = ("crypto_extras", coin_id)
    cached = _cached(cache_key, ttl=3600)
    if cached:
        return cached
    try:
        data = _cg_get(f"/coins/{coin_id}", {
            "localization": "false",
            "tickers": "false",
            "market_data": "false",
            "community_data": "true",
            "developer_data": "true",
            "sparkline": "false",
        })
        community = data.get("community_data") or {}
        dev = data.get("developer_data") or {}
        result = {
            "id": coin_id,
            "developer_score": data.get("developer_score"),
            "community_score": data.get("community_score"),
            "liquidity_score": data.get("liquidity_score"),
            "public_interest_score": data.get("public_interest_score"),
            "sentiment_votes_up_pct": data.get("sentiment_votes_up_percentage"),
            "sentiment_votes_down_pct": data.get("sentiment_votes_down_percentage"),
            "twitter_followers": community.get("twitter_followers"),
            "reddit_subscribers": community.get("reddit_subscribers"),
            "reddit_active_48h": community.get("reddit_accounts_active_48h"),
            "telegram_users": community.get("telegram_channel_user_count"),
            "github_forks": dev.get("forks"),
            "github_stars": dev.get("stars"),
            "github_subscribers": dev.get("subscribers"),
            "github_total_issues": dev.get("total_issues"),
            "github_closed_issues": dev.get("closed_issues"),
            "github_pull_requests_merged": dev.get("pull_requests_merged"),
            "github_commit_count_4w": dev.get("commit_count_4_weeks"),
            "categories": data.get("categories") or [],
        }
        _set_cache(cache_key, result, ttl=3600)
        return result
    except Exception as e:
        return {"id": coin_id, "error": str(e)}


# ─── Technical Indicators ─────────────────────────────────────────────────────

# ─── Options Chain ────────────────────────────────────────────────────────────

def get_options_dates(symbol: str) -> list:
    """Return list of expiry date strings for a symbol."""
    try:
        t = yf.Ticker(symbol)
        return list(t.options)
    except Exception:
        return []


def get_option_chain(symbol: str, date: str = None) -> dict:
    """Return calls and puts as list of dicts. Handles missing greeks gracefully."""
    try:
        t = yf.Ticker(symbol)
        if date:
            chain = t.option_chain(date)
        else:
            if not t.options:
                return {"calls": [], "puts": [], "error": "No options available"}
            chain = t.option_chain(t.options[0])

        def _df_to_list(df):
            # yfinance occasionally returns None for one side of the chain
            # (typically when the ETF doesn't have puts, or under rate-limit
            # partial responses). Treat as empty so the caller's iteration
            # doesn't crash.
            if df is None:
                return []
            result = []
            for _, row in df.iterrows():
                iv = _safe(row.get("impliedVolatility"))
                result.append({
                    "contractSymbol": row.get("contractSymbol", ""),
                    "strike": _safe(row.get("strike")),
                    "lastPrice": _safe(row.get("lastPrice")),
                    "bid": _safe(row.get("bid")),
                    "ask": _safe(row.get("ask")),
                    "change": _safe(row.get("change")),
                    "percentChange": _safe(row.get("percentChange")),
                    "volume": _safe(row.get("volume")),
                    "openInterest": _safe(row.get("openInterest")),
                    "impliedVolatility": round(iv * 100, 2) if iv is not None else None,
                    "inTheMoney": bool(row.get("inTheMoney", False)),
                    "delta": _safe(getattr(row, "delta", row.get("delta"))),
                    "gamma": _safe(getattr(row, "gamma", row.get("gamma"))),
                    "theta": _safe(getattr(row, "theta", row.get("theta"))),
                    "vega": _safe(getattr(row, "vega", row.get("vega"))),
                })
            return result

        return {
            "calls": _df_to_list(chain.calls),
            "puts": _df_to_list(chain.puts),
        }
    except Exception as e:
        return {"calls": [], "puts": [], "error": str(e)}


# ─── Analytics — Risk & Correlation ───────────────────────────────────────────

def _yahoo_direct_closes_df(symbols: list, period: str) -> "pd.DataFrame":
    """Build a closes DataFrame by hitting Yahoo's chart endpoint per symbol.
    Used when yf.download trips its broken-crumb rate-limit path. Returns
    a DataFrame indexed by datetime with one column per symbol; missing
    symbols are simply omitted."""
    series = {}
    for sym in symbols:
        bars = _yahoo_chart_history(sym, period=period, interval="1d")
        if not bars:
            continue
        idx = pd.to_datetime([b["time"] for b in bars], unit="s")
        series[sym] = pd.Series([b["close"] for b in bars], index=idx)
    if not series:
        return pd.DataFrame()
    return pd.DataFrame(series).sort_index()


def _get_returns_df(symbols: list, period: str = "1y") -> "pd.DataFrame":
    """Download adjusted closes for symbols and return daily returns DataFrame."""
    if not symbols:
        return pd.DataFrame()
    try:
        raw = yf.download(
            symbols if len(symbols) > 1 else symbols[0],
            period=period,
            progress=False,
            auto_adjust=True,
        )
        if raw.empty:
            closes = _yahoo_direct_closes_df(symbols, period)
            return closes.pct_change().dropna() if not closes.empty else pd.DataFrame()

        # Handle single symbol (flat) vs multi-symbol (MultiIndex)
        if len(symbols) == 1:
            if isinstance(raw.columns, pd.MultiIndex):
                closes = raw["Close"]
                if isinstance(closes, pd.DataFrame):
                    closes = closes.iloc[:, 0]
                closes = closes.to_frame(name=symbols[0])
            else:
                closes = raw[["Close"]].rename(columns={"Close": symbols[0]})
        else:
            if isinstance(raw.columns, pd.MultiIndex):
                closes = raw["Close"]
                if isinstance(closes, pd.Series):
                    closes = closes.to_frame(name=symbols[0])
            else:
                closes = raw[["Close"]]

        returns = closes.pct_change().dropna()
        if returns.empty:
            closes = _yahoo_direct_closes_df(symbols, period)
            return closes.pct_change().dropna() if not closes.empty else pd.DataFrame()
        return returns
    except Exception:
        closes = _yahoo_direct_closes_df(symbols, period)
        return closes.pct_change().dropna() if not closes.empty else pd.DataFrame()


def get_correlation_matrix(symbols: list, period: str = "3mo") -> dict:
    """Compute pairwise correlation matrix for portfolio symbols."""
    if not symbols:
        return {"symbols": [], "matrix": {}}
    if len(symbols) == 1:
        sym = symbols[0]
        return {"symbols": [sym], "matrix": {sym: {sym: 1.0}}}
    try:
        returns = _get_returns_df(symbols, period)
        sym_upper = [s.upper() for s in symbols]
        if returns.empty or returns.shape[1] < 2:
            # yf.download can silently truncate at >50 symbols; surface what
            # the caller asked for vs. what came back so the UI can flag it.
            returned_upper = [str(c).upper() for c in returns.columns] if not returns.empty else []
            missing = [s for s, u in zip(symbols, sym_upper) if u not in returned_upper]
            return {"symbols": symbols, "matrix": {}, "missing_symbols": missing}

        # Keep only columns that match our symbols (case-insensitive)
        available = [c for c in returns.columns if c.upper() in sym_upper]
        if len(available) < 2:
            returned_upper = [str(c).upper() for c in returns.columns]
            missing = [s for s, u in zip(symbols, sym_upper) if u not in returned_upper]
            out = {"symbols": symbols, "matrix": {}}
            if missing:
                out["missing_symbols"] = missing
            return out

        returns = returns[available].dropna(axis=1, how="all")
        corr = returns.corr()

        matrix = {}
        for sym in corr.columns:
            matrix[sym] = {}
            for sym2 in corr.columns:
                val = corr.loc[sym, sym2]
                matrix[sym][sym2] = round(float(val), 4) if not pd.isna(val) else None

        # Report any requested symbols we couldn't price so the UI can show
        # "partial coverage" instead of silently dropping them.
        covered_upper = [str(c).upper() for c in corr.columns]
        missing = [s for s, u in zip(symbols, sym_upper) if u not in covered_upper]
        result = {"symbols": list(corr.columns), "matrix": matrix}
        if missing:
            result["missing_symbols"] = missing
        return result
    except Exception as e:
        return {"symbols": symbols, "matrix": {}, "error": str(e)}


def get_risk_metrics(symbols: list, period: str = "1y") -> dict:
    """Compute per-symbol risk metrics.

    Returns a dict keyed by symbol. If yf.download silently truncated (it
    can with >50 symbols), a "_missing_symbols" key lists the requested
    symbols that did not come back so the UI can show partial coverage.
    """
    if not symbols:
        return {}
    try:
        returns = _get_returns_df(symbols, period)
        sym_upper = [s.upper() for s in symbols]
        if returns.empty:
            return {"_missing_symbols": list(symbols)}

        trading_days = 252
        rf_daily = 0.05 / trading_days  # 5% annual risk-free rate

        result = {}
        for col_name in returns.columns:
            r = returns[col_name].dropna()
            if len(r) < 5:
                result[col_name] = {}
                continue

            ann_return = float((1 + r.mean()) ** trading_days - 1) * 100
            ann_vol = float(r.std() * (trading_days ** 0.5)) * 100

            # Sharpe
            excess = r - rf_daily
            sharpe = float(excess.mean() / excess.std() * (trading_days ** 0.5)) if excess.std() > 0 else None

            # Sortino
            downside = r[r < 0]
            sortino_denom = float(downside.std() * (trading_days ** 0.5)) if len(downside) > 1 else None
            sortino = float((r.mean() - rf_daily) * (trading_days ** 0.5) / downside.std()) if sortino_denom and sortino_denom > 0 else None

            # Max drawdown
            cum = (1 + r).cumprod()
            rolling_max = cum.cummax()
            drawdown = (cum - rolling_max) / rolling_max
            max_dd = float(drawdown.min()) * 100

            # Calmar
            calmar = (ann_return / abs(max_dd)) if max_dd != 0 else None

            # VaR 95% (historical)
            var_95 = float(np.percentile(r, 5)) * 100

            # Total return
            total_ret = float((1 + r).prod() - 1) * 100

            result[str(col_name)] = {
                "annualized_return": round(ann_return, 2),
                "annualized_vol": round(ann_vol, 2),
                "sharpe_ratio": round(sharpe, 3) if sharpe is not None else None,
                "sortino_ratio": round(sortino, 3) if sortino is not None else None,
                "max_drawdown": round(max_dd, 2),
                "calmar_ratio": round(calmar, 3) if calmar is not None else None,
                "var_95": round(var_95, 2),
                "total_return": round(total_ret, 2),
            }
        # Surface any requested symbols missing from the returns frame so
        # the UI can show "partial coverage" instead of silently dropping.
        covered_upper = [str(c).upper() for c in result.keys()]
        missing = [s for s, u in zip(symbols, sym_upper) if u not in covered_upper]
        if missing:
            result["_missing_symbols"] = missing
        return result
    except Exception as e:
        return {"error": str(e)}


def get_benchmark_history(symbol: str = "SPY", period: str = "1y", base_value: float = None) -> list:
    """Return benchmark price history, optionally normalized to base_value."""
    ck = ("bench", symbol.upper(), period, base_value)
    hit = _cached(ck, ttl=12 * 3600)
    if hit is not None:
        return hit
    def _normalize(bars):
        if not bars:
            return []
        first_close = float(bars[0]["close"])
        out = []
        for b in bars:
            normalized = (float(b["close"]) / first_close) * (base_value if base_value else 1.0)
            out.append({"time": int(b["time"]), "value": round(normalized, 4)})
        return out
    try:
        t = yf.Ticker(symbol)
        hist = t.history(period=period, interval="1d", auto_adjust=True)
        if hist.empty:
            return _normalize(_yahoo_chart_history(symbol, period, "1d"))
        closes = hist["Close"]
        result = []
        first_close = float(closes.iloc[0])
        for ts, price in closes.items():
            ts_unix = int(ts.timestamp())
            normalized = (float(price) / first_close) * (base_value if base_value else 1.0)
            result.append({"time": ts_unix, "value": round(normalized, 4)})
        if result:
            _set_cache(ck, result, ttl=12 * 3600)
        return result
    except Exception:
        bars = _normalize(_yahoo_chart_history(symbol, period, "1d"))
        if bars:
            _set_cache(ck, bars, ttl=12 * 3600)
        return bars


def compute_indicators(ohlcv: list) -> dict:
    """Compute common technical indicators from OHLCV list."""
    if len(ohlcv) < 20:
        return {}
    closes = [c["close"] for c in ohlcv]
    highs = [c["high"] for c in ohlcv]
    lows = [c["low"] for c in ohlcv]
    volumes = [c["volume"] for c in ohlcv]

    def sma(data, period):
        return [round(sum(data[i - period:i]) / period, 4) for i in range(period, len(data) + 1)]

    def ema(data, period):
        k = 2 / (period + 1)
        result = [sum(data[:period]) / period]
        for price in data[period:]:
            result.append(price * k + result[-1] * (1 - k))
        return [round(v, 4) for v in result]

    def rsi(data, period=14):
        gains, losses = [], []
        for i in range(1, len(data)):
            diff = data[i] - data[i - 1]
            gains.append(max(diff, 0))
            losses.append(max(-diff, 0))
        if len(gains) < period:
            return None
        avg_gain = sum(gains[:period]) / period
        avg_loss = sum(losses[:period]) / period
        rsi_vals = []
        for i in range(period, len(gains)):
            avg_gain = (avg_gain * (period - 1) + gains[i]) / period
            avg_loss = (avg_loss * (period - 1) + losses[i]) / period
            rs = avg_gain / avg_loss if avg_loss else float("inf")
            rsi_vals.append(round(100 - 100 / (1 + rs), 2))
        return rsi_vals[-1] if rsi_vals else None

    n = len(closes)
    sma20 = sma(closes, 20)
    sma50 = sma(closes, 50) if n >= 50 else None
    sma200 = sma(closes, 200) if n >= 200 else None
    ema12 = ema(closes, 12)
    ema26 = ema(closes, 26)

    macd_line = None
    macd_signal = None
    if len(ema12) >= 9 and len(ema26) >= 9:
        offset = len(ema12) - len(ema26)
        macd = [e12 - e26 for e12, e26 in zip(ema12[offset:], ema26)]
        signal = ema(macd, 9)
        if macd and signal:
            macd_line = round(macd[-1], 4)
            macd_signal = round(signal[-1], 4)

    # Bollinger Bands (20, 2)
    bb_upper = bb_lower = bb_mid = None
    if len(closes) >= 20:
        window = closes[-20:]
        mid = sum(window) / 20
        std = (sum((x - mid) ** 2 for x in window) / 20) ** 0.5
        bb_mid = round(mid, 4)
        bb_upper = round(mid + 2 * std, 4)
        bb_lower = round(mid - 2 * std, 4)

    # ATR (14)
    atr = None
    if len(closes) >= 15:
        trs = []
        for i in range(1, min(15, len(closes))):
            tr = max(
                highs[-i] - lows[-i],
                abs(highs[-i] - closes[-i - 1]),
                abs(lows[-i] - closes[-i - 1]),
            )
            trs.append(tr)
        atr = round(sum(trs) / len(trs), 4)

    return {
        "rsi": rsi(closes),
        "macd": macd_line,
        "macd_signal": macd_signal,
        "sma20": sma20[-1] if sma20 else None,
        "sma50": sma50[-1] if sma50 else None,
        "sma200": sma200[-1] if sma200 else None,
        "ema12": ema12[-1] if ema12 else None,
        "ema26": ema26[-1] if ema26 else None,
        "bb_upper": bb_upper,
        "bb_mid": bb_mid,
        "bb_lower": bb_lower,
        "atr": atr,
        "current_price": closes[-1],
        "price_vs_sma20": round(((closes[-1] / sma20[-1]) - 1) * 100, 2) if sma20 else None,
        "price_vs_sma50": round(((closes[-1] / sma50[-1]) - 1) * 100, 2) if sma50 else None,
        "price_vs_sma200": round(((closes[-1] / sma200[-1]) - 1) * 100, 2) if sma200 else None,
    }


# ─── Dividends ────────────────────────────────────────────────────────────────

def get_dividend_data(symbol: str) -> dict:
    """
    Returns dividend yield, rate, ex-date, payment history (last 8 quarters),
    and projected annual income per share.
    """
    try:
        t = yf.Ticker(symbol)
        info = t.info or {}
        fast = t.fast_info

        # Current price
        price = _safe(fast.last_price) or _safe(info.get("regularMarketPrice"))

        # Annual dividend rate and yield
        div_rate   = _safe(info.get("dividendRate"))    # annual $/share
        div_yield  = _safe(info.get("dividendYield"))   # 0.018 = 1.8%
        if div_yield:
            div_yield = round(div_yield * 100, 3)       # store as percent

        # Ex-dividend date (Unix timestamp in info dict)
        ex_date = None
        try:
            cal = t.calendar or {}
            ex_d = cal.get("Ex-Dividend Date")
            if ex_d:
                ex_date = ex_d.isoformat() if hasattr(ex_d, "isoformat") else str(ex_d)[:10]
        except Exception:
            pass

        pay_date = None
        try:
            cal = t.calendar or {}
            pay_d = cal.get("Dividend Date")
            if pay_d:
                pay_date = pay_d.isoformat() if hasattr(pay_d, "isoformat") else str(pay_d)[:10]
        except Exception:
            pass

        # Historical dividend payments (last 12 entries)
        history = []
        try:
            divs = t.dividends
            if divs is not None and not divs.empty:
                for dt, val in divs.tail(12).items():
                    history.append({
                        "date": dt.date().isoformat() if hasattr(dt, "date") else str(dt)[:10],
                        "amount": round(float(val), 4),
                    })
                history = list(reversed(history))   # newest first
        except Exception:
            pass

        # Payout frequency (estimate from history spacing)
        freq = None
        if len(history) >= 2:
            from datetime import date
            try:
                d1 = date.fromisoformat(history[0]["date"])
                d2 = date.fromisoformat(history[1]["date"])
                gap_days = abs((d1 - d2).days)
                if gap_days <= 40:
                    freq = "Monthly"
                elif gap_days <= 100:
                    freq = "Quarterly"
                elif gap_days <= 200:
                    freq = "Semi-Annual"
                else:
                    freq = "Annual"
            except Exception:
                pass

        # 5-year dividend growth rate (CAGR from oldest to newest in history)
        div_growth_5y = None
        try:
            divs_all = t.dividends
            if divs_all is not None and len(divs_all) >= 8:
                annual = {}
                for dt, val in divs_all.items():
                    yr = dt.year if hasattr(dt, 'year') else int(str(dt)[:4])
                    annual[yr] = annual.get(yr, 0) + float(val)
                years = sorted(annual.keys())
                if len(years) >= 6:
                    old_yr, new_yr = years[-6], years[-1]
                    old_v, new_v = annual[old_yr], annual[new_yr]
                    n = new_yr - old_yr
                    if old_v > 0 and n > 0:
                        div_growth_5y = round(((new_v / old_v) ** (1 / n) - 1) * 100, 2)
        except Exception:
            pass

        result = {
            "symbol": symbol.upper(),
            "name": info.get("shortName") or info.get("longName") or symbol,
            "price": price,
            "div_rate": div_rate,
            "div_yield": div_yield,
            "ex_date": ex_date,
            "pay_date": pay_date,
            "frequency": freq,
            "div_growth_5y": div_growth_5y,
            "payout_ratio": _safe(info.get("payoutRatio")),
            "history": history,
        }
        # If yfinance succeeded but produced no dividend signal at all (likely
        # rate-limited info dict), try Finviz for at least the static fields.
        if div_rate is None and div_yield is None and not history:
            fb = _finviz_dividend_fallback(symbol)
            if "error" not in fb:
                # Preserve any price we already have
                if price is not None and fb.get("price") is None:
                    fb["price"] = price
                return fb
        return result
    except Exception as e:
        fb = _finviz_dividend_fallback(symbol)
        if "error" not in fb:
            return fb
        return {"symbol": symbol.upper(), "error": str(e), "div_yield": None, "div_rate": None}


def _finviz_dividend_fallback(symbol: str) -> dict:
    """Map Finviz dividend fields into get_dividend_data()'s shape. Finviz
    exposes Dividend %, Dividend TTM, Dividend Est., Dividend Ex-Date,
    Payout, and the Price field needed for the dashboard."""
    sym = symbol.upper()
    try:
        from finvizfinance.quote import finvizfinance as _Q
        fund = _Q(sym).ticker_fundament() or {}
    except Exception as e:
        return {"symbol": sym, "error": f"finviz dividend: {e}"}

    def _num(v):
        if v is None: return None
        s = str(v).strip().replace(",","")
        had_pct = s.endswith("%")
        if had_pct: s = s[:-1]
        if s in ("","-","—"): return None
        try:
            n = float(s.split()[0])
            return n / 100 if had_pct else n
        except (ValueError, IndexError):
            return None

    # Finviz "Dividend TTM" is shaped like "1.05 (0.35%)" — first num is $, second is yield
    div_ttm_raw = fund.get("Dividend TTM") or ""
    parts = div_ttm_raw.replace("(", " ").replace(")", "").replace("%", "").split()
    div_rate = None
    div_yield = None
    if len(parts) >= 1:
        try: div_rate = float(parts[0])
        except ValueError: pass
    if len(parts) >= 2:
        try: div_yield = float(parts[1])
        except ValueError: pass
    if div_yield is None:
        # Some Finviz pages use "Dividend %" directly
        div_yield = _num(fund.get("Dividend %"))

    return {
        "symbol": sym,
        "name": fund.get("Company", "") or sym,
        "price": _num(fund.get("Price")),
        "div_rate": div_rate,
        "div_yield": div_yield,
        "ex_date": fund.get("Dividend Ex-Date", None),
        "pay_date": None,
        "frequency": "Quarterly",  # Finviz doesn't expose; safe US-equity default
        "div_growth_5y": None,
        "payout_ratio": _num(fund.get("Payout")),
        "history": [],
        "source": "finviz",
    }


# ─── Macro Indicators ─────────────────────────────────────────────────────────

def get_macro_indicators() -> dict:
    """
    Fetch key macro indicators from yfinance.
    Returns current value + 1-day change for each indicator.
    """
    ck = ("macro_indicators",)
    hit = _cached(ck, ttl=300)  # 5 min — macro doesn't tick faster than daily
    if hit is not None:
        return hit
    tickers = {
        "vix":        "^VIX",
        "sp500":      "^GSPC",
        "nasdaq":     "^IXIC",
        "yield_10y":  "^TNX",
        "yield_2y":   "^IRX",
        "yield_30y":  "^TYX",
        "dxy":        "DX-Y.NYB",
        "crude_oil":  "CL=F",
        "gold":       "GC=F",
        "btc":        "BTC-USD",
    }

    def _bars_to_value(bars):
        closes = [b["close"] for b in bars if b.get("close") is not None]
        if not closes:
            return {"value": None, "change_pct": None, "prev": None}
        if len(closes) < 2:
            return {"value": round(closes[-1], 4), "change_pct": None, "prev": None}
        cur, prev = closes[-1], closes[-2]
        chg_pct = round((cur - prev) / prev * 100, 3) if prev else None
        return {"value": round(cur, 4), "prev": round(prev, 4), "change_pct": chg_pct}

    result = {}
    for key, ticker_sym in tickers.items():
        try:
            t = yf.Ticker(ticker_sym)
            hist = t.history(period="5d", interval="1d")
            if hist.empty:
                # yfinance returned empty (crumb broken / rate-limited) — go
                # direct to Yahoo's chart endpoint as a fallback.
                result[key] = _bars_to_value(_yahoo_chart_history(ticker_sym, "5d", "1d"))
                continue
            closes = hist["Close"].dropna()
            if len(closes) < 2:
                result[key] = {"value": round(float(closes.iloc[-1]), 4), "change_pct": None, "prev": None}
                continue
            cur  = float(closes.iloc[-1])
            prev = float(closes.iloc[-2])
            chg_pct = round((cur - prev) / prev * 100, 3) if prev else None
            result[key] = {
                "value": round(cur, 4),
                "prev":  round(prev, 4),
                "change_pct": chg_pct,
            }
        except Exception:
            # Last resort: hit Yahoo direct; if that also fails the slot is null.
            result[key] = _bars_to_value(_yahoo_chart_history(ticker_sym, "5d", "1d"))

    # Yield curve spread (10Y - 2Y) — inverted = recession warning
    try:
        y10 = result.get("yield_10y", {}).get("value")
        y2  = result.get("yield_2y",  {}).get("value")
        if y10 and y2:
            result["yield_curve_spread"] = round(y10 - y2, 3)
            result["yield_curve_inverted"] = result["yield_curve_spread"] < 0
        else:
            result["yield_curve_spread"] = None
            result["yield_curve_inverted"] = None
    except Exception:
        result["yield_curve_spread"] = None

    _set_cache(ck, result, ttl=300)
    return result


# ─── Portfolio Stress Test ────────────────────────────────────────────────────

# Historical scenario drawdowns by sector (approximate peak-to-trough)
_SCENARIOS = {
    "2008 Financial Crisis": {
        "description": "S&P 500 fell 56% over 17 months (Oct 2007 – Mar 2009). Financials and real estate devastated.",
        "market_drop": -56.0,
        "sector_overrides": {
            "Financial Services": -83, "Real Estate": -70, "Energy": -55,
            "Consumer Cyclical": -60, "Technology": -52, "Industrials": -57,
            "Basic Materials": -62, "Communication Services": -50,
            "Consumer Defensive": -25, "Healthcare": -28, "Utilities": -35,
        },
        "crypto_drop": -90,   # BTC barely existed; stand-in for speculative assets
        "bond_return": +20,
        "gold_return": +25,
    },
    "2020 COVID Crash": {
        "description": "S&P 500 fell 34% in 33 days (Feb–Mar 2020). Fastest bear market in history.",
        "market_drop": -34.0,
        "sector_overrides": {
            "Energy": -55, "Real Estate": -40, "Consumer Cyclical": -40,
            "Industrials": -38, "Financial Services": -38,
            "Communication Services": -28, "Technology": -28,
            "Basic Materials": -36, "Healthcare": -18,
            "Consumer Defensive": -15, "Utilities": -22,
        },
        "crypto_drop": -63,
        "bond_return": +10,
        "gold_return": -5,
    },
    "2022 Rate Hike Bear": {
        "description": "S&P 500 fell 27%, NASDAQ fell 38% as Fed raised rates from 0% to 5.25% in 2022.",
        "market_drop": -27.0,
        "sector_overrides": {
            "Technology": -40, "Communication Services": -45, "Consumer Cyclical": -38,
            "Real Estate": -30, "Industrials": -25, "Financial Services": -18,
            "Basic Materials": -22, "Energy": +55,   # energy SURGED in 2022
            "Healthcare": -10, "Consumer Defensive": -4, "Utilities": -5,
        },
        "crypto_drop": -77,
        "bond_return": -18,
        "gold_return": -5,
    },
    "2000 Dot-com Bust": {
        "description": "NASDAQ fell 78%, S&P 500 fell 49% over 30 months (Mar 2000 – Oct 2002).",
        "market_drop": -49.0,
        "sector_overrides": {
            "Technology": -80, "Communication Services": -70, "Consumer Cyclical": -45,
            "Industrials": -35, "Financial Services": -30, "Energy": -15,
            "Basic Materials": -30, "Real Estate": -15,
            "Healthcare": -25, "Consumer Defensive": -15, "Utilities": -30,
        },
        "crypto_drop": -95,
        "bond_return": +25,
        "gold_return": +15,
    },
}


def get_portfolio_stress_test(holdings: list, custom_drop_pct: float = None) -> dict:
    """
    Apply historical scenario drawdowns to a portfolio of holdings.
    Each holding must have: symbol, shares, avg_cost, market_value, asset_type.
    Also accepts a custom scenario with user-supplied market drop %.
    Returns scenario results keyed by scenario name.
    """
    # Fetch beta and sector for each stock holding
    enriched = []
    for h in holdings:
        entry = dict(h)
        sym = h["symbol"]
        asset_type = h.get("asset_type", "stock")

        if asset_type == "crypto":
            entry["sector"] = "Crypto"
            entry["beta"] = 2.5   # crypto is highly correlated but volatile
        else:
            try:
                t = yf.Ticker(sym)
                info = t.info or {}
                entry["sector"] = info.get("sector") or info.get("sectorKey") or "Unknown"
                entry["beta"] = _safe(info.get("beta")) or 1.0
                # Clamp beta to reasonable range
                if entry["beta"]:
                    entry["beta"] = max(0.1, min(3.0, entry["beta"]))
            except Exception:
                entry["sector"] = "Unknown"
                entry["beta"] = 1.0
        enriched.append(entry)

    scenarios_result = {}

    for scenario_name, params in _SCENARIOS.items():
        scenario_result = {
            "description": params["description"],
            "market_drop": params["market_drop"],
            "positions": [],
            "total_loss": 0,
            "total_loss_pct": 0,
            "new_portfolio_value": 0,
        }
        total_current_value = sum(h.get("market_value", 0) or 0 for h in enriched)
        total_new_value = 0

        for h in enriched:
            mv = h.get("market_value") or (h.get("shares", 0) * h.get("avg_cost", 0))
            asset_type = h.get("asset_type", "stock")
            sector = h.get("sector", "Unknown")
            beta = h.get("beta", 1.0) or 1.0

            if asset_type == "crypto":
                drop_pct = params.get("crypto_drop", params["market_drop"] * 1.5)
            elif sector in params.get("sector_overrides", {}):
                drop_pct = params["sector_overrides"][sector]
            else:
                # Beta-adjusted market drop
                drop_pct = params["market_drop"] * beta

            new_value = mv * (1 + drop_pct / 100)
            loss = new_value - mv
            total_new_value += new_value

            scenario_result["positions"].append({
                "symbol": h["symbol"],
                "sector": sector,
                "beta": round(beta, 2),
                "current_value": round(mv, 2),
                "new_value": round(new_value, 2),
                "loss": round(loss, 2),
                "drop_pct": round(drop_pct, 1),
            })

        total_loss = total_new_value - total_current_value
        scenario_result["total_loss"] = round(total_loss, 2)
        scenario_result["new_portfolio_value"] = round(total_new_value, 2)
        scenario_result["total_loss_pct"] = round(total_loss / total_current_value * 100, 2) if total_current_value else 0
        scenario_result["positions"].sort(key=lambda x: x["loss"])   # worst first
        scenarios_result[scenario_name] = scenario_result

    # Custom scenario
    drop = custom_drop_pct if custom_drop_pct is not None else -20.0
    custom_result = {
        "description": f"Custom scenario: market drops {abs(drop):.0f}%",
        "market_drop": drop,
        "positions": [],
        "total_loss": 0,
        "total_loss_pct": 0,
        "new_portfolio_value": 0,
    }
    total_current_value = sum(h.get("market_value", 0) or 0 for h in enriched)
    total_new_value = 0
    for h in enriched:
        mv = h.get("market_value") or (h.get("shares", 0) * h.get("avg_cost", 0))
        beta = h.get("beta", 1.0) or 1.0
        asset_type = h.get("asset_type", "stock")
        drop_pct = drop * (2.0 if asset_type == "crypto" else beta)
        new_value = mv * (1 + drop_pct / 100)
        total_new_value += new_value
        custom_result["positions"].append({
            "symbol": h["symbol"],
            "sector": h.get("sector", ""),
            "beta": round(beta, 2),
            "current_value": round(mv, 2),
            "new_value": round(new_value, 2),
            "loss": round(new_value - mv, 2),
            "drop_pct": round(drop_pct, 1),
        })
    custom_result["total_loss"] = round(total_new_value - total_current_value, 2)
    custom_result["new_portfolio_value"] = round(total_new_value, 2)
    custom_result["total_loss_pct"] = round((total_new_value - total_current_value) / total_current_value * 100, 2) if total_current_value else 0
    custom_result["positions"].sort(key=lambda x: x["loss"])
    scenarios_result["Custom Scenario"] = custom_result

    return {
        "scenarios": scenarios_result,
        "current_total_value": round(total_current_value, 2),
        "positions_analyzed": len(enriched),
    }


# ── Unusual Options Activity Scanner ──────────────────────────────────────────

def get_unusual_options_flow(symbol):
    """
    Scan all option chains for a symbol and return contracts with unusual activity:
    - volume > open_interest * 2  AND  volume > 200
    - Flags large bullish/bearish bets
    - Returns top unusual contracts sorted by volume/OI ratio
    """
    import math
    symbol = symbol.upper()
    ticker = yf.Ticker(symbol)

    try:
        exps = ticker.options
    except Exception:
        return {"symbol": symbol, "error": "No options data", "unusual": []}

    if not exps:
        return {"symbol": symbol, "error": "No expirations found", "unusual": []}

    # Get current price
    try:
        info = ticker.fast_info
        current_price = float(getattr(info, "last_price", 0) or 0)
    except Exception:
        current_price = 0

    # Only scan nearest 6 expirations to keep it fast
    scan_exps = exps[:6]
    unusual = []

    # Anchor "today" to 16:00 America/New_York (options-expiry wall clock)
    # and compare in UTC so 0/1DTE math doesn't drift by a calendar day.
    now_utc = datetime.now(timezone.utc)

    for exp in scan_exps:
        try:
            chain = ticker.option_chain(exp)
        except Exception:
            continue

        try:
            exp_date = datetime.strptime(exp, "%Y-%m-%d")
            if _NY_TZ is not None:
                exp_dt = exp_date.replace(hour=16, minute=0, second=0, tzinfo=_NY_TZ)
            else:
                exp_dt = exp_date.replace(hour=16, minute=0, second=0, tzinfo=timezone.utc)
            dte = int((exp_dt - now_utc).total_seconds() // 86400)
        except Exception:
            dte = 0

        for side, df in [("CALL", chain.calls), ("PUT", chain.puts)]:
            if df is None or df.empty:
                continue
            for _, row in df.iterrows():
                vol = _safe(row.get("volume")) or 0
                oi  = _safe(row.get("openInterest")) or 0
                strike = _safe(row.get("strike")) or 0
                last_price = _safe(row.get("lastPrice")) or 0
                iv = _safe(row.get("impliedVolatility")) or 0

                if vol < 200:
                    continue

                # Volume/OI ratio — primary unusual signal
                if oi > 0:
                    vol_oi_ratio = vol / oi
                else:
                    vol_oi_ratio = float(vol)  # treat as very unusual

                if oi > 0 and vol_oi_ratio < 2.0:
                    continue

                # OTM flag
                if current_price > 0:
                    if side == "CALL":
                        otm_pct = (strike - current_price) / current_price * 100
                    else:
                        otm_pct = (current_price - strike) / current_price * 100
                    is_otm = otm_pct > 0
                    otm_pct_abs = abs(otm_pct)
                else:
                    is_otm = False
                    otm_pct = 0
                    otm_pct_abs = 0

                # Notional value estimate
                notional = vol * last_price * 100  # each contract = 100 shares

                # Sentiment signal
                if side == "CALL" and (not is_otm or otm_pct_abs < 10):
                    sentiment = "BULLISH"
                elif side == "PUT" and (not is_otm or otm_pct_abs < 10):
                    sentiment = "BEARISH"
                elif side == "CALL" and is_otm:
                    sentiment = "SPECULATIVE CALL"
                else:
                    sentiment = "SPECULATIVE PUT"

                unusual.append({
                    "symbol":       symbol,
                    "expiry":       exp,
                    "dte":          dte,
                    "side":         side,
                    "strike":       round(strike, 2),
                    "last_price":   round(last_price, 2),
                    "volume":       int(vol),
                    "open_interest": int(oi),
                    "vol_oi_ratio": round(vol_oi_ratio, 1) if oi > 0 else None,
                    "iv_pct":       round(iv * 100, 1),
                    "notional":     round(notional),
                    "otm_pct":      round(otm_pct, 1),
                    "is_otm":       is_otm,
                    "sentiment":    sentiment,
                })

    # Sort by volume desc
    unusual.sort(key=lambda x: x["volume"], reverse=True)

    return {
        "symbol":          symbol,
        "current_price":   round(current_price, 2),
        "expirations_scanned": len(scan_exps),
        "unusual_count":   len(unusual),
        "unusual":         unusual[:50],
        "has_unusual":     len(unusual) > 0,
    }


def scan_unusual_options_portfolio(symbols):
    """
    Scan unusual options activity for a list of symbols.
    Returns aggregated results sorted by total unusual contracts.
    """
    all_results = []
    for sym in symbols:
        try:
            result = get_unusual_options_flow(sym)
            if result.get("has_unusual"):
                all_results.append(result)
        except Exception:
            pass

    all_results.sort(key=lambda x: x.get("unusual_count", 0), reverse=True)
    return all_results
