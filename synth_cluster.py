"""
synth_cluster.py — Multi-Source Signal Cluster Detection.

The thesis: when 2 sources agree on a stock, it's probably noise. When 4-5
agree in the same direction within a recent window, it's something worth
looking at. This module scans a universe of symbols and surfaces those
where many independent signal sources are firing in the same direction.

Each source is reduced to a single boolean (fired / didn't fire) for a
chosen direction. The composite score weighs sources by their estimated
reliability and incorporates how many fired.

Components (each contributes a fired-or-not boolean):
  - insider_form4         — Form 4 net buys/sells last 30 days
  - congress_60d          — Congressional net trades last 60 days
  - 13f_institutional     — Latest 13F / institutional ownership flow
  - ml_forecast           — rf_classifier.prob_up_20d threshold
  - narrative             — narrative phase ∈ {ACCELERATION, EMERGENCE}
  - smart_money           — smart_money_score > 65
  - alt_signals           — reddit/stocktwits sentiment > 0.5 (bull ratio)
  - options_skew          — put-call ratio < 0.7 / call OI growing
  - wiki_attention        — 7-day attention z-score > 1.0
  - reflexivity           — positive (bullish) feedback loop detected

Defensive design: any per-source exception logs and sets fired=False;
one bad source never crashes the whole scan for a symbol or universe.
"""

from __future__ import annotations

import hashlib
import logging
import math
import statistics
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

import safe_executor

log = logging.getLogger("augur.synth_cluster")

# ---------------------------------------------------------------------------
# Universe — SP500 top-100 by market cap (snapshot taken May 2026)
# Refresh this list periodically; we hardcode rather than scrape Wikipedia
# every call to keep the scan deterministic and offline-safe. A consumer
# can pass an override list via the `universe` arg.
# ---------------------------------------------------------------------------
SP500_TOP100: List[str] = [
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "GOOG", "META", "BRK.B", "LLY", "TSM",
    "AVGO", "TSLA", "JPM", "WMT", "V", "XOM", "UNH", "MA", "PG", "JNJ",
    "ORCL", "HD", "COST", "ABBV", "BAC", "NFLX", "KO", "CVX", "MRK", "AMD",
    "TMO", "ADBE", "CRM", "PEP", "ACN", "LIN", "MCD", "CSCO", "ABT", "WFC",
    "DHR", "TXN", "DIS", "QCOM", "INTU", "VZ", "CAT", "PM", "AMGN", "PFE",
    "AXP", "IBM", "GE", "NOW", "ISRG", "CMCSA", "GS", "T", "MS", "NEE",
    "RTX", "BX", "UBER", "SPGI", "PGR", "UNP", "AMAT", "HON", "BKNG", "LOW",
    "BLK", "TJX", "SYK", "C", "PLD", "MU", "VRTX", "ELV", "BSX", "MDT",
    "ADP", "SCHW", "LMT", "DE", "ETN", "GILD", "MMC", "REGN", "ADI", "PANW",
    "FI", "BMY", "CI", "KKR", "SBUX", "CB", "MO", "ANET", "AMT", "INTC",
]

# Source reliability weights — distilled from rough "how often does this
# precede meaningful directional move?" priors. Hand-tuned, not learned.
SOURCE_WEIGHTS: Dict[str, float] = {
    "insider_form4":      0.18,
    "congress_60d":       0.10,
    "13f_institutional":  0.10,
    "ml_forecast":        0.16,
    "narrative":          0.08,
    "smart_money":        0.14,
    "alt_signals":        0.06,
    "options_skew":       0.08,
    "wiki_attention":     0.04,
    "reflexivity":        0.06,
}

# Per-source timeout. EDGAR + congress PDF parsing can take 5–10s each
# on a cold cache; cap the whole symbol at ~25s so the 100-symbol scan
# always returns within a reasonable window.
SOURCE_TIMEOUT_S = 25.0
SYMBOL_TIMEOUT_S = 45.0
SCAN_TTL_S = 6 * 3600  # cache full scan results 6 hours

# Bullish narrative buckets vs bearish narrative buckets — informs how we
# interpret the dominant 7d bucket in narrative_engine output.
BULLISH_BUCKETS = {"GROWTH", "PROFITABILITY", "TURNAROUND", "M_AND_A"}
BEARISH_BUCKETS = {"REGULATORY", "SCANDAL", "MACRO"}

# Phases that signal direction is forming — analogue of "BREAKOUT / BUILDING"
ACTIVE_PHASES = {"ACCELERATION", "EMERGENCE"}


# ---------------------------------------------------------------------------
# Lazy module imports — each wrapped so an unavailable dependency simply
# disables that component rather than failing the whole scan.
# ---------------------------------------------------------------------------

def _try_import(name: str):
    try:
        return __import__(name)
    except Exception as e:
        log.warning("synth_cluster: cannot import %s: %s", name, e)
        return None


_modules_cache: Dict[str, Any] = {}
_modules_lock = threading.Lock()


def _mod(name: str):
    """Memoized import — modules stay loaded for the process lifetime."""
    with _modules_lock:
        if name not in _modules_cache:
            _modules_cache[name] = _try_import(name)
        return _modules_cache[name]


# ---------------------------------------------------------------------------
# Component evaluators — each returns (fired: bool, value: str, note: str)
# `fired` semantics depend on `direction`:
#   bullish: signal is positive / pro-buy
#   bearish: signal is negative / pro-sell
# Any internal error -> (False, "error", str(error_msg))
# ---------------------------------------------------------------------------

def _component_insider_form4(symbol: str, direction: str) -> Tuple[bool, str, str]:
    edgar = _mod("sec_edgar")
    if edgar is None:
        return (False, "n/a", "sec_edgar unavailable")
    try:
        txns = edgar.get_form4_transactions(symbol, limit=30) or []
    except Exception as e:
        return (False, "error", f"{type(e).__name__}: {e}")
    if not txns:
        return (False, "no data", "no Form 4 in 30d")

    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=30)
    buys = sells = 0
    buy_val = sell_val = 0.0
    for t in txns:
        date_str = t.get("date") or ""
        try:
            d = datetime.strptime(date_str[:10], "%Y-%m-%d")
        except Exception:
            continue
        if d < cutoff:
            continue
        val = float(t.get("value") or 0)
        ttype = (t.get("transaction_type") or "").upper()
        if ttype == "BUY":
            buys += 1
            buy_val += val
        elif ttype == "SELL":
            sells += 1
            sell_val += val
        # Unknown/blank transaction codes (e.g. derivative grants, exchanges)
        # are intentionally skipped — they used to fall through to the sell
        # bucket and bias the bearish branch.
    net_val = buy_val - sell_val
    if direction == "bullish":
        fired = buys > sells and net_val > 0
    else:
        fired = sells > buys and net_val < 0
    value = f"{buys}B/{sells}S net ${net_val/1000:.0f}k"
    note = f"{buys + sells} txns in 30d"
    return (fired, value, note)


def _component_congress_60d(symbol: str, direction: str) -> Tuple[bool, str, str]:
    congress = _mod("congress")
    if congress is None:
        return (False, "n/a", "congress unavailable")
    try:
        # get_trades_for_ticker calls get_recent_trades with days=180; we
        # then filter locally to a 60d window so the cached results are
        # broadly reusable across scans with different window choices.
        trades = congress.get_trades_for_ticker(symbol, days=180) or []
    except Exception as e:
        return (False, "error", f"{type(e).__name__}: {e}")
    if not trades:
        return (False, "no data", "no congressional trades")

    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=60)
    buys = sells = 0
    buy_val = sell_val = 0.0
    for t in trades:
        # txn_dt may be a datetime or None depending on cache rehydration
        dt = t.get("txn_dt")
        if dt is None:
            td = t.get("txn_date")
            if td:
                try:
                    dt = datetime.strptime(td, "%m/%d/%Y")
                except Exception:
                    dt = None
        if dt is None or dt < cutoff:
            continue
        # amount may be {min, max} dict or a raw number, depending on the
        # parser version. Use the midpoint when we have a range.
        amt = t.get("amount") or t.get("amount_mid") or 0
        if isinstance(amt, dict):
            lo = float(amt.get("min") or 0)
            hi = float(amt.get("max") or 0)
            amt = (lo + hi) / 2.0 if (lo or hi) else 0
        try:
            amt = float(amt)
        except Exception:
            amt = 0
        ttype = (t.get("transaction_type") or t.get("txn_type") or "").lower()
        if "purchase" in ttype or "buy" in ttype:
            buys += 1
            buy_val += amt
        elif "sale" in ttype or "sell" in ttype:
            sells += 1
            sell_val += amt
    net = buy_val - sell_val
    if direction == "bullish":
        fired = buys >= sells and net > 0
    else:
        fired = sells > buys and net < 0
    value = f"${net/1000:+.0f}k net"
    note = f"{buys} buys / {sells} sells in 60d"
    return (fired, value, note)


def _component_13f_institutional(symbol: str, direction: str) -> Tuple[bool, str, str]:
    """Approximation: yfinance institutionalOwnership/heldPercentInstitutions
    is what's actually available without scraping every 13F. A true 13F net
    flow needs aggregation across all funds holding the ticker, which is a
    distinct backfill project. We approximate using ownership level + a
    short-interest contra-signal — strong institutional grip + low shorts
    proxies "smart money long, weak hands not pressing"."""
    # Route through fetcher.get_fundamentals which surfaces the same
    # institutional/short fields and inherits the Yahoo + Finviz fallback
    # chain. A bare yf.Ticker(...).info call dies silently whenever
    # yfinance's crumb auth is rate-limited (v0.1.6 audit pattern).
    fetcher = _mod("fetcher")
    info: Dict[str, Any] = {}
    if fetcher is not None and hasattr(fetcher, "get_fundamentals"):
        try:
            info = fetcher.get_fundamentals(symbol) or {}
        except Exception as e:
            return (False, "error", f"{type(e).__name__}: {e}")
    if not info:
        # No direct yf.Ticker(symbol).info fallback: that path silently fails
        # whenever Yahoo's crumb auth is rate-limited (v0.1.6 audit pattern,
        # called out in the comment above). If fetcher.get_fundamentals
        # already returned empty, every fallback inside it (Yahoo HTML +
        # Finviz scrape) has also missed; firing the raw yfinance call here
        # just adds another 429 to the budget without producing data.
        return (False, "no data", "fundamentals unavailable from fetcher chain")

    inst = (
        info.get("heldPercentInstitutions")
        or info.get("institutionPercentHeld")
        or info.get("inst_pct")
    )
    short_pct = info.get("shortPercentOfFloat") or info.get("short_pct")
    try:
        inst_f = float(inst) if inst is not None else None
        short_f = float(short_pct) if short_pct is not None else None
    except Exception:
        inst_f, short_f = None, None

    if inst_f is None and short_f is None:
        return (False, "no data", "no institutional metrics")

    if direction == "bullish":
        # Healthy institutional ownership + low shorts
        fired = (inst_f is not None and inst_f > 0.65) and (short_f is None or short_f < 0.05)
    else:
        # Low institutional ownership + heavy shorts
        fired = (inst_f is not None and inst_f < 0.40) or (short_f is not None and short_f > 0.10)

    inst_str = f"{inst_f*100:.0f}%" if inst_f is not None else "?"
    short_str = f"{short_f*100:.1f}%" if short_f is not None else "?"
    value = f"inst {inst_str} / short {short_str}"
    return (fired, value, "yfinance proxy for 13F flow")


def _component_ml_forecast(symbol: str, direction: str) -> Tuple[bool, str, str]:
    ml = _mod("ml_forecast")
    if ml is None:
        return (False, "n/a", "ml_forecast unavailable")
    try:
        result = ml.ml_forecast(symbol) or {}
    except Exception as e:
        return (False, "error", f"{type(e).__name__}: {e}")
    if "error" in result:
        return (False, "no data", result.get("error", "ml error"))

    rf = result.get("rf_classifier")
    prob_up = (rf or {}).get("prob_up_20d") if rf else None
    composite = result.get("composite_ml_score")

    # Prefer the explicit RF prob, fall back to composite
    val = prob_up if prob_up is not None else composite
    if val is None:
        return (False, "no data", "no ml probability")
    try:
        valf = float(val)
    except Exception:
        return (False, "no data", "non-numeric ml probability")

    if direction == "bullish":
        fired = valf > 0.60
    else:
        fired = valf < 0.40
    return (fired, f"{valf:.2f} prob_up", "ML composite")


def _component_narrative(symbol: str, direction: str) -> Tuple[bool, str, str]:
    ne = _mod("narrative_engine")
    if ne is None:
        return (False, "n/a", "narrative_engine unavailable")
    try:
        result = ne.analyze_narrative(symbol) or {}
    except Exception as e:
        return (False, "error", f"{type(e).__name__}: {e}")
    if "error" in result:
        return (False, "no data", str(result.get("error"))[:60])

    phase = (result.get("narrative_phase") or "").upper()
    dom = (result.get("dominant_narrative") or "").upper()
    velocity = result.get("velocity_score") or 0
    in_active = phase in ACTIVE_PHASES

    if direction == "bullish":
        # Bullish: dominant bucket is bullish AND phase is breakout/building,
        # or velocity strongly positive
        fired = (in_active and dom in BULLISH_BUCKETS) or velocity >= 40
    else:
        fired = (in_active and dom in BEARISH_BUCKETS) or velocity <= -40

    return (fired, f"{phase}/{dom}", f"velocity {velocity}")


def _component_smart_money(symbol: str, direction: str) -> Tuple[bool, str, str]:
    sm = _mod("smart_money")
    if sm is None:
        return (False, "n/a", "smart_money unavailable")
    try:
        result = sm.compute_score(symbol) or {}
    except Exception as e:
        return (False, "error", f"{type(e).__name__}: {e}")
    score = result.get("score")
    if score is None:
        return (False, "no data", result.get("error", "no score"))
    try:
        scoref = float(score)
    except Exception:
        return (False, "no data", "non-numeric score")
    if direction == "bullish":
        fired = scoref > 65
    else:
        fired = scoref < 35
    return (fired, f"score {scoref:.0f}", result.get("signal", ""))


def _component_alt_signals(symbol: str, direction: str) -> Tuple[bool, str, str]:
    alt = _mod("alt_signals")
    if alt is None:
        return (False, "n/a", "alt_signals unavailable")
    try:
        result = alt.stocktwits_symbol_sentiment(symbol)
    except Exception as e:
        return (False, "error", f"{type(e).__name__}: {e}")
    if not result:
        return (False, "no data", "stocktwits empty")
    ratio = result.get("bull_ratio")
    total = (result.get("bullish") or 0) + (result.get("bearish") or 0)
    if ratio is None or total < 5:
        return (False, "no data", f"only {total} sentiment msgs")
    if direction == "bullish":
        fired = ratio > 0.65
    else:
        fired = ratio < 0.35
    return (fired, f"bull {ratio:.0%}", f"{total} stocktwits msgs")


def _component_options_skew(symbol: str, direction: str) -> Tuple[bool, str, str]:
    """Put-call ratio + call OI growth heuristic over front-month chain."""
    fetcher = _mod("fetcher")
    if fetcher is None:
        return (False, "n/a", "fetcher unavailable")
    try:
        dates = fetcher.get_options_dates(symbol) or []
        if not dates:
            return (False, "no data", "no options chain")
        chain = fetcher.get_option_chain(symbol, dates[0]) or {}
    except Exception as e:
        return (False, "error", f"{type(e).__name__}: {e}")

    calls = chain.get("calls") or []
    puts = chain.get("puts") or []
    if not calls and not puts:
        return (False, "no data", "empty chain")

    def _sum_oi(rows):
        s = 0
        for r in rows:
            try:
                s += int(r.get("openInterest") or 0)
            except Exception:
                pass
        return s

    def _sum_vol(rows):
        s = 0
        for r in rows:
            try:
                s += int(r.get("volume") or 0)
            except Exception:
                pass
        return s

    call_oi = _sum_oi(calls)
    put_oi = _sum_oi(puts)
    call_vol = _sum_vol(calls)
    put_vol = _sum_vol(puts)
    if call_oi == 0 and put_oi == 0:
        return (False, "no data", "no OI")
    pcr_oi = put_oi / call_oi if call_oi else float("inf")
    pcr_vol = put_vol / call_vol if call_vol else float("inf")

    if direction == "bullish":
        fired = pcr_oi < 0.70 or pcr_vol < 0.70
    else:
        fired = pcr_oi > 1.30 or pcr_vol > 1.30
    return (fired, f"PCR_oi {pcr_oi:.2f}", f"vol PCR {pcr_vol:.2f}")


def _component_wiki_attention(symbol: str, direction: str) -> Tuple[bool, str, str]:
    """Z-score of latest pageviews vs prior baseline. >1.0 = anomalous
    interest spike. Direction-agnostic on its own; we tag it as supporting
    the direction whenever attention is up (retail follows trend more often
    than not, especially in the bullish case)."""
    wiki = _mod("wiki_attention")
    if wiki is None:
        return (False, "n/a", "wiki_attention unavailable")
    try:
        result = wiki.fetch_pageviews(symbol, days=30) or {}
    except Exception as e:
        return (False, "error", f"{type(e).__name__}: {e}")
    if "error" in result and not result.get("points"):
        return (False, "no data", str(result.get("error"))[:60])
    points = result.get("points") or []
    if len(points) < 10:
        return (False, "no data", f"only {len(points)} days")
    views = [p.get("views") or 0 for p in points]
    latest = views[-1]
    prior = views[:-1]
    mean = statistics.mean(prior) if prior else 0
    stdev = statistics.pstdev(prior) if len(prior) > 1 else 0
    if stdev <= 0:
        return (False, "no data", "no variance")
    z = (latest - mean) / stdev
    # Attention spikes are more often bullish (people googling a name they
    # heard pumped). We flag both directions when |z| is high but only
    # 'fire' on the matching side.
    if direction == "bullish":
        fired = z > 1.0
    else:
        # Bearish only when attention spikes AND price likely under pressure —
        # without the price context, treat large negative deviations (drying
        # interest) as bearish signal.
        fired = z < -1.0
    return (fired, f"z {z:+.2f}", f"latest {latest:,} vs μ {mean:.0f}")


def _component_reflexivity(symbol: str, direction: str) -> Tuple[bool, str, str]:
    refl = _mod("reflexivity_detector")
    if refl is None:
        return (False, "n/a", "reflexivity_detector unavailable")
    try:
        result = refl.detect_loops(symbol) or {}
    except Exception as e:
        return (False, "error", f"{type(e).__name__}: {e}")
    if "error" in result:
        return (False, "no data", str(result.get("error"))[:60])

    loops = result.get("active_loops") or []
    detected = [lp for lp in loops if lp.get("detected")]
    if not detected:
        return (False, "no loop", result.get("overall_risk", "NONE"))

    # Map loop direction to bull/bear
    bullish_loops = [lp for lp in detected if (lp.get("direction") or "").upper() in ("UP", "BULLISH", "POSITIVE")]
    bearish_loops = [lp for lp in detected if (lp.get("direction") or "").upper() in ("DOWN", "BEARISH", "NEGATIVE")]

    if direction == "bullish":
        fired = bool(bullish_loops)
        sample = bullish_loops[0] if bullish_loops else (detected[0] if detected else {})
    else:
        fired = bool(bearish_loops)
        sample = bearish_loops[0] if bearish_loops else (detected[0] if detected else {})

    loop_type = sample.get("type") or result.get("dominant_loop") or "?"
    strength = result.get("max_strength", 0)
    return (fired, f"{loop_type}", f"strength {strength}")


COMPONENTS: List[Tuple[str, Callable[[str, str], Tuple[bool, str, str]]]] = [
    ("insider_form4",     _component_insider_form4),
    ("congress_60d",      _component_congress_60d),
    ("13f_institutional", _component_13f_institutional),
    ("ml_forecast",       _component_ml_forecast),
    ("narrative",         _component_narrative),
    ("smart_money",       _component_smart_money),
    ("alt_signals",       _component_alt_signals),
    ("options_skew",      _component_options_skew),
    ("wiki_attention",    _component_wiki_attention),
    ("reflexivity",       _component_reflexivity),
]

# Early-exit split. The "cheap" components sit on self-caching upstreams
# (EDGAR + congress via cache_store, fundamentals 24h, narrative 30min,
# stocktwits/wiki = one HTTP each, options skew = two small HTTP calls).
# The "expensive" three are the heavy engines: ml_forecast trains models on
# a cold cache, smart_money fans out to ~6 upstreams, reflexivity runs the
# loop detector. When the cheap phase leaves a symbol mathematically unable
# to reach min_sources even if every expensive component fired, the
# expensive phase is skipped — the symbol can never surface in `clusters`,
# so its envelope (marked "skipped") is observably identical to the caller.
_EXPENSIVE_COMPONENT_NAMES = ("ml_forecast", "smart_money", "reflexivity")
_CHEAP_IDX: List[int] = [i for i, (n, _) in enumerate(COMPONENTS)
                         if n not in _EXPENSIVE_COMPONENT_NAMES]
_EXPENSIVE_IDX: List[int] = [i for i, (n, _) in enumerate(COMPONENTS)
                             if n in _EXPENSIVE_COMPONENT_NAMES]


# ---------------------------------------------------------------------------
# Per-symbol scan
# ---------------------------------------------------------------------------

def _eval_component_safe(name, fn, symbol, direction):
    """Wrap a component call so any exception is caught here, not in the
    per-symbol coordinator. Returns the result dict shape directly."""
    try:
        fired, value, note = fn(symbol, direction)
    except Exception as e:
        log.warning("synth_cluster: %s/%s failed: %s", symbol, name, e)
        fired, value, note = False, "error", f"{type(e).__name__}: {e}"
    return {
        "name":  name,
        "fired": bool(fired),
        "value": str(value)[:80] if value is not None else "",
        "note":  str(note)[:160] if note is not None else "",
    }


def _finalize_scan(symbol: str, sources: List[Optional[Dict[str, Any]]]) -> Dict[str, Any]:
    """Fill any empty component slots, compute the composite, and return the
    per-symbol envelope dict. Called from every return path in _scan_symbol —
    including the interpreter-shutdown fallbacks, which previously returned the
    raw `sources` list and made the caller's `.get()` blow up (AttributeError)
    on the exact shutdown path the fallback was meant to survive."""
    for i, (name, _) in enumerate(COMPONENTS):
        if sources[i] is None:
            sources[i] = {"name": name, "fired": False,
                          "value": "timeout", "note": "future not completed"}

    n_fired = sum(1 for s in sources if s.get("fired"))
    total_w = sum(SOURCE_WEIGHTS.values()) or 1.0
    weighted = sum(SOURCE_WEIGHTS.get(s["name"], 0) for s in sources if s.get("fired"))
    base = weighted / total_w
    bonus = max(0, n_fired - 2) ** 1.5 * 0.04
    composite = min(1.0, base + bonus)
    return {
        "symbol":           symbol,
        "n_sources_firing": n_fired,
        "sources":          sources,
        "composite_score":  round(composite, 4),
    }


def _scan_symbol(symbol: str, direction: str,
                 min_sources: Optional[int] = None) -> Dict[str, Any]:
    """Run all components for one symbol, in parallel, with a hard timeout
    per component. Returns the per-symbol cluster dict (no min_sources
    filtering yet — caller decides whether to include).

    When `min_sources` is given, the components run in two phases (cheap
    then expensive — see _EXPENSIVE_COMPONENT_NAMES) and the expensive phase
    is skipped when the cheap phase already proves the symbol can't reach
    min_sources. Skipped symbols can never appear in the clusters output, so
    callers observe identical results either way.
    """
    symbol = symbol.upper().strip()

    # Per-symbol pool (small so we don't hammer upstreams). The outer
    # scanner runs ~6 symbols at once, this runs ~6 components per
    # symbol — total ceiling of ~36 in-flight upstream HTTP calls, which
    # is still polite by Yahoo/EDGAR standards because each upstream has
    # its own per-source caching.
    #
    # safe_executor.parallel_map runs on daemon threads (so stragglers can't
    # block interpreter exit the way abandoned TPE workers did), enforces
    # SYMBOL_TIMEOUT_S as the same overall deadline the old
    # as_completed(timeout=SYMBOL_TIMEOUT_S) loop had, and falls back to a
    # serial loop by itself when threads can't be spawned.
    def _eval_slot(i: int) -> Dict[str, Any]:
        name, fn = COMPONENTS[i]
        try:
            return _eval_component_safe(name, fn, symbol, direction)
        except Exception as exc:
            return {"name": name, "fired": False, "value": "error",
                    "note": f"{type(exc).__name__}: {exc}"}

    t0 = time.time()
    sources: List[Optional[Dict[str, Any]]] = [None] * len(COMPONENTS)

    # Phase 1 — cheap components. Slots still None after the deadline are
    # marked as timed-out by _finalize_scan ("future not completed").
    cheap_results = safe_executor.parallel_map(
        _eval_slot,
        _CHEAP_IDX,
        max_workers=6,
        timeout_per_item=SYMBOL_TIMEOUT_S,
        thread_name_prefix=f"sc-{symbol}",
    )
    for i, res in zip(_CHEAP_IDX, cheap_results):
        sources[i] = res
    cheap_fired = sum(1 for r in cheap_results if r and r.get("fired"))

    # Early exit: even if EVERY expensive component fired, the symbol still
    # couldn't reach min_sources — don't pay for ml/smart-money/reflexivity.
    if min_sources is not None and cheap_fired + len(_EXPENSIVE_IDX) < min_sources:
        for i in _EXPENSIVE_IDX:
            sources[i] = {"name": COMPONENTS[i][0], "fired": False,
                          "value": "skipped",
                          "note": "early-exit: cheap sources below min_sources"}
    else:
        # Phase 2 — expensive components, on whatever remains of the
        # per-symbol budget so the SYMBOL_TIMEOUT_S cap still holds overall.
        # ml_forecast goes FIRST, alone: smart_money's internal ML part then
        # hits ml_forecast's 1h module cache instead of racing a duplicate
        # model training for the same symbol (ml_forecast has no in-flight
        # coalescing, so two concurrent calls both pay the full training).
        ml_idx = [i for i in _EXPENSIVE_IDX if COMPONENTS[i][0] == "ml_forecast"]
        rest_idx = [i for i in _EXPENSIVE_IDX if COMPONENTS[i][0] != "ml_forecast"]
        remaining = max(5.0, SYMBOL_TIMEOUT_S - (time.time() - t0))
        ml_results = safe_executor.parallel_map(
            _eval_slot,
            ml_idx,
            max_workers=1,
            timeout_per_item=remaining,
            thread_name_prefix=f"sc-{symbol}",
        )
        for i, res in zip(ml_idx, ml_results):
            sources[i] = res
        remaining = max(5.0, SYMBOL_TIMEOUT_S - (time.time() - t0))
        exp_results = safe_executor.parallel_map(
            _eval_slot,
            rest_idx,
            max_workers=2,
            timeout_per_item=remaining,
            thread_name_prefix=f"sc-{symbol}",
        )
        for i, res in zip(rest_idx, exp_results):
            sources[i] = res

    # Composite score: weighted sum of fired flags / sum of all weights, plus
    # a nonlinear bonus for breadth of agreement. See _finalize_scan.
    return _finalize_scan(symbol, sources)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def _normalize_universe(universe) -> Tuple[List[str], str]:
    """Resolve a universe spec into (symbols, label). Honors:
      - None / "" / "sp500_top100"      -> SP500_TOP100
      - list/tuple                      -> deduped uppercase, cap 100
    """
    if universe is None or universe == "" or universe == "sp500_top100":
        return list(SP500_TOP100), "sp500_top100"
    if isinstance(universe, (list, tuple)):
        seen = []
        for s in universe:
            if not s:
                continue
            su = str(s).upper().strip()
            if su and su not in seen:
                seen.append(su)
        seen = seen[:100]
        label = f"custom-{len(seen)}"
        return seen, label
    if isinstance(universe, str):
        # comma-separated string
        parts = [p.strip().upper() for p in universe.split(",") if p.strip()]
        return parts[:100], f"custom-{min(len(parts), 100)}"
    return list(SP500_TOP100), "sp500_top100"


def _universe_hash(symbols: List[str]) -> str:
    """Short stable hash of the universe for cache keys."""
    payload = ",".join(symbols).encode("utf-8")
    return hashlib.sha1(payload).hexdigest()[:10]


def cluster_scan(
    universe: Optional[Any] = None,
    direction: str = "bullish",
    min_sources: int = 4,
) -> Dict[str, Any]:
    """Scan a universe and return symbols where at least `min_sources`
    independent components agree on the same direction.

    Args:
        universe:    None | "sp500_top100" | list[str]  (cap 100)
        direction:   "bullish" or "bearish"
        min_sources: 1..len(COMPONENTS) — minimum sources required to
                     include a symbol in the clusters output.

    Returns: dict matching the spec (see module docstring).
    """
    direction = (direction or "bullish").lower()
    if direction not in ("bullish", "bearish"):
        direction = "bullish"
    try:
        min_sources = int(min_sources)
    except Exception:
        min_sources = 4
    min_sources = max(1, min(len(COMPONENTS), min_sources))

    symbols, universe_label = _normalize_universe(universe)
    if not symbols:
        return {
            "universe":         universe_label,
            "direction":        direction,
            "min_sources":      min_sources,
            "as_of":            datetime.now(tz=timezone.utc).isoformat(),
            "n_symbols_scanned": 0,
            "clusters":         [],
            "error":            "empty universe",
        }

    cache_store = _mod("cache_store")
    uhash = _universe_hash(symbols)
    cache_key = ("clusterscan", direction, min_sources, uhash)

    def _do_scan():
        t0 = time.time()
        results: List[Dict[str, Any]] = []

        # Outer pool: 6 symbols at a time. Each scans its 10 components
        # internally in a smaller inner pool. The inner pools are short-lived
        # so total live threads stay bounded (~6 * 6 = ~36 worst case).
        # safe_executor.parallel_map handles the can't-spawn-threads fallback
        # (serial loop) by itself; each symbol is internally bounded by
        # SYMBOL_TIMEOUT_S inside _scan_symbol, matching the old per-future
        # result(timeout=SYMBOL_TIMEOUT_S + 5) intent without an extra cap.
        def _scan_or_placeholder(sym):
            try:
                return _scan_symbol(sym, direction, min_sources)
            except Exception as e:
                log.warning("synth_cluster: %s outer failed: %s", sym, e)
                return None

        raw = safe_executor.parallel_map(
            _scan_or_placeholder, symbols, max_workers=6,
            thread_name_prefix="sc-outer",
        )
        for sym, res in zip(symbols, raw):
            if res is None:
                res = {
                    "symbol":           sym,
                    "n_sources_firing": 0,
                    "sources":          [{"name": n, "fired": False, "value": "skipped",
                                          "note": "symbol-level failure"} for n, _ in COMPONENTS],
                    "composite_score":  0.0,
                }
            results.append(res)

        # Filter by min_sources, then sort by composite_score desc
        clusters = [r for r in results if r.get("n_sources_firing", 0) >= min_sources]
        clusters.sort(key=lambda r: r.get("composite_score", 0), reverse=True)

        elapsed = round(time.time() - t0, 2)
        return {
            "universe":         universe_label,
            "direction":        direction,
            "min_sources":      min_sources,
            "as_of":            datetime.now(tz=timezone.utc).isoformat(),
            "n_symbols_scanned": len(symbols),
            "clusters":         clusters,
            "elapsed_seconds":  elapsed,
        }

    # Persistent cache — full-universe scans take 30-90s, so we keep
    # results for 6h. A consumer who wants a fresh scan can call with a
    # custom universe (different hash) or bust the cache externally.
    if cache_store is not None:
        try:
            return cache_store.coalesce(cache_key, SCAN_TTL_S, _do_scan)
        except Exception as e:
            log.warning("synth_cluster: cache layer failed (%s); running uncached", e)
    return _do_scan()


# ---------------------------------------------------------------------------
# CLI smoke test — `python -m synth_cluster`
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import json
    logging.basicConfig(level=logging.INFO)
    print("Running small smoke test (5 symbols, min_sources=3)...")
    out = cluster_scan(
        universe=["AAPL", "MSFT", "NVDA", "META", "GOOGL"],
        direction="bullish",
        min_sources=3,
    )
    # Trim for readability
    print(json.dumps({
        "universe":         out["universe"],
        "direction":        out["direction"],
        "min_sources":      out["min_sources"],
        "n_symbols_scanned": out["n_symbols_scanned"],
        "elapsed_seconds":  out.get("elapsed_seconds"),
        "clusters_count":   len(out["clusters"]),
        "clusters":         out["clusters"],
    }, indent=2, default=str))
