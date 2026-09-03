"""synth_peerdiv — Peer Divergence Detector.

For any symbol, pull its top-N peers (via ``contagion_graph.build_graph``
when available, otherwise via a small curated industry → peers fallback
map), then lay every signal side-by-side and algorithmically rank
"where does this symbol diverge most from its peers?"

Public API
----------
peer_divergence(symbol, n_peers=5) -> dict
    Returns a structured envelope::

        {
          "symbol": "NVDA",
          "peers":  ["AMD", "INTC", ...],
          "n_peers": 5,
          "metrics": {
              "<metric_name>": {
                  "NVDA": <value>, "AMD": <value>, ...,
                  "peer_median": <value>,
                  "peer_mad": <value>,
                  "z_score": <signed z>,
              },
              ...
          },
          "top_divergences": [
              {"metric": ..., "z_score": ..., "interpretation": ...},
              ...
          ],
          "as_of": "<iso utc>",
          "errors": [...]   # only present when something partial-failed
        }

The z-score is computed as ``(symbol_value - peer_median) / max(peer_mad, eps)``
where MAD = median absolute deviation, which is robust to a single outlier
peer that would otherwise blow up a stdev-based score.

All outbound data fetches are issued from a small thread pool so the
N peers are queried in parallel; results that error out are simply omitted
from that metric's median + z calculation.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from datetime import datetime, timezone, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple

import safe_executor

log = logging.getLogger(__name__)

# ── Optional dependencies — we degrade gracefully when any are missing ───
try:
    import cache_store
except Exception:  # pragma: no cover
    cache_store = None  # type: ignore

try:
    import contagion_graph
except Exception:  # pragma: no cover
    contagion_graph = None  # type: ignore

try:
    import fetcher
except Exception:  # pragma: no cover
    fetcher = None  # type: ignore

try:
    import ml_forecast
except Exception:  # pragma: no cover
    ml_forecast = None  # type: ignore

try:
    import smart_money
except Exception:  # pragma: no cover
    smart_money = None  # type: ignore

try:
    import research_factors
except Exception:  # pragma: no cover
    research_factors = None  # type: ignore

try:
    import sec_edgar
except Exception:  # pragma: no cover
    sec_edgar = None  # type: ignore

try:
    import congress
except Exception:  # pragma: no cover
    congress = None  # type: ignore

try:
    import narrative_engine
except Exception:  # pragma: no cover
    narrative_engine = None  # type: ignore


_CACHE_TTL_S = 900  # 15-minute peerdiv cache; matches research_* modules

# ── Curated industry → peers fallback ─────────────────────────────────────
# Used when contagion_graph yields zero usable peers (e.g. the symbol's
# 10-K parse failed AND it's not in KNOWN_SUPPLY_CHAINS). Light-weight —
# we only need to ensure the headline tickers have *some* peer list.
_INDUSTRY_PEERS: Dict[str, List[str]] = {
    "semiconductors": ["NVDA", "AMD", "INTC", "AVGO", "QCOM", "MRVL", "TXN", "MU", "ON", "NXPI"],
    "software—infrastructure": ["MSFT", "ORCL", "NOW", "PANW", "CRWD", "FTNT", "DDOG", "NET", "ZS"],
    "software—application": ["CRM", "ADBE", "INTU", "SNOW", "WDAY", "TEAM", "ZM"],
    "internet content & information": ["GOOGL", "META", "NFLX", "PINS", "SNAP", "SPOT"],
    "internet retail": ["AMZN", "MELI", "EBAY", "ETSY"],
    "consumer electronics": ["AAPL", "SONY", "HPQ", "DELL"],
    "auto manufacturers": ["TSLA", "F", "GM", "TM", "STLA", "RIVN", "LCID"],
    "banks—diversified": ["JPM", "BAC", "WFC", "C", "USB", "PNC"],
    "capital markets": ["GS", "MS", "BLK", "SCHW", "RJF"],
    "drug manufacturers—general": ["LLY", "PFE", "JNJ", "MRK", "ABBV", "BMY", "NVS", "AZN"],
    "healthcare plans": ["UNH", "ELV", "CVS", "HUM", "CI"],
    "aerospace & defense": ["BA", "RTX", "LMT", "NOC", "GD", "TDG"],
    "oil & gas integrated": ["XOM", "CVX", "BP", "SHEL", "TTE", "COP"],
    "discount stores": ["WMT", "COST", "TGT", "DG", "DLTR"],
    "entertainment": ["DIS", "NFLX", "WBD", "PARA", "CMCSA"],
    "credit services": ["V", "MA", "AXP", "DFS", "COF"],
    "specialty retail": ["HD", "LOW", "TJX", "ROST", "BBY"],
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _safe_float(v: Any) -> Optional[float]:
    try:
        f = float(v)
        if math.isnan(f) or math.isinf(f):
            return None
        return f
    except (TypeError, ValueError):
        return None


# ── Peer discovery ───────────────────────────────────────────────────────
def _peers_from_contagion(symbol: str, n: int) -> List[str]:
    """Pull top-N peer tickers from contagion_graph.

    Strategy: prefer edges with ``type == "peer"`` (highest signal), then
    backfill with any other strong edges (sorted by weight desc). Drops
    foreign-listed tickers (those with a "." in them, e.g. ``005930.KS``)
    since most downstream modules can't price them.
    """
    if contagion_graph is None:
        return []
    try:
        g = contagion_graph.build_graph(symbol)
    except Exception as e:
        log.warning("contagion_graph.build_graph(%s) failed: %s", symbol, e)
        return []
    if not isinstance(g, dict) or "error" in g:
        return []

    edges = g.get("edges") or []
    if not edges:
        # nodes only — pull tickers from there, sorted by mention_count
        nodes = g.get("nodes") or []
        return [
            n_["ticker"] for n_ in nodes
            if "." not in (n_.get("ticker") or "") and n_.get("ticker") != symbol
        ][:n]

    # Sort: peer relationships first, then weight desc
    def _rank(edge: Dict[str, Any]) -> Tuple[int, float]:
        kind = (edge.get("type") or "").lower()
        kind_pri = 0 if kind == "peer" else 1
        w = _safe_float(edge.get("weight")) or 0.0
        return (kind_pri, -w)

    ranked = sorted(edges, key=_rank)
    peers: List[str] = []
    seen = set([symbol.upper()])
    for e in ranked:
        t = (e.get("target") or "").upper()
        if not t or t in seen or "." in t:
            continue
        peers.append(t)
        seen.add(t)
        if len(peers) >= n:
            break
    return peers


def _peers_from_industry(symbol: str, n: int) -> List[str]:
    """Fallback peer discovery via ``fetcher.get_fundamentals(symbol)["industry"]``
    and a small curated industry → peers map."""
    if fetcher is None:
        return []
    try:
        fund = fetcher.get_fundamentals(symbol)
    except Exception:
        fund = {}
    if not isinstance(fund, dict):
        return []
    industry = (fund.get("industry") or "").strip().lower()
    if not industry:
        return []

    # Try exact match first, then fuzzy contains-on-key.
    candidates = _INDUSTRY_PEERS.get(industry)
    if not candidates:
        for key, peers in _INDUSTRY_PEERS.items():
            if key in industry or industry in key:
                candidates = peers
                break
    if not candidates:
        return []

    out: List[str] = []
    for c in candidates:
        if c.upper() == symbol.upper():
            continue
        out.append(c.upper())
        if len(out) >= n:
            break
    return out


def discover_peers(symbol: str, n_peers: int = 5) -> List[str]:
    """Return up to ``n_peers`` peer tickers for ``symbol``.

    Tries contagion_graph first, then industry-map fallback. Result is
    deterministic per (symbol, n_peers) for the lifetime of the cache.
    """
    sym = symbol.upper()
    peers = _peers_from_contagion(sym, n_peers)
    if len(peers) < n_peers:
        # Backfill from industry map without duplicating
        seen = set(peers + [sym])
        for p in _peers_from_industry(sym, n_peers * 2):
            if p in seen:
                continue
            peers.append(p)
            seen.add(p)
            if len(peers) >= n_peers:
                break
    return peers[:n_peers]


# ── Per-metric fetchers ──────────────────────────────────────────────────
def _m_pe_ratio(sym: str) -> Optional[float]:
    if fetcher is None:
        return None
    try:
        f = fetcher.get_fundamentals(sym) or {}
        return _safe_float(f.get("pe_ratio"))
    except Exception:
        return None


def _m_market_beta(sym: str) -> Optional[float]:
    if fetcher is None:
        return None
    try:
        f = fetcher.get_fundamentals(sym) or {}
        return _safe_float(f.get("beta"))
    except Exception:
        return None


def _m_ml_prob_up_20d(sym: str) -> Optional[float]:
    if ml_forecast is None:
        return None
    try:
        r = ml_forecast.ml_forecast(sym) or {}
        rf = r.get("rf_classifier") or {}
        if isinstance(rf, dict):
            return _safe_float(rf.get("prob_up_20d"))
        return None
    except Exception:
        return None


def _m_smart_money(sym: str) -> Optional[float]:
    if smart_money is None:
        return None
    try:
        r = smart_money.compute_score(sym) or {}
        return _safe_float(r.get("score"))
    except Exception:
        return None


def _m_factor_alpha_annual(sym: str) -> Optional[float]:
    if research_factors is None:
        return None
    try:
        r = research_factors.factor_exposure(sym, 3) or {}
        return _safe_float(r.get("alpha_annual_pct"))
    except Exception:
        return None


def _m_factor_mkt_beta(sym: str) -> Optional[float]:
    if research_factors is None:
        return None
    try:
        r = research_factors.factor_exposure(sym, 3) or {}
        exposures = r.get("exposures") or {}
        mkt = exposures.get("Mkt-RF") or {}
        if isinstance(mkt, dict):
            return _safe_float(mkt.get("beta"))
        return None
    except Exception:
        return None


def _m_ytd_return_pct(sym: str) -> Optional[float]:
    """Compute year-to-date return from a 1y daily chart.

    Baseline is the PRIOR year's final close (the bar just before the first
    bar of the current year), so the year's first session move counts toward
    the return — measuring from the first trading day's CLOSE silently
    excluded any Jan-2 gap for the whole year. Falls back to the first
    in-year close only when the 1y window holds no prior-year bar.
    """
    if fetcher is None:
        return None
    try:
        bars = fetcher.get_chart_data(sym, "1y", "1d") or []
        if not bars or len(bars) < 2:
            return None
        last = bars[-1]
        last_close = _safe_float(last.get("close"))
        last_ts = last.get("time")
        if last_close is None or last_ts is None:
            return None
        year = datetime.utcfromtimestamp(int(last_ts)).year
        first_idx: Optional[int] = None
        for idx, b in enumerate(bars):
            ts = b.get("time")
            if ts is None:
                continue
            if datetime.utcfromtimestamp(int(ts)).year == year:
                first_idx = idx
                break
        if first_idx is None:
            return None
        start_close = None
        if first_idx > 0:
            # Prior-year close = the true YTD reference.
            start_close = _safe_float(bars[first_idx - 1].get("close"))
        if not start_close:
            # No usable prior-year bar in the window → old behaviour.
            start_close = _safe_float(bars[first_idx].get("close"))
        if not start_close:
            return None
        return round((last_close / start_close - 1.0) * 100.0, 3)
    except Exception:
        return None


def _m_insider_form4_net_60d(sym: str) -> Optional[float]:
    """Net insider Form-4 dollar flow in past 60 days, in $M.

    Positive = net buying, negative = net selling. Returns 0.0 when there
    are zero Form-4s in the window (so 0 still ranks differently from
    'unknown' = None).
    """
    if sec_edgar is None:
        return None
    try:
        txns = sec_edgar.get_form4_transactions(sym, limit=80) or []
    except Exception:
        return None
    if not isinstance(txns, list):
        return None
    cutoff = datetime.now(timezone.utc) - timedelta(days=60)
    net = 0.0
    seen_any = False
    for t in txns:
        if not isinstance(t, dict):
            continue
        date_str = (t.get("date") or "")[:10]
        if not date_str:
            continue
        try:
            d = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except Exception:
            continue
        if d < cutoff:
            continue
        seen_any = True
        val = _safe_float(t.get("value")) or 0.0
        side = (t.get("transaction_type") or "").upper()
        if side == "BUY":
            net += val
        elif side == "SELL":
            net -= val
    # Return in millions, rounded to 3 decimals
    return round(net / 1e6, 3) if seen_any or net != 0.0 else 0.0


# Shared congress trade cache: one network round-trip per (set of symbols,
# 60d), reused across all per-ticker calls in a single peer_divergence run.
# Without this we'd download the same 200 PTR PDFs N+1 times in parallel.
#
# Entries carry their populate timestamp so they expire after _CONGRESS_TTL_S.
# Without a TTL the module-level dict would (a) cache transient failures as
# permanent "no trades" entries, and (b) serve stale 60-day windows forever
# once populated.
_CONGRESS_TTL_S = 900  # 15min — matches the outer peerdiv cache
_congress_trades_cache: Dict[str, Tuple[float, List[Dict[str, Any]]]] = {}
_congress_lock = threading.Lock()


def _ensure_congress_trades_for(symbols: List[str]) -> None:
    """Pre-fetch the 60-day congress trades for all symbols in a single call.

    Stores results in ``_congress_trades_cache[symbol] = (ts, trades)``.
    Idempotent within the TTL window; a second call with overlapping
    symbols is a no-op for tickers cached < _CONGRESS_TTL_S ago. Failures
    are silent — affected tickers map to ``[]`` with the current ts so we
    don't retry on every metric call within the scan, but the next scan
    after TTL expiry will retry.
    """
    if congress is None:
        return
    now = time.time()
    syms_to_fetch = []
    with _congress_lock:
        for s in symbols:
            entry = _congress_trades_cache.get(s)
            if entry is None or (now - entry[0]) > _CONGRESS_TTL_S:
                syms_to_fetch.append(s)
    if not syms_to_fetch:
        return
    try:
        result = congress.get_recent_trades(
            days=60, max_pdfs=80, tickers=syms_to_fetch
        ) or {}
    except Exception as e:
        log.warning("congress.get_recent_trades failed: %s", e)
        result = {}
    trades_all = result.get("trades") if isinstance(result, dict) else None
    if not isinstance(trades_all, list):
        trades_all = []
    # Partition by ticker
    partition: Dict[str, List[Dict[str, Any]]] = {s: [] for s in syms_to_fetch}
    for t in trades_all:
        if not isinstance(t, dict):
            continue
        sym = (t.get("ticker") or "").upper()
        if sym in partition:
            partition[sym].append(t)
    with _congress_lock:
        for s in syms_to_fetch:
            _congress_trades_cache[s] = (now, partition.get(s, []))


def _m_congress_net_60d(sym: str) -> Optional[float]:
    """Net congressional buys-minus-sells (count, not dollars) over 60 days.

    Counts are far more reliable than amounts here — PTR amounts are
    declared as wide ranges ("$1,001 – $15,000") and our parser already
    midpoint-estimates them, but the resulting noise dwarfs the signal at
    a 60-day horizon. So we use signed count: +N buys − M sells.

    Reads from the pre-populated ``_congress_trades_cache``. The orchestrator
    calls ``_ensure_congress_trades_for(all_symbols)`` once before fanning
    out the per-metric workers — that way we only download PTRs once.
    """
    if congress is None:
        return None
    trades: Optional[List[Dict[str, Any]]] = None
    with _congress_lock:
        entry = _congress_trades_cache.get(sym)
        if entry is not None and (time.time() - entry[0]) <= _CONGRESS_TTL_S:
            trades = entry[1]
    if trades is None:
        # Fallback path: caller didn't pre-warm (or entry expired). Fetch on-demand (slow).
        try:
            trades = congress.get_trades_for_ticker(sym, days=60) or []
        except Exception:
            return None
    if not isinstance(trades, list):
        return None
    buys, sells = 0, 0
    for t in trades:
        if not isinstance(t, dict):
            continue
        raw = (t.get("txn_type_raw") or "").upper()
        # Match congress.get_congress_summary's classification: "SP" is the
        # SPOUSE owner code, not a sell, so don't catch it with startswith("S").
        if raw in ("P", "PE"):
            buys += 1
        elif raw in ("S", "S (PARTIAL)", "SE", "S (EXCHANGE)", "E (EXCHANGE)"):
            sells += 1
    # If we found no trades, return 0.0 (neutral) rather than None — absence
    # of congressional activity is itself a signal, not "unknown".
    return float(buys - sells)


def _m_narrative_velocity(sym: str) -> Optional[float]:
    if narrative_engine is None:
        return None
    try:
        r = narrative_engine.analyze_narrative(sym) or {}
        return _safe_float(r.get("velocity_score"))
    except Exception:
        return None


def _m_options_iv_30d(sym: str) -> Optional[float]:
    """Mean implied vol across the front-month chain (calls+puts), in %.

    We use the nearest expiry > 7 days, then average all IVs we have.
    Skips contracts without IV or with IV == 0. Cheap enough to do per
    peer in parallel.
    """
    if fetcher is None:
        return None
    try:
        dates = fetcher.get_options_dates(sym) or []
    except Exception:
        dates = []
    if not dates:
        return None
    today = datetime.now(timezone.utc).date()
    target = None
    for d in dates:
        try:
            dt = datetime.strptime(d, "%Y-%m-%d").date()
        except Exception:
            continue
        if (dt - today).days >= 7:
            target = d
            break
    if target is None:
        target = dates[0]
    try:
        chain = fetcher.get_option_chain(sym, target) or {}
    except Exception:
        return None
    calls = chain.get("calls") or []
    puts = chain.get("puts") or []
    ivs: List[float] = []
    for arr in (calls, puts):
        for c in arr:
            iv = _safe_float(c.get("impliedVolatility"))
            if iv is None or iv <= 0:
                continue
            ivs.append(iv)
    if not ivs:
        return None
    return round(sum(ivs) / len(ivs), 2)


# Ordered registry — controls the column order in the response.
_METRIC_REGISTRY: List[Tuple[str, Callable[[str], Optional[float]], str]] = [
    ("pe_ratio",              _m_pe_ratio,              "Trailing P/E (higher = pricier vs earnings)"),
    ("ml_prob_up_20d",        _m_ml_prob_up_20d,        "RF model P(positive 20d return)"),
    ("smart_money",           _m_smart_money,           "AUGUR Smart Money composite (0-100)"),
    ("factor_alpha_annual",   _m_factor_alpha_annual,   "FF5+Mom annualised alpha %"),
    ("ytd_return_pct",        _m_ytd_return_pct,        "Year-to-date price return %"),
    ("insider_form4_net_60d", _m_insider_form4_net_60d, "Net insider Form-4 $ flow, 60d ($M)"),
    ("congress_net_60d",      _m_congress_net_60d,      "Congressional buys − sells, 60d (count)"),
    ("narrative_velocity",    _m_narrative_velocity,    "News narrative velocity (-100..+100)"),
    ("options_iv_30d",        _m_options_iv_30d,        "Front-month avg implied vol %"),
    ("factor_mkt_beta",       _m_factor_mkt_beta,       "FF Mkt-RF beta"),
]


# ── Robust statistics ────────────────────────────────────────────────────
def _median(xs: List[float]) -> float:
    s = sorted(xs)
    n = len(s)
    if n == 0:
        raise ValueError("empty")
    if n % 2:
        return float(s[n // 2])
    return float((s[n // 2 - 1] + s[n // 2]) / 2.0)


def _mad(xs: List[float], med: float) -> float:
    if not xs:
        return 0.0
    devs = [abs(x - med) for x in xs]
    return _median(devs)


def _stdev(xs: List[float]) -> float:
    """Sample standard deviation (0.0 for <2 points or no spread)."""
    n = len(xs)
    if n < 2:
        return 0.0
    mean = sum(xs) / n
    var = sum((x - mean) ** 2 for x in xs) / (n - 1)
    return math.sqrt(var) if var > 0 else 0.0


def _interpret(metric: str, z: float, sym_val: float, peer_med: float) -> str:
    """Generate a short human-readable interpretation of a divergence."""
    direction = "above" if z > 0 else "below"
    label_map = {
        "pe_ratio":              ("P/E", "richer", "cheaper"),
        "ml_prob_up_20d":        ("ML 20d up-prob", "more bullish", "more bearish"),
        "smart_money":           ("smart-money score", "stronger", "weaker"),
        "factor_alpha_annual":   ("annual alpha", "higher", "lower"),
        "ytd_return_pct":        ("YTD return", "far ahead of", "far behind"),
        "insider_form4_net_60d": ("net insider flow", "net buying vs peers", "net selling vs peers"),
        "congress_net_60d":      ("Congress net trades", "more buys vs peers", "more sells vs peers"),
        "narrative_velocity":    ("narrative velocity", "narrative accelerating vs peers",
                                  "narrative fading vs peers"),
        "options_iv_30d":        ("front-month IV", "options pricing more risk", "options pricing less risk"),
        "factor_mkt_beta":       ("market beta", "more cyclical", "more defensive"),
    }
    if metric in label_map:
        name, pos_phrase, neg_phrase = label_map[metric]
        phrase = pos_phrase if z > 0 else neg_phrase
        return f"{name} {phrase} (z={z:+.2f}, peer median {peer_med:.3g})"
    return f"{metric} {direction} peer median (z={z:+.2f})"


# ── Core orchestration ───────────────────────────────────────────────────
def _gather_metrics(
    symbol: str,
    peers: List[str],
) -> Tuple[Dict[str, Dict[str, Any]], List[str]]:
    """Run every metric × every ticker, in parallel.

    Returns ``(metrics, errors)`` where:
      - metrics[metric_name] = {ticker: value or None, ...}
      - errors = list of "<metric>:<ticker>:<msg>" strings
    """
    all_syms = [symbol.upper()] + list(peers)
    errors: List[str] = []

    # Pre-allocate result structure
    metrics: Dict[str, Dict[str, Any]] = {
        name: {s: None for s in all_syms}
        for (name, _, _) in _METRIC_REGISTRY
    }

    # Submit (metric, sym) pairs. Bound the pool at 16 — enough for 6 peers
    # × 10 metrics but small enough to keep yfinance / EDGAR happy.
    lock = threading.Lock()

    def _worker(metric_name: str, fn: Callable[[str], Optional[float]], sym: str):
        try:
            v = fn(sym)
        except Exception as e:
            v = None
            with lock:
                errors.append(f"{metric_name}:{sym}:{type(e).__name__}: {e}")
        with lock:
            metrics[metric_name][sym] = v

    # Run every (metric, symbol) cell on safe_executor's daemon threads.
    # ``_worker`` writes into the lock-guarded ``metrics``/``errors``
    # structures and swallows its own exceptions, so parallel_map's return
    # value is ignored. parallel_map falls back to a serial loop by itself
    # when worker threads can't be spawned, and its daemon workers can't
    # block interpreter exit the way non-daemon TPE workers did.
    cells = [
        (metric_name, fn, sym)
        for metric_name, fn, _desc in _METRIC_REGISTRY
        for sym in all_syms
    ]
    safe_executor.parallel_map(
        lambda cell: _worker(cell[0], cell[1], cell[2]), cells,
        max_workers=16, thread_name_prefix="peerdiv",
    )

    return metrics, errors


def _enrich_with_stats(
    symbol: str,
    metrics: Dict[str, Dict[str, Any]],
    peers: List[str],
) -> Dict[str, Dict[str, Any]]:
    """Attach peer_median, peer_mad, and z_score to each metric block.

    A peer with a missing value is excluded from that specific metric's
    median+MAD — it doesn't pollute the comparison.
    """
    out: Dict[str, Dict[str, Any]] = {}
    sym = symbol.upper()
    for metric_name in metrics:
        block: Dict[str, Any] = dict(metrics[metric_name])
        peer_vals = [
            _safe_float(block.get(p)) for p in peers
        ]
        peer_vals = [v for v in peer_vals if v is not None]
        sym_val = _safe_float(block.get(sym))
        if peer_vals:
            med = _median(peer_vals)
            mad = _mad(peer_vals, med)
            block["peer_median"] = round(med, 4)
            block["peer_mad"] = round(mad, 4)
        else:
            block["peer_median"] = None
            block["peer_mad"] = None
            med = None
            mad = 0.0
        # Require at least 3 peers before computing a z-score: a median/MAD
        # (or stdev) over 1-2 points is not a meaningful dispersion estimate,
        # and a 2-peer MAD is frequently exactly 0 (collapsing to the stdev
        # fallback on every metric). Below 3 peers the z is undefined.
        if sym_val is not None and med is not None and len(peer_vals) >= 3:
            # WHY (S8): when peers are (near) identical, MAD collapses to 0 and
            # the old `denom = max(mad, 1e-9)` produced z = ±(diff)/1e-9 → it
            # always pinned to the ±99 cap, manufacturing a "huge divergence"
            # out of a rounding-level difference and crowding genuine outliers
            # out of the top-K ranking. Fall back to the stdev when MAD is 0;
            # if there's no spread at all, the z is undefined → None.
            #
            # SCALE FIX: a raw MAD is ~0.6745× the stdev for normal data, so a
            # MAD-branch z and a stdev-fallback z lived on DIFFERENT scales yet
            # were ranked against each other in the top-K. Multiply MAD by the
            # consistency constant 1.4826 so 1.4826·MAD ≈ σ — now both branches
            # produce comparable, σ-equivalent z-scores.
            # SCALE-STABILITY FIX: at n==3 the MAD is frequently exactly 0
            # (two of three peers identical), so the branch would flip to the
            # stdev fallback on some metrics and use 1.4826·MAD on others —
            # ranking σ-equivalent z's against MAD-derived z's on DIFFERENT
            # effective scales within the same top-K. Require n>=4 before
            # trusting the MAD; below that, use the stdev UNIFORMLY for every
            # metric so all z's in the ranking share one scale. (1.4826·MAD ≈ σ
            # for normal data, so both branches stay σ-comparable when used.)
            if len(peer_vals) >= 4 and mad > 0:
                denom = 1.4826 * mad
            else:
                sd = _stdev(peer_vals)
                denom = sd if sd > 0 else None
            if denom is None:
                block["z_score"] = None
            else:
                z = (sym_val - med) / denom
                if math.isinf(z) or math.isnan(z):
                    z = 0.0
                z = max(-99.0, min(99.0, z))
                block["z_score"] = round(z, 3)
        else:
            block["z_score"] = None
        out[metric_name] = block
    return out


def _rank_top_divergences(
    metrics: Dict[str, Dict[str, Any]],
    symbol: str,
    top_k: int = 5,
) -> List[Dict[str, Any]]:
    """Pick the top-K metrics by absolute z-score (ties broken by metric name)."""
    sym = symbol.upper()
    ranked: List[Tuple[str, float, float, float]] = []  # (name, abs_z, signed_z, sym_val)
    for name, block in metrics.items():
        z = block.get("z_score")
        if z is None:
            continue
        sym_val = _safe_float(block.get(sym))
        if sym_val is None:
            continue
        peer_med = _safe_float(block.get("peer_median"))
        if peer_med is None:
            continue
        ranked.append((name, abs(float(z)), float(z), sym_val))
    ranked.sort(key=lambda r: (-r[1], r[0]))

    out: List[Dict[str, Any]] = []
    for name, _abs_z, signed_z, sym_val in ranked[:top_k]:
        peer_med = _safe_float(metrics[name].get("peer_median")) or 0.0
        out.append({
            "metric": name,
            "z_score": round(signed_z, 3),
            "symbol_value": sym_val,
            "peer_median": peer_med,
            "interpretation": _interpret(name, signed_z, sym_val, peer_med),
        })
    return out


def _compute_peer_divergence(symbol: str, n_peers: int) -> Dict[str, Any]:
    sym = symbol.upper()
    peers = discover_peers(sym, n_peers)
    if not peers:
        return {
            "symbol": sym,
            "peers": [],
            "n_peers": 0,
            "metrics": {},
            "top_divergences": [],
            "as_of": _now_iso(),
            "error": "no peers found (contagion graph empty and no industry fallback hit)",
        }

    # Pre-warm the shared congress trades cache so the N+1 metric workers
    # don't each kick off a 200-PDF download in parallel.
    try:
        _ensure_congress_trades_for([sym] + peers)
    except Exception as e:  # pragma: no cover
        log.warning("congress pre-warm failed: %s", e)

    raw_metrics, errors = _gather_metrics(sym, peers)
    enriched = _enrich_with_stats(sym, raw_metrics, peers)
    top = _rank_top_divergences(enriched, sym, top_k=5)

    # Attach metric descriptions in a sidecar dict so the JS can show
    # tooltips without hard-coding any of the labels.
    descriptions = {name: desc for (name, _fn, desc) in _METRIC_REGISTRY}

    envelope: Dict[str, Any] = {
        "symbol": sym,
        "peers": peers,
        "n_peers": len(peers),
        "metrics": enriched,
        "metric_descriptions": descriptions,
        "top_divergences": top,
        "as_of": _now_iso(),
    }
    if errors:
        # Cap errors list to keep responses bounded
        envelope["errors"] = errors[:50]
    return envelope


# ── Public entry ─────────────────────────────────────────────────────────
def peer_divergence(symbol: str, n_peers: int = 5) -> Dict[str, Any]:
    """Public API. Cached via ``cache_store.coalesce`` for 15 minutes."""
    if not symbol:
        return {"error": "no symbol", "as_of": _now_iso()}
    sym = symbol.upper().strip()
    try:
        n = int(n_peers)
    except (TypeError, ValueError):
        n = 5
    n = max(1, min(n, 12))

    if cache_store is None:
        return _compute_peer_divergence(sym, n)

    try:
        return cache_store.coalesce(
            ("peerdiv", sym, n),
            _CACHE_TTL_S,
            lambda: _compute_peer_divergence(sym, n),
        )
    except Exception as e:
        log.warning("cache_store.coalesce(peerdiv) failed: %s", e)
        return _compute_peer_divergence(sym, n)


# ── CLI smoke test ───────────────────────────────────────────────────────
if __name__ == "__main__":  # pragma: no cover
    import json as _json
    import sys

    sym = sys.argv[1] if len(sys.argv) > 1 else "NVDA"
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 5
    out = peer_divergence(sym, n)
    print(_json.dumps(out, indent=2, default=str))
