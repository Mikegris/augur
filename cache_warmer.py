"""
Background cache warmer — keeps hot data already populated when users arrive.

Problem this solves: even with a persistent cache, after a long pause (e.g.
overnight, weekend) the first user navigation triggers a flurry of API
calls as caches expire across panels in rapid succession. Under Yahoo /
Finviz rate-limits that's enough to throttle a fresh IP. The warmer keeps
the hot endpoints continuously fresh on a schedule, so user requests just
read from cache.

Cadences (chosen for two goals: stay well below upstream rate limits, AND
keep data fresh enough to be useful):

  every 90s   indices, sector heatmap, top movers
  every 180s  macro indicators (5min TTL — we refresh inside that window)
  every 300s  portfolio + watchlist quotes batch
  every 6h    fundamentals + dividends for portfolio symbols
  every 6h    news for portfolio symbols
  every 12h   benchmark history (SPY)

Each "cycle" runs sequentially with small sleeps between requests so we
never burst the upstream API. The thread is a daemon (doesn't block app
shutdown) and silently no-ops if any module isn't available.

Python 3.9 compatible.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional

log = logging.getLogger("augur.warmer")

# Tunables — exposed as module-level so a future settings panel can adjust.
BOOT_DELAY_SECONDS = 25
MARKET_INTERVAL = 90
MACRO_INTERVAL = 180
QUOTES_INTERVAL = 300
# Fundamentals TTL in fetcher is 86400 (24h). A 6h warmer cycle would fire
# 4× per day but only the first cycle hits the upstream — the other 3 just
# read from cache and look stale in `status()`. Match the warmer to half
# the TTL so we refresh once per day in the middle of the TTL window.
FUNDAMENTALS_INTERVAL = 12 * 3600
# News TTL is 900s (15 min) in fetcher.get_news, so 6h here actually means
# we refresh well inside the TTL window — leave it alone.
NEWS_INTERVAL = 6 * 3600
BENCHMARK_INTERVAL = 12 * 3600
CHART_INTERVAL = 12 * 3600         # default research charts (6mo daily)
SCORE_INTERVAL = 6 * 3600          # research_tracker scoring loop
HYPOTHESIS_SCORE_INTERVAL = 24 * 3600  # research_hypothesis daily scoring
# v0.3.0 synthesis: heavy fan-out scans worth pre-warming so the first
# user click is instant. Each scan covers ~100 symbols × N sources and
# would otherwise take 30-90s on a cold cache.
CLUSTER_INTERVAL = 12 * 3600       # synth_cluster bull+bear scans
DIVMAP_INTERVAL = 6 * 3600         # synth_divmap divergence scan
SECTORFLOW_INTERVAL = 30 * 60      # synth_sectorflow heatmap (11 ETFs × 12 signals)
INTER_REQUEST_DELAY = 1.2          # spacing between requests within a cycle

# Cap how many portfolio symbols we warm per cycle so an unusually large
# portfolio doesn't blow our request budget. The user sees a stale row
# rather than a banned IP.
PORTFOLIO_WARM_CAP = 25

_thread: Optional[threading.Thread] = None
_started = False
_started_lock = threading.Lock()
_last_cycle: dict = {}              # task -> unix ts of last successful run


def _safe(label: str, fn, *args, **kwargs):
    """Wrap a warm call so a single endpoint failure doesn't kill the thread.
    Records the timestamp on success for the status endpoint."""
    try:
        fn(*args, **kwargs)
        _last_cycle[label] = time.time()
    except Exception as e:
        log.debug("warmer %s failed: %s", label, e)


def _portfolio_symbols():
    """Best-effort fetch of (equity, crypto) symbol lists. Empty on failure."""
    try:
        import database as db
        holdings = db.get_portfolio() or []
        eq = [h["symbol"] for h in holdings if h.get("asset_type") != "crypto"]
        crypto = [h["symbol"] for h in holdings if h.get("asset_type") == "crypto"]
        return eq[:PORTFOLIO_WARM_CAP], crypto[:PORTFOLIO_WARM_CAP]
    except Exception:
        return [], []


def _watchlist_symbols():
    try:
        import database as db
        rows = db.get_watchlist() or []
        return [r["symbol"] for r in rows][:PORTFOLIO_WARM_CAP]
    except Exception:
        return []


def _loop():
    log.info("cache warmer thread starting — boot delay %ds", BOOT_DELAY_SECONDS)
    time.sleep(BOOT_DELAY_SECONDS)

    try:
        import fetcher
    except Exception as e:
        log.warning("warmer: fetcher import failed, exiting: %s", e)
        return

    while True:
        now = time.time()

        # ── fast cadence: market data ────────────────────────────────
        if now - _last_cycle.get("indices", 0) >= MARKET_INTERVAL:
            _safe("indices", fetcher.get_market_indices)
            time.sleep(INTER_REQUEST_DELAY)
        if now - _last_cycle.get("sectors", 0) >= MARKET_INTERVAL:
            _safe("sectors", fetcher.get_sector_performance)
            time.sleep(INTER_REQUEST_DELAY)
        if now - _last_cycle.get("movers", 0) >= MARKET_INTERVAL:
            _safe("movers", fetcher.get_top_movers)
            time.sleep(INTER_REQUEST_DELAY)

        # ── medium cadence: macro ────────────────────────────────────
        if now - _last_cycle.get("macro", 0) >= MACRO_INTERVAL:
            _safe("macro", fetcher.get_macro_indicators)
            time.sleep(INTER_REQUEST_DELAY)

        # ── portfolio quotes (cheap when batched) ────────────────────
        if now - _last_cycle.get("quotes", 0) >= QUOTES_INTERVAL:
            eq, crypto = _portfolio_symbols()
            wl = _watchlist_symbols()
            all_eq = list(dict.fromkeys(eq + wl))  # dedupe, keep order
            if all_eq:
                _safe("quotes", fetcher.get_quotes_batch, all_eq)
                time.sleep(INTER_REQUEST_DELAY)
            if crypto:
                yf_crypto = [s + "-USD" for s in crypto]
                _safe("quotes_crypto", fetcher.get_quotes_batch, yf_crypto)
                time.sleep(INTER_REQUEST_DELAY)

        # ── slow cadence: fundamentals + news + benchmark ────────────
        if now - _last_cycle.get("fundamentals", 0) >= FUNDAMENTALS_INTERVAL:
            eq, _ = _portfolio_symbols()
            for sym in eq:
                _safe("fundamentals", fetcher.get_fundamentals, sym)
                time.sleep(INTER_REQUEST_DELAY)
            _last_cycle["fundamentals"] = time.time()

        if now - _last_cycle.get("news", 0) >= NEWS_INTERVAL:
            eq, _ = _portfolio_symbols()
            for sym in eq:
                _safe("news", fetcher.get_news, sym)
                time.sleep(INTER_REQUEST_DELAY)
            _last_cycle["news"] = time.time()

        if now - _last_cycle.get("benchmark", 0) >= BENCHMARK_INTERVAL:
            _safe("benchmark", fetcher.get_benchmark_history, "SPY", "1y")
            time.sleep(INTER_REQUEST_DELAY)

        # Chart history for portfolio + watchlist symbols at the Research
        # view's default period (6mo / 1d). Without this, the first time a
        # user clicks Research the chart panel is empty until they wait for
        # a fresh yfinance fetch — which is the exact moment we don't want
        # to pile on rate-limited upstreams.
        if now - _last_cycle.get("chart", 0) >= CHART_INTERVAL:
            eq, _ = _portfolio_symbols()
            wl = _watchlist_symbols()
            symbols = list(dict.fromkeys(eq + wl))[:PORTFOLIO_WARM_CAP]
            for sym in symbols:
                _safe("chart", fetcher.get_chart_data, sym, "6mo", "1d")
                time.sleep(INTER_REQUEST_DELAY)
            _last_cycle["chart"] = time.time()

        # ── score-due forecasts cadence: every 6h ────────────────────────
        # research_tracker queues a row each time a tracked signal panel
        # is hit. After `horizon_days` elapse we price each against the
        # close and compute hit-rate. Module owns its own DB connection;
        # the scorer is idempotent and capped at 200 rows/call so a long
        # backlog can't pin the warmer for minutes.
        if now - _last_cycle.get("tracker_score", 0) >= SCORE_INTERVAL:
            try:
                import research_tracker
                _safe("tracker_score", research_tracker.score_due_forecasts)
            except Exception as e:
                log.debug("tracker_score skipped: %s", e)
            time.sleep(INTER_REQUEST_DELAY)

        # ── score-due hypotheses cadence: every 24h ──────────────────────
        # research_hypothesis owns its own `hypotheses` table; scoring
        # walks all open rows whose horizon has elapsed and computes
        # realized vs predicted return + direction match.
        if now - _last_cycle.get("hypothesis_score", 0) >= HYPOTHESIS_SCORE_INTERVAL:
            try:
                import research_hypothesis
                _safe("hypothesis_score", research_hypothesis.score_due_hypotheses)
            except Exception as e:
                log.debug("hypothesis_score skipped: %s", e)
            time.sleep(INTER_REQUEST_DELAY)

        # ── v0.3.0 synth_cluster cadence: every 12h, bull + bear ─────────
        # A full SP500-top-100 scan takes 30-90s on cold cache; warm both
        # directions so the first user click on the CLUSTER view is instant.
        # Offsetting the bear scan after a small delay keeps the EDGAR /
        # yfinance call burst spread out.
        if now - _last_cycle.get("cluster_bull", 0) >= CLUSTER_INTERVAL:
            try:
                import synth_cluster
                _safe("cluster_bull", synth_cluster.cluster_scan,
                      universe=None, direction="bullish", min_sources=4)
            except Exception as e:
                log.debug("cluster_bull skipped: %s", e)
            time.sleep(INTER_REQUEST_DELAY)
        if now - _last_cycle.get("cluster_bear", 0) >= CLUSTER_INTERVAL:
            try:
                import synth_cluster
                _safe("cluster_bear", synth_cluster.cluster_scan,
                      universe=None, direction="bearish", min_sources=4)
            except Exception as e:
                log.debug("cluster_bear skipped: %s", e)
            time.sleep(INTER_REQUEST_DELAY)

        # ── v0.3.0 synth_divmap cadence: every 6h ────────────────────────
        # The divergence map runs ~100 symbols × 5 signal pairs and is
        # cached server-side for 1h. Warming every 6h keeps the default
        # SP500-top-100 result fresh for users hitting the DIVERGENCES view.
        if now - _last_cycle.get("divmap", 0) >= DIVMAP_INTERVAL:
            try:
                import synth_divmap
                if hasattr(synth_divmap, "warm_default_scan"):
                    _safe("divmap", synth_divmap.warm_default_scan)
                else:
                    _safe("divmap", synth_divmap.divergence_map,
                          universe="sp500_top100", top_n=20)
            except Exception as e:
                log.debug("divmap skipped: %s", e)
            time.sleep(INTER_REQUEST_DELAY)

        # ── v0.3.3 synth_sectorflow cadence: every 30 min ────────────────
        # The sector-flow heatmap fans 11 sector ETFs against ~12 overlay
        # signals (price, RS, narrative, insider, congress, options, etc.)
        # and is cached server-side for 10 min. Cold-cache cost is 20-40s,
        # so we keep it pre-warm for the MARKETS → SECTOR FLOW view.
        if now - _last_cycle.get("sectorflow", 0) >= SECTORFLOW_INTERVAL:
            try:
                import synth_sectorflow
                _safe("sectorflow", synth_sectorflow.sector_flow)
            except Exception as e:
                log.debug("sectorflow skipped: %s", e)
            time.sleep(INTER_REQUEST_DELAY)

        # Sleep until the next eligible cycle. 15s granularity keeps the
        # thread responsive without burning CPU.
        time.sleep(15)


def start() -> None:
    """Spawn the warmer once. Idempotent — safe to call from multiple boot
    paths (app.py, desktop.py) without spawning duplicates."""
    global _thread, _started
    with _started_lock:
        if _started:
            return
        _thread = threading.Thread(target=_loop, daemon=True, name="augur-cache-warmer")
        _thread.start()
        _started = True
    log.info("cache warmer thread launched")


def status() -> dict:
    """Return last-success timestamps for each task — used by a debug endpoint."""
    return {
        "started": _started,
        "last_cycle": dict(_last_cycle),
        "config": {
            "boot_delay": BOOT_DELAY_SECONDS,
            "market_interval": MARKET_INTERVAL,
            "macro_interval": MACRO_INTERVAL,
            "quotes_interval": QUOTES_INTERVAL,
            "fundamentals_interval": FUNDAMENTALS_INTERVAL,
            "news_interval": NEWS_INTERVAL,
            "benchmark_interval": BENCHMARK_INTERVAL,
            "score_interval": SCORE_INTERVAL,
            "hypothesis_score_interval": HYPOTHESIS_SCORE_INTERVAL,
            "cluster_interval": CLUSTER_INTERVAL,
            "divmap_interval": DIVMAP_INTERVAL,
        },
    }
