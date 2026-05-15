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

    # Hydrate
    loaded = 0
    try:
        rows = c.execute("SELECT key, value, expiry FROM api_cache").fetchall()
        with _mem_lock:
            for key, raw, expiry in rows:
                try:
                    val = json.loads(raw)
                except Exception:
                    continue
                _mem[key] = (val, expiry)
                loaded += 1
    except Exception as e:
        log.warning("cache hydrate failed: %s", e)
    log.info("cache hydrated %d entries from disk", loaded)


def cache_get(key, ttl: Optional[float] = None):
    """Return value if fresh, else None. The `ttl` kwarg is decorative
    (kept for compat with the old helper) — the writer's expiry wins."""
    k = _serialize_key(key)
    hit = _mem.get(k)
    if hit is None:
        return None
    value, expiry = hit
    if time.time() >= expiry:
        with _mem_lock:
            _mem.pop(k, None)
        return None
    return value


def cache_set(key, value, ttl: float) -> None:
    """Store in memory + (if ttl >= _PERSIST_MIN_TTL) write through to disk.
    Cheap and best-effort — persistence failures are logged at debug and
    swallowed so they never break the calling request path."""
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
    # Skip persisting error envelopes — there's no point hydrating a stale
    # "Too Many Requests" string on the next session.
    if isinstance(value, dict) and "error" in value and len(value) <= 3:
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
        # Don't cache None / error envelopes — they'd freeze a broken state
        # in place. Caller's existing error handling is still in effect.
        if value is not None and not (isinstance(value, dict) and "error" in value and len(value) <= 3):
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
