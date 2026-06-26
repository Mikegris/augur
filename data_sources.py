"""
data_sources.py — No-key Tier 1 external data integrations.

Each provider exposes one or more fetch functions with TTL caching.
All sources here require NO API key, NO OAuth — only a polite User-Agent.

Providers:
    Senate STOCK Act     — github.com/timothycarambat/senate-stock-watcher-data
    House STOCK Act      — housestockwatcher.com (bulk JSON)
    GDELT 2.0 DOC API    — api.gdeltproject.org
    DefiLlama            — api.llama.fi
    mempool.space        — mempool.space/api
    Treasury fiscaldata  — fiscaldata.treasury.gov
    CBOE put/call        — cboe.com daily CSV
    FinanceDatabase      — github.com/JerBouma/FinanceDatabase
    SEC EDGAR XBRL       — data.sec.gov/api/xbrl
"""

import bz2
import csv
import io
import logging
import threading
import time
from datetime import datetime, timezone

import requests

log = logging.getLogger("augur.datasources")

# SEC's fair-access policy rejects reserved/.local contact domains with 429.
# Use a routable contact so data.sec.gov XBRL fetches aren't silently throttled.
UA = "AUGUR/1.0 (contact: augur-research@gmail.com)"
HEADERS = {"User-Agent": UA, "Accept": "application/json"}

# ── Shared TTL cache ────────────────────────────────────────────────
_cache = {}
_cache_lock = threading.Lock()


def _cache_get(key):
    with _cache_lock:
        item = _cache.get(key)
        if item and item[1] > time.time():
            return item[0]
        return None


def _cache_set(key, value, ttl):
    with _cache_lock:
        _cache[key] = (value, time.time() + ttl)
        if len(_cache) > 200:
            now = time.time()
            for k in [k for k, v in _cache.items() if v[1] < now][:50]:
                _cache.pop(k, None)
            # If everything is still fresh, the expired sweep frees nothing and
            # the dict grows unbounded. Evict the soonest-to-expire entries to
            # bring it back under the cap.
            if len(_cache) > 200:
                for k, _ in sorted(_cache.items(), key=lambda kv: kv[1][1])[: len(_cache) - 200]:
                    _cache.pop(k, None)


_session = None
_session_lock = threading.Lock()


def _get_session():
    """Lazily build the shared requests.Session, fully initialized, under a
    lock. Double-checked so the common (already-built) path stays lock-free,
    while concurrent first callers can't each build a Session — one of which
    would be used mid-init (headers not yet applied) and leak its pool."""
    global _session
    if _session is None:
        with _session_lock:
            if _session is None:
                s = requests.Session()
                s.headers.update(HEADERS)
                _session = s
    return _session


_NEG_SENTINEL = object()  # cached value meaning "upstream failed, don't retry yet"


def _get(url, *, ttl=900, params=None, timeout=20, json_resp=True, headers=None):
    """Cached GET. Returns parsed JSON (default) or raw text.

    Negative results (5xx, 429, network errors) are cached for a short
    window so the dashboard's per-second pollers don't hammer providers
    that are already failing — that's how we used to end up blacklisted
    from DefiLlama / GDELT for an hour after a single bad minute.
    """
    cache_key = (url, tuple(sorted((params or {}).items())), json_resp)
    hit = _cache_get(cache_key)
    if hit is _NEG_SENTINEL:
        return None
    if hit is not None:
        return hit
    session = _get_session()
    try:
        r = session.get(url, params=params, timeout=timeout, headers=headers)
        # Distinguish "transient throttle" from "permanent shape change".
        if r.status_code == 429:
            ra = r.headers.get("Retry-After")
            try:
                neg_ttl = float(ra) if ra else 60.0
            except (TypeError, ValueError):
                neg_ttl = 60.0
            _cache_set(cache_key, _NEG_SENTINEL, min(max(neg_ttl, 30.0), 600.0))
            log.warning("fetch 429 %s — cooling down %.0fs", url, neg_ttl)
            return None
        if r.status_code in (403, 404, 451):
            # Endpoint moved / removed. Cache longer; a one-minute retry
            # storm won't fix a renamed protocol slug.
            _cache_set(cache_key, _NEG_SENTINEL, 1800)
            log.warning("fetch %d %s", r.status_code, url)
            return None
        r.raise_for_status()
        out = r.json() if json_resp else r.text
        _cache_set(cache_key, out, ttl)
        return out
    except Exception as e:
        log.warning("fetch failed %s: %s", url, e)
        # Short TTL on generic failures so transient blips recover quickly.
        _cache_set(cache_key, _NEG_SENTINEL, 60)
        return None


# ════════════════════════════════════════════════════════════════════
# CONGRESS — Senate (timothycarambat/senate-stock-watcher-data)
# ════════════════════════════════════════════════════════════════════
SENATE_BASE = (
    "https://raw.githubusercontent.com/timothycarambat/"
    "senate-stock-watcher-data/master/aggregate"
)


def get_senate_trades(symbol=None, limit=200):
    """Senate STOCK Act disclosures.
    symbol: optional ticker filter (case-insensitive)
    """
    data = _get(f"{SENATE_BASE}/all_transactions.json", ttl=3600)
    if not isinstance(data, list):
        return []
    out = []
    sym_u = (symbol or "").upper().strip()
    for t in data:
        ticker = (t.get("ticker") or "").upper()
        if sym_u and ticker != sym_u:
            continue
        out.append({
            "chamber": "Senate",
            "name": t.get("senator") or "",
            "ticker": ticker,
            "asset_description": t.get("asset_description") or "",
            "type": t.get("type") or "",
            "amount": t.get("amount") or "",
            "date": t.get("transaction_date") or "",
            "filed": t.get("disclosure_date") or "",
            "ptr_link": t.get("ptr_link") or "",
        })
        if len(out) >= limit:
            break
    return out


# ════════════════════════════════════════════════════════════════════
# CONGRESS — House (existing PDF parser in congress.py is canonical)
# ════════════════════════════════════════════════════════════════════
# Note: housestockwatcher.com mirrors are intermittent; we keep using
# congress.py's PDF/XML parser as the primary House source.


# ════════════════════════════════════════════════════════════════════
# GDELT 2.0 DOC API
# ════════════════════════════════════════════════════════════════════
GDELT_DOC = "https://api.gdeltproject.org/api/v2/doc/doc"


def gdelt_articles(query, *, max_records=20, timespan="3d", lang="english"):
    """GDELT DOC API article search. Returns list of articles."""
    params = {
        "query": f'{query} sourcelang:{lang}',
        "mode": "ArtList",
        "format": "json",
        "maxrecords": min(max(int(max_records), 1), 250),
        "timespan": timespan,
        "sort": "DateDesc",
    }
    data = _get(GDELT_DOC, params=params, ttl=600)
    if not data or not isinstance(data, dict):
        return []
    arts = data.get("articles") or []
    return [{
        "title": a.get("title", ""),
        "url": a.get("url", ""),
        "domain": a.get("domain", ""),
        "language": a.get("language", ""),
        "seendate": a.get("seendate", ""),
        "sourcecountry": a.get("sourcecountry", ""),
        "tone": a.get("tone"),
    } for a in arts]


def gdelt_tone_timeline(query, *, timespan="2w"):
    """GDELT tone timeline — returns daily tone series for a query."""
    params = {
        "query": query,
        "mode": "TimelineTone",
        "format": "json",
        "timespan": timespan,
    }
    data = _get(GDELT_DOC, params=params, ttl=1800)
    if not data:
        return []
    _tl = [t for t in (data.get("timeline") or []) if isinstance(t, dict)]
    # Prefer the series explicitly labeled as tone (GDELT can return — or
    # reorder — multiple series); fall back to the first if none is labeled,
    # preserving the previous index-0 behavior.
    tone_entry = next(
        (t for t in _tl if "tone" in str(t.get("series", "")).lower()),
        (_tl[0] if _tl else None),
    )
    series = (tone_entry.get("data") if isinstance(tone_entry, dict) else []) or []
    return [{"date": d.get("date"), "value": d.get("value")} for d in series]


# ════════════════════════════════════════════════════════════════════
# DefiLlama — TVL, stablecoins, yields
# ════════════════════════════════════════════════════════════════════
LLAMA = "https://api.llama.fi"
LLAMA_STABLE = "https://stablecoins.llama.fi"
LLAMA_YIELDS = "https://yields.llama.fi"


def defillama_tvl_summary():
    """Top protocols by TVL + total chains TVL."""
    protos = _get(f"{LLAMA}/protocols", ttl=900) or []
    chains_raw = _get(f"{LLAMA}/v2/chains", ttl=900) or []
    if isinstance(protos, list):
        protos = sorted(
            [p for p in protos if isinstance(p.get("tvl"), (int, float))],
            key=lambda p: p["tvl"], reverse=True
        )[:25]
        protos = [{
            "name": p.get("name"),
            "category": p.get("category"),
            "chain": p.get("chain"),
            "tvl": p.get("tvl"),
            "change_1d": p.get("change_1d"),
            "change_7d": p.get("change_7d"),
            "url": p.get("url"),
            "logo": p.get("logo"),
        } for p in protos]
    else:
        protos = []

    # Compute the true total TVL across ALL chains *before* truncating to the
    # top 15 we surface in the response. The old version summed only the
    # top-15 slice and labeled it "total_tvl", under-reporting total DeFi TVL
    # by everything beyond the 15th chain.
    if isinstance(chains_raw, list):
        all_chains_with_tvl = [c for c in chains_raw if isinstance(c.get("tvl"), (int, float))]
        # Floor the sum at positive TVL: the chains endpoint can transiently
        # return negative/zero-coerced tvl that subtracts from a legitimate
        # total. Filtering to > 0 excludes those without dropping real chains.
        total = sum(float(c["tvl"]) for c in all_chains_with_tvl if float(c["tvl"]) > 0)
        chains_sorted = sorted(all_chains_with_tvl, key=lambda c: c["tvl"], reverse=True)[:15]
        chains = [{
            "name": c.get("name"),
            "tokenSymbol": c.get("tokenSymbol"),
            "tvl": c.get("tvl"),
            "gecko_id": c.get("gecko_id"),
        } for c in chains_sorted]
    else:
        chains = []
        total = 0
    return {"total_tvl": total, "chains": chains, "protocols": protos}


def defillama_stablecoins():
    """Top stablecoins by circulating supply."""
    data = _get(f"{LLAMA_STABLE}/stablecoins", params={"includePrices": "true"}, ttl=1800)
    if not data:
        return []
    coins = data.get("peggedAssets") or []
    out = []
    for c in coins[:25]:
        circ = c.get("circulating") or {}
        out.append({
            "symbol": c.get("symbol"),
            "name": c.get("name"),
            "peg": (c.get("pegMechanism") or "") + "/" + (c.get("pegType") or ""),
            "price": (c.get("price") or None),
            "circulating_usd": circ.get("peggedUSD"),
            "chains": (c.get("chains") or [])[:6],
        })
    return out


def defillama_top_yields(limit=20):
    """Top stablecoin yields across DeFi."""
    data = _get(f"{LLAMA_YIELDS}/pools", ttl=1800)
    if not data:
        return []
    pools = data.get("data") or []
    # Keep the presence check separate from the value: a pool reporting
    # apy == 0 (a parked stable) is real data, not missing data, so drop only
    # pools with no apy reading at all. The descending sort already orders any
    # genuine 0% pool last, so no top-yield pool is lost.
    pools = [p for p in pools if (p.get("stablecoin") and p.get("apy") is not None)]
    pools.sort(key=lambda p: p.get("apy") or 0, reverse=True)
    return [{
        "project": p.get("project"),
        "chain": p.get("chain"),
        "symbol": p.get("symbol"),
        "tvl_usd": p.get("tvlUsd"),
        "apy": p.get("apy"),
        "apy_base": p.get("apyBase"),
        "il_risk": p.get("ilRisk"),
    } for p in pools[:limit]]


# ════════════════════════════════════════════════════════════════════
# mempool.space — BTC stats
# ════════════════════════════════════════════════════════════════════
MEMPOOL = "https://mempool.space/api"


def mempool_btc_stats():
    """Current BTC mempool, fees, blocks."""
    fees = _get(f"{MEMPOOL}/v1/fees/recommended", ttl=120) or {}
    mempool = _get(f"{MEMPOOL}/mempool", ttl=120) or {}
    diff = _get(f"{MEMPOOL}/v1/difficulty-adjustment", ttl=600) or {}
    blocks = _get(f"{MEMPOOL}/blocks", ttl=300) or []
    tip_height = (
        blocks[0].get("height")
        if (isinstance(blocks, list) and blocks and isinstance(blocks[0], dict))
        else None
    )
    return {
        "fees": fees,
        "mempool_count": mempool.get("count"),
        "mempool_vsize": mempool.get("vsize"),
        "mempool_total_fee": mempool.get("total_fee"),
        "tip_height": tip_height,
        "difficulty_change_pct": diff.get("difficultyChange"),
        "remaining_blocks": diff.get("remainingBlocks"),
        "estimated_retarget_date": diff.get("estimatedRetargetDate"),
    }


# ════════════════════════════════════════════════════════════════════
# Treasury fiscaldata — yield curve
# ════════════════════════════════════════════════════════════════════
TREASURY = "https://api.fiscaldata.treasury.gov/services/api/fiscal_service"


def treasury_yield_curve():
    """Latest U.S. Treasury yield curve (all tenors)."""
    url = f"{TREASURY}/v2/accounting/od/avg_interest_rates"
    params = {
        "filter": "security_desc:in:(Treasury Bills,Treasury Notes,Treasury Bonds)",
        "sort": "-record_date",
        "page[size]": "20",
    }
    data = _get(url, params=params, ttl=43200)
    if not data:
        return {"as_of": None, "rates": []}
    rows = data.get("data") or []
    rates = []
    for r in rows[:20]:
        try:
            rate = float(r["avg_interest_rate_amt"]) if r.get("avg_interest_rate_amt") else None
        except (TypeError, ValueError):
            rate = None
        rates.append({
            "security": r.get("security_desc"),
            "rate": rate,
            "date": r.get("record_date"),
        })
    as_of = rates[0]["date"] if rates else None
    return {"as_of": as_of, "rates": rates}


# ════════════════════════════════════════════════════════════════════
# CBOE — VIX history (NOT put/call — see note below)
# ════════════════════════════════════════════════════════════════════
# This URL serves the CBOE VIX daily OHLC CSV. The function used to be
# misnamed `cboe_put_call_ratio` even though it has never returned P/C data.
CBOE_VIX = (
    "https://cdn.cboe.com/api/global/us_indices/daily_prices/"
    "VIX_History.csv"
)
CBOE_RATIOS = "https://www.cboe.com/us/options/market_statistics/daily/"


def cboe_vix_history():
    """Latest CBOE VIX index daily OHLC from cboe.com's CSV feed.
    Returns the most recent row as {vix_date, vix_close, vix_high, vix_low}
    or None if the CSV is unreachable. (Note: despite the legacy module
    name `cboe_put_call_ratio`, this endpoint has never carried put/call
    ratio data — it's the VIX daily history CSV.)"""
    txt = _get(CBOE_VIX, ttl=3600, json_resp=False)
    if not txt:
        return None
    last = None
    for row in csv.DictReader(io.StringIO(txt)):
        # Skip blank trailing rows (a stray newline yields all-empty values).
        if any(row.values()):
            last = row
    if not last:
        return None

    def _num(field):
        v = last.get(field)
        if not v:
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    return {
        "vix_date": last.get("DATE"),
        "vix_close": _num("CLOSE"),
        "vix_high": _num("HIGH"),
        "vix_low": _num("LOW"),
    }


def cboe_put_call_ratio(*_a, **_kw):
    """Removed: the old name was a misnomer (it fetched VIX, not the
    put/call ratio). Use `cboe_vix_history()` instead. Kept as a loud
    stub so any forgotten caller fails fast rather than silently
    receiving mislabeled VIX data."""
    raise NotImplementedError(
        "cboe_put_call_ratio() was renamed to cboe_vix_history() — it "
        "never returned put/call ratio data; the old name was misleading. "
        "Update the caller."
    )


# ════════════════════════════════════════════════════════════════════
# JerBouma/FinanceDatabase — equity universe
# ════════════════════════════════════════════════════════════════════
FINDB_BASE = (
    "https://raw.githubusercontent.com/JerBouma/FinanceDatabase/main/compression"
)
_FINDB_LOCK = threading.Lock()
_FINDB_UNIVERSE = {}  # asset -> list[dict]


def financedb_universe(asset="equities"):
    """Returns parsed JerBouma FinanceDatabase rows (bz2-compressed CSV)."""
    if asset not in ("equities", "etfs", "funds", "indices", "currencies", "cryptos", "moneymarkets"):
        asset = "equities"
    # Double-checked locking: the lock guards only the cache dict, NOT the
    # 30s download+decompress. Holding it across requests.get() serialized
    # every asset's first load behind any other in-flight load — a single slow
    # bz2 fetch froze all the others. Worst case now is two threads racing the
    # same cold asset both download (rare, idempotent), which is far cheaper
    # than a global stall.
    with _FINDB_LOCK:
        cached = _FINDB_UNIVERSE.get(asset)
    if cached is not None:
        return cached
    try:
        url = f"{FINDB_BASE}/{asset}.bz2"
        r = requests.get(url, headers=HEADERS, timeout=30)
        r.raise_for_status()
        text = bz2.decompress(r.content).decode("utf-8", errors="replace")
        rows = list(csv.DictReader(io.StringIO(text)))
    except Exception as e:
        log.warning("financedb load failed (%s): %s", asset, e)
        return []
    with _FINDB_LOCK:
        # Re-check: another thread may have populated it while we downloaded.
        existing = _FINDB_UNIVERSE.get(asset)
        if existing is not None:
            return existing
        _FINDB_UNIVERSE[asset] = rows
    return rows


def financedb_filter(asset="equities", *, country=None, sector=None, industry=None, exchange=None, limit=200):
    """Filter the FinanceDatabase universe."""
    universe = financedb_universe(asset)
    out = []
    for row in universe:
        if country and (row.get("country") or "").lower() != country.lower():
            continue
        if sector and (row.get("sector") or "").lower() != sector.lower():
            continue
        if industry and industry.lower() not in (row.get("industry") or "").lower():
            continue
        if exchange and (row.get("exchange") or "").lower() != exchange.lower():
            continue
        out.append({
            "symbol": row.get("symbol") or "",
            "name": row.get("name") or "",
            "sector": row.get("sector") or "",
            "industry_group": row.get("industry_group") or "",
            "industry": row.get("industry") or "",
            "country": row.get("country") or "",
            "exchange": row.get("exchange") or "",
            "currency": row.get("currency") or "",
            "market_cap": row.get("market_cap") or "",
        })
        if len(out) >= limit:
            break
    return out


def financedb_facets(asset="equities"):
    """Return distinct sectors / industries / countries / exchanges for filter UIs."""
    universe = financedb_universe(asset)
    sectors, industries, countries, exchanges = set(), set(), set(), set()
    for row in universe:
        if row.get("sector"): sectors.add(row["sector"])
        if row.get("industry"): industries.add(row["industry"])
        if row.get("country"): countries.add(row["country"])
        if row.get("exchange"): exchanges.add(row["exchange"])
    return {
        "sectors": sorted(s for s in sectors if s),
        "industries": sorted(i for i in industries if i),
        "countries": sorted(c for c in countries if c),
        "exchanges": sorted(e for e in exchanges if e),
        "total_symbols": len(universe),
    }


# ════════════════════════════════════════════════════════════════════
# SEC EDGAR XBRL — companyfacts
# ════════════════════════════════════════════════════════════════════
SEC_BASE = "https://data.sec.gov"


def xbrl_company_facts(cik):
    """All standardized XBRL facts for a CIK (10-digit zero-padded)."""
    if not cik:
        return None
    cik = str(cik).zfill(10)
    return _get(f"{SEC_BASE}/api/xbrl/companyfacts/CIK{cik}.json", ttl=21600)


def xbrl_key_metrics(cik):
    """Pull a small dict of headline metrics from companyfacts."""
    facts = xbrl_company_facts(cik)
    if not facts:
        return {}
    us_gaap = (facts.get("facts") or {}).get("us-gaap") or {}

    def latest(concept, unit_pref=("USD", "USD/shares", "shares", "pure")):
        node = us_gaap.get(concept)
        if not node:
            return None
        units = node.get("units") or {}
        for u in unit_pref:
            entries = units.get(u)
            if not entries:
                continue
            best = sorted(
                [e for e in entries if e.get("end")],
                key=lambda e: e["end"], reverse=True
            )
            if best:
                e = best[0]
                return {"value": e.get("val"), "end": e.get("end"), "unit": u, "form": e.get("form")}
        return None

    return {
        "entity": facts.get("entityName"),
        "revenue":      latest("Revenues") or latest("RevenueFromContractWithCustomerExcludingAssessedTax"),
        "net_income":   latest("NetIncomeLoss"),
        "assets":       latest("Assets"),
        "liabilities":  latest("Liabilities"),
        "stockholders_equity": latest("StockholdersEquity"),
        "cash":         latest("CashAndCashEquivalentsAtCarryingValue"),
        "shares_out":   latest("CommonStockSharesOutstanding") or latest("EntityCommonStockSharesOutstanding"),
        "eps_basic":    latest("EarningsPerShareBasic"),
        "eps_diluted":  latest("EarningsPerShareDiluted"),
        "rd_expense":   latest("ResearchAndDevelopmentExpense"),
        "operating_income": latest("OperatingIncomeLoss"),
    }
