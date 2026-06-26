"""
Idea Generator — Random investment idea engine.

Picks a random symbol from a weighted universe (S&P 500 / Nasdaq 100 / top
crypto / user watchlist), fans out to existing analytics modules
(opportunity scanner, ML forecast, narrative engine, fetcher), and returns
a unified "dossier" describing strength, opportunity, and forecast.

Python 3.9 compatible.
"""

import random
import logging
import time
import threading
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List

import database as db
import fetcher
from fetcher import _cached, _set_cache
import opportunity_scanner as scanner
import ml_forecast
import narrative_engine
import ai_summarizer
import sec_edgar
import alt_signals
import congress as congress_module
import earnings as earnings_module
import safe_executor

logger = logging.getLogger(__name__)

# ── Universe ─────────────────────────────────────────────────────────────────
# Large-cap names commonly on S&P 500 / Nasdaq 100. Combined with the
# scanner's curated growth/value/income lists for breadth.

UNIVERSE_LARGE_CAP = [
    # Mega-cap tech
    "AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA", "TSLA", "AVGO",
    "ORCL", "ADBE", "CRM", "AMD", "INTC", "CSCO", "QCOM", "TXN",
    "IBM", "NOW", "INTU", "AMAT", "MU", "LRCX", "KLAC", "ADI",
    # Financials
    "JPM", "BAC", "WFC", "GS", "MS", "C", "BLK", "SCHW", "AXP",
    "V", "MA", "PYPL", "COF", "USB", "PNC", "TFC",
    # Healthcare / Pharma
    "UNH", "JNJ", "LLY", "ABBV", "MRK", "PFE", "TMO", "ABT", "DHR",
    "BMY", "AMGN", "GILD", "CVS", "CI", "ELV", "ISRG", "REGN", "VRTX",
    # Consumer
    "WMT", "HD", "COST", "PG", "KO", "PEP", "MCD", "NKE", "SBUX",
    "TGT", "LOW", "DIS", "CMCSA", "NFLX", "BKNG", "ABNB", "UBER",
    # Energy / Industrial
    "XOM", "CVX", "COP", "SLB", "EOG", "BA", "CAT", "GE", "HON",
    "UPS", "RTX", "LMT", "DE", "MMM", "UNP",
    # Communication / Media
    "T", "VZ", "TMUS", "DIS", "CHTR",
    # Other megas
    "BRK-B", "LIN", "SPGI", "CME", "ICE",
    # ETFs (broad index exposure)
    "SPY", "QQQ", "IWM", "DIA", "VTI", "VOO", "IVV", "EFA", "EEM",
    "XLK", "XLF", "XLE", "XLV", "XLY", "XLP", "XLI", "XLU", "XLRE", "XLB",
]

# De-dup with scanner universes to maximize coverage
EQUITY_UNIVERSE = sorted(set(
    UNIVERSE_LARGE_CAP
    + scanner.UNIVERSE_GROWTH
    + scanner.UNIVERSE_VALUE
    + scanner.UNIVERSE_INCOME
))

CRYPTO_UNIVERSE = list(scanner.UNIVERSE_CRYPTO_IDS.keys())

# Pick weights (must sum to 1.0)
SOURCE_WEIGHTS = {
    "equity": 0.70,
    "crypto": 0.15,
    "watchlist": 0.15,
}

# Strategy chosen at random per idea (small bias toward growth)
STRATEGY_WEIGHTS = [
    ("growth", 0.40),
    ("value", 0.25),
    ("momentum", 0.25),
    ("income", 0.10),
]

# Minimum composite score to consider an idea "interesting".
# Re-rolls below this threshold (up to MAX_REROLLS).
MIN_INTERESTING_SCORE = 40
MAX_REROLLS = 3

# Per-symbol dossier cache TTL
DOSSIER_CACHE_TTL = 600  # 10 minutes

# ── Full-universe lazy loaders ───────────────────────────────────────────────
# Discovery mode "full" pulls from these much larger pools instead of the
# hand-curated lists above. Caches are in-memory only.

_FULL_EQUITY_TTL = 24 * 3600       # SEC ticker file ~refreshed daily
_FULL_CRYPTO_TTL = 3600            # CoinGecko top-N
_FULL_CRYPTO_TARGET = 500          # how many coins to pull (paginated)

_full_equity_cache: Dict[str, Any] = {"data": None, "ts": 0.0}
_full_equity_lock = threading.Lock()
_full_crypto_cache: Dict[str, Any] = {"data": None, "ts": 0.0}
_full_crypto_lock = threading.Lock()


def get_full_equity_universe() -> List[str]:
    """
    Lazy-load every US-listed ticker from SEC's company_tickers.json
    (~10,000 entries). Filters obvious junk; returns sorted unique list.
    """
    with _full_equity_lock:
        now = time.time()
        cached = _full_equity_cache.get("data")
        if cached and (now - _full_equity_cache.get("ts", 0)) < _FULL_EQUITY_TTL:
            return cached

        try:
            # Reuse sec_edgar's session — it has the proper User-Agent
            # (SEC rejects requests without one) and rate limiting baked in.
            resp = sec_edgar._edgar_get(
                "https://www.sec.gov/files/company_tickers.json",
                timeout=20,
            )
            data = resp.json()
            tickers = set()
            for entry in data.values():
                t = (entry.get("ticker") or "").strip().upper()
                # Skip obvious junk: empty, too long, or non-alphanum (besides . / -)
                if not t or len(t) > 5:
                    continue
                if not all(c.isalnum() or c in ".-" for c in t):
                    continue
                tickers.add(t)
            result = sorted(tickers)
            _full_equity_cache["data"] = result
            _full_equity_cache["ts"] = now
            logger.info("Loaded %d SEC tickers into full universe", len(result))
            return result
        except Exception as e:
            logger.warning("SEC universe fetch failed (%s) — falling back to curated list", e)
            return list(EQUITY_UNIVERSE)


def get_full_crypto_universe(top_n: int = _FULL_CRYPTO_TARGET) -> List[Dict[str, str]]:
    """
    Pull top-N coins from CoinGecko (paginated; ~250 per page).
    Returns [{symbol, id, name}, ...]. Falls back to curated crypto list on failure.
    """
    with _full_crypto_lock:
        now = time.time()
        cached = _full_crypto_cache.get("data")
        if cached and (now - _full_crypto_cache.get("ts", 0)) < _FULL_CRYPTO_TTL and len(cached) >= top_n:
            return cached[:top_n]

        try:
            entries: List[Dict[str, str]] = []
            seen_symbols = set()
            pages = max(1, (top_n + 249) // 250)
            for page in range(1, pages + 1):
                page_data = fetcher._cg_get("/coins/markets", {
                    "vs_currency": "usd",
                    "order": "market_cap_desc",
                    "per_page": 250,
                    "page": page,
                    "sparkline": "false",
                })
                if not page_data:
                    break
                for c in page_data:
                    sym = (c.get("symbol") or "").upper().strip()
                    cid = c.get("id")
                    if not sym or not cid or sym in seen_symbols:
                        continue
                    seen_symbols.add(sym)
                    entries.append({"symbol": sym, "id": cid, "name": c.get("name") or sym})
                    if len(entries) >= top_n:
                        break
                if len(entries) >= top_n:
                    break
                # Be polite to the free CoinGecko endpoint
                time.sleep(1.2)
            if entries:
                _full_crypto_cache["data"] = entries
                _full_crypto_cache["ts"] = now
                logger.info("Loaded %d crypto entries into full universe", len(entries))
                return entries
        except Exception as e:
            logger.warning("CoinGecko universe fetch failed (%s) — falling back to curated list", e)

        fallback = [
            {"symbol": s, "id": cid, "name": s}
            for s, cid in scanner.UNIVERSE_CRYPTO_IDS.items()
        ]
        return fallback


# ══════════════════════════════════════════════════════════════════════════════
# UNIVERSE / FILTERING
# ══════════════════════════════════════════════════════════════════════════════

def _watchlist_symbols():
    try:
        rows = db.get_watchlist() or []
        return [(r["symbol"], r.get("asset_type") or "stock") for r in rows]
    except Exception:
        return []


# Runtime-built map of crypto symbol -> CoinGecko id, so quotes resolve for
# coins outside the curated UNIVERSE_CRYPTO_IDS. Populated from the full pull.
_dynamic_crypto_id_map: Dict[str, str] = {}


def _resolve_crypto_id(symbol: str) -> Optional[str]:
    """Look up the CoinGecko id for a crypto symbol from any known source."""
    s = symbol.upper()
    if s in scanner.UNIVERSE_CRYPTO_IDS:
        return scanner.UNIVERSE_CRYPTO_IDS[s]
    return _dynamic_crypto_id_map.get(s)


def _filtered_universe(
    asset_class: Optional[str],
    sector: Optional[str],
    discovery_mode: str = "curated",
):
    """
    Build a list of (symbol, asset_class, source) candidates after applying
    user-provided filters. `discovery_mode` controls pool size:
      - "curated": ~180 hand-picked names (fast, high signal-to-noise)
      - "full":    SEC's full ~10k US equity universe + top ~500 crypto
    """
    pools = {"equity": [], "crypto": [], "watchlist": []}

    # ── Equity pool ──
    if asset_class in (None, "any", "stock", "etf"):
        if discovery_mode == "full":
            sec_tickers = get_full_equity_universe()
            for sym in sec_tickers:
                pools["equity"].append((sym, "stock", "sec"))
            # Always also seed with the curated list (covers ETFs/dedup naturally)
            seen = set(t for t, _, _ in pools["equity"])
            for sym in EQUITY_UNIVERSE:
                if sym not in seen:
                    pools["equity"].append((sym, "stock", "universe"))
        else:
            for sym in EQUITY_UNIVERSE:
                pools["equity"].append((sym, "stock", "universe"))

    # ── Crypto pool ──
    if asset_class in (None, "any", "crypto"):
        if discovery_mode == "full":
            full_crypto = get_full_crypto_universe()
            for entry in full_crypto:
                sym = entry["symbol"]
                _dynamic_crypto_id_map[sym] = entry["id"]
                pools["crypto"].append((sym, "crypto", "coingecko"))
        else:
            for sym in CRYPTO_UNIVERSE:
                pools["crypto"].append((sym, "crypto", "universe"))

    # ── Watchlist pool ──
    for sym, atype in _watchlist_symbols():
        ac = "crypto" if (atype or "").lower() == "crypto" else "stock"
        if asset_class in (None, "any") or asset_class == ac:
            pools["watchlist"].append((sym, ac, "watchlist"))

    return pools


def _weighted_pick(pools, exclude=None):
    """Pick one (symbol, asset_class, source) using SOURCE_WEIGHTS."""
    exclude = exclude or set()
    sources = []
    weights = []
    for src, items in pools.items():
        if items:
            sources.append(src)
            weights.append(SOURCE_WEIGHTS.get(src, 0.0))
    if not sources:
        return None

    # Only non-empty pools contribute sources/weights above; random.choices
    # normalizes the remaining weights, so an absent (empty) pool's weight is
    # effectively redistributed pro-rata across the present sources.
    src = random.choices(sources, weights=weights, k=1)[0]
    candidates = [c for c in pools[src] if c[0] not in exclude]
    if not candidates:
        # Fall back to any non-excluded
        candidates = [c for items in pools.values() for c in items if c[0] not in exclude]
        if not candidates:
            return None
    return random.choice(candidates)


# ══════════════════════════════════════════════════════════════════════════════
# DOSSIER ASSEMBLY
# ══════════════════════════════════════════════════════════════════════════════

def _safe_call(fn, *args, **kwargs):
    """Run fn(*args), return None on any exception (logged)."""
    try:
        return fn(*args, **kwargs)
    except Exception as e:
        logger.warning("idea_generator: %s failed: %s", getattr(fn, "__name__", fn), e)
        return None


def _run_jobs_parallel(jobs, max_workers=8, prefix="idea-jobs"):
    """Run an ordered {name: zero-arg thunk} dict on safe_executor's daemon
    threads; returns {name: result-or-None}. Replaces the old heterogeneous
    ThreadPoolExecutor.submit() pattern — parallel_map falls back to serial
    by itself when worker threads can't be spawned, and its daemon workers
    can't block interpreter exit."""
    keys = list(jobs.keys())
    vals = safe_executor.parallel_map(
        lambda k: jobs[k](), keys, max_workers=max_workers,
        thread_name_prefix=prefix,
    )
    return dict(zip(keys, vals))


# ══════════════════════════════════════════════════════════════════════════════
# FACTOR BLOCK BUILDERS — collapse raw module output into compact dossier blocks
# ══════════════════════════════════════════════════════════════════════════════

def _build_social_block(symbol, stwits, reddit_mentions):
    """
    Combine StockTwits sentiment + Reddit mention count into a unified social
    sentiment block with a 0-100 composite score.
    """
    bull = bear = 0
    msgs_total = 0
    bull_ratio = None
    sample = []
    if stwits:
        bull = stwits.get("bullish") or 0
        bear = stwits.get("bearish") or 0
        msgs_total = stwits.get("messages_total") or 0
        bull_ratio = stwits.get("bull_ratio")
        sample = stwits.get("sample") or []

    # Reddit mentions for this symbol
    sym_u = symbol.upper()
    reddit_match = next((r for r in (reddit_mentions or []) if (r.get("ticker") or "").upper() == sym_u), None)
    reddit_count = (reddit_match or {}).get("mentions") or 0
    reddit_total_score = (reddit_match or {}).get("total_score") or 0

    # Social score: bull_ratio (60%) + chatter intensity (40%)
    score_components = []
    if bull_ratio is not None:
        score_components.append(bull_ratio * 100)
    if msgs_total > 0:
        chatter = min(100, (msgs_total / 30.0) * 100)  # 30+ msgs = max chatter
        score_components.append(chatter)
    if reddit_count > 0:
        reddit_score = min(100, reddit_count * 25)  # 4+ mentions = max
        score_components.append(reddit_score)

    composite = round(sum(score_components) / len(score_components), 1) if score_components else None

    if bull_ratio is None:
        sentiment_label = "NO DATA"
    elif bull_ratio >= 0.65:
        sentiment_label = "BULLISH"
    elif bull_ratio <= 0.35:
        sentiment_label = "BEARISH"
    else:
        sentiment_label = "MIXED"

    return {
        "available": stwits is not None or reddit_count > 0,
        "stocktwits_bull": bull,
        "stocktwits_bear": bear,
        "stocktwits_total": msgs_total,
        "bull_ratio": bull_ratio,
        "sentiment_label": sentiment_label,
        "reddit_mentions": reddit_count,
        "reddit_score": reddit_total_score,
        "social_score": composite,
        "sample_messages": [
            {
                "body": m.get("body"),
                "sentiment": m.get("sentiment"),
                "user": m.get("user"),
                "likes": m.get("likes"),
            }
            for m in (sample or [])[:3]
        ],
    }


def _build_insider_block(transactions):
    """
    Reduce Form 4 transactions to a compact buy/sell summary for the last 60 days.
    """
    from datetime import datetime, timedelta, timezone
    cutoff = (datetime.now(timezone.utc) - timedelta(days=60)).date()

    def _parse(d):
        try:
            return datetime.strptime(d[:10], "%Y-%m-%d").date()
        except Exception:
            return None

    recent = []
    for t in transactions:
        dt = _parse(t.get("date") or t.get("transaction_date") or t.get("filing_date") or "")
        if dt and dt >= cutoff:
            recent.append(t)

    buys = [t for t in recent if (t.get("transaction_type") or "").upper() in ("BUY", "P", "PURCHASE")]
    sells = [t for t in recent if (t.get("transaction_type") or "").upper() in ("SELL", "S", "SALE")]
    buy_value = sum((t.get("value") or 0) for t in buys)
    sell_value = sum((t.get("value") or 0) for t in sells)

    if not recent:
        signal = "NO ACTIVITY"
    elif buy_value > sell_value * 2 and len(buys) >= 2:
        signal = "BULLISH"
    elif sell_value > buy_value * 3:
        signal = "BEARISH"
    else:
        signal = "MIXED"

    sample = []
    for t in recent[:3]:
        sample.append({
            "date": (t.get("date") or t.get("transaction_date") or t.get("filing_date") or "")[:10],
            "insider": t.get("insider_name") or t.get("filer_name"),
            "title": t.get("insider_title") or t.get("title"),
            "type": t.get("transaction_type"),
            "shares": t.get("shares"),
            "value": t.get("value"),
            "price": t.get("price"),
        })

    return {
        "available": len(recent) > 0,
        "window_days": 60,
        "total_transactions": len(recent),
        "buys": len(buys),
        "sells": len(sells),
        "buy_value": buy_value,
        "sell_value": sell_value,
        "net_value": buy_value - sell_value,
        "signal": signal,
        "sample": sample,
    }


def _build_congress_block(trades):
    """Compact summary of recent congressional trades for the symbol."""
    if not trades:
        return {"available": False, "total_trades": 0, "buys": 0, "sells": 0, "signal": "NO ACTIVITY", "sample": []}

    buys = sells = 0
    members = set()
    for t in trades:
        # congress.get_trades_for_ticker emits txn_type / member_name / txn_date /
        # amount_str — the old transaction_type/member keys never matched, so this
        # whole block was dead (signal always "MIXED", 0 members).
        ttype = (t.get("txn_type") or t.get("transaction_type") or "").lower()
        if "purchase" in ttype or "buy" in ttype:
            buys += 1
        elif "sale" in ttype or "sell" in ttype:
            sells += 1
        member = t.get("member_name") or t.get("member")
        if member:
            members.add(member)

    if buys > sells * 2:
        signal = "BULLISH"
    elif sells > buys * 2:
        signal = "BEARISH"
    else:
        signal = "MIXED"

    sample = []
    for t in trades[:3]:
        sample.append({
            "date": t.get("txn_date") or t.get("transaction_date") or t.get("filing_date"),
            "member": t.get("member_name") or t.get("member"),
            "party": t.get("party"),
            "transaction_type": t.get("txn_type") or t.get("transaction_type"),
            "amount": t.get("amount_str") or t.get("amount") or t.get("amount_range"),
        })

    return {
        "available": True,
        "total_trades": len(trades),
        "buys": buys,
        "sells": sells,
        "members_count": len(members),
        "signal": signal,
        "sample": sample,
    }


def _build_options_block(flow):
    """Reduce unusual options flow to bull/bear bias + top contracts."""
    if not flow or flow.get("error"):
        return {"available": False, "unusual_count": 0, "bias": "NO DATA", "top_contracts": []}

    unusual = flow.get("unusual") or []
    if not unusual:
        return {"available": False, "unusual_count": 0, "bias": "NO DATA", "top_contracts": []}

    call_volume = sum((c.get("volume") or 0) for c in unusual if (c.get("side") or "").upper() == "CALL")
    put_volume = sum((c.get("volume") or 0) for c in unusual if (c.get("side") or "").upper() == "PUT")
    total = call_volume + put_volume
    call_pct = (call_volume / total * 100) if total else None

    if call_pct is None:
        bias = "NO DATA"
    elif call_pct >= 65:
        bias = "BULLISH (CALLS)"
    elif call_pct <= 35:
        bias = "BEARISH (PUTS)"
    else:
        bias = "MIXED"

    top = sorted(unusual, key=lambda c: c.get("vol_oi_ratio") or 0, reverse=True)[:3]
    top_contracts = [
        {
            "side": c.get("side"),
            "strike": c.get("strike"),
            "expiration": c.get("expiration"),
            "dte": c.get("dte"),
            "volume": c.get("volume"),
            "open_interest": c.get("open_interest"),
            "vol_oi_ratio": c.get("vol_oi_ratio"),
            "premium": c.get("premium") or c.get("last_price"),
        }
        for c in top
    ]

    return {
        "available": True,
        "unusual_count": len(unusual),
        "call_volume": call_volume,
        "put_volume": put_volume,
        "call_pct": round(call_pct, 1) if call_pct is not None else None,
        "bias": bias,
        "top_contracts": top_contracts,
    }


def _build_events_block(earnings_cal, dividend):
    """Next earnings + next ex-dividend dates."""
    next_earnings = None
    days_to_earnings = None
    if earnings_cal:
        # earnings_module.get_earnings_calendar([symbol]) returns a list
        if isinstance(earnings_cal, list) and earnings_cal:
            entry = earnings_cal[0]
            next_earnings = entry.get("earnings_date") or entry.get("date")
            days_to_earnings = entry.get("days_until")
        elif isinstance(earnings_cal, dict):
            next_earnings = earnings_cal.get("earnings_date")
            days_to_earnings = earnings_cal.get("days_until")

    ex_date = (dividend or {}).get("ex_date")
    div_yield = (dividend or {}).get("div_yield")
    div_rate = (dividend or {}).get("div_rate")

    return {
        "available": bool(next_earnings or ex_date),
        "next_earnings_date": next_earnings,
        "days_to_earnings": days_to_earnings,
        "next_ex_dividend_date": ex_date,
        "dividend_yield_pct": div_yield,
        "dividend_rate": div_rate,
    }


def _build_fast_dossier(symbol: str, asset_class: str, source: str, strategy: str):
    """
    Phase 1 — fast blocks (~5-8s when parallel):
      snapshot, opportunity (narrative), social, events, risk, headlines.
    Returns a dossier dict missing the strength/forecast/insider/congress/
    options/peers/historical_analog/thesis blocks (those go in enrichment).
    The returned dict has `enrichment_pending: True` so the UI knows to call
    /api/ideas/enrich/<symbol> next.
    """
    cache_key = ("idea_dossier_fast", symbol, asset_class, strategy)
    cached = _cached(cache_key, ttl=DOSSIER_CACHE_TTL)
    if cached:
        cached = dict(cached)
        cached["source"] = source
        cached["from_cache"] = True
        return cached

    yf_symbol = symbol if asset_class == "stock" else f"{symbol}-USD"
    started = time.time()

    # Heterogeneous fan-out via _run_jobs_parallel (safe_executor daemon
    # threads). Each thunk goes through _safe_call, so a slot is None on
    # failure — same as the old future-based collection.
    jobs = {}
    if asset_class == "crypto":
        coin_id = _resolve_crypto_id(symbol)
        if coin_id:
            jobs["quote"] = lambda: _safe_call(fetcher.get_crypto_quote, coin_id)
    else:
        jobs["quote"] = lambda: _safe_call(fetcher.get_quote, symbol)
        jobs["fund"] = lambda: _safe_call(fetcher.get_fundamentals, symbol)
        jobs["earnings"] = lambda: _safe_call(earnings_module.get_earnings_calendar, [symbol])
        jobs["dividend"] = lambda: _safe_call(fetcher.get_dividend_data, symbol)
    jobs["narr"] = lambda: _safe_call(narrative_engine.analyze_narrative, symbol)
    jobs["news"] = lambda: _safe_call(fetcher.get_news, yf_symbol, 5)
    jobs["risk"] = lambda: _safe_call(fetcher.get_risk_metrics, [yf_symbol], "1y")
    jobs["stwits"] = lambda: _safe_call(alt_signals.stocktwits_symbol_sentiment, symbol)
    jobs["reddit"] = lambda: _safe_call(alt_signals.reddit_ticker_mentions)

    got = _run_jobs_parallel(jobs, max_workers=8, prefix="idea-fast")
    narrative = got.get("narr")
    news = got.get("news") or []
    risk_all = got.get("risk") or {}
    quote = got.get("quote")
    fundamentals = got.get("fund")
    stwits = got.get("stwits")
    reddit_mentions = got.get("reddit") or []
    earnings_cal = got.get("earnings")
    dividend = got.get("dividend")

    risk = (risk_all.get(yf_symbol) or risk_all.get(symbol) or {}) if isinstance(risk_all, dict) else {}

    snapshot = _build_snapshot_block(symbol, asset_class, quote, fundamentals)
    opportunity = _build_opportunity_block(narrative)
    # fetcher.get_risk_metrics emits `annualized_return` / `annualized_vol` /
    # `sharpe_ratio` / `sortino_ratio` — the old shorter aliases never existed,
    # so the risk_block was effectively all-None and downstream callers (trade
    # plan / sizing) silently fell back to a hardcoded 25% vol for every name.
    risk_block = {
        "annual_return_pct": risk.get("annualized_return"),
        "annual_volatility_pct": risk.get("annualized_vol"),
        "sharpe": risk.get("sharpe_ratio"),
        "sortino": risk.get("sortino_ratio"),
        "max_drawdown": risk.get("max_drawdown"),
    }
    social = _build_social_block(symbol, stwits, reddit_mentions)
    events = _build_events_block(earnings_cal, dividend)
    headlines = _build_headlines_block(news)

    elapsed = round((time.time() - started) * 1000)

    dossier = {
        "symbol": symbol,
        "asset_class": asset_class,
        "source": source,
        "strategy": strategy,
        "snapshot": snapshot,
        "opportunity": opportunity,
        "risk": risk_block,
        "social": social,
        "events": events,
        "headlines": headlines,
        # Enrichment placeholders — frontend renders skeletons until the
        # /api/ideas/enrich/<symbol> response replaces these.
        "strength": None,
        "forecast": None,
        "insider": None,
        "congress": None,
        "options_flow": None,
        "peers": None,
        "historical_analog": None,
        "thesis": None,
        "enrichment_pending": True,
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "fast_computed_in_ms": elapsed,
        "from_cache": False,
    }

    _set_cache(cache_key, dossier, ttl=DOSSIER_CACHE_TTL)
    return dossier


def _build_enrichment(symbol: str, asset_class: str, strategy: str, fast: dict):
    """
    Phase 2 — slow blocks (~25-40s when parallel):
      strength (scanner), forecast (full ML), insider, congress, options,
      peers, historical_analog, thesis (composes everything).
    Returns a dict of enrichment fields suitable for shallow-merging into the
    fast dossier on the frontend.
    """
    cache_key = ("idea_enrichment", symbol, asset_class, strategy)
    cached = _cached(cache_key, ttl=DOSSIER_CACHE_TTL)
    if cached:
        return dict(cached)

    profile = scanner.PROFILE_DEFAULTS.copy()
    weights = scanner.STRATEGY_WEIGHTS[strategy]
    yf_symbol = symbol if asset_class == "stock" else f"{symbol}-USD"
    sector = (fast.get("snapshot") or {}).get("sector")

    started = time.time()

    # Heterogeneous fan-out via _run_jobs_parallel (safe_executor daemon
    # threads). Each thunk goes through _safe_call, so a slot is None on
    # failure — same as the old future-based collection.
    jobs = {}
    if asset_class == "crypto":
        jobs["score"] = lambda: _safe_call(scanner._score_crypto, symbol, profile, weights)
        coin_id = _resolve_crypto_id(symbol)
        if coin_id:
            jobs["crypto_extras"] = lambda: _safe_call(fetcher.get_crypto_extras, coin_id)
    else:
        jobs["score"] = lambda: _safe_call(scanner._score_equity, symbol, profile, weights)
        jobs["insider"] = lambda: _safe_call(sec_edgar.get_form4_transactions, symbol, 30)
        jobs["congress"] = lambda: _safe_call(congress_module.get_trades_for_ticker, symbol, 180)
        jobs["options"] = lambda: _safe_call(fetcher.get_unusual_options_flow, symbol)
        jobs["peers"] = lambda: _safe_call(_compute_peers, symbol, sector)
    jobs["ml"] = lambda: _safe_call(ml_forecast.ml_forecast, yf_symbol)
    jobs["analog"] = lambda: _safe_call(_compute_historical_analog, yf_symbol)
    # Decision-support fetches that don't depend on scanner/ML output
    jobs["portfolio_value"] = lambda: _safe_call(_portfolio_total_value)
    jobs["correlation"] = lambda: _safe_call(_compute_correlation_to_holdings, symbol, asset_class)

    got = _run_jobs_parallel(jobs, max_workers=8, prefix="idea-enrich")
    score_result = got.get("score")
    ml_result = got.get("ml")
    insider_txns = got.get("insider") or []
    congress_trades = got.get("congress") or []
    options_flow = got.get("options")
    peers_block = got.get("peers")
    analog = got.get("analog")
    crypto_extras = got.get("crypto_extras")
    portfolio_value = got.get("portfolio_value") or 0.0
    correlation = got.get("correlation") or {"available": False}

    strength = _build_strength_block(score_result, strategy)
    forecast = _build_forecast_block(ml_result)
    insider = _build_insider_block(insider_txns or [])
    congress = _build_congress_block(congress_trades or [])
    options_block = _build_options_block(options_flow or {})

    # ── Decision support — sizing + trade plan need price/forecast/risk ──
    snapshot = fast.get("snapshot") or {}
    risk_block = fast.get("risk") or {}
    sizing = _compute_sizing(
        price=snapshot.get("price"),
        vol_pct=risk_block.get("annual_volatility_pct"),
        portfolio_value=portfolio_value,
        stop_pct=None,  # auto-derive from vol
    )
    trade_plan = _compute_trade_plan(snapshot, forecast, risk_block)

    # Compose thesis using everything (fast + enrichment)
    opportunity = fast.get("opportunity") or {}
    social = fast.get("social") or {}
    events = fast.get("events") or {}
    headlines = fast.get("headlines") or []

    thesis_input = {
        "symbol": symbol,
        "name": snapshot.get("name"),
        "asset_class": asset_class,
        "price": snapshot.get("price"),
        "sector": snapshot.get("sector"),
        "signal": strength.get("signal"),
        "composite": strength.get("composite_score"),
        "strategy": strategy,
        "sub_scores": strength.get("sub_scores") or {},
        "early_signals": strength.get("early_signals") or [],
        "ml_signal": forecast.get("ml_signal"),
        "forecast_pct": forecast.get("forecast_pct"),
        "regime": forecast.get("regime"),
        "narrative_phase": opportunity.get("narrative_phase"),
        "dominant_narrative": opportunity.get("dominant_narrative"),
        "velocity_label": opportunity.get("velocity_label"),
        "contrarian_signal": opportunity.get("contrarian_signal"),
        "key_metrics": {
            "pe_ratio": snapshot.get("pe_ratio"),
            "forward_pe": snapshot.get("forward_pe"),
            "dividend_yield": snapshot.get("dividend_yield"),
            "beta": snapshot.get("beta"),
            "short_percent_float": snapshot.get("short_percent_float"),
            "annual_volatility_pct": risk_block.get("annual_volatility_pct"),
            "sharpe": risk_block.get("sharpe"),
        },
        "headlines": [h["title"] for h in headlines],
        "social": social,
        "insider": insider,
        "congress": congress,
        "options_flow": options_block,
        "events": events,
        "peers": peers_block,
        "historical_analog": analog,
    }
    thesis = ai_summarizer.generate_idea_thesis(thesis_input)

    elapsed = round((time.time() - started) * 1000)

    enrichment = {
        "strength": strength,
        "forecast": forecast,
        "insider": insider,
        "congress": congress,
        "options_flow": options_block,
        "peers": peers_block,
        "historical_analog": analog,
        "crypto_extras": crypto_extras,
        "sizing": sizing,
        "correlation": correlation,
        "trade_plan": trade_plan,
        "thesis": thesis,
        "enrichment_pending": False,
        "enrichment_computed_in_ms": elapsed,
    }
    _set_cache(cache_key, enrichment, ttl=DOSSIER_CACHE_TTL)
    return enrichment


def _build_dossier(symbol: str, asset_class: str, source: str, strategy: str):
    """
    Full dossier (fast + enrichment merged). Used by the pre-warmer to
    populate the persistent cache and by callers that want the complete
    blocking compute (no streaming).
    """
    cache_key = ("idea_dossier", symbol, asset_class, strategy)
    cached = _cached(cache_key, ttl=DOSSIER_CACHE_TTL)
    if cached:
        cached = dict(cached)
        cached["source"] = source
        cached["from_cache"] = True
        return cached

    fast = _build_fast_dossier(symbol, asset_class, source, strategy)
    enrichment = _build_enrichment(symbol, asset_class, strategy, fast)

    merged = dict(fast)
    merged.update(enrichment)
    merged["from_cache"] = False
    merged["computed_in_ms"] = (
        (fast.get("fast_computed_in_ms") or 0) + (enrichment.get("enrichment_computed_in_ms") or 0)
    )
    _set_cache(cache_key, merged, ttl=DOSSIER_CACHE_TTL)
    return merged


# ── Block builders shared by fast + enrichment ──────────────────────────────

def _build_snapshot_block(symbol, asset_class, quote, fundamentals):
    if asset_class == "crypto":
        return {
            "symbol": symbol,
            "name": (quote or {}).get("name", symbol),
            "price": (quote or {}).get("price"),
            "change_24h": (quote or {}).get("change_24h"),
            "change_7d": (quote or {}).get("change_7d"),
            "change_30d": (quote or {}).get("change_30d"),
            "market_cap": (quote or {}).get("market_cap"),
            "volume_24h": (quote or {}).get("volume_24h"),
            "rank": (quote or {}).get("market_cap_rank"),
            "ath_change_pct": (quote or {}).get("ath_change_pct"),
            "asset_class": "crypto",
            "sector": "Crypto",
        }
    return {
        "symbol": symbol,
        "name": (fundamentals or {}).get("name", symbol),
        "price": (quote or {}).get("price"),
        "change_pct": (quote or {}).get("change_pct"),
        "day_high": (quote or {}).get("day_high"),
        "day_low": (quote or {}).get("day_low"),
        "fifty_two_week_high": (quote or {}).get("fifty_two_week_high"),
        "fifty_two_week_low": (quote or {}).get("fifty_two_week_low"),
        "fifty_day_avg": (fundamentals or {}).get("50d_avg"),
        "two_hundred_day_avg": (fundamentals or {}).get("200d_avg"),
        "market_cap": (fundamentals or {}).get("market_cap") or (quote or {}).get("market_cap"),
        "sector": (fundamentals or {}).get("sector"),
        "industry": (fundamentals or {}).get("industry"),
        "pe_ratio": (fundamentals or {}).get("pe_ratio"),
        "forward_pe": (fundamentals or {}).get("forward_pe"),
        "ps_ratio": (fundamentals or {}).get("ps_ratio"),
        "pb_ratio": (fundamentals or {}).get("pb_ratio"),
        "dividend_yield": (fundamentals or {}).get("dividend_yield"),
        "beta": (fundamentals or {}).get("beta"),
        "short_ratio": (fundamentals or {}).get("short_ratio"),
        "short_percent_float": (fundamentals or {}).get("short_percent_float"),
        "analyst_target": (fundamentals or {}).get("analyst_target"),
        "recommendation": (fundamentals or {}).get("recommendation"),
        "asset_class": "stock",
    }


def _build_opportunity_block(narrative):
    if not narrative:
        return {}
    return {
        "dominant_narrative": narrative.get("dominant_narrative"),
        "narrative_phase": narrative.get("narrative_phase"),
        "phase_detail": narrative.get("phase_detail"),
        "velocity_score": narrative.get("velocity_score"),
        "velocity_label": narrative.get("velocity_label"),
        "contrarian_signal": narrative.get("contrarian_signal"),
        "contrarian_detail": narrative.get("contrarian_detail"),
        "article_count": narrative.get("article_count", 0),
    }


def _build_strength_block(score_result, strategy):
    composite = (score_result or {}).get("composite") or 0
    signal = (score_result or {}).get("signal") or "HOLD"
    sub_scores = (score_result or {}).get("scores") or {}
    early_signals = (score_result or {}).get("early_signals") or []
    return {
        "composite_score": round(float(composite), 1),
        "signal": signal,
        "sub_scores": {k: round(float(v), 1) for k, v in sub_scores.items() if v is not None},
        "early_signals": early_signals[:5],
        "strategy_lens": strategy.upper(),
    }


def _build_forecast_block(ml_result):
    if not ml_result:
        return {}
    if ml_result.get("error"):
        return {"error": ml_result.get("error")}
    trend = ml_result.get("trend_forecast") or {}
    rf = ml_result.get("rf_classifier") or {}
    regime = ml_result.get("regime") or {}
    mr = ml_result.get("mean_reversion") or {}
    return {
        "horizon_days": trend.get("days_ahead", 30),
        "current_price": trend.get("current_price"),
        "forecast_price": trend.get("forecast_price"),
        "forecast_pct": trend.get("forecast_pct"),
        "upper_ci": trend.get("upper_ci"),
        "lower_ci": trend.get("lower_ci"),
        "trend_short": trend.get("trend_short"),
        "trend_mid": trend.get("trend_mid"),
        "regime": regime.get("current_regime"),
        "days_in_regime": regime.get("days_in_regime"),
        "prob_up_20d": rf.get("prob_up_20d"),
        "ml_signal": ml_result.get("composite_signal"),
        "ml_confidence": rf.get("confidence"),
        "mean_reversion_signal": mr.get("signal"),
        "zscore": mr.get("zscore"),
        "path": (trend.get("path") or [])[:30],
    }


def _build_headlines_block(news):
    return [
        {
            "title": n.get("title"),
            "source": n.get("source"),
            "url": n.get("url"),
            "published": n.get("published"),
        }
        for n in (news or [])[:3]
        if n.get("title")
    ]


# ── Peer comparison ─────────────────────────────────────────────────────────
# Hand-curated sector → peer-tickers map. Lets us pick comparables without
# fetching fundamentals for the entire universe just to read sectors.

SECTOR_PEERS = {
    "Technology": [
        "AAPL", "MSFT", "NVDA", "GOOGL", "META", "AMD", "AVGO", "ORCL",
        "ADBE", "CRM", "INTC", "CSCO", "QCOM", "TXN", "INTU", "AMAT",
        "MU", "LRCX", "KLAC", "ADI", "NOW", "PANW", "CRWD", "NET", "DDOG",
        "SNOW", "ZS", "MDB", "ANET", "SMCI", "ARM",
    ],
    "Healthcare": [
        "UNH", "JNJ", "LLY", "ABBV", "MRK", "PFE", "TMO", "ABT", "DHR",
        "BMY", "AMGN", "GILD", "CVS", "CI", "ELV", "ISRG", "REGN", "VRTX",
    ],
    "Financial Services": [
        "JPM", "BAC", "WFC", "GS", "MS", "C", "BLK", "SCHW", "AXP",
        "V", "MA", "USB", "PNC", "TFC", "SPGI", "CME", "ICE",
    ],
    "Financials": [
        "JPM", "BAC", "WFC", "GS", "MS", "C", "BLK", "SCHW", "AXP",
        "V", "MA", "USB", "PNC", "TFC", "SPGI", "CME", "ICE",
    ],
    "Consumer Cyclical": [
        "AMZN", "TSLA", "HD", "NKE", "MCD", "SBUX", "TGT", "LOW", "BKNG",
        "ABNB", "UBER", "DIS", "CMCSA", "MELI", "DASH", "RBLX",
    ],
    "Consumer Defensive": [
        "WMT", "COST", "PG", "KO", "PEP", "PM", "MO", "MDLZ",
    ],
    "Communication Services": [
        "GOOGL", "META", "DIS", "NFLX", "CMCSA", "T", "VZ", "TMUS", "CHTR",
    ],
    "Energy": ["XOM", "CVX", "COP", "SLB", "EOG", "OXY", "PSX", "MPC"],
    "Industrials": ["BA", "CAT", "GE", "HON", "UPS", "RTX", "LMT", "DE", "MMM", "UNP"],
    "Basic Materials": ["LIN", "FCX", "NEM", "DOW", "NUE", "APD"],
    "Utilities": ["NEE", "DUK", "SO", "AEP", "D"],
    "Real Estate": ["O", "AMT", "PLD", "CCI", "SPG", "AVB", "WPC", "STAG"],
}


def _compute_peers(symbol: str, sector: Optional[str]):
    """
    Pick 4 sector peers, fetch lightweight metrics for each in parallel,
    and rank the picked symbol on key dimensions.
    """
    if not sector:
        return {"available": False, "reason": "No sector for symbol"}

    peers_pool = SECTOR_PEERS.get(sector) or SECTOR_PEERS.get(sector.title()) or []
    candidates = [p for p in peers_pool if p.upper() != symbol.upper()][:6]
    if not candidates:
        return {"available": False, "reason": "No peer tickers for sector " + sector}

    rows = []  # one row per ticker (including the picked symbol)
    all_symbols = [symbol] + candidates

    def _row_for(sym, q, f):
        if not q.get("price"):
            return None
        return {
            "symbol": sym,
            "is_pick": sym.upper() == symbol.upper(),
            "name": f.get("name") or sym,
            "price": q.get("price"),
            "change_pct": q.get("change_pct"),
            "market_cap": f.get("market_cap") or q.get("market_cap"),
            "pe_ratio": f.get("pe_ratio"),
            "forward_pe": f.get("forward_pe"),
            "ps_ratio": f.get("ps_ratio"),
            "dividend_yield": f.get("dividend_yield"),
            "revenue_growth": f.get("revenue_growth"),
            "earnings_growth": f.get("earnings_growth"),
            "return_on_equity": f.get("return_on_equity"),
            "profit_margin": f.get("profit_margin"),
            "beta": f.get("beta"),
        }

    # One task per symbol (quote + fundamentals) on safe_executor's daemon
    # threads. parallel_map preserves input order, so rows come back in
    # all_symbols order like the old ordered future_map did, and it falls
    # back to a serial loop by itself when threads can't be spawned.
    def _peer_row(sym):
        q = _safe_call(fetcher.get_quote, sym) or {}
        f = _safe_call(fetcher.get_fundamentals, sym) or {}
        return _row_for(sym, q, f)

    for row in safe_executor.parallel_map(
        _peer_row, all_symbols, max_workers=6, thread_name_prefix="idea-peers",
    ):
        if row is not None:
            rows.append(row)

    if not any(r["is_pick"] for r in rows):
        return {"available": False, "reason": "Pick failed to load comparable metrics"}

    # Compute the pick's percentile rank for each metric, "lower is better" or
    # "higher is better" depending on the metric.
    METRICS = {
        # metric_key: (label, higher_is_better)
        "pe_ratio": ("P/E", False),
        "forward_pe": ("Forward P/E", False),
        "ps_ratio": ("P/S", False),
        "dividend_yield": ("Div Yield", True),
        "revenue_growth": ("Rev Growth", True),
        "earnings_growth": ("EPS Growth", True),
        "return_on_equity": ("ROE", True),
        "profit_margin": ("Margin", True),
    }

    ranks = {}
    for key, (label, higher_better) in METRICS.items():
        vals = [(r["symbol"], r.get(key)) for r in rows if isinstance(r.get(key), (int, float))]
        if len(vals) < 2:
            continue
        sorted_vals = sorted(vals, key=lambda x: x[1], reverse=higher_better)
        order = [s for s, _ in sorted_vals]
        # Locate the pick case-insensitively: `order` holds raw (possibly
        # mixed-case) symbols, which may not match symbol.upper() or symbol.
        order_upper = [str(s).upper() for s in order]
        sym_u = symbol.upper()
        if sym_u not in order_upper:
            continue
        pick_pos = order_upper.index(sym_u)
        ranks[key] = {
            "label": label,
            "rank": pick_pos + 1,
            "of": len(order),
            "higher_better": higher_better,
            "pick_value": next((v for s, v in vals if s.upper() == symbol.upper()), None),
            "best_value": sorted_vals[0][1],
            "best_symbol": sorted_vals[0][0],
        }

    # Aggregate verdict: count metrics where pick is in top half
    top_half_count = sum(1 for r in ranks.values() if r["rank"] <= max(1, r["of"] // 2))
    if ranks:
        top_half_pct = top_half_count / len(ranks) * 100
        if top_half_pct >= 70:
            verdict = "BEST-IN-CLASS"
        elif top_half_pct >= 50:
            verdict = "ABOVE PEERS"
        elif top_half_pct >= 30:
            verdict = "MIXED VS PEERS"
        else:
            verdict = "LAGGARD"
    else:
        verdict = "INSUFFICIENT DATA"

    return {
        "available": True,
        "sector": sector,
        "peer_count": len([r for r in rows if not r["is_pick"]]),
        "rows": rows,
        "ranks": ranks,
        "verdict": verdict,
        "top_half_pct": round(top_half_pct, 1) if ranks else None,
    }


def _compute_historical_analog(yf_symbol):
    """Wrapper around historical_analog.compute_historical_analog with safe import."""
    try:
        import historical_analog
        return historical_analog.compute_historical_analog(yf_symbol)
    except Exception as e:
        logger.warning("historical_analog unavailable: %s", e)
        return {"available": False, "error": str(e)}


# ══════════════════════════════════════════════════════════════════════════════
# DECISION SUPPORT — sizing, correlation to portfolio, trade plan
# ══════════════════════════════════════════════════════════════════════════════

# Risk-per-position tiers used by the position-sizing block.
SIZING_TIERS = [
    {"label": "CONSERVATIVE", "risk_pct": 0.5},
    {"label": "MODERATE",     "risk_pct": 1.0},
    {"label": "AGGRESSIVE",   "risk_pct": 2.0},
]


def _portfolio_total_value():
    """
    Sum the current market value of all open positions. Used for sizing tiers.
    Returns 0 if no holdings are tracked yet.
    """
    try:
        holdings = db.get_portfolio() or []
    except Exception:
        return 0.0
    if not holdings:
        return 0.0
    symbols = list({h.get("symbol") for h in holdings if h.get("symbol")})
    if not symbols:
        return 0.0
    quotes = _safe_call(fetcher.get_quotes_batch, symbols) or {}
    total = 0.0
    for h in holdings:
        sym = h.get("symbol")
        shares = float(h.get("shares") or 0)
        q = quotes.get(sym) or {}
        # explicit None — a real quoted price of 0 must not silently fall back
        # to avg_cost (which would overstate the book value).
        _qp = q.get("price")
        price = _qp if _qp is not None else (h.get("avg_cost") or 0)
        total += shares * float(price or 0)
    return round(total, 2)


def _compute_sizing(price, vol_pct, portfolio_value, stop_pct):
    """
    Risk-based position sizing.

    Logic (per tier):
      max_loss_$  = portfolio_value × risk_pct / 100
      shares      = max_loss_$ / (price × stop_pct/100)
      position_$  = shares × price
      pct_of_book = position_$ / portfolio_value

    Returns a list of tiers, plus a `notes` field explaining the math.
    """
    if not price or price <= 0 or not portfolio_value:
        return {"available": False, "reason": "Need portfolio value + price to size"}

    # Default stop_pct: derived from volatility — 1× annualized vol clamped 5–25%.
    # Higher vol → wider stop → smaller position.
    if not stop_pct:
        if vol_pct and vol_pct > 0:
            stop_pct = max(5.0, min(25.0, vol_pct / 4))  # quarter of annual vol
        else:
            stop_pct = 8.0  # generic default

    tiers = []
    for tier in SIZING_TIERS:
        max_loss = portfolio_value * (tier["risk_pct"] / 100.0)
        per_share_risk = price * (stop_pct / 100.0)
        shares = max_loss / per_share_risk if per_share_risk > 0 else 0
        # Cap position at 10% of book regardless of vol — sanity guard
        position_value = shares * price
        cap = portfolio_value * 0.10
        capped = position_value > cap
        if capped:
            position_value = cap
            shares = position_value / price
        tiers.append({
            "label": tier["label"],
            "risk_pct": tier["risk_pct"],
            "max_loss_dollars": round(max_loss, 0),
            "shares": round(shares, 2 if price < 50 else 0),
            "position_value": round(position_value, 0),
            "pct_of_book": round(position_value / portfolio_value * 100, 2),
            "capped_at_10pct": capped,
        })

    return {
        "available": True,
        "portfolio_value": portfolio_value,
        "vol_pct": round(vol_pct, 1) if vol_pct else None,
        "stop_pct": round(stop_pct, 1),
        "tiers": tiers,
        "notes": (
            f"Stop set at {stop_pct:.1f}% (¼ of annualized vol). "
            f"Each tier risks the listed % of book if the stop trips. "
            f"Positions capped at 10% of book for concentration safety."
        ),
    }


def _compute_correlation_to_holdings(symbol, asset_class):
    """
    For each portfolio holding, compute its 3-month return correlation with
    the picked symbol. Classify the pick as DIVERSIFIES / NEUTRAL / CONCENTRATES.
    Skipped for crypto picks (correlation across asset classes is misleading).
    """
    if asset_class != "stock":
        return {"available": False, "reason": "Correlation analysis is stocks-only"}

    try:
        holdings = db.get_portfolio() or []
    except Exception as e:
        return {"available": False, "reason": f"Portfolio unavailable: {e}"}

    holding_symbols = sorted({
        h.get("symbol") for h in holdings
        if h.get("symbol") and h.get("symbol") != symbol
        and (h.get("asset_type") or "stock").lower() == "stock"
    })
    if not holding_symbols:
        return {
            "available": False,
            "reason": "No stock holdings to compare against",
        }

    syms = [symbol] + holding_symbols
    matrix = _safe_call(fetcher.get_correlation_matrix, syms, "3mo") or {}
    pairs = matrix.get("matrix") or {}
    pick_row = pairs.get(symbol) or {}
    if not pick_row:
        return {"available": False, "reason": "Correlation matrix unavailable"}

    rows = []
    for h_sym in holding_symbols:
        corr = pick_row.get(h_sym)
        if corr is None:
            continue
        if corr >= 0.7:
            label = "HIGH"
        elif corr >= 0.4:
            label = "MODERATE"
        elif corr >= 0:
            label = "LOW"
        else:
            label = "NEGATIVE"
        rows.append({"symbol": h_sym, "corr": round(corr, 2), "label": label})

    if not rows:
        return {"available": False, "reason": "Could not compute pairwise correlations"}

    rows.sort(key=lambda r: r["corr"], reverse=True)
    avg_corr = sum(r["corr"] for r in rows) / len(rows)
    high_count = sum(1 for r in rows if r["corr"] >= 0.7)

    if avg_corr >= 0.6 or high_count >= 3:
        verdict = "CONCENTRATES"
        verdict_detail = (
            f"Average correlation {avg_corr:.2f} with {high_count} high-corr "
            f"holdings — adding {symbol} would reinforce existing exposure."
        )
    elif avg_corr <= 0.3:
        verdict = "DIVERSIFIES"
        verdict_detail = (
            f"Average correlation only {avg_corr:.2f} — {symbol} would "
            "meaningfully diversify the book."
        )
    else:
        verdict = "NEUTRAL"
        verdict_detail = (
            f"Average correlation {avg_corr:.2f} — modest portfolio impact."
        )

    return {
        "available": True,
        "verdict": verdict,
        "verdict_detail": verdict_detail,
        "average_correlation": round(avg_corr, 2),
        "high_correlation_count": high_count,
        "holdings_count": len(rows),
        "rows": rows[:10],  # Top 10 by correlation
        "period": "3mo",
    }


def _compute_trade_plan(snapshot, forecast, risk):
    """
    Derive a concrete trade plan: entry zone, stop loss, two profit targets,
    and a risk:reward ratio. Pure math from data already in the dossier.
    """
    price = (snapshot or {}).get("price")
    if not price:
        return {"available": False, "reason": "No price"}

    forecast_price = (forecast or {}).get("forecast_price")
    upper_ci = (forecast or {}).get("upper_ci")
    lower_ci = (forecast or {}).get("lower_ci")
    annual_vol = (risk or {}).get("annual_volatility_pct") or 25.0
    fifty_two_low = (snapshot or {}).get("fifty_two_week_low")
    sma_50 = (snapshot or {}).get("fifty_day_avg")
    sma_200 = (snapshot or {}).get("two_hundred_day_avg")

    # Entry zone: ±0.5σ daily (annual_vol / sqrt(252)) around current price.
    # This is the typical 1-day range — gives a few-day window to scale in.
    daily_vol = annual_vol / (252 ** 0.5)
    entry_low = round(price * (1 - daily_vol / 100 * 0.5), 2)
    entry_high = round(price * (1 + daily_vol / 100 * 0.5), 2)

    # Stop loss candidates: max of (lower_ci, sma_50 if below price, recent
    # low support, fixed 1×ATR). Take the closest one to current price that
    # is still meaningfully below — we want a tight, defensible stop.
    candidates = []
    if lower_ci and lower_ci < price:
        candidates.append(("ML lower-CI", round(lower_ci, 2)))
    if sma_50 and sma_50 < price:
        candidates.append(("50d SMA", round(sma_50, 2)))
    # Fixed ATR-style fallback: 1× monthly vol below price
    monthly_vol = annual_vol / (12 ** 0.5)
    atr_stop = round(price * (1 - monthly_vol / 100 * 1.5), 2)
    candidates.append(("1.5× monthly vol", atr_stop))

    # Pick the candidate closest to (but below) entry_low — the tightest defensible stop
    valid = [(label, lvl) for (label, lvl) in candidates if lvl < entry_low]
    if not valid:
        return {"available": False, "reason": "No valid stop-loss candidate below entry"}
    stop_label, stop_loss = max(valid, key=lambda x: x[1])  # closest stop = highest value still below entry
    stop_distance_pct = round((price - stop_loss) / price * 100, 2)
    risk_per_share = price - stop_loss

    # Profit targets:
    # T1: forecast_price (or +1× risk if forecast missing)
    # T2: upper_ci (or +2× risk)
    if forecast_price and forecast_price > price:
        t1 = round(forecast_price, 2)
        t1_label = "ML target (30d)"
    else:
        t1 = round(price + risk_per_share, 2)
        t1_label = "+1× risk (1:1 R:R)"
    if upper_ci and upper_ci > t1:
        t2 = round(upper_ci, 2)
        t2_label = "ML upper-CI"
    else:
        t2 = round(price + risk_per_share * 2, 2)
        t2_label = "+2× risk (2:1 R:R)"

    reward1 = t1 - price
    reward2 = t2 - price
    rr1 = round(reward1 / risk_per_share, 2) if risk_per_share > 0 else None
    rr2 = round(reward2 / risk_per_share, 2) if risk_per_share > 0 else None

    if rr1 is not None:
        if rr1 >= 2.0:
            quality = "EXCELLENT"
        elif rr1 >= 1.0:
            quality = "GOOD"
        elif rr1 >= 0.5:
            quality = "MARGINAL"
        else:
            quality = "POOR"
    else:
        quality = "UNKNOWN"

    return {
        "available": True,
        "entry": {
            "low": entry_low,
            "high": entry_high,
            "current": round(price, 2),
        },
        "stop": {
            "price": stop_loss,
            "label": stop_label,
            "distance_pct": stop_distance_pct,
            "risk_per_share": round(risk_per_share, 2),
        },
        "target_1": {
            "price": t1,
            "label": t1_label,
            "reward_pct": round(reward1 / price * 100, 2),
            "rr": rr1,
        },
        "target_2": {
            "price": t2,
            "label": t2_label,
            "reward_pct": round(reward2 / price * 100, 2),
            "rr": rr2,
        },
        "rr_quality": quality,
        "notes": (
            f"Entry zone is ½ daily-vol band around spot. "
            f"Stop at {stop_label} ({stop_distance_pct:.1f}% below). "
            f"T1 = {t1_label}, T2 = {t2_label}."
        ),
    }


# ══════════════════════════════════════════════════════════════════════════════
# PUBLIC API
# ══════════════════════════════════════════════════════════════════════════════

def generate_random_idea(
    asset_class: Optional[str] = None,
    sector: Optional[str] = None,
    strategy: Optional[str] = None,
    min_score: Optional[int] = None,
    exclude: Optional[List[str]] = None,
    discovery_mode: str = "curated",
):
    """
    Generate one random investment idea dossier.

    Args:
        asset_class: "stock", "crypto", "etf", or None/"any"
        sector: Optional sector filter (applied post-pick for equities)
        strategy: "growth", "value", "income", "momentum", or None for random
        min_score: Reject picks below this composite score, re-roll up to MAX_REROLLS times
        exclude: Symbols to skip (typically already-shown picks in this session)
        discovery_mode: "curated" (default, ~180 names) or "full" (~10k SEC + 500 crypto)

    Returns:
        Dossier dict, or {"error": "..."} on failure.
    """
    discovery_mode = (discovery_mode or "curated").lower()
    if discovery_mode not in ("curated", "full"):
        discovery_mode = "curated"

    pools = _filtered_universe(asset_class, sector, discovery_mode=discovery_mode)
    if not any(pools.values()):
        return {"error": "No symbols match the requested filters."}

    chosen_strategy = strategy if strategy in scanner.STRATEGY_WEIGHTS else _random_strategy()
    threshold = MIN_INTERESTING_SCORE if min_score is None else min_score
    excluded_set = set((exclude or []))

    # ── 1. Try the pre-warmed pool first (instant if it covers our filters) ──
    if discovery_mode == "curated":  # only the warmer pool is worth checking in curated mode
        warmed = _try_warmed_pick(asset_class, sector, chosen_strategy, threshold, excluded_set)
        if warmed is not None:
            warmed["discovery_mode"] = discovery_mode
            warmed["from_warmed_pool"] = True
            return warmed

    # ── 2. Cold pick — fast dossier only; UI calls /api/ideas/enrich next ──
    max_attempts = (MAX_REROLLS * 3) if discovery_mode == "full" else MAX_REROLLS
    last_dossier = None
    for attempt in range(max_attempts + 1):
        pick = _weighted_pick(pools, exclude=excluded_set)
        if not pick:
            break
        symbol, ac, source = pick
        excluded_set.add(symbol)

        dossier = _build_fast_dossier(symbol, ac, source, chosen_strategy)

        snap = dossier.get("snapshot") or {}
        if snap.get("price") in (None, 0):
            continue

        last_dossier = dossier

        if sector and ac == "stock":
            sec = snap.get("sector") or ""
            if sec.lower() != sector.lower():
                continue

        # We don't have a strength score yet — that's in enrichment. Skip the
        # min_score gate here for fast picks (it's still enforced for warmed
        # picks since those have full dossiers). User can re-roll if needed.
        dossier["discovery_mode"] = discovery_mode
        dossier["min_score_filter"] = threshold
        return dossier

    if last_dossier is not None:
        last_dossier["below_threshold"] = True
        last_dossier["discovery_mode"] = discovery_mode
        return last_dossier

    return {"error": "Could not generate idea after multiple attempts.",
            "discovery_mode": discovery_mode}


def _try_warmed_pick(asset_class, sector, strategy, min_score, exclude_set):
    """
    Try to find a fresh warmed dossier matching the filters.
    Returns a dossier dict or None if no suitable warmed pick exists.
    """
    try:
        import idea_pool_warmer
    except Exception:
        return None
    try:
        warmed_rows = idea_pool_warmer.list_warmed_symbols(asset_class=asset_class)
    except Exception as e:
        logger.warning("warmer pool query failed: %s", e)
        return None
    if not warmed_rows:
        return None

    candidates = []
    for row in warmed_rows:
        if row.get("symbol") in exclude_set:
            continue
        if row.get("strategy") != strategy:
            continue
        if (row.get("composite_score") or 0) < min_score:
            continue
        if sector and (row.get("sector") or "").lower() != sector.lower():
            continue
        candidates.append(row)
    if not candidates:
        return None

    chosen_row = random.choice(candidates)
    full = idea_pool_warmer.get_warmed_dossier(
        chosen_row["symbol"], chosen_row.get("asset_class") or "stock", strategy
    )
    if not full:
        return None
    full = dict(full)
    full["source"] = "warmed-pool"
    full["enrichment_pending"] = False
    return full


def enrich_idea(symbol: str, asset_class: str, strategy: str):
    """
    Public entry point used by /api/ideas/enrich/<symbol>.
    Returns the enrichment dict (strength, forecast, insider, congress,
    options_flow, peers, historical_analog, crypto_extras, thesis).
    """
    asset_class = (asset_class or "stock").lower()
    strategy = (strategy or "growth").lower()
    if strategy not in scanner.STRATEGY_WEIGHTS:
        strategy = "growth"

    # Re-derive the fast dossier (cached, so cheap) so enrichment has context
    # without requiring the frontend to round-trip the whole payload.
    fast = _build_fast_dossier(symbol, asset_class, source="enrich", strategy=strategy)
    return _build_enrichment(symbol, asset_class, strategy, fast)


def _random_strategy():
    strategies, weights = zip(*STRATEGY_WEIGHTS)
    return random.choices(strategies, weights=weights, k=1)[0]


def list_universe(discovery_mode: str = "curated"):
    """
    For the UI — what symbols/buckets are in the picker.
    `discovery_mode="full"` reports counts from the lazy-loaded full universe
    (which may trigger a fetch on first call; cached for 24h after).
    """
    discovery_mode = (discovery_mode or "curated").lower()

    curated_eq = len(EQUITY_UNIVERSE)
    curated_cx = len(CRYPTO_UNIVERSE)
    full_eq = None
    full_cx = None

    if discovery_mode == "full":
        try:
            full_eq = len(get_full_equity_universe())
        except Exception:
            full_eq = curated_eq
        try:
            full_cx = len(get_full_crypto_universe())
        except Exception:
            full_cx = curated_cx

    return {
        "equity_count": full_eq if full_eq is not None else curated_eq,
        "crypto_count": full_cx if full_cx is not None else curated_cx,
        "curated_equity_count": curated_eq,
        "curated_crypto_count": curated_cx,
        "watchlist_count": len(_watchlist_symbols()),
        "strategies": list(scanner.STRATEGY_WEIGHTS.keys()),
        "sectors": scanner.SECTORS,
        "discovery_mode": discovery_mode,
    }
