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

import base64
import gzip
import json
import logging
import os
import sqlite3
import threading
import time
from typing import Any, Callable, Optional, Tuple

log = logging.getLogger("augur.cache")

# Same DB AUGUR uses for everything else — keeps backup / migration simple.
# Resolved to absolute at import time so an in-process os.chdir() can't cause
# a thread spawned later to open a SECOND wealth.db in the new working dir.
# Must mirror database.py's resolution exactly so both modules hit the same
# file regardless of subsequent cwd changes.
_DB_PATH = os.path.abspath(os.environ.get("AUGUR_DB_PATH", "wealth.db"))

_mem: dict = {}                    # key -> (value, expiry_ts, last_access_ts)
_mem_lock = threading.RLock()
_MEM_SOFT_CAP = 2000
_MEM_EVICT_TARGET = 1800

# Per-key locking so a stampede on key A doesn't block traffic for key B.
# key -> (threading.Event, claimed_at_ts). The timestamp lets a periodic
# sweep release events whose fetch_fn never returned (e.g. a network deadlock
# inside a third-party HTTP client that ignored our timeout); without the
# sweep the Event sits in memory forever and any subsequent caller for the
# same key wastes 15s waiting on it before falling through.
_inflight: dict = {}
_inflight_lock = threading.Lock()
# Events older than this are assumed orphaned and force-released by
# _sweep_inflight(). The longest legitimate upstream calls (full SEC EDGAR
# 10-K fetch + AI summary) take 60-90s, so 5min is a comfortable buffer.
_INFLIGHT_MAX_AGE_SEC = 300.0

# Thread-local sqlite connection so writes from the warmer thread don't
# fight the request threads for the same connection object.
_local = threading.local()

# Skip persistence for keys with TTL < this — quotes refresh every 30s and
# would otherwise generate ~120 disk writes per minute per ticker.
_PERSIST_MIN_TTL = 60.0

# Per-entry cap on what we'll persist. SEC EDGAR XML responses can be
# 10-20 MB raw; even gzipped, persisting hundreds of them balloons the
# data dir to a gigabyte. If an entry exceeds this AFTER compression, we
# skip the disk write and rely on the in-memory cache only.
_PERSIST_MAX_BYTES = 2 * 1024 * 1024  # 2 MB compressed
# Aggregate cap on api_cache rows; when init() exceeds this we evict the
# largest entries first.
_TOTAL_DISK_TARGET_BYTES = 250 * 1024 * 1024  # 250 MB
# Compress payloads larger than this — small JSON responses don't benefit
# meaningfully and the inline base64 prefix costs us a few bytes either way.
_COMPRESS_MIN_BYTES = 4 * 1024
# Prefix used to mark gzip+base64 entries so cache_get can transparently
# decompress legacy + new rows.
_GZ_PREFIX = "gz:"


def _maybe_compress(raw: str) -> str:
    """Compress a JSON string above the threshold, return as `gz:<b64>`.
    Below threshold, return the raw string unchanged."""
    if len(raw) < _COMPRESS_MIN_BYTES:
        return raw
    try:
        gz = gzip.compress(raw.encode("utf-8"), compresslevel=6)
        return _GZ_PREFIX + base64.b64encode(gz).decode("ascii")
    except Exception:
        return raw


def _maybe_decompress(raw: str) -> str:
    """Reverse of _maybe_compress. Transparent for un-prefixed rows so
    legacy uncompressed cache entries keep working."""
    if not raw or not raw.startswith(_GZ_PREFIX):
        return raw
    try:
        return gzip.decompress(base64.b64decode(raw[len(_GZ_PREFIX):])).decode("utf-8")
    except Exception as e:
        log.warning("cache decompress failed: %s", e)
        return raw


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
        c = sqlite3.connect(_DB_PATH, check_same_thread=False, timeout=10.0)
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        # Match database.get_conn (5000ms). Previously 3000ms here, which
        # caused this connection to bail with SQLITE_BUSY while the main
        # database.py connection on the same file was still happy to wait.
        c.execute("PRAGMA busy_timeout=5000")
        # foreign_keys is a per-connection PRAGMA. database.get_conn enables
        # it so ON DELETE SET NULL on portfolio.account_id fires correctly.
        # Without the same setting here, this connection can issue writes
        # against the same DB file with FK enforcement OFF — any future
        # cache_store helper that touches portfolio/transactions (or any code
        # path that runs cross-table cleanup through this connection) would
        # silently leave dangling FKs. Keep PRAGMAs aligned across every
        # connection opened against wealth.db.
        c.execute("PRAGMA foreign_keys = ON")
        _local.c = c
    return c


def close_thread_conn():
    """Explicitly close this thread's cache_store connection. Short-lived
    worker threads should call this before exiting so the FD is released
    immediately rather than waiting for GC. No-op if no connection was
    opened on this thread.

    Commits before close: sqlite3.Connection.close() rolls back any open
    implicit transaction. cache_set writes inside an explicit commit
    today, but the defensive commit here guards against a future helper
    that buffers a write through this connection without committing
    (the api_cache row would otherwise vanish on thread exit)."""
    c = getattr(_local, "c", None)
    if c is not None:
        try:
            c.commit()
        except Exception:
            pass
        try:
            c.close()
        except Exception:
            pass
        try:
            del _local.c
        except AttributeError:
            pass


def _serialize_key(key) -> str:
    """Stable string form of a tuple/dict/etc. so SQLite can use it as PK."""
    if isinstance(key, str):
        return key
    try:
        return json.dumps(key, default=str, sort_keys=True)
    except (TypeError, ValueError):
        return repr(key)


_table_ready = False
_table_ready_lock = threading.Lock()


def _ensure_table() -> bool:
    """Create the api_cache table if it doesn't exist. Returns True if the
    table is usable, False otherwise. Called from init() and lazily from
    cache_set() if init() failed (e.g. DB was momentarily locked at app
    startup) — so a transient boot-time SQLITE_BUSY doesn't disable the
    disk cache for the rest of the process lifetime."""
    global _table_ready
    if _table_ready:
        return True
    with _table_ready_lock:
        if _table_ready:
            return True
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
            _table_ready = True
            return True
        except Exception as e:
            log.warning("cache table init failed: %s", e)
            return False


def init() -> None:
    """Create the cache table and hydrate the in-memory map from disk.
    Idempotent — safe to call multiple times. Called once at app startup."""
    if not _ensure_table():
        return
    c = _conn()

    # Sweep expired rows before loading — keeps the in-memory map lean and
    # the SQLite table from growing without bound across many sessions.
    try:
        now = time.time()
        c.execute("DELETE FROM api_cache WHERE expiry < ?", (now,))
        c.commit()
    except Exception:
        pass

    # Aggregate-size cap: if the cache has grown larger than
    # _TOTAL_DISK_TARGET_BYTES (default 250 MB), evict the largest rows
    # until we're under the target. SEC EDGAR filing-text bodies can be
    # 10-20 MB each; without this an active user can balloon the data dir
    # past a gigabyte in a day even with TTL-based expiry working.
    try:
        total_size = c.execute("SELECT COALESCE(SUM(length(value)), 0) FROM api_cache").fetchone()[0]
        if total_size and total_size > _TOTAL_DISK_TARGET_BYTES:
            to_drop = c.execute(
                "SELECT key, length(value) FROM api_cache ORDER BY length(value) DESC"
            ).fetchall()
            dropped = 0
            dropped_bytes = 0
            for key_to_drop, sz in to_drop:
                c.execute("DELETE FROM api_cache WHERE key=?", (key_to_drop,))
                dropped += 1
                dropped_bytes += sz
                if total_size - dropped_bytes <= _TOTAL_DISK_TARGET_BYTES:
                    break
            c.commit()
            log.info("cache size cap evicted %d largest rows (%.1f MB freed)",
                     dropped, dropped_bytes / 1024.0 / 1024.0)
    except Exception as e:
        log.debug("cache size-cap eviction failed: %s", e)

    # Hydrate, skipping rows that look like cached failures from a prior
    # broken session. Otherwise a transient upstream outage at one boot would
    # serve blank panels for the full TTL window across every subsequent boot.
    loaded = 0
    skipped = 0
    poisoned_keys = []
    try:
        rows = c.execute("SELECT key, value, expiry FROM api_cache").fetchall()
        now = time.time()
        with _mem_lock:
            for key, raw, expiry in rows:
                try:
                    val = json.loads(_maybe_decompress(raw))
                except Exception:
                    continue
                if _looks_like_failure(val):
                    poisoned_keys.append(key)
                    skipped += 1
                    continue
                _mem[key] = (val, expiry, now)
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
    # Hydrate from legacy 2-tuple format if a prior version's _mem leaked in.
    if len(hit) == 2:
        value, expiry = hit
        last_access = expiry  # best effort
    else:
        value, expiry, last_access = hit
    now = time.time()
    if now >= expiry:
        with _mem_lock:
            _mem.pop(k, None)
        return None
    if _looks_like_failure(value):
        with _mem_lock:
            _mem.pop(k, None)
        return None
    # Refresh last_access so LRU eviction favors recently-read entries.
    # Avoid the lock on every read by writing the tuple back directly —
    # dict item replacement is atomic in CPython.
    _mem[k] = (value, expiry, now)
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
    now = time.time()
    expiry = now + float(ttl)
    with _mem_lock:
        _mem[k] = (value, expiry, now)
        # Bound in-memory size — sweeps run only when we cross the threshold
        # so the hot path stays cheap. Eviction is two-stage: (1) drop
        # already-expired entries; (2) if still over cap, drop the
        # least-recently-accessed entries (true LRU, not just oldest-by-write).
        if len(_mem) > _MEM_SOFT_CAP:
            for stale_k in [kk for kk, t in _mem.items() if t[1] < now]:
                _mem.pop(stale_k, None)
            if len(_mem) > _MEM_SOFT_CAP:
                # Tuple is (value, expiry, last_access); evict by last_access.
                # Legacy 2-tuples (unlikely after the in-place hydration in
                # cache_get) fall back to expiry as the access proxy.
                def _lru_key(kv):
                    t = kv[1]
                    return t[2] if len(t) >= 3 else t[1]
                ordered = sorted(_mem.items(), key=_lru_key)
                for kk, _ in ordered[: len(_mem) - _MEM_EVICT_TARGET]:
                    _mem.pop(kk, None)

    if ttl < _PERSIST_MIN_TTL:
        return
    # Lazy table creation: if init() was called before the DB was writable
    # (rare boot-time SQLITE_BUSY), retry here so the disk cache doesn't
    # stay permanently disabled for the rest of the process.
    if not _ensure_table():
        return
    try:
        raw = json.dumps(value, default=str)
    except Exception:
        return
    stored = _maybe_compress(raw)
    # Hard cap on stored size — SEC filing text bodies, options chains for
    # high-symbol-count scans, and full equity 10y daily histories can all
    # blow past a few MB. Caching them on disk pushes the data dir well
    # over a gigabyte over a day of use (observed: 927MB at v0.3.3). The
    # in-memory cache already has the value for this session; future
    # sessions just refetch.
    if len(stored) > _PERSIST_MAX_BYTES:
        log.debug("cache persist skipped (too large): %s bytes for %s", len(stored), k[:60])
        return
    try:
        c = _conn()
        c.execute(
            "INSERT OR REPLACE INTO api_cache(key, value, expiry, written_at) VALUES(?,?,?,?)",
            (k, stored, expiry, time.time()),
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

    # Opportunistic sweep of orphaned events so callers don't pay the
    # full wait timeout on a key whose previous fetch_fn never returned.
    _sweep_inflight()

    # Claim the in-flight slot or wait for the existing fetch.
    with _inflight_lock:
        entry = _inflight.get(k)
        if entry is not None:
            ev, _claimed_at = entry
            owner = False
        else:
            ev = threading.Event()
            _inflight[k] = (ev, time.time())
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
                # Only remove the slot if it still belongs to *this* owner.
                # Race scenario this guards against: _sweep_inflight()
                # declared us orphaned and dropped our entry, then a fresh
                # caller claimed the same key and installed a NEW (Event,
                # ts) tuple. A blind pop(k) here would delete that fresh
                # caller's slot, letting a third caller stampede with a
                # duplicate fetch_fn while the second waiter blocks until
                # its 15s wait timeout.
                cur = _inflight.get(k)
                if cur is not None and cur[0] is ev:
                    _inflight.pop(k, None)
            ev.set()


def _sweep_inflight() -> int:
    """Release in-flight events whose fetch_fn appears to have died.

    Returns the number of entries swept. Safe to call from any thread.
    Cheap (lock-protected dict scan) and idempotent."""
    now = time.time()
    swept = 0
    with _inflight_lock:
        # Materialize to a list so we can mutate the dict during iteration.
        stale = [
            (kk, ev)
            for kk, (ev, claimed_at) in _inflight.items()
            if now - claimed_at > _INFLIGHT_MAX_AGE_SEC
        ]
        for kk, ev in stale:
            _inflight.pop(kk, None)
            swept += 1
    # Set events OUTSIDE the lock — Event.set() can wake other threads that
    # may try to re-acquire _inflight_lock immediately.
    for _, ev in stale:
        try:
            ev.set()
        except Exception:
            pass
    if swept:
        log.info("cache: swept %d orphaned in-flight events", swept)
    return swept


def stats() -> dict:
    """Lightweight introspection for a debug endpoint."""
    with _mem_lock:
        n_mem = len(_mem)
        now = time.time()
        # Tuple is (value, expiry, last_access); we only need expiry here.
        fresh = sum(1 for t in _mem.values() if t[1] > now)
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
