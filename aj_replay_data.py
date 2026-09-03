"""AJTA — point-in-time data layer for the replay engine (aj_replay).

The single rule this module exists to enforce: DURING A REPLAY, NOTHING MAY
SEE DATA FROM AFTER THE SIMULATED DAY. It provides:

  * PITStore — daily OHLCV bars per symbol, loaded from a disk cache and
    served AS-OF the simulated date (quotes = that day's close; charts =
    bars ending at the simulated day, windowed like the live fetcher).
  * build_cache() — one-time (network) download of the bar history via the
    real fetcher, normalized to {date(ET), open, high, low, close, volume}
    JSON files. Replays themselves then run fully offline.
  * patch()/unpatch() — swap fetcher.get_quote / get_quotes_batch /
    get_chart_data for PIT-serving versions, disable the forecast-ensemble
    result cache (whose (symbol, horizon) key would leak day D's forecast
    into day D+1), and BLOCK raw network (requests.*) so a signal adapter
    that talks to a live API can't inject present-day information into the
    past (look-ahead). Fail-open adapters degrade to 'no signal' — honest.

Only ever used inside a dedicated replay process (isolated AUGUR_DB_PATH).
Python 3.9 compatible.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("augur.aj_replay_data")

DEFAULT_CACHE_DIR = os.path.join("data", "replay_cache")

# period string -> approximate trading-bar count for chart windowing (matches
# how consumers use the live fetcher: "6mo" ~ 126 daily bars, etc.)
_PERIOD_BARS = {
    "5d": 5, "1mo": 21, "3mo": 63, "6mo": 126, "1y": 252, "2y": 504,
    "5y": 1260, "10y": 2520, "max": 10 ** 6,
}

# A cache file whose LAST bar is older than this many calendar days no longer
# covers a present-day counterfactual window — build_cache refetches it (see
# #283). 7 days tolerates weekends/holidays while keeping same-week replays
# repeatable on the frozen file.
_CACHE_STALE_DAYS = 7


class ReplayNetworkBlocked(RuntimeError):
    """Raised when replayed code attempts raw network I/O. Fail-open callers
    (signal adapters, news fetchers) treat it as 'source unavailable' — which
    is exactly the honest answer inside a historical simulation."""


def _cache_path(cache_dir: str, symbol: str) -> str:
    safe = symbol.upper().replace("/", "_").replace(":", "_")
    return os.path.join(cache_dir, "{}.json".format(safe))


def _et_date_of_epoch(t: int) -> Optional[str]:
    try:
        dt = datetime.fromtimestamp(int(t), tz=timezone.utc)
        try:
            from zoneinfo import ZoneInfo
            dt = dt.astimezone(ZoneInfo("America/New_York"))
        except Exception:
            pass
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return None


def _epoch_of_et_date(d: str) -> int:
    """16:00 ET on date `d` as a unix epoch — the daily bar's close instant.
    Consumers (aj_benchmark._index_closes, aj_council) convert bar epochs back
    to ET dates, so the round-trip must land on the same calendar day."""
    try:
        from zoneinfo import ZoneInfo
        dt = datetime.strptime(d, "%Y-%m-%d").replace(
            hour=16, tzinfo=ZoneInfo("America/New_York"))
    except Exception:
        dt = datetime.strptime(d, "%Y-%m-%d").replace(
            hour=21, tzinfo=timezone.utc)
    return int(dt.timestamp())


def build_cache(symbols: List[str], period: str = "10y",
                cache_dir: str = DEFAULT_CACHE_DIR,
                refresh: bool = False) -> Dict[str, int]:
    """Download + normalize daily bars for `symbols` via the REAL fetcher
    (network) into the disk cache. Returns {symbol: n_bars}. Existing cache
    files are kept unless refresh=True — replays are meant to be repeatable,
    so the cache is the frozen dataset of record."""
    import fetcher
    os.makedirs(cache_dir, exist_ok=True)
    out: Dict[str, int] = {}
    for sym in dict.fromkeys(s.upper() for s in symbols if s):
        path = _cache_path(cache_dir, sym)
        if os.path.exists(path) and not refresh:
            try:
                with open(path) as f:
                    meta = json.load(f)
                cached = meta.get("bars") or []
                # WHY (#283): reuse must check COVERAGE, not mere existence.
                # A file built with a shorter period (nightly 2y build vs a
                # 10y request) silently truncated the replayed window while
                # the run artifact still reported the requested start date —
                # so require the stored period to cover the requested one.
                # Likewise refetch when the last cached bar has gone stale
                # (> _CACHE_STALE_DAYS old): a weeks-old file cuts a
                # present-day counterfactual window short.
                # EXCEPTION: a stored period we don't recognize (e.g. the
                # test suites' hand-built 'synthetic' datasets) marks a
                # frozen dataset of record — coverage can't be compared, and
                # refetching would DESTROY it with live data (the module
                # docstring's repeatability contract), so reuse it as-is.
                have = _PERIOD_BARS.get(str(meta.get("period") or "").lower())
                if cached and have is None:
                    out[sym] = len(cached)
                    continue
                want = _PERIOD_BARS.get(str(period).lower(), 0)
                fresh = False
                if cached:
                    try:
                        last = datetime.strptime(cached[-1]["date"],
                                                 "%Y-%m-%d")
                        fresh = ((datetime.now() - last).days
                                 <= _CACHE_STALE_DAYS)
                    except Exception:
                        fresh = False
                if cached and (have or 0) >= want and fresh:
                    out[sym] = len(cached)
                    continue
                # insufficient coverage / stale / empty -> refetch below
            except Exception:
                pass  # unreadable cache -> refetch
        try:
            rows = fetcher.get_chart_data(sym, period, "1d") or []
        except Exception:
            log.warning("build_cache: fetch failed for %s", sym, exc_info=True)
            rows = []
        bars = []
        for r in rows:
            d = _et_date_of_epoch(r.get("time")) if r.get("time") is not None else None
            c = r.get("close")
            if not d or c is None:
                continue
            bars.append({"date": d,
                         "open": r.get("open"), "high": r.get("high"),
                         "low": r.get("low"), "close": c,
                         "volume": r.get("volume")})
        bars.sort(key=lambda b: b["date"])
        if not bars:
            # WHY (#282): NEVER persist an empty bars file. A transient
            # network failure/429 (e.g. during the nightly --refresh) would
            # otherwise overwrite a symbol's good multi-year cache with
            # {'bars': []}, and the existence check above would then skip
            # refetching forever — permanently poisoning every offline
            # replay/lab fold for that symbol. Keep the existing file as
            # last-good (or leave nothing, so the next run refetches).
            n_prev = 0
            try:
                with open(path) as f:
                    n_prev = len(json.load(f).get("bars") or [])
            except Exception:
                pass
            out[sym] = n_prev
            log.warning("build_cache: %s fetched 0 bars — keeping last-good "
                        "cache (%d bars), not writing", sym, n_prev)
            continue
        with open(path, "w") as f:
            json.dump({"symbol": sym, "period": period,
                       "built_at": datetime.now(timezone.utc).isoformat(),
                       "bars": bars}, f)
        out[sym] = len(bars)
        log.info("build_cache: %s -> %d bars", sym, len(bars))
    return out


class PITStore:
    """Point-in-time bar store. `set_asof(date)` moves the simulated day;
    every quote/chart read then sees ONLY bars with date <= asof."""

    # A quote older than this many calendar days is stale enough to be
    # worthless (suspended/delisted name) — serve None instead, so the agent
    # can't keep trading a symbol that stopped printing (mirrors live, where
    # the quote feed would also go quiet).
    MAX_QUOTE_STALENESS_DAYS = 7

    def __init__(self, cache_dir: str = DEFAULT_CACHE_DIR):
        self.cache_dir = cache_dir
        self._bars: Dict[str, List[Dict[str, Any]]] = {}
        self._dates: Dict[str, List[str]] = {}
        self.asof: Optional[str] = None

    # ── loading ────────────────────────────────────────────────────────────────
    def load(self, symbols: List[str]) -> List[str]:
        """Load cached bars for `symbols`; returns the symbols that loaded."""
        ok = []
        for sym in dict.fromkeys(s.upper() for s in symbols if s):
            path = _cache_path(self.cache_dir, sym)
            try:
                with open(path) as f:
                    bars = json.load(f).get("bars") or []
            except Exception:
                log.warning("PITStore: no cache for %s (%s)", sym, path)
                continue
            bars = [b for b in bars if b.get("date") and b.get("close") is not None]
            if not bars:
                # WHY (#284): a cache file with ZERO usable bars must count as
                # NOT loaded — reporting it as loaded defeated run_replay's
                # missing-symbol warning (the symbol silently never traded)
                # and, for the BENCHMARK, let the run proceed without
                # benchmark/alpha metrics instead of failing clearly.
                log.warning("PITStore: empty cache for %s (%s)", sym, path)
                continue
            bars.sort(key=lambda b: b["date"])
            self._bars[sym] = bars
            self._dates[sym] = [b["date"] for b in bars]
            ok.append(sym)
        return ok

    def inject(self, symbol: str, bars: List[Dict[str, Any]]) -> None:
        """Test seam: install synthetic bars directly (no disk, no network)."""
        sym = symbol.upper()
        bars = sorted((dict(b) for b in bars), key=lambda b: b["date"])
        self._bars[sym] = bars
        self._dates[sym] = [b["date"] for b in bars]

    def symbols(self) -> List[str]:
        return sorted(self._bars.keys())

    def trading_dates(self, start: str, end: str,
                      anchor: str = "SPY") -> List[str]:
        """The replay calendar: the anchor symbol's bar dates within
        [start, end] — actual sessions the market printed, holidays free."""
        ds = self._dates.get(anchor.upper()) or []
        if not ds:
            # fall back to the union of all symbols' dates
            all_d = set()
            for v in self._dates.values():
                all_d.update(v)
            ds = sorted(all_d)
        return [d for d in ds if start <= d <= end]

    # ── as-of reads ────────────────────────────────────────────────────────────
    def set_asof(self, date_str: str) -> None:
        self.asof = date_str

    def _idx_asof(self, sym: str) -> int:
        """Index of the last bar with date <= asof (-1 if none)."""
        import bisect
        ds = self._dates.get(sym)
        if not ds or self.asof is None:
            return -1
        return bisect.bisect_right(ds, self.asof) - 1

    def close_asof(self, symbol: str) -> Optional[float]:
        sym = symbol.upper()
        i = self._idx_asof(sym)
        if i < 0:
            return None
        bar = self._bars[sym][i]
        # staleness guard (suspended/delisted names go quiet, not stale-priced)
        try:
            asof_dt = datetime.strptime(self.asof, "%Y-%m-%d")
            bar_dt = datetime.strptime(bar["date"], "%Y-%m-%d")
            if (asof_dt - bar_dt) > timedelta(days=self.MAX_QUOTE_STALENESS_DAYS):
                return None
        except Exception:
            pass
        try:
            c = float(bar["close"])
            return c if c > 0 else None
        except (TypeError, ValueError):
            return None

    def quote(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Live-fetcher-shaped quote dict as of the simulated day."""
        sym = symbol.upper()
        i = self._idx_asof(sym)
        c = self.close_asof(sym)
        if c is None:
            return None
        bars = self._bars[sym]
        prev = None
        if i >= 1:
            try:
                prev = float(bars[i - 1].get("close") or 0) or None
            except (TypeError, ValueError):
                prev = None
        chg = ((c / prev - 1.0) * 100.0) if prev else 0.0
        vol = bars[i].get("volume")
        return {"symbol": sym, "price": c, "change_pct": round(chg, 4),
                "volume": vol, "market_cap": None}

    def chart(self, symbol: str, period: str = "6mo",
              interval: str = "1d") -> List[Dict[str, Any]]:
        """Bars ENDING at the simulated day, windowed like the live fetcher.
        Only daily replay data exists; intraday interval requests get the
        daily series (the aj stack only consumes daily)."""
        sym = symbol.upper()
        i = self._idx_asof(sym)
        if i < 0:
            return []
        n = _PERIOD_BARS.get(str(period).lower(), 126)
        window = self._bars[sym][max(0, i + 1 - n):i + 1]
        return [{"time": _epoch_of_et_date(b["date"]), "date": b["date"],
                 "open": b.get("open"), "high": b.get("high"),
                 "low": b.get("low"), "close": b.get("close"),
                 "volume": b.get("volume")} for b in window]


# ── patching (installed once per replay process) ──────────────────────────────

_PATCH_STATE: Dict[str, Any] = {}


def _blocked(*_a, **_k):
    raise ReplayNetworkBlocked(
        "network I/O is blocked inside a replay (look-ahead guard)")


def patch(store: PITStore, block_network: bool = True) -> None:
    """Route all market-data reads through the PIT store and (by default)
    block raw network so nothing can peek at the present. Idempotent."""
    if _PATCH_STATE.get("on"):
        return
    import fetcher
    _PATCH_STATE["fetcher"] = (fetcher.get_quote, fetcher.get_quotes_batch,
                               fetcher.get_chart_data)
    fetcher.get_quote = lambda s, *a, **k: store.quote(s)
    fetcher.get_quotes_batch = lambda syms, *a, **k: {
        s: q for s in (syms or []) for q in [store.quote(s)] if q is not None}
    fetcher.get_chart_data = lambda s, period="6mo", interval="1d", *a, **k: \
        store.chart(s, period, interval)

    # The forecast-ensemble result cache keys on (symbol, horizon) with a TTL:
    # inside a replay it would serve day D's forecast on day D+1. Disable it.
    try:
        import forecast_ensemble
        _PATCH_STATE["fe_cache"] = forecast_ensemble.cache_store
        forecast_ensemble.cache_store = None
    except Exception:
        _PATCH_STATE["fe_cache"] = None

    if block_network:
        try:
            import requests
            _PATCH_STATE["requests"] = (requests.request, requests.get,
                                        requests.post,
                                        requests.Session.request)
            requests.request = _blocked
            requests.get = _blocked
            requests.post = _blocked
            requests.Session.request = _blocked
        except Exception:
            log.warning("could not block requests", exc_info=True)
    _PATCH_STATE["on"] = True
    log.info("replay data layer patched (network blocked=%s)", block_network)


def unpatch() -> None:
    if not _PATCH_STATE.get("on"):
        return
    import fetcher
    (fetcher.get_quote, fetcher.get_quotes_batch,
     fetcher.get_chart_data) = _PATCH_STATE["fetcher"]
    try:
        import forecast_ensemble
        forecast_ensemble.cache_store = _PATCH_STATE.get("fe_cache")
    except Exception:
        pass
    if "requests" in _PATCH_STATE:
        import requests
        (requests.request, requests.get, requests.post,
         requests.Session.request) = _PATCH_STATE["requests"]
    _PATCH_STATE.clear()
