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

# Hit/miss counters for stats(). Plain `int +=` without a lock: under the GIL
# each increment is close-enough-to-atomic that the worst case is an
# occasionally lost count — these are approximate observability counters, not
# billing, so the cost of a lock on every cache read isn't worth it.
_hits = 0
_misses = 0

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
# Don't run the orphan sweep more than once per this many seconds. coalesce()
# used to sweep on EVERY cache miss, which under a miss storm (warmer cycle +
# UI fan-out) serialized all misses on _inflight_lock for a scan whose useful
# work happens at most once per _INFLIGHT_MAX_AGE_SEC anyway.
_SWEEP_MIN_INTERVAL_SEC = 5.0
_last_sweep_ts = [0.0]  # mutable epoch; benign race — worst case one extra sweep

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
# Aggregate cap on api_cache payload bytes; eviction (largest-first) keeps the
# table under this. Previously 250 MB and only enforced at boot — which let the
# table balloon to ~90 MB during long-running sessions (expired rows are only
# swept at startup too). Now a sane default, env-overridable, and enforced
# periodically by the cache warmer as well as at boot. See prune_disk().
_TOTAL_DISK_TARGET_BYTES = int(
    os.environ.get("AUGUR_CACHE_MAX_BYTES", str(40 * 1024 * 1024)))  # 40 MB
# Compress payloads larger than this — small JSON responses don't benefit
# meaningfully and the inline base64 prefix costs us a few bytes either way.
_COMPRESS_MIN_BYTES = 4 * 1024
# Prefix used to mark gzip+base64 entries so cache_get can transparently
# decompress legacy + new rows.
_GZ_PREFIX = "gz:"
# How long an EXPIRED disk row is kept before prune_disk deletes it. Must
# cover cache_get_stale's default max_age_seconds (24h) so its serve-stale
# disk fallback survives the boot/warmer sweeps.
_EXPIRED_GRACE_SEC = 24 * 3600.0


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


# Sentinel wrapper for failure-shaped values persisted deliberately via
# cache_set(..., allow_empty=True) — i.e. NEGATIVE caches ("this PTR really
# has no trades"). The disk read paths (_load_from_disk and init()'s hydrate)
# unconditionally drop failure-shaped rows as poison from a broken session,
# which silently killed persisted negative caches on LRU eviction or restart
# and re-triggered the exact thundering herd (PDF re-download + re-parse)
# they exist to prevent. Wrapping on write lets the readers tell
# "deliberately cached empty" apart from "poison" and unwrap it.
_NEG_SENTINEL = "__allow_empty__"


def _unwrap_negative(val):
    """Return (value, was_negative). Transparent for unwrapped rows so every
    legacy cache entry keeps working."""
    if isinstance(val, dict) and val.get(_NEG_SENTINEL) is True and "v" in val:
        return val["v"], True
    return val, False


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
            # Boot hydrate does `ORDER BY written_at DESC LIMIT N`; without this
            # index that's a full-table sort on every startup (observed ~8k
            # rows). written_at is a REAL epoch, so the index orders cleanly.
            c.execute("CREATE INDEX IF NOT EXISTS api_cache_written ON api_cache(written_at)")
            c.commit()
            _table_ready = True
            return True
        except Exception as e:
            log.warning("cache table init failed: %s", e)
            return False


def prune_disk(target_bytes: Optional[int] = None) -> dict:
    """Delete expired api_cache rows, then — if payload bytes still exceed the
    cap — evict largest-first until under it. Safe to call repeatedly (boot +
    the periodic warmer pass); fail-open. Uses cache_store's own connection, so
    it never touches database._write_lock (the VACUUM-wedge hazard). Returns a
    summary dict {expired, evicted, freed_mb, total_mb}."""
    summary = {"expired": 0, "evicted": 0, "freed_mb": 0.0, "total_mb": 0.0}
    if not _ensure_table():
        return summary
    target = _TOTAL_DISK_TARGET_BYTES if target_bytes is None else int(target_bytes)
    # 1) Expired sweep — the piece that was previously boot-only, which let
    #    expired rows pile up across a long-running session.
    #    Expired rows get a GRACE window rather than deletion the instant
    #    their TTL passes: cache_get_stale's disk fallback (_load_from_disk
    #    with allow_expired=True) exists to serve recently-expired rows for
    #    graceful degradation after a restart, and this sweep runs at boot
    #    BEFORE any lookup (plus every 30 min via the warmer) — a zero-grace
    #    delete made that path unreachable and contradicted
    #    database.prune_api_cache's documented 3-day grace. The window
    #    matches cache_get_stale's default max_age_seconds (24h); the size
    #    cap below still evicts in-grace expired rows first when over target.
    try:
        c = _conn()
        cur = c.execute("DELETE FROM api_cache WHERE expiry < ?",
                        (time.time() - _EXPIRED_GRACE_SEC,))
        c.commit()
        summary["expired"] = cur.rowcount if (cur.rowcount and cur.rowcount > 0) else 0
    except Exception as e:
        log.debug("prune_disk expired sweep failed: %s", e)
    # 2) Size cap — evict the largest payloads first until under target.
    try:
        c = _conn()
        total = c.execute(
            "SELECT COALESCE(SUM(length(value)), 0) FROM api_cache").fetchone()[0] or 0
        if total > target:
            rows = c.execute(
                "SELECT key, length(value) FROM api_cache "
                # Already-expired rows first (any survived the sweep above),
                # then largest payloads, then oldest-written among equal sizes —
                # so a hot, freshly-written large entry isn't evicted ahead of
                # cold/stale rows, avoiding refetch churn for the hot key.
                "ORDER BY (expiry < ?) DESC, length(value) DESC, written_at ASC",
                (time.time(),)).fetchall()
            dropped = 0
            freed = 0
            for key, sz in rows:
                c.execute("DELETE FROM api_cache WHERE key=?", (key,))
                dropped += 1
                freed += (sz or 0)
                if total - freed <= target:
                    break
            c.commit()
            summary["evicted"] = dropped
            summary["freed_mb"] = freed / 1024.0 / 1024.0
            total -= freed
            log.info("cache size cap evicted %d largest rows (%.1f MB freed)",
                     dropped, summary["freed_mb"])
        summary["total_mb"] = total / 1024.0 / 1024.0
    except Exception as e:
        log.debug("prune_disk size cap failed: %s", e)
    return summary


def reclaim(force_vacuum: bool = False) -> dict:
    """Heavier one-time reclamation: prune, then VACUUM to actually return the
    freed space to the OS (a plain DELETE only moves pages to the freelist for
    reuse — the file doesn't shrink). VACUUM rewrites the whole DB, so it runs
    only when there's real slack (large freelist / bloated file) or when forced;
    that keeps it from firing on every boot. Best-effort — never raises."""
    summary = prune_disk()
    try:
        c = _conn()
        page_size = c.execute("PRAGMA page_size").fetchone()[0]
        page_count = c.execute("PRAGMA page_count").fetchone()[0]
        freelist = c.execute("PRAGMA freelist_count").fetchone()[0]
        file_mb = page_size * page_count / 1024.0 / 1024.0
        slack_mb = freelist * page_size / 1024.0 / 1024.0
        # Only worth a rewrite when there's meaningful reclaimable slack.
        bloated = slack_mb > 20 or (file_mb > 60 and freelist > page_count * 0.2)
        if force_vacuum or bloated:
            c.execute("VACUUM")
            c.commit()
            summary["vacuumed_mb"] = round(slack_mb, 1)
            log.info("cache reclaim VACUUM reclaimed ~%.1f MB", slack_mb)
    except Exception as e:
        log.debug("reclaim VACUUM skipped: %s", e)
    return summary


def init() -> None:
    """Create the cache table and hydrate the in-memory map from disk.
    Idempotent — safe to call multiple times. Called once at app startup."""
    if not _ensure_table():
        return
    c = _conn()

    # Prune expired + over-cap rows before loading, and reclaim file slack with
    # a guarded VACUUM (only fires when the DB is actually bloated — e.g. the
    # one-time shrink after this cap was lowered). Keeps the in-memory map lean
    # and the SQLite file from growing without bound across sessions.
    try:
        reclaim()
    except Exception as e:
        log.debug("cache boot reclaim failed: %s", e)

    # Hydrate, skipping rows that look like cached failures from a prior
    # broken session. Otherwise a transient upstream outage at one boot would
    # serve blank panels for the full TTL window across every subsequent boot.
    #
    # Only the newest _MEM_EVICT_TARGET rows are loaded eagerly. The map is
    # capped at _MEM_SOFT_CAP anyway, so the old full-table hydrate (observed:
    # ~8k rows / ~100 MB / 2.2s of json.loads at every reload) did work that
    # the first cache_set() promptly threw away via the LRU sweep. Rows beyond
    # the limit are NOT lost — cache_get/_load_from_disk lazily pulls any
    # still-fresh row into memory on first miss.
    loaded = 0
    skipped = 0
    poisoned_keys = []
    try:
        rows = c.execute(
            "SELECT key, value, expiry FROM api_cache "
            "ORDER BY written_at DESC LIMIT ?",
            (_MEM_EVICT_TARGET,),
        ).fetchall()
        now = time.time()
        with _mem_lock:
            for key, raw, expiry in rows:
                try:
                    val = json.loads(_maybe_decompress(raw))
                except Exception:
                    continue
                # Deliberate negative caches (sentinel-wrapped by cache_set
                # with allow_empty=True) are NOT poison — hydrate them.
                val, is_negative = _unwrap_negative(val)
                if not is_negative and _looks_like_failure(val):
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


def _load_from_disk(k: str, allow_expired: bool = False):
    """Lazily hydrate one row from SQLite into the in-memory map.

    Boot-time hydration only eagerly loads the newest _MEM_EVICT_TARGET rows;
    anything older (or LRU-evicted mid-session) is recovered here on first
    miss. Returns the freshly-installed (value, expiry, last_access) tuple, or
    None if the row is missing/failure-shaped. A point lookup on the PK costs
    ~10-50µs — three orders of magnitude cheaper than the upstream refetch a
    miss would otherwise trigger.

    By default expired rows are NOT returned (cache_get wants freshness). With
    allow_expired=True the row is returned even past its expiry — this is what
    makes cache_get_stale's disk fallback work after a restart, where the
    expired row only lives on disk and was never hydrated into _mem. The
    expired entry is NOT installed into _mem (it would pollute the fresh-read
    path); the caller gets the tuple directly for graceful degradation."""
    if not _table_ready:
        return None
    try:
        c = _conn()
        row = c.execute(
            "SELECT value, expiry FROM api_cache WHERE key=?", (k,)
        ).fetchone()
    except Exception:
        return None
    if not row:
        return None
    raw, expiry = row
    now = time.time()
    expired = (not expiry) or expiry <= now
    if expired and not allow_expired:
        return None
    try:
        val = json.loads(_maybe_decompress(raw))
    except Exception:
        return None
    # A sentinel-wrapped negative cache is a deliberate write — skip the
    # poison check for it (see _NEG_SENTINEL).
    val, is_negative = _unwrap_negative(val)
    if not is_negative and _looks_like_failure(val):
        return None
    hit = (val, expiry, now)
    if not expired:
        # Only install fresh rows into _mem; an expired row must not be served
        # by the fresh-read path (cache_get).
        with _mem_lock:
            _mem[k] = hit
    return hit


def cache_get(key, ttl: Optional[float] = None):
    """Return value if fresh, else None. The `ttl` kwarg is decorative
    (kept for compat with the old helper) — the writer's expiry wins.

    A defensive failure check runs at read time too, so even if some legacy
    code path slipped a null-shaped value into the map, we treat it as a
    miss and let the caller refetch."""
    global _hits, _misses
    k = _serialize_key(key)
    hit = _mem.get(k)
    if hit is None:
        hit = _load_from_disk(k)
    if hit is None:
        _misses += 1
        return None
    # Hydrate from legacy 2-tuple format if a prior version's _mem leaked in.
    if len(hit) == 2:
        value, expiry = hit
        last_access = expiry  # best effort
    else:
        value, expiry, last_access = hit
    now = time.time()
    if now >= expiry:
        # Expired — but leave the entry in _mem so cache_get_stale can still
        # serve it for graceful degradation. It's evicted by the LRU sweep
        # in cache_set, not here. (Popping it on read is what made
        # cache_get_stale dead in its documented "call after cache_get
        # returns None" pattern.)
        _misses += 1
        return None
    # Read-time defense against legacy null-shaped values, but NOT against
    # empty collections — those may be a legitimate negative cache written
    # with allow_empty=True (cache_set already gated the write). Only drop
    # genuine error-envelope dicts here.
    if isinstance(value, dict) and value.get("error") and len(value) <= 3:
        with _mem_lock:
            _mem.pop(k, None)
        _misses += 1
        return None
    # Refresh last_access so LRU eviction favors recently-read entries.
    # Do the write-back under the lock and only if the slot still holds the
    # SAME tuple we read — otherwise a concurrent cache_set/eviction may have
    # already popped k or replaced it with a newer value, and an unlocked
    # write here would resurrect an evicted key or clobber the newer value.
    with _mem_lock:
        if _mem.get(k) is hit:
            _mem[k] = (value, expiry, now)
    _hits += 1
    return value


def cache_get_stale(key, max_age_seconds: float = 24 * 3600) -> Any:
    """Return a cached value even if its TTL expired, as long as it was
    written within `max_age_seconds`. Returns None if the entry is missing,
    older than `max_age_seconds`, or shaped like a failure.

    Use case: graceful degradation when an upstream is temporarily down.
    Callers that prefer "slightly stale data" over "no data at all"
    (e.g. gex_engine needs a spot price; better an hour-old quote than
    a blank panel) can call this after their normal `cache_get` returns
    None and the live fetch also fails.

    Doesn't refresh `last_access` so LRU still treats the entry as old.
    """
    k = _serialize_key(key)
    from_disk = False
    hit = _mem.get(k)
    if hit is None:
        # Fall back to disk so stale degradation survives a restart. Pass
        # allow_expired so an entry that lives ONLY on disk (never hydrated
        # because it was already expired) is still served here — without this
        # the disk fallback was dead, since _load_from_disk dropped expired
        # rows and cache_get had already popped nothing into _mem.
        hit = _load_from_disk(k, allow_expired=True)
        from_disk = True
    if hit is None:
        return None
    if len(hit) == 2:
        value, expiry = hit
        last_access = expiry
    else:
        value, expiry, last_access = hit
    now = time.time()
    if from_disk:
        # A disk-loaded expired row has last_access == now (set at load time),
        # which would make age ≈ 0 and bypass the cap. Use expiry as the age
        # anchor: for a fresh-on-disk row age is negative (treated as 0); for
        # an expired one age ≈ now - expiry, the real staleness.
        age = max(0.0, now - expiry)
    else:
        # Compute age from last_access; for an entry that just expired in
        # memory, last_access ≈ expiry, so age ≈ now - expiry.
        age = now - last_access
    if age > max_age_seconds:
        return None
    if _looks_like_failure(value):
        return None
    return value


def cache_set(key, value, ttl: float, allow_empty: bool = False,
              persist: bool = True) -> None:
    """Store in memory + (if ttl >= _PERSIST_MIN_TTL) write through to disk.

    Refuses to cache values that look like upstream failures — neither in
    memory nor on disk. A previous version stored failures in memory and
    only blocked the disk write, which caused a transient rate-limit on the
    first request after boot to freeze blank panels for the full TTL window.

    `allow_empty=True` opts a caller out of the failure heuristic when an
    empty/falsy value is a LEGITIMATE answer to cache (e.g. a NEGATIVE cache:
    "this PTR genuinely has no trades / 404'd" — caching that for a short TTL
    is the whole point, and the heuristic would otherwise silently drop it).

    `persist=False` keeps the entry memory-only even when its TTL clears the
    persistence threshold. Negative caches want this: a "symbol had no quote"
    verdict must not survive a restart (the upstream may simply have been
    mid-outage when the verdict was recorded).
    """
    if not allow_empty and _looks_like_failure(value):
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
            n_before = len(_mem)
            for stale_k in [kk for kk, t in _mem.items() if t[1] < now]:
                _mem.pop(stale_k, None)
            if len(_mem) > _MEM_SOFT_CAP:
                # Tuple is (value, expiry, last_access); evict by last_access.
                # Legacy 2-tuples (unlikely after the in-place hydration in
                # cache_get) fall back to expiry as the access proxy.
                def _lru_key(kv):
                    # 3-tuples sort by real last_access; legacy 2-tuples have
                    # no last_access, and their t[1] is an *expiry* (a
                    # different clock) — mixing the two could evict a hot
                    # 3-tuple before a stale 2-tuple. Anchor 2-tuples to 0.0
                    # so they always sort oldest and get evicted first.
                    t = kv[1]
                    return t[2] if len(t) >= 3 else 0.0
                ordered = sorted(_mem.items(), key=_lru_key)
                for kk, _ in ordered[: len(_mem) - _MEM_EVICT_TARGET]:
                    _mem.pop(kk, None)
            # One summary line instead of silence — eviction storms are the
            # first thing to look for when "cached" panels keep refetching.
            log.debug("cache mem sweep evicted %d entries (%d -> %d)",
                      n_before - len(_mem), n_before, len(_mem))

    if not persist or ttl < _PERSIST_MIN_TTL:
        return
    # Lazy table creation: if init() was called before the DB was writable
    # (rare boot-time SQLITE_BUSY), retry here so the disk cache doesn't
    # stay permanently disabled for the rest of the process.
    if not _ensure_table():
        return
    payload = value
    if allow_empty and _looks_like_failure(value):
        # A deliberate negative cache heading to disk — wrap it so the disk
        # read paths don't drop it as poison. See _NEG_SENTINEL.
        payload = {_NEG_SENTINEL: True, "v": value}
    try:
        raw = json.dumps(payload, default=str)
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


def cache_delete(key) -> None:
    """Drop a single entry from both the in-memory map and disk. Fail-open:
    used to invalidate a stale negative verdict once a real value lands, so a
    failure here must never propagate into the hot path."""
    k = _serialize_key(key)
    with _mem_lock:
        _mem.pop(k, None)
    if not _table_ready:
        return
    try:
        c = _conn()
        c.execute("DELETE FROM api_cache WHERE key=?", (k,))
        c.commit()
    except Exception:
        pass


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
    # Throttled: the orphan reaper's useful work happens at most once per
    # _INFLIGHT_MAX_AGE_SEC, so scanning _inflight under the global lock on
    # EVERY miss serialized a miss storm for nothing.
    now_ts = time.time()
    if now_ts - _last_sweep_ts[0] >= _SWEEP_MIN_INTERVAL_SEC:
        _last_sweep_ts[0] = now_ts
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

    # Bounded re-claim loop: a single timed-out waiter must never block
    # forever (the original "better stale-shadow than blocking" guarantee), so
    # after a couple of wait/re-claim cycles we give up and fetch ourselves.
    reclaim_budget = 2
    while not owner:
        # Wait for the in-flight fetch to finish, in slices, re-checking that
        # the owner is still alive. A flat 15s gave up while the expensive
        # fetches this exists to protect (cluster/divmap/SEC, 30-90s) were
        # still running, so the waiter duplicated the work serially. We wait
        # up to _INFLIGHT_MAX_AGE_SEC as long as the owner's slot persists.
        deadline = time.time() + _INFLIGHT_MAX_AGE_SEC
        while time.time() < deadline:
            if ev.wait(timeout=5.0):
                break
            with _inflight_lock:
                cur = _inflight.get(k)
            if cur is None or cur[0] is not ev:
                break  # owner finished (or was reaped) — stop waiting
        cached = cache_get(key)
        if cached is not None:
            return cached
        if reclaim_budget <= 0:
            break  # don't block forever — fall through and fetch ourselves
        reclaim_budget -= 1
        # Owner gave us nothing (still running past the deadline, reaped, or
        # finished with a failure that wasn't cached). Instead of every timed-
        # out waiter stampeding fetch_fn in parallel — the exact thing coalesce
        # exists to prevent on the 60-90s SEC/cluster fetches — re-claim the
        # slot under the lock. The FIRST late waiter installs a fresh Event and
        # becomes owner (so it caches the result and repopulates _inflight);
        # any other late waiter finds that live slot and loops back to wait on
        # it rather than duplicating the upstream call.
        with _inflight_lock:
            entry = _inflight.get(k)
            if entry is None:
                ev = threading.Event()
                _inflight[k] = (ev, time.time())
                owner = True
            else:
                ev, _claimed_at = entry
        # If we didn't become owner here, `ev` now points at the live slot and
        # the while-loop re-waits on it.

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
    # Snapshot the counters once so hits/misses/hit_rate are mutually
    # consistent within this response even if reads land mid-call.
    hits, misses = _hits, _misses
    total = hits + misses
    return {
        "in_memory": n_mem,
        "fresh": fresh,
        "on_disk": n_disk,
        "in_flight": n_inflight,
        "hits": hits,
        "misses": misses,
        "hit_rate": round(hits / total, 2) if total else 0.0,
    }


def clear() -> int:
    """Drop all entries (used by the 'clear cache' settings button)."""
    with _mem_lock:
        n = len(_mem)
        _mem.clear()
    # Disk delete with one retry: a transient SQLITE_BUSY (reclaim()'s VACUUM
    # or a long write holding the file past the 5s busy_timeout) must not be
    # silently swallowed — the supposedly-cleared rows would resurrect via
    # lazy hydration on the next cache_get while clear() returned a count
    # implying success. Retry once after a short backoff; if it still fails,
    # log at WARNING so the failure is at least observable. (The int return
    # shape is kept for existing callers.)
    for attempt in (1, 2):
        try:
            c = _conn()
            c.execute("DELETE FROM api_cache")
            c.commit()
            break
        except Exception as e:
            if attempt == 2:
                log.warning("cache clear: disk delete failed (%s) — old rows "
                            "will resurrect from disk via lazy hydration", e)
            else:
                time.sleep(0.5)
    return n
