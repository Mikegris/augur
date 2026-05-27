"""
synth_sectorflow.py — Multi-signal sector-rotation heatmap.

The stock screen's existing sector heatmap is price-only — green/red squares
keyed off the 11 SPDR sector ETFs and nothing else. That tells the user WHICH
sectors are rotating but not WHY or WHO is driving it. This module fuses every
signal AUGUR already collects into a per-sector composite so the rotation
narrative is legible at a glance:

  - price moves         (1d / 5d / 1mo) and relative strength vs SPY
  - narrative phase     (BULLISH / BEARISH / DEVELOPING / ...)
  - factor exposure     (today's SMB/HML/Mom return, useful for style rotation)
  - insider flow        (Finviz "latest insider trading" filtered to sector)
  - congress flow       (House PTRs aggregated by holding's sector)
  - reddit sentiment    (mention deltas + stocktwits bull ratio when present)
  - HN polarity         (slow but real — leading indicator on a few sectors)
  - Wikipedia attention (z-score of recent pageviews vs trailing baseline)
  - options PCR         (sector ETF put/call ratio — gut check on positioning)

The output is a list of 11 rows, one per GICS sector, plus an `as_of` stamp
and the leader/laggard picks. Composite score is a weighted sum of the
normalised component z-scores, clamped to [-3, +3]. Weights were picked to
keep typical days in the +-1 range; a true single-signal blow-out can push
to +-2.5 without saturating.

Everything is best-effort: any source can fail silently and the row still
renders with the remaining signals. Caching is via `cache_store.coalesce`
with a 10-minute TTL — long enough to weather Yahoo rate-limits, short
enough that an intraday rotation isn't stale.

Python 3.9 compatible.
"""

from __future__ import annotations

import logging
import math
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("augur.synth_sectorflow")

try:
    import cache_store
except Exception:  # pragma: no cover — defensive fallback
    cache_store = None

try:
    import fetcher
except Exception:  # pragma: no cover
    fetcher = None
    log.warning("synth_sectorflow: fetcher unavailable")

try:
    import finviz_data
except Exception:
    finviz_data = None

try:
    import narrative_engine
except Exception:
    narrative_engine = None

try:
    import congress as congress_mod
except Exception:
    congress_mod = None

try:
    import alt_signals
except Exception:
    alt_signals = None

try:
    import wiki_attention
except Exception:
    wiki_attention = None

try:
    import hn_sentiment
except Exception:
    hn_sentiment = None

try:
    import research_factors
except Exception:
    research_factors = None


# ─── GICS sector universe ────────────────────────────────────────────

# Sector → ETF + display + Finviz/Wikipedia hint. The Finviz "Sector" string
# below has to match what finvizfinance returns from ticker_fundament so we
# can cross-reference insider trades and (eventually) congress holdings.
SECTORS: List[Dict[str, str]] = [
    {"sector": "Technology",          "etf": "XLK",  "finviz": "Technology",                  "wiki_hint": "S&P 500"},
    {"sector": "Financials",          "etf": "XLF",  "finviz": "Financial",                   "wiki_hint": "Financial sector"},
    {"sector": "Healthcare",          "etf": "XLV",  "finviz": "Healthcare",                  "wiki_hint": "Healthcare industry"},
    {"sector": "Consumer Discretionary", "etf": "XLY", "finviz": "Consumer Cyclical",         "wiki_hint": "Consumer discretionary"},
    {"sector": "Consumer Staples",    "etf": "XLP",  "finviz": "Consumer Defensive",          "wiki_hint": "Consumer staples"},
    {"sector": "Energy",              "etf": "XLE",  "finviz": "Energy",                      "wiki_hint": "Energy industry"},
    {"sector": "Industrials",         "etf": "XLI",  "finviz": "Industrials",                 "wiki_hint": "Industrial sector"},
    {"sector": "Materials",           "etf": "XLB",  "finviz": "Basic Materials",             "wiki_hint": "Basic materials"},
    {"sector": "Utilities",           "etf": "XLU",  "finviz": "Utilities",                   "wiki_hint": "Utility"},
    {"sector": "Real Estate",         "etf": "XLRE", "finviz": "Real Estate",                 "wiki_hint": "Real estate"},
    {"sector": "Communication Services", "etf": "XLC", "finviz": "Communication Services",    "wiki_hint": "Telecommunications"},
]

# Reddit subreddits that loosely map to sector themes. Many of these will
# return very little — we just take whatever signal is there.
SECTOR_SUBREDDITS: Dict[str, List[str]] = {
    "Technology":              ["technology", "wallstreetbets"],
    "Financials":              ["wallstreetbets", "investing"],
    "Healthcare":              ["biotechplays", "Healthcare_anon"],
    "Consumer Discretionary":  ["wallstreetbets"],
    "Consumer Staples":        ["dividends"],
    "Energy":                  ["energy", "oilandgasworkers"],
    "Industrials":             ["investing"],
    "Materials":               ["investing"],
    "Utilities":               ["dividends"],
    "Real Estate":             ["realestateinvesting"],
    "Communication Services":  ["wallstreetbets"],
}

# Per-process micro-cache for symbol→sector. The Finviz fundament lookup is
# one HTTP request per symbol so we keep results around for a day — Apple
# isn't going to switch sectors at lunch.
_SECTOR_LOOKUP: Dict[str, Tuple[str, float]] = {}
_SECTOR_LOOKUP_TTL = 24 * 3600

CACHE_TTL = 600  # 10 min — bounded enough to feel live, long enough to dodge rate-limits


# ─── small helpers ───────────────────────────────────────────────────

def _safe_float(v) -> Optional[float]:
    if v is None:
        return None
    try:
        f = float(v)
        if math.isnan(f) or math.isinf(f):
            return None
        return f
    except (TypeError, ValueError):
        return None


def _clip(x: float, lo: float, hi: float) -> float:
    if x < lo:
        return lo
    if x > hi:
        return hi
    return x


def _ticker_sector(symbol: str) -> Optional[str]:
    """Best-effort symbol → GICS-sector lookup. Uses Finviz's fundament page
    and caches in-process. None on any failure — the caller should treat it
    as "uncategorised" and skip the row."""
    sym = (symbol or "").upper().strip()
    if not sym or len(sym) > 6:
        return None
    hit = _SECTOR_LOOKUP.get(sym)
    if hit is not None:
        sec, exp = hit
        if exp > time.time():
            return sec
    if finviz_data is None:
        return None
    try:
        lib = finviz_data._lib()
        if not lib.get("ok"):
            return None
        q = lib["Quote"](ticker=sym)
        fund = q.ticker_fundament() or {}
        sec = fund.get("Sector") or None
    except Exception:
        sec = None
    _SECTOR_LOOKUP[sym] = (sec, time.time() + _SECTOR_LOOKUP_TTL)
    return sec


def _finviz_to_gics(finviz_sector: Optional[str]) -> Optional[str]:
    """Map Finviz's sector taxonomy (Technology, Consumer Cyclical, ...) to
    our GICS-style display names. Returns None if no match."""
    if not finviz_sector:
        return None
    s = finviz_sector.strip()
    for spec in SECTORS:
        if spec["finviz"].lower() == s.lower():
            return spec["sector"]
    # loose fallbacks
    low = s.lower()
    if "financ" in low: return "Financials"
    if "tech" in low: return "Technology"
    if "health" in low: return "Healthcare"
    if "energy" in low: return "Energy"
    if "util" in low: return "Utilities"
    if "real estate" in low: return "Real Estate"
    if "communic" in low: return "Communication Services"
    if "industrial" in low: return "Industrials"
    if "material" in low: return "Materials"
    if "cyclical" in low or "discretion" in low: return "Consumer Discretionary"
    if "defensive" in low or "staples" in low: return "Consumer Staples"
    return None


# ─── component fetchers ──────────────────────────────────────────────

def _spy_bars() -> List[dict]:
    """Fetch the SPY 1y daily chart exactly once per scan.

    Without this helper, _price_panel pulls SPY once per sector (11×),
    multiplying the cache_store.coalesce traffic and the cold-cache cost
    by 11. The underlying fetcher call is cached, but each invocation
    still serialises through the coalesce machinery and can wake an
    in-flight event."""
    if fetcher is None:
        return []
    try:
        return fetcher.get_chart_data("SPY", "1y", "1d") or []
    except Exception:
        return []


def _price_panel(etf: str, spy_bars: Optional[List[dict]] = None) -> Dict[str, Optional[float]]:
    """1d/5d/1mo price moves for the sector ETF plus relative strength vs SPY.
    Pulls a 1-year daily chart and computes %-from-close-N-days-ago at the
    last bar. Falls back to None on any miss.

    ``spy_bars`` lets the caller fetch SPY once and pass it in; if omitted
    we fetch it here (preserves backward compatibility for callers that
    use _price_panel in isolation)."""
    out: Dict[str, Optional[float]] = {
        "price_1d_pct":      None,
        "price_5d_pct":      None,
        "price_1mo_pct":     None,
        "rs_vs_spy_5d":      None,
        "rs_vs_spy_1mo":     None,
    }
    if fetcher is None:
        return out

    try:
        bars = fetcher.get_chart_data(etf, "1y", "1d") or []
    except Exception:
        return out
    spy = spy_bars if spy_bars is not None else _spy_bars()

    def _pct(arr: List[dict], offset: int) -> Optional[float]:
        # offset = number of trading days back from the last bar to compare.
        if not arr or len(arr) < offset + 1:
            return None
        last = arr[-1].get("close")
        ref  = arr[-1 - offset].get("close")
        if last is None or ref is None or ref == 0:
            return None
        try:
            return round((float(last) / float(ref) - 1.0) * 100.0, 3)
        except Exception:
            return None

    out["price_1d_pct"]  = _pct(bars, 1)
    out["price_5d_pct"]  = _pct(bars, 5)
    out["price_1mo_pct"] = _pct(bars, 21)

    spy_5d  = _pct(spy, 5)
    spy_1mo = _pct(spy, 21)
    if out["price_5d_pct"] is not None and spy_5d is not None:
        out["rs_vs_spy_5d"] = round(out["price_5d_pct"] - spy_5d, 3)
    if out["price_1mo_pct"] is not None and spy_1mo is not None:
        out["rs_vs_spy_1mo"] = round(out["price_1mo_pct"] - spy_1mo, 3)
    return out


def _narrative_panel(etf: str) -> Dict[str, Any]:
    """Pass the sector ETF to narrative_engine. Phase string + velocity score."""
    out = {"narrative_phase": None, "narrative_velocity": None}
    if narrative_engine is None:
        return out
    try:
        n = narrative_engine.analyze_narrative(etf) or {}
        phase = n.get("narrative_phase") or n.get("dominant_narrative")
        out["narrative_phase"] = phase
        v = _safe_float(n.get("velocity_score"))
        out["narrative_velocity"] = round(v, 3) if v is not None else None
    except Exception as e:
        log.debug("narrative_panel %s: %s", etf, e)
    return out


def _insider_index() -> Dict[str, Dict[str, float]]:
    """Aggregate Finviz "latest insider trading" feed into per-sector buy/sell
    dollar totals. Each unique ticker on the feed gets one Finviz sector
    lookup (cached). Returns {sector: {buy: $, sell: $}}."""
    blank: Dict[str, Dict[str, float]] = {s["sector"]: {"buy": 0.0, "sell": 0.0} for s in SECTORS}
    if finviz_data is None:
        return blank
    try:
        feed = finviz_data.insider_trades("latest") or {}
        trades = feed.get("trades") or []
    except Exception:
        return blank

    # cap the number of unique tickers we look up so we don't melt finviz on
    # a particularly active day
    seen_syms = set()
    for t in trades:
        sym = (t.get("ticker") or "").upper()
        if not sym:
            continue
        seen_syms.add(sym)
        if len(seen_syms) > 80:
            break

    sym_to_sector: Dict[str, Optional[str]] = {}
    for sym in seen_syms:
        sym_to_sector[sym] = _finviz_to_gics(_ticker_sector(sym))

    agg = blank
    for t in trades:
        sym = (t.get("ticker") or "").upper()
        if not sym or sym not in sym_to_sector:
            continue
        sec = sym_to_sector.get(sym)
        if not sec:
            continue
        txn = (t.get("transaction") or "").lower()
        val = _safe_float(t.get("value")) or 0.0
        if val <= 0:
            continue
        if "buy" in txn or "purchase" in txn or "acq" in txn:
            agg[sec]["buy"] += val
        elif "sale" in txn or "sell" in txn or "disp" in txn:
            agg[sec]["sell"] += val
    return agg


def _insider_buy_ratio(agg: Dict[str, Dict[str, float]], sector: str) -> Optional[float]:
    row = agg.get(sector) or {}
    buy = row.get("buy", 0.0)
    sell = row.get("sell", 0.0)
    total = buy + sell
    if total <= 0:
        return None
    return round(buy / total, 3)


def _congress_index() -> Dict[str, float]:
    """Aggregate the last 30 days of House PTRs into a per-sector net dollar
    flow. Best-effort: if the congress module isn't importable or the parser
    times out, return zeros."""
    blank: Dict[str, float] = {s["sector"]: 0.0 for s in SECTORS}
    if congress_mod is None:
        return blank
    try:
        # Bound the PDF pull — we just need the headline aggregate, not every filing
        result = congress_mod.get_recent_trades(days=30, max_pdfs=40) or {}
        trades = result.get("trades") or []
    except Exception:
        return blank

    sym_to_sector: Dict[str, Optional[str]] = {}
    for t in trades:
        sym = (t.get("ticker") or "").upper()
        if not sym:
            continue
        if sym not in sym_to_sector:
            sym_to_sector[sym] = _finviz_to_gics(_ticker_sector(sym))

    agg = blank
    for t in trades:
        sym = (t.get("ticker") or "").upper()
        sec = sym_to_sector.get(sym)
        if not sec:
            continue
        txn = (t.get("transaction") or t.get("type") or "").lower()
        # amount_min/amount_max are bands; use midpoint
        amt_min = _safe_float(t.get("amount_min"))
        amt_max = _safe_float(t.get("amount_max"))
        if amt_min is None and amt_max is None:
            amt_min = _safe_float(t.get("amount"))
            amt_max = amt_min
        if amt_min is None:
            continue
        amt = ((amt_min or 0.0) + (amt_max or amt_min or 0.0)) / 2.0
        if "purchase" in txn or "buy" in txn:
            agg[sec] += amt
        elif "sale" in txn or "sell" in txn:
            agg[sec] -= amt
    # round to whole dollars
    return {k: round(v, 2) for k, v in agg.items()}


def _reddit_panel(sector: str) -> Dict[str, Optional[float]]:
    """Reddit mention delta proxy. We compare the count of hot-list posts in
    the sector's subreddit(s) right now to a soft baseline of 25 posts so the
    UI gets a number it can sort on. This is intentionally crude — the
    important comparator is "more or fewer than yesterday" once we have
    trend data."""
    out = {"reddit_mention_delta_pct": None}
    if alt_signals is None:
        return out
    subs = SECTOR_SUBREDDITS.get(sector) or []
    total = 0
    hits = 0
    for sub in subs[:2]:  # cap per-sector subreddits hit
        try:
            posts = alt_signals.reddit_subreddit_posts(sub, sort="hot", limit=50) or []
        except Exception:
            continue
        total += len(posts)
        hits += 1
    if hits == 0:
        return out
    baseline = 25.0 * hits
    if baseline <= 0:
        return out
    out["reddit_mention_delta_pct"] = round((total - baseline) / baseline * 100.0, 2)
    return out


def _hn_panel(etf: str, sector: str) -> Dict[str, Optional[float]]:
    """HN net polarity for the sector ETF symbol. Many sectors won't have
    enough mentions to register — that's fine, we return None and the
    composite weights skip it."""
    out = {"hn_polarity": None}
    if hn_sentiment is None:
        return out
    try:
        # ETF symbol — short and low-noise. Sector-name search would pull in
        # too much off-topic stuff.
        data = hn_sentiment.fetch_mentions(etf, hours=168) or {}
        stats = data.get("stats") or {}
        n = data.get("mention_count") or 0
        if n < 2:
            return out
        net = _safe_float(stats.get("net_polarity"))
        if net is None:
            return out
        # Normalise to roughly [-1, +1] by mention count (sub-linear so a
        # handful of strong-polarity comments still moves the needle)
        out["hn_polarity"] = round(net / max(1.0, math.sqrt(n)), 3)
    except Exception as e:
        log.debug("hn_panel %s: %s", etf, e)
    return out


def _wiki_panel(etf: str) -> Dict[str, Optional[float]]:
    out = {"wiki_attention_z": None}
    if wiki_attention is None:
        return out
    try:
        data = wiki_attention.fetch_pageviews(etf, days=30) or {}
        points = data.get("points") or []
        if len(points) < 14:
            return out
        views = [p.get("views") or 0 for p in points]
        latest = views[-1]
        prior = views[:-1]
        if not prior:
            return out
        mu = sum(prior) / len(prior)
        var = sum((v - mu) ** 2 for v in prior) / max(1, len(prior) - 1)
        sd = math.sqrt(var) if var > 0 else 0.0
        if sd <= 0:
            return out
        out["wiki_attention_z"] = round((latest - mu) / sd, 3)
    except Exception as e:
        log.debug("wiki_panel %s: %s", etf, e)
    return out


def _options_pcr(etf: str) -> Optional[float]:
    """Aggregate put/call volume ratio for the nearest expiry. None on any
    miss (no options, rate-limited, etc)."""
    if fetcher is None:
        return None
    try:
        chain = fetcher.get_option_chain(etf) or {}
    except Exception:
        return None
    if chain.get("error"):
        return None
    calls = chain.get("calls") or []
    puts  = chain.get("puts")  or []
    c_vol = sum((_safe_float(c.get("volume")) or 0.0) for c in calls)
    p_vol = sum((_safe_float(p.get("volume")) or 0.0) for p in puts)
    total = c_vol + p_vol
    if total <= 0:
        return None
    return round(p_vol / total, 3)


def _factor_panel() -> Dict[str, Optional[float]]:
    """Today's factor returns from the Ken French daily CSV. The same number
    is shared across all sectors — this isn't a per-sector signal per se, but
    it gives the user the style backdrop (Mom-up day, SMB-flat, etc.). We
    blend the most-recent row's overall magnitude into the composite for
    sectors whose ETF beta on that factor leans the right way."""
    out = {"latest_date": None, "factors": {}}
    if research_factors is None:
        return out
    try:
        dates, fac, _rf = research_factors._load_factor_data()
    except Exception:
        return out
    if not dates or fac is None or len(fac) == 0:
        return out
    last_idx = len(dates) - 1
    out["latest_date"] = dates[last_idx]
    # Column order in research_factors is ["Mkt-RF","SMB","HML","RMW","CMA","Mom"]
    cols = ["Mkt-RF", "SMB", "HML", "RMW", "CMA", "Mom"]
    last_row = fac[last_idx]
    out["factors"] = {cols[i]: round(float(last_row[i]), 3) for i in range(min(len(cols), len(last_row)))}
    return out


# ─── composite scoring ───────────────────────────────────────────────

def _composite_score(row: Dict[str, Any]) -> float:
    """Hand-tuned weighted average of normalised component signals. We
    intentionally keep the weights simple (and small) so the score lands in
    roughly [-3, +3] without saturating on a single big input. Anything that's
    None just drops out of the weighted sum.

    Each component contributes a "z-like" number in roughly [-2, +2]:
      - prices: percent moves / scale, clipped
      - rs:     pp moves / scale, clipped
      - narrative_velocity: already a signed number, divided to soften
      - insider_buy_ratio: centred at 0.5, scaled so 0 → -1.5 and 1 → +1.5
      - congress_net: log-scaled around 0
      - reddit_mention_delta_pct: divided by 50
      - hn_polarity: already roughly [-1, +1]
      - wiki_attention_z: already a z, clipped
      - options_pcr: centred at 0.6 (typical baseline), inverted
    """
    parts: List[Tuple[float, float]] = []  # (weight, value)

    p1d  = row.get("price_1d_pct")
    p5d  = row.get("price_5d_pct")
    p1mo = row.get("price_1mo_pct")
    if p1d  is not None: parts.append((0.10, _clip(p1d  / 1.5, -2, 2)))
    if p5d  is not None: parts.append((0.10, _clip(p5d  / 3.0, -2, 2)))
    if p1mo is not None: parts.append((0.10, _clip(p1mo / 8.0, -2, 2)))

    rs5  = row.get("rs_vs_spy_5d")
    rs1m = row.get("rs_vs_spy_1mo")
    if rs5  is not None: parts.append((0.15, _clip(rs5  / 2.0, -2, 2)))
    if rs1m is not None: parts.append((0.15, _clip(rs1m / 5.0, -2, 2)))

    nv = row.get("narrative_velocity")
    if nv is not None:
        parts.append((0.08, _clip(nv / 2.0, -2, 2)))
    phase = (row.get("narrative_phase") or "").upper()
    if "BULL" in phase:
        parts.append((0.04, 1.0))
    elif "BEAR" in phase:
        parts.append((0.04, -1.0))

    insider = row.get("insider_buy_ratio_30d")
    if insider is not None:
        parts.append((0.10, _clip((insider - 0.5) * 3.0, -1.5, 1.5)))

    cong = row.get("congress_net_30d_usd")
    if cong is not None and cong != 0:
        # log10-scale, signed
        sign = 1.0 if cong > 0 else -1.0
        mag = math.log10(max(1.0, abs(cong)))
        parts.append((0.06, _clip(sign * (mag - 3.0) * 0.5, -2, 2)))

    rd = row.get("reddit_mention_delta_pct")
    if rd is not None:
        parts.append((0.04, _clip(rd / 50.0, -2, 2)))

    hn = row.get("hn_polarity")
    if hn is not None:
        parts.append((0.03, _clip(hn, -2, 2)))

    wz = row.get("wiki_attention_z")
    if wz is not None:
        parts.append((0.03, _clip(wz, -3, 3)))

    pcr = row.get("options_pcr")
    if pcr is not None:
        # Lower PCR = more bullish positioning, so invert.
        # Baseline ~0.6; 0.4 → +0.4, 0.9 → -0.6.
        parts.append((0.05, _clip((0.6 - pcr) * 2.0, -1.5, 1.5)))

    fac = row.get("factor_1d_return_pct")
    if fac is not None:
        parts.append((0.02, _clip(fac / 1.0, -1.5, 1.5)))

    if not parts:
        return 0.0
    wsum = sum(w for w, _ in parts)
    if wsum <= 0:
        return 0.0
    score = sum(w * v for w, v in parts) / wsum
    # The internal score is already roughly z-like; multiply by 3 so the
    # advertised range is ~[-3, +3].
    return round(_clip(score * 3.0, -3.0, 3.0), 3)


# ─── factor → sector tilt ────────────────────────────────────────────

# Coarse mapping of which factor returns are *positively* correlated with
# each sector's index — used only to assign the daily factor return into the
# `factor_1d_return_pct` column. Numbers are heuristic, not regressed.
SECTOR_FACTOR_TILT: Dict[str, Dict[str, float]] = {
    "Technology":              {"Mom": 0.5, "SMB": 0.1, "HML": -0.3, "Mkt-RF": 1.0},
    "Financials":              {"HML": 0.5, "Mkt-RF": 1.1},
    "Healthcare":              {"RMW": 0.3, "Mkt-RF": 0.8},
    "Consumer Discretionary":  {"Mom": 0.3, "Mkt-RF": 1.1},
    "Consumer Staples":        {"CMA": 0.3, "Mkt-RF": 0.6},
    "Energy":                  {"HML": 0.4, "Mkt-RF": 1.0},
    "Industrials":             {"Mkt-RF": 1.05},
    "Materials":               {"HML": 0.2, "Mkt-RF": 1.0},
    "Utilities":               {"CMA": 0.4, "Mkt-RF": 0.5},
    "Real Estate":             {"HML": 0.3, "Mkt-RF": 0.7},
    "Communication Services":  {"Mom": 0.3, "Mkt-RF": 1.0},
}


def _factor_contribution(sector: str, factor_panel: Dict[str, Any]) -> Optional[float]:
    """Per-sector blended factor return using the heuristic tilts above.
    Mostly informational — the composite picks it up at a small weight."""
    facs = factor_panel.get("factors") or {}
    if not facs:
        return None
    tilt = SECTOR_FACTOR_TILT.get(sector) or {}
    if not tilt:
        return None
    num = 0.0
    den = 0.0
    for k, w in tilt.items():
        v = facs.get(k)
        if v is None:
            continue
        num += w * v
        den += abs(w)
    if den == 0:
        return None
    return round(num / den, 3)


# ─── public API ──────────────────────────────────────────────────────

def _compute() -> Dict[str, Any]:
    insiders = _insider_index()
    congress = _congress_index()
    factors  = _factor_panel()
    # Fetch SPY's 1y chart exactly once for the whole scan — every sector's
    # rs_vs_spy_* computation reuses it. Without this we'd issue 11
    # cache_store.coalesce(("chart","SPY",…)) calls per scan even though
    # the chart cache itself dedupes them; the coalesce machinery is
    # cheap-but-not-free per call (lock, dict lookup, optional Event wait).
    spy_bars = _spy_bars()

    rows: List[Dict[str, Any]] = []
    for spec in SECTORS:
        sector = spec["sector"]
        etf = spec["etf"]
        row: Dict[str, Any] = {"sector": sector, "etf": etf}

        # price + RS
        row.update(_price_panel(etf, spy_bars=spy_bars))

        # narrative
        row.update(_narrative_panel(etf))

        # factor return (per-sector tilt)
        row["factor_1d_return_pct"] = _factor_contribution(sector, factors)

        # insider
        row["insider_buy_ratio_30d"] = _insider_buy_ratio(insiders, sector)

        # congress
        row["congress_net_30d_usd"] = congress.get(sector, 0.0) or None

        # reddit
        row.update(_reddit_panel(sector))

        # hn
        row.update(_hn_panel(etf, sector))

        # wikipedia
        row.update(_wiki_panel(etf))

        # options PCR
        row["options_pcr"] = _options_pcr(etf)

        # composite
        row["composite_flow_score"] = _composite_score(row)

        rows.append(row)

    # leader / laggard by composite
    scored = [r for r in rows if r.get("composite_flow_score") is not None]
    leader = max(scored, key=lambda r: r["composite_flow_score"]) if scored else None
    laggard = min(scored, key=lambda r: r["composite_flow_score"]) if scored else None

    return {
        "as_of": datetime.now(tz=timezone.utc).isoformat(),
        "sectors": rows,
        "leader_sector":  leader["sector"]  if leader  else None,
        "laggard_sector": laggard["sector"] if laggard else None,
        "factor_snapshot": factors,
    }


def sector_flow() -> Dict[str, Any]:
    """Cached entry point. Returns the synthesised multi-signal sector
    rotation snapshot. Always returns a dict, never raises."""
    if cache_store is not None:
        try:
            return cache_store.coalesce(("sectorflow",), CACHE_TTL, _compute)
        except Exception as e:
            log.warning("sectorflow cache_store.coalesce failed: %s", e)
    try:
        return _compute()
    except Exception as e:
        log.exception("sectorflow compute failed: %s", e)
        return {
            "as_of": datetime.now(tz=timezone.utc).isoformat(),
            "sectors": [],
            "leader_sector": None,
            "laggard_sector": None,
            "error": str(e),
        }


if __name__ == "__main__":  # smoke test
    import json
    logging.basicConfig(level=logging.INFO)
    out = sector_flow()
    print(json.dumps(out, indent=2, default=str)[:4000])
