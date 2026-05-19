"""
Persistent in-memory cache with SQLite write-through + request coalescing.

Two problems this solves:

1. **Cold-start hammering.** The plain `_cache = {}` dict in fetcher.py is
   in-memory only, so every app launch starts with an empty cache and the
   first paint of every view triggers fresh API calls. With Yahoo / Finviz
   running tight rate-limits this is enough to get the user's IP throttled
   on the very first session. The SQLite-backed store survives restarts
   and quietly hydrates the in-memory map on import.

2. **Cache stampede.** If two requests for the same key arrive while a
   refresh is in flight, both would otherwise launch duplicate upstream
   calls. The in-flight map (`_inflight`) makes the second caller wait on
   the first's result instead.

Public API matches the old `_cached / _set_cache` so fetcher.py keeps
working unchanged:

    cache_get(key, ttl=None)        # ttl is decorative — writer's TTL wins
    cache_set(key, value, ttl)
    coalesce(key, ttl, fetch_fn)    # fetch_fn is called at most once per key

Keys are arbitrary hashable values (typically tuples like
`("quote", "AAPL")`). They get JSON-serialised for the SQLite primary key.
Values must be JSON-serialisable; we deliberately use JSON rather than
pickle so a stale cache from an older AUGUR build can't deserialise into
a class that no longer exists.

Python 3.9 compatible.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from typing import Any, Callable, Optional, Tuple

log = logging.getLogger("augur.cache")

# Same DB AUGUR uses for everything else — keeps backup / migration simple.
_DB_PATH = os.environ.get("AUGUR_DB_PATH", "wealth.db")

_mem: dict = {}                    # key -> (value, expiry_ts)
_mem_lock = threading.RLock()

# Per-key locking so a stampede on key A doesn't block traffic for key B.
_inflight: dict = {}               # key -> threading.Event
_inflight_lock = threading.Lock()

# Thread-local sqlite connection so writes from the warmer thread don't
# fight the request threads for the same connection object.
_local = threading.local()

# Skip persistence for keys with TTL < this — quotes refresh every 30s and
# would otherwise generate ~120 disk writes per minute per ticker.
_PERSIST_MIN_TTL = 60.0


_IDENT_KEYS = {"symbol", "name", "id", "label", "source", "exchange", "currency"}


def _is_null_value(v) -> bool:
    """Treat as "no signal" anything that won't render usefully in the UI.

    Recurses into dicts/lists so a nested wrapper like
    ``{value: None, prev: None, change_pct: None}`` is detected as null even
    though its *outer* shape is non-empty. Identifier-only payloads (just a
    `symbol` echoed back with nothing else) also count as null."""
    if v is None or v == "" or v == {} or v == []:
        return True
    if isinstance(v, dict):
        # error envelope → null
        if "error" in v and len(v) <= 3:
            return True
        # all sub-values are themselves null-ish
        non_ident = {k: vv for k, vv in v.items() if k not in _IDENT_KEYS}
        if non_ident and all(_is_null_value(vv) for vv in non_ident.values()):
            return True
        if not non_ident:
            # value carried only identifier fields → no data
            return True
        return False
    if isinstance(v, list):
        if not v:
            return True
        if all(_is_null_value(x) for x in v):
            return True
        return False
    return False


def _looks_like_failure(value) -> bool:
    """Heuristic: would caching this value freeze a broken state in place?

    Returns True for the shapes that indicate "upstream said no":
      - single-dict error envelopes ({"symbol": "X", "error": "..."})
      - empty lists (chart with 0 bars, news with 0 items, …)
      - lists where every dict entry has an "error" field (indices/movers
        when every upstream call rate-limited)
      - empty dicts (correlation matrix {}, fundamentals {} …)
      - dicts of nested null-shaped dicts (macro={vix:{value:None,…},…})

    Real-data responses always have something useful in them, so this is
    safe-by-default: false negatives (caching something that turns out to
    be junk) are bounded by TTL; false positives (refusing to cache real
    data) just mean one extra upstream call per TTL window."""
    if value is None:
        return True
    if isinstance(value, dict):
        if not value:
            return True
        if "error" in value and len(value) <= 3:
            return True
        if all(_is_null_value(v) for v in value.values()):
            return True
        return False
    if isinstance(value, list):
        if not value:
            return True
        if all(isinstance(x, dict) and "error" in x for x in value):
            return True
        if all(_is_null_value(x) for x in value):
            return True
        return False
    return False


def _conn():
    c = getattr(_local, "c", None)
    if c is None:
        c = sqlite3.connect(_DB_PATH, check_same_thread=False, timeout=5.0)
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        c.execute("PRAGMA busy_timeout=3000")
        _local.c = c
    return c


def _serialize_key(key) -> str:
    """Stable string form of a tuple/dict/etc. so SQLite can use it as PK."""
    if isinstance(key, str):
        return key
    try:
        return json.dumps(key, default=str, sort_keys=True)
    except (TypeError, ValueError):
        return repr(key)


def init() -> None:
    """Create the cache table and hydrate the in-memory map from disk.
    Idempotent — safe to call multiple times. Called once at app startup."""
    try:
        c = _conn()
        c.execute("""CREATE TABLE IF NOT EXISTS api_cache (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            expiry REAL NOT NULL,
            written_at REAL NOT NULL
        )""")
        c.execute("CREATE INDEX IF NOT EXISTS api_cache_expiry ON api_cache(expiry)")
        c.commit()
    except Exception as e:
        log.warning("cache table init failed: %s", e)
        return

    # Sweep expired rows before loading — keeps the in-memory map lean and
    # the SQLite table from growing without bound across many sessions.
    try:
        now = time.time()
        c.execute("DELETE FROM api_cache WHERE expiry < ?", (now,))
        c.commit()
    except Exception:
        pass

    # Hydrate, skipping rows that look like cached failures from a prior
    # broken session. Otherwise a transient upstream outage at one boot would
    # serve blank panels for the full TTL window across every subsequent boot.
    loaded = 0
    skipped = 0
    poisoned_keys = []
    try:
        rows = c.execute("SELECT key, value, expiry FROM api_cache").fetchall()
        with _mem_lock:
            for key, raw, expiry in rows:
                try:
                    val = json.loads(raw)
                except Exception:
                    continue
                if _looks_like_failure(val):
                    poisoned_keys.append(key)
                    skipped += 1
                    continue
                _mem[key] = (val, expiry)
                loaded += 1
    except Exception as e:
        log.warning("cache hydrate failed: %s", e)
    if poisoned_keys:
        try:
            c.executemany("DELETE FROM api_cache WHERE key=?", [(k,) for k in poisoned_keys])
            c.commit()
        except Exception:
            pass
    log.info("cache hydrated %d entries from disk (skipped %d cached failures)",
             loaded, skipped)


def cache_get(key, ttl: Optional[float] = None):
    """Return value if fresh, else None. The `ttl` kwarg is decorative
    (kept for compat with the old helper) — the writer's expiry wins.

    A defensive failure check runs at read time too, so even if some legacy
    code path slipped a null-shaped value into the map, we treat it as a
    miss and let the caller refetch."""
    k = _serialize_key(key)
    hit = _mem.get(k)
    if hit is None:
        return None
    value, expiry = hit
    if time.time() >= expiry:
        with _mem_lock:
            _mem.pop(k, None)
        return None
    if _looks_like_failure(value):
        with _mem_lock:
            _mem.pop(k, None)
        return None
    return value


def cache_set(key, value, ttl: float) -> None:
    """Store in memory + (if ttl >= _PERSIST_MIN_TTL) write through to disk.

    Refuses to cache values that look like upstream failures — neither in
    memory nor on disk. A previous version stored failures in memory and
    only blocked the disk write, which caused a transient rate-limit on the
    first request after boot to freeze blank panels for the full TTL window.
    """
    if _looks_like_failure(value):
        return
    k = _serialize_key(key)
    expiry = time.time() + float(ttl)
    with _mem_lock:
        _mem[k] = (value, expiry)
        # Bound in-memory size — same policy as the old helper. Sweeps run
        # only when we cross the threshold so the hot path stays cheap.
        if len(_mem) > 2000:
            now = time.time()
            for stale_k in [kk for kk, (_, e) in _mem.items() if e < now]:
                _mem.pop(stale_k, None)
            if len(_mem) > 2000:
                # Drop the oldest 10% by expiry (effectively LRU-by-write).
                ordered = sorted(_mem.items(), key=lambda kv: kv[1][1])
                for kk, _ in ordered[: len(_mem) - 1800]:
                    _mem.pop(kk, None)

    if ttl < _PERSIST_MIN_TTL:
        return
    try:
        raw = json.dumps(value, default=str)
    except Exception:
        return
    try:
        c = _conn()
        c.execute(
            "INSERT OR REPLACE INTO api_cache(key, value, expiry, written_at) VALUES(?,?,?,?)",
            (k, raw, expiry, time.time()),
        )
        c.commit()
    except Exception as e:
        log.debug("cache persist failed for %s: %s", k[:60], e)


def coalesce(key, ttl: float, fetch_fn: Callable[[], Any]) -> Any:
    """Return cached value if fresh; else run fetch_fn() exactly once even
    if many threads call coalesce(key, ...) concurrently.

    Pattern:
        result = cache_store.coalesce(("quote", "AAPL"), 30, lambda: _hit_yahoo())

    Stampede-safe: a second caller arriving while the first is mid-fetch
    waits on the in-flight Event and then reads the populated cache."""
    cached = cache_get(key)
    if cached is not None:
        return cached
    k = _serialize_key(key)

    # Claim the in-flight slot or wait for the existing fetch.
    with _inflight_lock:
        ev = _inflight.get(k)
        if ev is not None:
            owner = False
        else:
            ev = threading.Event()
            _inflight[k] = ev
            owner = True

    if not owner:
        # Wait for the in-flight fetch to finish; fall back to whatever it
        # wrote to cache. If it failed/timed out we just refetch ourselves.
        ev.wait(timeout=15.0)
        cached = cache_get(key)
        if cached is not None:
            return cached
        # Fall through and fetch — better stale-shadow than blocking forever.

    try:
        value = fetch_fn()
        if not _looks_like_failure(value):
            cache_set(key, value, ttl)
        return value
    finally:
        if owner:
            with _inflight_lock:
                _inflight.pop(k, None)
            ev.set()


def stats() -> dict:
    """Lightweight introspection for a debug endpoint."""
    with _mem_lock:
        n_mem = len(_mem)
        now = time.time()
        fresh = sum(1 for _, exp in _mem.values() if exp > now)
    try:
        c = _conn()
        n_disk = c.execute("SELECT COUNT(*) FROM api_cache").fetchone()[0]
    except Exception:
        n_disk = -1
    with _inflight_lock:
        n_inflight = len(_inflight)
    return {
        "in_memory": n_mem,
        "fresh": fresh,
        "on_disk": n_disk,
        "in_flight": n_inflight,
    }


def clear() -> int:
    """Drop all entries (used by a future 'clear cache' settings button)."""
    with _mem_lock:
        n = len(_mem)
        _mem.clear()
    try:
        c = _conn()
        c.execute("DELETE FROM api_cache")
        c.commit()
    except Exception:
        pass
    return n
