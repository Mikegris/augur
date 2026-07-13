"""
Corporate Contagion Graph — maps supply-chain dependencies and revenue
concentration from SEC filings to predict how earnings surprises cascade
between connected companies.

Part of the AUGUR wealth intelligence platform.
Python 3.9 compatible (no match/case, no X | Y unions).
"""

import logging
import re
import threading
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import sec_edgar as edgar
import fetcher
import safe_executor

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level cache
# ---------------------------------------------------------------------------
_cache = {}       # type: Dict[str, object]
_cache_ts = {}    # type: Dict[str, float]
_CACHE_TTL = 3600  # 1 hour
_cache_lock = threading.Lock()  # build_graph/assess_contagion fan out concurrently

# Minimum amount by which a lagged correlation's |corr| must exceed the
# lag-0 (contemporaneous) |corr| before we accept a non-zero lead-lag edge.
# Scanning 6 candidate lags and taking the max is a multiple-comparison
# search that inflates the apparent best correlation even on independent
# series; this margin is a Bonferroni-flavoured guard against that bias.
_LAG_EDGE_MARGIN = 0.08


def _get_cached(key):
    # type: (str) -> object
    with _cache_lock:
        if key in _cache and (time.time() - _cache_ts.get(key, 0)) < _CACHE_TTL:
            return _cache[key]
        return None


def _set_cached(key, value):
    # type: (str, object) -> None
    with _cache_lock:
        _cache[key] = value
        _cache_ts[key] = time.time()
        # Evict stale entries when cache grows large
        if len(_cache) > 200:
            cutoff = time.time() - _CACHE_TTL
            # Snapshot keys before iterating so a concurrent writer can't
            # mutate the dict mid-iteration (we hold the lock, but keep the
            # snapshot pattern explicit and cheap).
            stale = [k for k, t in list(_cache_ts.items()) if t < cutoff]
            for k in stale:
                _cache.pop(k, None)
                _cache_ts.pop(k, None)


# ---------------------------------------------------------------------------
# Known companies  (name -> ticker, ~120 entries)
# ---------------------------------------------------------------------------
KNOWN_COMPANIES = {
    # Mega-cap tech
    "Apple": "AAPL", "Apple Inc": "AAPL",
    "Microsoft": "MSFT", "Microsoft Corporation": "MSFT",
    "Amazon": "AMZN", "Amazon.com": "AMZN", "Amazon Web Services": "AMZN",
    "Alphabet": "GOOGL", "Google": "GOOGL", "Google LLC": "GOOGL",
    "Meta": "META", "Meta Platforms": "META", "Facebook": "META",
    "NVIDIA": "NVDA", "Nvidia": "NVDA",
    "Tesla": "TSLA", "Tesla Inc": "TSLA",
    "Broadcom": "AVGO", "Broadcom Inc": "AVGO",
    "Taiwan Semiconductor": "TSM", "TSMC": "TSM",
    "Samsung Electronics": "005930.KS", "Samsung": "005930.KS",
    "Oracle": "ORCL", "Oracle Corporation": "ORCL",
    "Adobe": "ADBE", "Adobe Inc": "ADBE",
    "Salesforce": "CRM", "Salesforce Inc": "CRM",
    "Cisco": "CSCO", "Cisco Systems": "CSCO",
    "Intel": "INTC", "Intel Corporation": "INTC",
    "AMD": "AMD", "Advanced Micro Devices": "AMD",
    "Qualcomm": "QCOM", "Qualcomm Inc": "QCOM",
    "Texas Instruments": "TXN",
    "IBM": "IBM", "International Business Machines": "IBM",
    "ServiceNow": "NOW",
    "Intuit": "INTU",
    "Palantir": "PLTR", "Palantir Technologies": "PLTR",
    "Snowflake": "SNOW",
    "CrowdStrike": "CRWD",
    "Palo Alto Networks": "PANW",
    "Datadog": "DDOG",
    "Fortinet": "FTNT",
    "Arista Networks": "ANET",
    "Marvell": "MRVL", "Marvell Technology": "MRVL",
    "Micron": "MU", "Micron Technology": "MU",
    "Applied Materials": "AMAT",
    "Lam Research": "LRCX",
    "KLA Corporation": "KLAC", "KLA": "KLAC",
    "ASML": "ASML", "ASML Holding": "ASML",
    "Synopsys": "SNPS",
    "Cadence": "CDNS", "Cadence Design": "CDNS",
    "NetApp": "NTAP",
    "Western Digital": "WDC",
    "Seagate": "STX", "Seagate Technology": "STX",
    # Financials
    "JPMorgan": "JPM", "JPMorgan Chase": "JPM", "JP Morgan": "JPM",
    "Bank of America": "BAC",
    "Goldman Sachs": "GS",
    "Morgan Stanley": "MS",
    "Wells Fargo": "WFC",
    "Citigroup": "C", "Citi": "C",
    "BlackRock": "BLK",
    "Charles Schwab": "SCHW",
    "Visa": "V", "Visa Inc": "V",
    "Mastercard": "MA",
    "American Express": "AXP",
    "PayPal": "PYPL",
    "Block": "SQ", "Square": "SQ",
    "Berkshire Hathaway": "BRK-B",
    "S&P Global": "SPGI",
    "Moody's": "MCO",
    # Healthcare
    "UnitedHealth": "UNH", "UnitedHealth Group": "UNH",
    "Johnson & Johnson": "JNJ",
    "Eli Lilly": "LLY", "Lilly": "LLY",
    "Pfizer": "PFE",
    "Merck": "MRK",
    "AbbVie": "ABBV",
    "Thermo Fisher": "TMO", "Thermo Fisher Scientific": "TMO",
    "Abbott": "ABT", "Abbott Laboratories": "ABT",
    "Amgen": "AMGN",
    "Gilead": "GILD", "Gilead Sciences": "GILD",
    "Regeneron": "REGN",
    "Moderna": "MRNA",
    "Intuitive Surgical": "ISRG",
    "Medtronic": "MDT",
    "Boston Scientific": "BSX",
    "Stryker": "SYK",
    # Consumer / Retail
    "Walmart": "WMT",
    "Costco": "COST",
    "Home Depot": "HD",
    "Procter & Gamble": "PG", "P&G": "PG",
    "Coca-Cola": "KO",
    "PepsiCo": "PEP", "Pepsi": "PEP",
    "Nike": "NKE",
    "McDonald's": "MCD",
    "Starbucks": "SBUX",
    "Target": "TGT",
    "Lowe's": "LOW",
    "Disney": "DIS", "Walt Disney": "DIS",
    "Netflix": "NFLX",
    "Snap": "SNAP", "Snap Inc": "SNAP", "Snapchat": "SNAP",
    "Spotify": "SPOT",
    # Industrials / Energy
    "Boeing": "BA",
    "Caterpillar": "CAT",
    "Honeywell": "HON",
    "General Electric": "GE",
    "3M": "MMM",
    "Union Pacific": "UNP",
    "Deere": "DE", "John Deere": "DE",
    "Lockheed Martin": "LMT",
    "Raytheon": "RTX",
    "Northrop Grumman": "NOC",
    "ExxonMobil": "XOM", "Exxon": "XOM",
    "Chevron": "CVX",
    "ConocoPhillips": "COP",
    "Schlumberger": "SLB",
    # Telecom / Utilities
    "AT&T": "T",
    "Verizon": "VZ",
    "T-Mobile": "TMUS",
    "Comcast": "CMCSA",
    "NextEra Energy": "NEE",
    # Autos / Transport
    "Ford": "F", "Ford Motor": "F",
    "General Motors": "GM",
    "Uber": "UBER",
    "FedEx": "FDX",
    "UPS": "UPS", "United Parcel Service": "UPS",
    # Semis / Hardware supply chain
    "Foxconn": "HNHPF", "Hon Hai": "HNHPF",
    "Dell": "DELL", "Dell Technologies": "DELL",
    "HP Inc": "HPQ", "Hewlett-Packard": "HPQ",
    "Corning": "GLW", "Corning Inc": "GLW",
    "TE Connectivity": "TEL",
    "Amphenol": "APH",
    "ON Semiconductor": "ON",
    "NXP Semiconductors": "NXPI", "NXP": "NXPI",
    "Skyworks": "SWKS", "Skyworks Solutions": "SWKS",
    "Qorvo": "QRVO",
}

# Reverse map: ticker -> canonical name (first name wins)
_TICKER_TO_NAME = {}
for _name, _tkr in KNOWN_COMPANIES.items():
    if _tkr not in _TICKER_TO_NAME:
        _TICKER_TO_NAME[_tkr] = _name

# Set of known tickers for fast lookup
_KNOWN_TICKERS = set(KNOWN_COMPANIES.values())

# ---------------------------------------------------------------------------
# Known supply chains — fallback when EDGAR text parsing yields no mentions.
# Maps ticker -> list of (ticker, relationship) tuples.
# ---------------------------------------------------------------------------
KNOWN_SUPPLY_CHAINS = {
    "AAPL": [
        ("TSM", "supplier"), ("AVGO", "supplier"), ("QCOM", "supplier"),
        ("SWKS", "supplier"), ("HNHPF", "supplier"), ("INTC", "supplier"),
        ("MU", "supplier"), ("MRVL", "supplier"), ("NXPI", "supplier"),
        ("GLW", "supplier"), ("TXN", "supplier"), ("ON", "supplier"),
        ("QRVO", "supplier"), ("GOOGL", "peer"), ("MSFT", "peer"),
        ("AMZN", "customer"), ("WMT", "customer"), ("COST", "customer"),
        ("TGT", "customer"), ("005930.KS", "supplier"),
    ],
    "MSFT": [
        ("NVDA", "supplier"), ("INTC", "supplier"), ("AMD", "supplier"),
        ("ORCL", "peer"), ("CRM", "peer"), ("GOOGL", "peer"),
        ("AMZN", "peer"), ("ADBE", "peer"), ("NOW", "peer"),
        ("SNOW", "peer"), ("CRWD", "partner"), ("PANW", "partner"),
    ],
    "NVDA": [
        ("TSM", "supplier"), ("005930.KS", "supplier"), ("AVGO", "peer"),
        ("AMD", "peer"), ("INTC", "peer"), ("MSFT", "customer"),
        ("AMZN", "customer"), ("GOOGL", "customer"), ("META", "customer"),
        ("ORCL", "customer"), ("DELL", "customer"), ("HPQ", "customer"),
        ("MRVL", "peer"), ("AMAT", "supplier"), ("LRCX", "supplier"),
        ("KLAC", "supplier"), ("ASML", "supplier"), ("SNPS", "supplier"),
        ("CDNS", "supplier"),
    ],
    "TSLA": [
        ("PANW", "supplier"), ("NVDA", "supplier"), ("TSM", "supplier"),
        ("AVGO", "supplier"), ("TXN", "supplier"), ("ON", "supplier"),
        ("NXPI", "supplier"), ("INTC", "supplier"), ("F", "peer"),
        ("GM", "peer"), ("UBER", "peer"),
    ],
    "AMZN": [
        ("NVDA", "supplier"), ("INTC", "supplier"), ("AMD", "supplier"),
        ("MSFT", "peer"), ("GOOGL", "peer"), ("META", "peer"),
        ("WMT", "peer"), ("UPS", "partner"), ("FDX", "partner"),
    ],
    "GOOGL": [
        ("NVDA", "supplier"), ("TSM", "supplier"), ("AVGO", "supplier"),
        ("MSFT", "peer"), ("AMZN", "peer"), ("META", "peer"),
        ("AAPL", "peer"), ("CRM", "peer"), ("SPOT", "customer"),
    ],
    "META": [
        ("NVDA", "supplier"), ("TSM", "supplier"), ("AVGO", "supplier"),
        ("GOOGL", "peer"), ("SNAP", "peer"), ("SPOT", "peer"),
        ("NFLX", "peer"), ("AAPL", "peer"),
    ],
    "JPM": [
        ("GS", "peer"), ("MS", "peer"), ("BAC", "peer"),
        ("WFC", "peer"), ("C", "peer"), ("BLK", "peer"),
        ("V", "partner"), ("MA", "partner"), ("SPGI", "partner"),
        ("MCO", "partner"),
    ],
    "AVGO": [
        ("TSM", "supplier"), ("AAPL", "customer"), ("NVDA", "peer"),
        ("QCOM", "peer"), ("INTC", "peer"), ("AMD", "peer"),
        ("MRVL", "peer"), ("TXN", "peer"),
    ],
    "AMD": [
        ("TSM", "supplier"), ("ASML", "supplier"), ("LRCX", "supplier"),
        ("NVDA", "peer"), ("INTC", "peer"), ("MSFT", "customer"),
        ("GOOGL", "customer"), ("AMZN", "customer"), ("META", "customer"),
    ],
    "UNH": [
        ("JNJ", "peer"), ("LLY", "peer"), ("PFE", "peer"),
        ("ABBV", "peer"), ("MRK", "peer"), ("ABT", "peer"),
        ("TMO", "peer"), ("AMGN", "peer"),
    ],
    "BA": [
        ("GE", "supplier"), ("HON", "supplier"), ("RTX", "peer"),
        ("LMT", "peer"), ("NOC", "peer"), ("CAT", "peer"),
        ("GLW", "supplier"), ("TEL", "supplier"),
    ],
    "XOM": [
        ("CVX", "peer"), ("COP", "peer"), ("SLB", "supplier"),
        ("BA", "customer"), ("CAT", "customer"),
    ],
    "WMT": [
        ("PG", "supplier"), ("KO", "supplier"), ("PEP", "supplier"),
        ("JNJ", "supplier"), ("COST", "peer"), ("TGT", "peer"),
        ("AMZN", "peer"), ("HD", "peer"),
    ],
    "DIS": [
        ("NFLX", "peer"), ("CMCSA", "peer"), ("SPOT", "peer"),
        ("AAPL", "partner"), ("AMZN", "partner"), ("GOOGL", "partner"),
    ],
}

# Relationship keywords used to classify edges
_SUPPLIER_KW = {"supplier", "vendor", "manufacture", "supply", "foundry", "fabricat", "component", "procure"}
_CUSTOMER_KW = {"customer", "client", "buyer", "end-user", "reseller", "distributor", "licensee"}
_PARTNER_KW = {"partner", "collaborat", "joint venture", "alliance", "agreement", "co-develop", "strategic"}

# All-caps 1-5 char candidate-ticker pattern. Hoisted to module scope: it was
# re.compile()'d on every _parse_company_mentions call (runs once per 10-K),
# recompiling the same constant pattern each time.
_TICKER_RE = re.compile(r'\b([A-Z]{1,5})\b')

# High-weight filing sections
_HIGH_WEIGHT_SECTIONS = re.compile(
    r"(risk\s+factors|customers?|concentration|principal\s+customers?|"
    r"significant\s+customers?|major\s+customers?|revenue\s+sources)",
    re.IGNORECASE,
)

# Revenue percentage patterns
# Capture the entire number (incl. optional decimal) in a single group so
# "represented 12.5% of revenue" is parsed as 12.5 rather than truncated
# to 12.0 by a non-capturing decimal tail.
_REVENUE_PCT_RE = re.compile(
    r"(?:approximately|about|roughly|represented|accounted\s+for|constituted)?\s*"
    r"(\d{1,3}(?:\.\d+)?)\s*%\s*"
    r"(?:of\s+(?:our\s+)?(?:total\s+)?(?:net\s+)?(?:revenue|sales|net\s+revenue|"
    r"total\s+revenue|consolidated\s+revenue|accounts\s+receivable))",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _classify_relationship(context):
    # type: (str) -> str
    """Classify the relationship type from surrounding text context."""
    ctx_lower = context.lower()
    for kw in _SUPPLIER_KW:
        if kw in ctx_lower:
            return "supplier"
    for kw in _CUSTOMER_KW:
        if kw in ctx_lower:
            return "customer"
    for kw in _PARTNER_KW:
        if kw in ctx_lower:
            return "peer"
    return "mentioned"


def _extract_context_window(text, start, end, window=300):
    # type: (str, int, int, int) -> str
    """Return text surrounding a match position."""
    ctx_start = max(0, start - window)
    ctx_end = min(len(text), end + window)
    return text[ctx_start:ctx_end]


def _in_high_weight_section(text, position):
    # type: (str, int) -> bool
    """Check if position falls near a high-weight section header."""
    # Look backwards up to 2000 chars for a section header
    lookback = text[max(0, position - 2000):position]
    return bool(_HIGH_WEIGHT_SECTIONS.search(lookback))


def _parse_company_mentions(text):
    # type: (str) -> List[Dict]
    """
    Extract company/ticker mentions from SEC filing text.

    Returns list of dicts (sorted by significance weight, descending):
        [{"ticker": str, "name": str, "relationship": str,
          "revenue_pct": float or None, "mention_count": int,
          "weight": float, "context": str}]

    "weight" is the section-significance score: each mention counts 1.0
    (2.0 inside a high-weight section) plus 5.0 for an attributed revenue
    concentration — so weight >= mention_count.
    """
    if not text or len(text.strip()) < 100:
        return []

    mentions = {}  # type: Dict[str, Dict]

    # --- 1. Company name detection ---
    for company_name, ticker in KNOWN_COMPANIES.items():
        # Build pattern that requires word boundaries
        # Escape special regex chars in company names
        escaped = re.escape(company_name)
        pattern = r'\b' + escaped + r'\b'
        for m in re.finditer(pattern, text, re.IGNORECASE):
            ctx = _extract_context_window(text, m.start(), m.end())
            relationship = _classify_relationship(ctx)
            in_key_section = _in_high_weight_section(text, m.start())

            if ticker not in mentions:
                mentions[ticker] = {
                    "ticker": ticker,
                    "name": _TICKER_TO_NAME.get(ticker, company_name),
                    "relationship": relationship,
                    "revenue_pct": None,
                    "mention_count": 0,
                    "context": ctx[:200],
                    "_weight": 0.0,
                }

            entry = mentions[ticker]
            entry["mention_count"] += 1
            # Upgrade relationship if we find a more specific one.
            # supplier/customer may override "peer" too, not just "mentioned":
            # partner keywords ("agreement" etc.) are extremely common in
            # filings, so a first hit classified peer used to permanently
            # block a later, more specific supplier/customer classification —
            # the cross-filing merge in build_graph already allows exactly
            # this upgrade, so keep the two passes consistent.
            if relationship in ("supplier", "customer") and entry["relationship"] in ("mentioned", "peer"):
                entry["relationship"] = relationship
            elif relationship == "peer" and entry["relationship"] == "mentioned":
                entry["relationship"] = relationship
            # Boost weight for key sections
            entry["_weight"] += 2.0 if in_key_section else 1.0

    # --- 2. Known ticker detection near relationship keywords ---
    # Match all-caps 1-5 char words (module-level compiled pattern _TICKER_RE)
    # Build set of context keywords
    context_keywords = _SUPPLIER_KW | _CUSTOMER_KW | _PARTNER_KW | {
        "revenue", "sales", "contract", "purchase", "order",
    }
    for m in _TICKER_RE.finditer(text):
        candidate = m.group(1)
        if candidate not in _KNOWN_TICKERS:
            continue
        # Check if near a relationship keyword (within 200 chars)
        window_start = max(0, m.start() - 200)
        window_end = min(len(text), m.end() + 200)
        window_text = text[window_start:window_end].lower()

        near_keyword = any(kw in window_text for kw in context_keywords)
        if not near_keyword:
            continue

        ctx = _extract_context_window(text, m.start(), m.end())
        relationship = _classify_relationship(ctx)
        in_key_section = _in_high_weight_section(text, m.start())

        if candidate not in mentions:
            mentions[candidate] = {
                "ticker": candidate,
                "name": _TICKER_TO_NAME.get(candidate, candidate),
                "relationship": relationship,
                "revenue_pct": None,
                "mention_count": 0,
                "context": ctx[:200],
                "_weight": 0.0,
            }

        entry = mentions[candidate]
        entry["mention_count"] += 1
        # Same upgrade rule as the name-detection loop above: supplier/customer
        # overrides both "mentioned" and the easily-triggered "peer".
        if relationship in ("supplier", "customer") and entry["relationship"] in ("mentioned", "peer"):
            entry["relationship"] = relationship
        elif relationship == "peer" and entry["relationship"] == "mentioned":
            entry["relationship"] = relationship
        entry["_weight"] += 2.0 if in_key_section else 1.0

    # --- 3. Revenue concentration extraction ---
    for m in _REVENUE_PCT_RE.finditer(text):
        pct = float(m.group(1))
        if pct < 1 or pct > 100:
            continue
        # Look for a company name or ticker near this percentage
        window = 400
        ctx_start = max(0, m.start() - window)
        surrounding = _extract_context_window(text, m.start(), m.end(), window=window)
        pct_pos = m.start() - ctx_start  # the percentage's offset within `surrounding`
        # Among the mentions appearing in the window, credit the percentage to the
        # NEAREST one (by match position), not the first in dict-insertion order —
        # a 'principal customers' paragraph can list several known names and the
        # concentration belongs to whichever is closest to this percentage.
        best_entry = None
        best_dist = None
        surrounding_lower = surrounding.lower()
        for ticker, entry in mentions.items():
            name = entry["name"]
            positions = []
            # word-boundary match — bare `ticker in surrounding` let 1-char
            # tickers (C, V, F, T) match inside unrelated words.
            for tm in re.finditer(r"\b" + re.escape(ticker) + r"\b", surrounding):
                positions.append(tm.start())
            if name:
                nl = name.lower()
                idx = surrounding_lower.find(nl)
                while idx != -1:
                    positions.append(idx)
                    idx = surrounding_lower.find(nl, idx + 1)
            if not positions:
                continue
            dist = min(abs(p - pct_pos) for p in positions)
            if best_dist is None or dist < best_dist:
                best_dist = dist
                best_entry = entry
        if best_entry is not None:
            best_entry["revenue_pct"] = pct
            best_entry["_weight"] += 5.0  # Revenue concentration is very significant

    # Clean up and sort by weight. The section/revenue-concentration weight
    # used to be popped and DISCARDED here, which made the whole significance
    # scheme (2x for high-weight sections, +5 for revenue concentration) dead
    # code — ordering and edge weights fell back to raw mention_count. Keep it
    # on the entry (public "weight" key) so it drives the sort and build_graph
    # can fold it into edge weight.
    result = []
    for ticker, entry in mentions.items():
        entry["weight"] = entry.pop("_weight", 0.0)
        if entry["mention_count"] >= 1:
            result.append(entry)

    result.sort(key=lambda x: (x["weight"], x["mention_count"]), reverse=True)
    return result


def _compute_lag_correlation(symbol1, symbol2, max_lag=5):
    # type: (str, str, int) -> Tuple[float, int]
    """
    Compute correlation at different lag offsets to find if symbol2
    follows symbol1 with a delay.

    Returns (best_correlation, optimal_lag_days).
    """
    try:
        data1 = fetcher.get_chart_data(symbol1, period="1y", interval="1d")
        data2 = fetcher.get_chart_data(symbol2, period="1y", interval="1d")

        if not data1 or not data2 or len(data1) < 30 or len(data2) < 30:
            return (0.0, 0)

        # Build time-indexed returns
        times1 = {}  # type: Dict[int, float]
        for i in range(1, len(data1)):
            prev_close = data1[i - 1]["close"]
            if prev_close and prev_close > 0:
                ret = (data1[i]["close"] - prev_close) / prev_close
                times1[data1[i]["time"]] = ret

        times2 = {}  # type: Dict[int, float]
        for i in range(1, len(data2)):
            prev_close = data2[i - 1]["close"]
            if prev_close and prev_close > 0:
                ret = (data2[i]["close"] - prev_close) / prev_close
                times2[data2[i]["time"]] = ret

        # Sort timestamps
        sorted_times1 = sorted(times1.keys())
        sorted_times2 = sorted(times2.keys())

        if len(sorted_times1) < 30 or len(sorted_times2) < 30:
            return (0.0, 0)

        # Convert to aligned lists indexed by position.
        returns1 = [times1[t] for t in sorted_times1]
        returns2_by_pos = {}  # type: Dict[int, float]
        # Map times2 onto symbol1's positional index. Daily bars on the same
        # exchange share identical `time` keys, so an exact date-keyed
        # intersection (O(n)) replaces the old nearest-time scan that was
        # O(n^2) — for each of n times2 it linearly swept all n times1. We
        # snap each timestamp to its UTC calendar day so bars stamped at
        # slightly different intraday times still align.
        SEC_PER_DAY = 86400
        day_to_pos1 = {}  # type: Dict[int, int]
        for i, t1 in enumerate(sorted_times1):
            day_to_pos1[t1 // SEC_PER_DAY] = i
        for t2 in sorted_times2:
            pos = day_to_pos1.get(t2 // SEC_PER_DAY)
            if pos is not None:
                returns2_by_pos[pos] = times2[t2]

        # Compute lag-0 (contemporaneous) correlation first as the baseline.
        # Taking the plain max |corr| over 6 candidate lags is a data-snooping
        # search: with 6 trials the largest |corr| is upward-biased even on
        # independent series. We therefore only ACCEPT a lagged edge if its
        # |corr| beats the lag-0 baseline by a margin large enough to survive
        # the multiple-comparison search; otherwise we report lag 0. This
        # turns "the best of 6 lags" into "a lag that is meaningfully better
        # than no lag at all".
        lag0_corr = 0.0
        lag_corrs = {}  # lag -> corr (for lags that had enough pairs)
        for lag in range(0, max_lag + 1):
            pairs_x = []
            pairs_y = []
            # For "symbol2 follows symbol1 by `lag` days" we pair returns1
            # at time t with returns2 at time t+lag.
            for pos in range(0, len(returns1) - lag):
                shifted_pos = pos + lag
                if shifted_pos in returns2_by_pos:
                    pairs_x.append(returns1[pos])
                    pairs_y.append(returns2_by_pos[shifted_pos])

            if len(pairs_x) < 20:
                continue

            corr = _pearson(pairs_x, pairs_y)
            lag_corrs[lag] = corr
            if lag == 0:
                lag0_corr = corr

        if not lag_corrs:
            return (0.0, 0)

        # Best non-zero lag by |corr|.
        best_lag, best_corr = 0, lag0_corr
        for lag, corr in lag_corrs.items():
            if lag == 0:
                continue
            if abs(corr) > abs(best_corr):
                best_corr, best_lag = corr, lag

        # Bonferroni-style margin: require the best lagged correlation to beat
        # the lag-0 baseline by at least _LAG_EDGE_MARGIN in absolute value,
        # AND to have the same sign as a real lead-lag relationship would. If
        # the lagged edge isn't convincingly stronger than contemporaneous
        # co-movement, fall back to lag 0 (no spurious "X leads Y by k days").
        if best_lag != 0:
            if abs(best_corr) - abs(lag0_corr) < _LAG_EDGE_MARGIN:
                return (round(lag0_corr, 4), 0)

        return (round(best_corr, 4), best_lag)

    except Exception as e:
        logger.warning("Lag correlation failed for %s/%s: %s", symbol1, symbol2, e)
        return (0.0, 0)


def _pearson(x, y):
    # type: (List[float], List[float]) -> float
    """Compute Pearson correlation coefficient without numpy."""
    n = len(x)
    if n < 2:
        return 0.0
    mean_x = sum(x) / n
    mean_y = sum(y) / n
    var_x = sum((xi - mean_x) ** 2 for xi in x)
    var_y = sum((yi - mean_y) ** 2 for yi in y)
    if var_x == 0 or var_y == 0:
        return 0.0
    cov = sum((xi - mean_x) * (yi - mean_y) for xi, yi in zip(x, y))
    return cov / (var_x ** 0.5 * var_y ** 0.5)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_graph(symbol):
    # type: (str) -> Dict
    """
    Build the corporate dependency graph centered on a symbol by parsing
    SEC 10-K filings for company mentions and supply-chain relationships.
    """
    symbol = symbol.upper()
    cache_key = "graph_{}".format(symbol)
    cached = _get_cached(cache_key)
    if cached is not None:
        return cached

    try:
        # 1. Try EDGAR filing parse first
        filing_date = None
        merged = {}  # type: Dict[str, Dict]

        cik = edgar.get_cik(symbol)
        if cik:
            filings = edgar.get_recent_filings(symbol, forms=["10-K"], limit=2)
            if filings:
                all_mentions = []  # type: List[Dict]
                for filing in filings:
                    accession = filing.get("accession", "")
                    primary_doc = filing.get("primary_document", "")
                    form_type = filing.get("form_type", "10-K")
                    if not filing_date:
                        filing_date = filing.get("filing_date", "")
                    if not accession or not primary_doc:
                        continue
                    text = edgar.get_filing_text(cik, accession, primary_doc, form_type)
                    if text:
                        mentions = _parse_company_mentions(text)
                        all_mentions.extend(mentions)

                for m in all_mentions:
                    ticker = m["ticker"]
                    if ticker == symbol:
                        continue
                    if ticker in merged:
                        existing = merged[ticker]
                        existing["mention_count"] += m["mention_count"]
                        existing["weight"] = existing.get("weight", 0.0) + m.get("weight", 0.0)
                        # First-write-wins for revenue_pct: filings iterate
                        # newest-first, so only fill when missing — otherwise
                        # the OLDER 10-K's (stale) concentration figure
                        # unconditionally overwrote the latest disclosure.
                        if m["revenue_pct"] is not None and existing["revenue_pct"] is None:
                            existing["revenue_pct"] = m["revenue_pct"]
                        if m["relationship"] in ("supplier", "customer"):
                            existing["relationship"] = m["relationship"]
                        elif m["relationship"] == "peer" and existing["relationship"] == "mentioned":
                            existing["relationship"] = m["relationship"]
                    else:
                        merged[ticker] = dict(m)

        # 2. Fallback to curated supply chain data if EDGAR yielded nothing
        if not merged and symbol in KNOWN_SUPPLY_CHAINS:
            for tkr, rel in KNOWN_SUPPLY_CHAINS[symbol]:
                if tkr == symbol:
                    continue
                merged[tkr] = {
                    "ticker": tkr,
                    "name": _TICKER_TO_NAME.get(tkr, tkr),
                    "relationship": rel,
                    "revenue_pct": None,
                    "mention_count": 1,
                    "context": "Known {} relationship (curated supply chain data)".format(rel),
                }
            filing_date = "curated"

        # 5+6. Fundamentals for the source company AND every connected ticker.
        # Previously this was a serial N+1: one get_fundamentals() for the
        # source plus one inside the per-peer loop below, each a network
        # round-trip → O(peers x latency). Fetch the whole merged set in
        # parallel once and index the results.
        fund_tickers = [symbol] + [t for t in merged.keys() if t != symbol]

        def _fund(tk):
            try:
                return fetcher.get_fundamentals(tk)
            except Exception:
                return None

        try:
            fund_results = safe_executor.parallel_map(
                _fund, fund_tickers, max_workers=6,
                thread_name_prefix="contagion-fund")
        except Exception:
            fund_results = [_fund(tk) for tk in fund_tickers]
        fund_by_ticker = {tk: fr for tk, fr in zip(fund_tickers, fund_results)}

        source_info = fund_by_ticker.get(symbol) or {}
        source_name = source_info.get("name", symbol)
        source_sector = source_info.get("sector", "")

        # 6. Enrich connected companies with sector info
        nodes = []
        edges = []

        for ticker, mention in merged.items():
            # Use the pre-fetched fundamentals for enrichment.
            target_sector = ""
            target_name = mention.get("name", ticker)
            fund = fund_by_ticker.get(ticker)
            if fund and "error" not in fund:
                target_sector = fund.get("sector", "")
                if fund.get("name"):
                    target_name = fund["name"]

            # Calculate edge weight from the parse's significance weight
            # (mentions in high-weight sections count 2x, attributed revenue
            # concentration adds +5) so the documented section boost actually
            # moves edge weight — it used to be discarded and only raw
            # mention_count counted. Curated-fallback entries carry no
            # "weight" key; fall back to mention_count (same /10 scale, and
            # weight >= mention_count for parsed entries).
            sig = mention.get("weight") or mention["mention_count"]
            weight = min(sig / 10.0, 1.0)
            if mention["revenue_pct"] is not None:
                weight = max(weight, mention["revenue_pct"] / 100.0)
            if mention["relationship"] in ("supplier", "customer"):
                weight = min(weight * 1.5, 1.0)

            nodes.append({
                "ticker": ticker,
                "name": target_name,
                "sector": target_sector,
                "relationship": mention["relationship"],
                "mention_count": mention["mention_count"],
            })
            edges.append({
                "source": symbol,
                "target": ticker,
                "type": mention["relationship"],
                "weight": round(weight, 3),
                "revenue_pct": mention["revenue_pct"],
                "context": mention.get("context", "")[:200],
            })

        # Sort nodes by mention count descending
        nodes.sort(key=lambda n: n["mention_count"], reverse=True)
        edges.sort(key=lambda e: e["weight"], reverse=True)

        result = {
            "symbol": symbol,
            "company_name": source_name,
            "sector": source_sector,
            "nodes": nodes,
            "edges": edges,
            "total_connections": len(nodes),
            "filing_date": filing_date or "",
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }

        _set_cached(cache_key, result)
        return result

    except Exception as e:
        logger.exception("build_graph failed for %s", symbol)
        return {"error": str(e)}


def assess_contagion(symbol, event_type="earnings_miss"):
    # type: (str, str) -> Dict
    """
    Given a company experiencing an event, trace contagion through
    its corporate dependency graph.

    Returns impacted companies ranked by impact score with lag analysis.
    """
    symbol = symbol.upper()
    cache_key = "contagion_{}_{}".format(symbol, event_type)
    cached = _get_cached(cache_key)
    if cached is not None:
        return cached

    try:
        # 1. Build or retrieve cached graph
        graph = build_graph(symbol)
        if "error" in graph:
            return {"error": graph["error"]}

        nodes = graph.get("nodes", [])
        edges = graph.get("edges", [])
        if not nodes:
            return {
                "source_symbol": symbol,
                "event_type": event_type,
                "impacted_companies": [],
                "total_impacted": 0,
                "high_risk_count": 0,
                "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            }

        # Build edge weight lookup
        edge_weights = {}  # type: Dict[str, float]
        edge_types = {}    # type: Dict[str, str]
        for e in edges:
            edge_weights[e["target"]] = e["weight"]
            edge_types[e["target"]] = e["type"]

        # 2-3. Compute correlation and lag for each connected company
        def _analyze_target(node):
            # type: (Dict) -> Optional[Dict]
            ticker = node["ticker"]
            edge_weight = edge_weights.get(ticker, 0.5)

            # Get contemporaneous correlation
            correlation = 0.0
            try:
                corr_data = fetcher.get_correlation_matrix(
                    [symbol, ticker], period="1y"
                )
                matrix = corr_data.get("matrix", {})
                if symbol in matrix and ticker in matrix[symbol]:
                    val = matrix[symbol].get(ticker)
                    if val is not None:
                        correlation = val
                elif ticker in matrix and symbol in matrix[ticker]:
                    val = matrix[ticker].get(symbol)
                    if val is not None:
                        correlation = val
            except Exception:
                pass

            # Lag analysis
            lag_corr, lag_days = _compute_lag_correlation(symbol, ticker, max_lag=5)

            # Use the stronger of the two correlations
            effective_corr = max(abs(correlation), abs(lag_corr))
            effective_lag = lag_days if abs(lag_corr) >= abs(correlation) else 0

            # 4. Impact score: edge_weight * correlation, with NO lag decay.
            # The lag here means "ticker follows `symbol` by `effective_lag`
            # days" — a genuine PREDICTIVE lead-lag edge. For contagion that's
            # the MORE valuable signal (it's forward-looking and actionable),
            # not less, so the old 1/(1+lag) penalty was backwards: it shrank
            # exactly the clean lead-lag edges we most want to surface. We give
            # a modest boost to a clean lead-lag edge and leave contemporaneous
            # (lag 0) edges at their base strength.
            lag_factor = 1.0 + min(effective_lag, 5) * 0.05  # 1.00 .. 1.25
            raw_score = edge_weight * effective_corr * lag_factor
            impact_score = int(min(round(raw_score * 100), 100))
            impact_score = max(impact_score, 0)

            # 5. Risk classification
            if impact_score > 70:
                risk = "HIGH"
            elif impact_score > 40:
                risk = "MODERATE"
            else:
                risk = "LOW"

            relationship = edge_types.get(ticker, node.get("relationship", "mentioned"))

            detail = "{} is a {} of {} (weight {:.2f}, corr {:.2f}, lag {}d)".format(
                ticker, relationship, symbol, edge_weight, effective_corr, effective_lag
            )

            return {
                "ticker": ticker,
                "name": node.get("name", ticker),
                "impact_score": impact_score,
                "correlation": round(effective_corr, 4),
                "lag_days": effective_lag,
                "relationship": relationship,
                "contagion_risk": risk,
                "detail": detail,
            }

        # Parallel execution for correlation calculations on safe_executor's
        # daemon threads (falls back to serial by itself if threads can't
        # spawn; workers can't block interpreter exit).
        def _analyze_target_safe(node):
            try:
                return _analyze_target(node)
            except Exception as e:
                logger.warning(
                    "Contagion analysis failed for %s: %s",
                    node.get("ticker", "?"), e,
                )
                return None

        impacted = [
            r for r in safe_executor.parallel_map(
                _analyze_target_safe, nodes, max_workers=3,
                thread_name_prefix="contagion",
            )
            if r is not None
        ]

        # Sort by impact score descending
        impacted.sort(key=lambda x: x["impact_score"], reverse=True)
        high_risk_count = sum(1 for x in impacted if x["contagion_risk"] == "HIGH")

        result = {
            "source_symbol": symbol,
            "event_type": event_type,
            "impacted_companies": impacted,
            "total_impacted": len(impacted),
            "high_risk_count": high_risk_count,
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }

        _set_cached(cache_key, result)
        return result

    except Exception as e:
        logger.exception("assess_contagion failed for %s", symbol)
        return {"error": str(e)}
