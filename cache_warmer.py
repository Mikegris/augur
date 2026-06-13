"""
Background cache warmer — keeps hot data already populated when users arrive.

Problem this solves: even with a persistent cache, after a long pause (e.g.
overnight, weekend) the first user navigation triggers a flurry of API
calls as caches expire across panels in rapid succession. Under Yahoo /
Finviz rate-limits that's enough to throttle a fresh IP. The warmer keeps
the hot endpoints continuously fresh on a schedule, so user requests just
read from cache.

Cadences (chosen for two goals: stay well below upstream rate limits, AND
keep data fresh enough to be useful — see per-constant comments below for
the TTL each one is matched against):

  every 60s   indices, sector heatmap, top movers (TTL 60s)
  every 300s  macro indicators (TTL 300s)
  every 300s  portfolio + watchlist quotes batch (prime-after-idle only)
  every 9m    sector-flow heatmap (TTL 10m)
  every 55m   divergence map scan (TTL 1h)
  every 5.5h  cluster bull+bear scans (TTL 6h)
  every 12h   fundamentals + dividends for portfolio symbols (TTL 24h)
  every 6h    news for portfolio symbols (prime only — TTL 15m)
  every 12h   benchmark history + research charts (TTL 12h)

Tasks that fail (exception or soft timeout) back off exponentially
(60s → 1h cap) instead of being retried on every loop tick.

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
#
# Cadence rule of thumb (2026-06 audit): for a read-through cache the only
# interval that delivers "user requests always hit warm cache" is one at or
# just *under* the cache's TTL — warming more often is a no-op (the warm call
# itself reads the still-fresh cache), warming less often leaves cold windows
# exactly as long as (interval - TTL).
BOOT_DELAY_SECONDS = 25
# Indices / sectors / movers are cached 60s in fetcher. The old 90s interval
# left a 30s cold window every cycle (users hitting the dashboard in that
# window paid the cold fetch — the exact thing the warmer exists to prevent).
# 60s = every warm call lands right at expiry; +20 batched calls/hr/endpoint.
MARKET_INTERVAL = 60
# Macro TTL is 300s. The old 180s interval made every other call a pure
# cache-read no-op (refresh effectively happened every 360s, with up to 60s
# cold past the TTL). Matching the TTL refreshes exactly once per window.
MACRO_INTERVAL = 300
# Portfolio/watchlist quote TTL is only 30s (120s crypto) — keeping that warm
# would cost 120 batch calls/hr, which we don't want. 300s here is therefore
# NOT an always-warm guarantee; it just primes the cache after idle/boot so
# the first dashboard paint has data. A user mid-session refresh is a single
# batched call — acceptable. Deliberately left as-is.
QUOTES_INTERVAL = 300
# Fundamentals TTL in fetcher is 86400 (24h). A 6h warmer cycle would fire
# 4× per day but only the first cycle hits the upstream — the other 3 just
# read from cache and look stale in `status()`. Match the warmer to half
# the TTL so we refresh once per day in the middle of the TTL window.
FUNDAMENTALS_INTERVAL = 12 * 3600
# News TTL is 900s (15 min) in fetcher.get_news — far shorter than this
# interval, so warmed news rows are fresh for only 15 min of every 6h.
# That's fine: this task exists to prime news after boot/idle, not to keep
# it perpetually warm (which would cost ~4 calls/hr/symbol). A cold news
# fetch is one cheap HTTP request, so we deliberately don't chase the TTL.
NEWS_INTERVAL = 6 * 3600
BENCHMARK_INTERVAL = 12 * 3600     # daily-bar chart TTL is 12h — aligned
CHART_INTERVAL = 12 * 3600         # default research charts (6mo daily, TTL 12h — aligned)
SCORE_INTERVAL = 6 * 3600          # research_tracker scoring loop
HYPOTHESIS_SCORE_INTERVAL = 24 * 3600  # research_hypothesis daily scoring
# v0.3.0 synthesis: heavy fan-out scans worth pre-warming so the first
# user click is instant. Each scan covers ~100 symbols × N sources.
#
# 2026-06 re-derivation: the scan engines got 2.6-5× faster (and smart_money
# is memoized 600s), so the old long intervals — chosen when a scan cost
# 30-90s — were leaving the views cold most of the day:
#   cluster:    TTL 6h, warmed every 12h  → cold 50% of the time
#   divmap:     TTL 1h, warmed every 6h   → cold 83% of the time
#   sectorflow: TTL 10m, warmed every 30m → cold 67% of the time
# Each is now set just under its cache TTL so the view is ALWAYS warm.
# Budget math: cluster runs 2.2× more often at ÷2.6-5 per-scan cost → net
# at or below the old budget. divmap/sectorflow run more often, but their
# per-symbol inputs (charts 30min, insider/congress 6-24h, smart_money
# 600s) are themselves cached, so re-scans mostly recompute from cache.
CLUSTER_INTERVAL = int(5.5 * 3600)  # synth_cluster bull+bear scans (SCAN_TTL_S = 6h)
DIVMAP_INTERVAL = 55 * 60           # synth_divmap divergence scan (_CACHE_TTL = 1h)
SECTORFLOW_INTERVAL = 9 * 60        # synth_sectorflow heatmap (CACHE_TTL = 600s)
# Jarvis briefing (_BRIEFING_TTL = 240s) — warmed at ≤ TTL/2 so the Overview's
# first paint reads a hot cache instead of paying the full briefing pipeline
# (quote batch + earnings + regime, multi-second cold).
# get_briefing() is read-through: a tick that lands while the entry is still
# fresh is a cheap cache read (a no-op refresh), and a tick after expiry
# recomputes once. The OLD 200s interval against a 240s TTL left a ~160s cold
# window — the entry expired at 240s but the next warm didn't fire until 400s,
# so reads in that gap paid the full cold pipeline. At 115s the warm cadence
# guarantees a recompute within ~115s of expiry, collapsing the cold window.
# Tradeoff: we deliberately keep a PLAIN get_briefing() (not force_refresh)
# so the warmer never triggers an extra LLM-voice call each tick; the 115s
# cadence is what actually keeps the entry warm, not a forced recompute.
JARVIS_BRIEFING_INTERVAL = 115
# Daily housekeeping: prune monotonically-growing tables + occasional VACUUM.
# Without this an active user's wealth.db grows without bound (observed 535MB).
PRUNE_INTERVAL = 24 * 3600
VACUUM_INTERVAL = 7 * 24 * 3600
INTER_REQUEST_DELAY = 1.2          # spacing between requests within a cycle

# Cap how many portfolio symbols we warm per cycle so an unusually large
# portfolio doesn't blow our request budget. The user sees a stale row
# rather than a banned IP.
PORTFOLIO_WARM_CAP = 25

_thread: Optional[threading.Thread] = None
_started = False
_started_lock = threading.Lock()
_last_cycle: dict = {}              # task -> unix ts of last successful run

# Failure backoff. _last_cycle is only stamped on SUCCESS, so before this
# existed a task that threw (or blew the soft timeout) every time was
# re-attempted on EVERY 15s loop tick — 240 attempts/hr against an upstream
# that's already unhappy, each timeout leaking another orphaned thread.
# Now consecutive failures back off exponentially: 60s, 120s, ... capped at
# 1h. A transient blip still retries within a minute (much sooner than the
# 6-12h cadences would), while a persistent failure converges to ~1/hr.
_fail_count: dict = {}              # task -> consecutive failure count
_next_retry: dict = {}              # task -> unix ts before which we won't retry
BACKOFF_BASE_SECONDS = 60.0
BACKOFF_MAX_SECONDS = 3600.0


# Soft watchdog: max wall-clock we'll wait for a single warm call before
# moving on. If the upstream blocks past this, the orphaned thread keeps
# running (we can't safely abort a Python thread mid-call) but the warmer
# loop continues so other cadences don't fall behind.
WARM_CALL_TIMEOUT = 45.0


def _safe(label: str, fn, *args, **kwargs):
    """Wrap a warm call so a single endpoint failure doesn't kill the thread.
    Records the timestamp on success for the status endpoint.

    Bounded by WARM_CALL_TIMEOUT — if the call doesn't return in time we log
    and move on, otherwise one slow upstream (yfinance 60s timeout, SEC
    EDGAR 90s response) can starve all the other cadences."""
    holder = {"err": None, "done": False}

    def _runner():
        try:
            fn(*args, **kwargs)
        except Exception as e:
            holder["err"] = e
        finally:
            holder["done"] = True

    t = threading.Thread(target=_runner, name=f"warm-{label}", daemon=True)
    t.start()
    t.join(WARM_CALL_TIMEOUT)
    if not holder["done"]:
        log.warning("warmer %s exceeded %.0fs soft timeout; moving on",
                    label, WARM_CALL_TIMEOUT)
        _record_failure(label)
        return False
    if holder["err"] is not None:
        log.debug("warmer %s failed: %s", label, holder["err"])
        _record_failure(label)
        return False
    _last_cycle[label] = time.time()
    _fail_count.pop(label, None)
    _next_retry.pop(label, None)
    return True


def _record_failure(label: str) -> None:
    """Bump the consecutive-failure count and schedule the next allowed retry."""
    fails = _fail_count.get(label, 0) + 1
    _fail_count[label] = fails
    delay = min(BACKOFF_MAX_SECONDS, BACKOFF_BASE_SECONDS * (2 ** (fails - 1)))
    _next_retry[label] = time.time() + delay
    if fails >= 3:
        log.warning("warmer %s has failed %d times in a row; next retry in %.0fs",
                    label, fails, delay)


def _due(label: str, interval: float, now: float) -> bool:
    """True when a task's cadence has elapsed AND it isn't in failure backoff."""
    if now - _last_cycle.get(label, 0) < interval:
        return False
    return now >= _next_retry.get(label, 0)


def _vacuum_checked(_db):
    """vacuum_db() signals failure by RETURN VALUE (-1), not by raising —
    convert that to an exception so _safe's success/backoff accounting is
    accurate (a skipped VACUUM must not be stamped as done for a week)."""
    if _db.vacuum_db() == -1:
        raise RuntimeError("VACUUM skipped (WAL readers active or DB locked)")


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
    """Best-effort (equity, crypto) split of watchlist symbols. Crypto must be
    warmed under the same yfinance `SYM-USD` form the quote path uses; merging
    it into the equity batch (the old behavior) asked Yahoo for a bare coin
    ticker it doesn't serve, so those rows never warmed."""
    try:
        import database as db
        rows = db.get_watchlist() or []
        eq = [r["symbol"] for r in rows if r.get("asset_type") != "crypto"]
        crypto = [r["symbol"] for r in rows if r.get("asset_type") == "crypto"]
        return eq[:PORTFOLIO_WARM_CAP], crypto[:PORTFOLIO_WARM_CAP]
    except Exception:
        return [], []


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
        if _due("indices", MARKET_INTERVAL, now):
            _safe("indices", fetcher.get_market_indices)
            time.sleep(INTER_REQUEST_DELAY)
        if _due("sectors", MARKET_INTERVAL, now):
            _safe("sectors", fetcher.get_sector_performance)
            time.sleep(INTER_REQUEST_DELAY)
        if _due("movers", MARKET_INTERVAL, now):
            _safe("movers", fetcher.get_top_movers)
            time.sleep(INTER_REQUEST_DELAY)

        # ── medium cadence: macro ────────────────────────────────────
        if _due("macro", MACRO_INTERVAL, now):
            _safe("macro", fetcher.get_macro_indicators)
            time.sleep(INTER_REQUEST_DELAY)

        # ── Jarvis briefing cadence: every 200s (TTL 240s) ───────────
        # Lazy import: jarvis pulls the whole engine stack; a broken or
        # missing module must degrade to "briefing warms on first user hit"
        # rather than killing the warmer loop. Failures ride the standard
        # backoff via _safe, like every other task.
        if _due("jarvis_briefing", JARVIS_BRIEFING_INTERVAL, now):
            try:
                import jarvis
                _safe("jarvis_briefing", jarvis.get_briefing)
            except Exception as e:
                log.debug("jarvis_briefing skipped: %s", e)
            time.sleep(INTER_REQUEST_DELAY)

        # ── portfolio quotes (cheap when batched) ────────────────────
        # Equity and crypto legs are gated on SEPARATE cadence labels. The old
        # code gated the whole block on "quotes": a crypto-only portfolio left
        # all_eq empty, so _safe("quotes",…) never ran and never stamped
        # _last_cycle["quotes"] — making _due("quotes") true on every 15s tick
        # and re-warming the crypto batch ~20× more often than intended (its
        # failures even recorded under "quotes_crypto", a key the gate never
        # checked). Each leg now stamps/backs-off under its own key.
        eq, crypto = _portfolio_symbols()
        wl_eq, wl_crypto = _watchlist_symbols()
        if _due("quotes", QUOTES_INTERVAL, now):
            all_eq = list(dict.fromkeys(eq + wl_eq))  # dedupe, keep order
            if all_eq:
                _safe("quotes", fetcher.get_quotes_batch, all_eq)
                time.sleep(INTER_REQUEST_DELAY)
        if _due("quotes_crypto", QUOTES_INTERVAL, now):
            all_crypto = list(dict.fromkeys(crypto + wl_crypto))
            if all_crypto:
                # Mirror get_portfolio_quotes' guard: holdings already stored
                # as "BTC-USD" must NOT become "BTC-USD-USD" (which Yahoo 404s),
                # so only append the suffix when it isn't already present.
                yf_crypto = [s if s.upper().endswith("-USD") else s + "-USD"
                             for s in all_crypto]
                _safe("quotes_crypto", fetcher.get_quotes_batch, yf_crypto)
                time.sleep(INTER_REQUEST_DELAY)

        # ── slow cadence: fundamentals + news + benchmark ────────────
        if _due("fundamentals", FUNDAMENTALS_INTERVAL, now):
            eq, _ = _portfolio_symbols()
            any_ok = False
            for sym in eq:
                any_ok = _safe("fundamentals", fetcher.get_fundamentals, sym) or any_ok
                time.sleep(INTER_REQUEST_DELAY)
            # Only advance the cadence if SOMETHING succeeded — an all-failed
            # sweep must stay in backoff, not be stamped done for 12h.
            if any_ok or not eq:
                _last_cycle["fundamentals"] = time.time()

        if _due("news", NEWS_INTERVAL, now):
            eq, _ = _portfolio_symbols()
            any_ok = False
            for sym in eq:
                any_ok = _safe("news", fetcher.get_news, sym) or any_ok
                time.sleep(INTER_REQUEST_DELAY)
            if any_ok or not eq:
                _last_cycle["news"] = time.time()

        # NOTE: benchmark warm task dropped (was wasted budget). It warmed
        # the cache key ("bench","SPY","1y", base_value=None), but the route
        # always calls get_benchmark_history with a per-user base_value (the
        # portfolio's starting value) so the bars are normalized into that
        # row — a different cache key the warm could never match. Since the
        # normalization is baked into the cached value, there is no shared
        # base_value=None raw form for the route to reuse, so warming it just
        # spent a yfinance round-trip on an entry nothing reads. If a raw
        # (unnormalized) cache form is added later, warm that instead.

        # Chart history for portfolio + watchlist symbols at the Research
        # view's default period (6mo / 1d). Without this, the first time a
        # user clicks Research the chart panel is empty until they wait for
        # a fresh yfinance fetch — which is the exact moment we don't want
        # to pile on rate-limited upstreams.
        if _due("chart", CHART_INTERVAL, now):
            eq, _ = _portfolio_symbols()
            wl_eq, _ = _watchlist_symbols()
            symbols = list(dict.fromkeys(eq + wl_eq))[:PORTFOLIO_WARM_CAP]
            any_ok = False
            for sym in symbols:
                any_ok = _safe("chart", fetcher.get_chart_data, sym, "6mo", "1d") or any_ok
                time.sleep(INTER_REQUEST_DELAY)
            if any_ok or not symbols:
                _last_cycle["chart"] = time.time()

        # ── score-due forecasts cadence: every 6h ────────────────────────
        # research_tracker queues a row each time a tracked signal panel
        # is hit. After `horizon_days` elapse we price each against the
        # close and compute hit-rate. Module owns its own DB connection;
        # the scorer is idempotent and capped at 200 rows/call so a long
        # backlog can't pin the warmer for minutes.
        if _due("tracker_score", SCORE_INTERVAL, now):
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
        if _due("hypothesis_score", HYPOTHESIS_SCORE_INTERVAL, now):
            try:
                import research_hypothesis
                _safe("hypothesis_score", research_hypothesis.score_due_hypotheses)
            except Exception as e:
                log.debug("hypothesis_score skipped: %s", e)
            time.sleep(INTER_REQUEST_DELAY)

        # ── tracker prune cadence: every 24h ────────────────────────────
        # signal_forecasts grows monotonically as the tracker logs every
        # signal. 1 year of history is plenty for hit-rate / Sharpe stats;
        # without this, an active user can accumulate hundreds of MB of
        # tracker rows over a few years.
        if _due("tracker_prune", HYPOTHESIS_SCORE_INTERVAL, now):
            try:
                import research_tracker
                _safe("tracker_prune", research_tracker.prune_old_forecasts, 365)
            except Exception as e:
                log.debug("tracker_prune skipped: %s", e)
            time.sleep(INTER_REQUEST_DELAY)

        # ── v0.3.0 synth_cluster cadence: every 12h, bull + bear ─────────
        # A full SP500-top-100 scan takes 30-90s on cold cache; warm both
        # directions so the first user click on the CLUSTER view is instant.
        # Offsetting the bear scan after a small delay keeps the EDGAR /
        # yfinance call burst spread out.
        if _due("cluster_bull", CLUSTER_INTERVAL, now):
            try:
                import synth_cluster
                _safe("cluster_bull", synth_cluster.cluster_scan,
                      universe=None, direction="bullish", min_sources=4)
            except Exception as e:
                log.debug("cluster_bull skipped: %s", e)
            time.sleep(INTER_REQUEST_DELAY)
        if _due("cluster_bear", CLUSTER_INTERVAL, now):
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
        if _due("divmap", DIVMAP_INTERVAL, now):
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

        # ── daily prune of monotonically-growing tables ─────────────────
        # Scanner history, snapshots (downsampled), TTL-expired cache rows,
        # idea_pool, and ai_call_log all grow without bound without this.
        if _due("prune", PRUNE_INTERVAL, now):
            try:
                import database as _db
                _safe("prune", _db.run_daily_prune)
            except Exception as e:
                log.debug("prune skipped: %s", e)
            time.sleep(INTER_REQUEST_DELAY)

        # ── weekly VACUUM to reclaim freed pages ────────────────────────
        # DELETEs leave SQLite pages fragmented; without VACUUM the file
        # never shrinks even after a big prune. database.vacuum_db() returns
        # -1 instead of raising when SQLite refuses VACUUM (WAL readers
        # active) — previously that silently counted as success here, so a
        # failed VACUUM wasn't retried for a whole week. Observed result: a
        # 116MB wealth.db that was 94.6% freelist pages (6.2MB live data).
        # Raise on -1 so the backoff machinery retries within the hour.
        if _due("vacuum", VACUUM_INTERVAL, now):
            try:
                import database as _db
                _safe("vacuum", _vacuum_checked, _db)
            except Exception as e:
                log.debug("vacuum skipped: %s", e)
            time.sleep(INTER_REQUEST_DELAY)

        # ── v0.3.3 synth_sectorflow cadence: every 30 min ────────────────
        # The sector-flow heatmap fans 11 sector ETFs against ~12 overlay
        # signals (price, RS, narrative, insider, congress, options, etc.)
        # and is cached server-side for 10 min. Cold-cache cost is 20-40s,
        # so we keep it pre-warm for the MARKETS → SECTOR FLOW view.
        if _due("sectorflow", SECTORFLOW_INTERVAL, now):
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
        "failing": dict(_fail_count),          # task -> consecutive failures
        "next_retry": dict(_next_retry),       # task -> unix ts (backoff gate)
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
            "sectorflow_interval": SECTORFLOW_INTERVAL,
            "jarvis_briefing_interval": JARVIS_BRIEFING_INTERVAL,
        },
    }
