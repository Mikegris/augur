"""Offline tests for cache_store api_cache eviction (v2.4).

Verifies the fix for wealth.db bloat: prune_disk() must drop expired rows,
enforce the byte cap largest-first, no-op cleanly on an empty table, and
reclaim() must prune + VACUUM without raising. Runs against a throwaway temp
DB — never touches the real wealth.db.
"""
import os
import sys
import tempfile
import time

import cache_store as cs


def _fresh_db():
    """Point cache_store at a brand-new temp DB and reset its thread conn."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    cs._DB_PATH = path
    # Drop any cached thread-local connection so _conn() reopens on the new path.
    try:
        if getattr(cs._local, "c", None) is not None:
            cs._local.c.close()
    except Exception:
        pass
    cs._local.c = None
    cs._table_ready = False  # force _ensure_table to recreate on the temp DB
    assert cs._ensure_table()
    return path


def _insert(key, value, expiry):
    c = cs._conn()
    c.execute("INSERT OR REPLACE INTO api_cache(key, value, expiry, written_at) "
              "VALUES (?,?,?,?)", (key, value, expiry, time.time()))
    c.commit()


def _count():
    return cs._conn().execute("SELECT COUNT(*) FROM api_cache").fetchone()[0]


def _keys():
    return {r[0] for r in cs._conn().execute("SELECT key FROM api_cache").fetchall()}


def test_prune_expired_only():
    _fresh_db()
    now = time.time()
    _insert("fresh1", "x" * 100, now + 3600)
    _insert("fresh2", "y" * 100, now + 3600)
    # Beyond the serve-stale grace window → swept.
    _insert("old1", "z" * 100, now - cs._EXPIRED_GRACE_SEC - 10)
    _insert("old2", "w" * 100, now - cs._EXPIRED_GRACE_SEC - 3600)
    # Expired but INSIDE the grace window — must survive the sweep so
    # cache_get_stale's disk fallback keeps working after a restart.
    _insert("graced", "g" * 100, now - 10)
    out = cs.prune_disk(target_bytes=10 ** 9)  # cap effectively disabled
    assert out["expired"] == 2, out
    assert out["evicted"] == 0, out
    assert _keys() == {"fresh1", "fresh2", "graced"}, _keys()
    print("  [OK] prune_disk removes only expired-beyond-grace rows")


def test_size_cap_evicts_largest_first():
    _fresh_db()
    now = time.time()
    # All fresh; total ~6000 bytes. Cap at 2500 → must evict the biggest until
    # under, keeping the smallest.
    _insert("big", "B" * 4000, now + 3600)
    _insert("mid", "M" * 1500, now + 3600)
    _insert("small", "S" * 500, now + 3600)
    out = cs.prune_disk(target_bytes=2500)
    assert out["evicted"] >= 1, out
    ks = _keys()
    assert "big" not in ks, ks            # largest dropped first
    assert "small" in ks, ks              # smallest survives
    print("  [OK] size cap evicts largest-first, keeps small rows")


def test_empty_table_no_raise():
    _fresh_db()
    out = cs.prune_disk()
    assert out["expired"] == 0 and out["evicted"] == 0, out
    print("  [OK] prune_disk on empty table is a clean no-op")


def test_reclaim_vacuums():
    path = _fresh_db()
    now = time.time()
    for i in range(50):
        # all expired beyond the grace window → all pruned
        _insert("k%d" % i, "Q" * 2000, now - cs._EXPIRED_GRACE_SEC - 10)
    out = cs.reclaim(force_vacuum=True)
    assert out["expired"] == 50, out
    assert _count() == 0, _count()
    # force_vacuum path executed without raising; file still valid.
    assert os.path.exists(path)
    print("  [OK] reclaim prunes then VACUUMs without raising")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    print("cache_store prune — {} tests".format(len(fns)))
    failed = 0
    for fn in fns:
        try:
            fn()
        except AssertionError as e:
            failed += 1
            print("  [XX] {}: {}".format(fn.__name__, e))
        except Exception as e:
            failed += 1
            print("  [XX] {}: unexpected {}: {}".format(fn.__name__, type(e).__name__, e))
    print("PASS" if failed == 0 else "{} FAILED".format(failed))
    sys.exit(1 if failed else 0)
