"""
safe_executor — bounded parallel map on DAEMON threads.

History: this module originally wrapped ThreadPoolExecutor to survive the
`cannot schedule new futures after interpreter shutdown` RuntimeError (the
concurrent.futures global `_shutdown` flag flips when any atexit handler
fires — observed in the py2app bundle via pywebview/joblib). The wrapper
fixed the 500s, but TPE had a second, worse failure mode we hit in dev:

  Python 3.9 TPE worker threads are NON-daemon, and concurrent.futures
  registers an atexit join over all of them. A warmer-spawned 200-item
  sweep mid-flight at shutdown therefore blocks interpreter exit for
  minutes — which wedges the Werkzeug reloader while it holds the port,
  and hangs the desktop bundle on quit.

Plain daemon threads have neither problem: nothing joins them at exit, and
there is no global shutdown flag to trip over. So `parallel_map` now runs
on its own tiny daemon-thread pool:

`parallel_map(work_fn, items, max_workers, timeout_per_item, ...) -> list`

  • Results come back in input order; a slot is None if its work_fn raised
    (or didn't finish before the overall deadline).
  • `timeout_per_item` is an OVERALL batch deadline (same semantics the
    as_completed(timeout=...) implementation had). On expiry we return a
    snapshot — stragglers keep running on daemon threads but can't mutate
    the returned list.
  • If no worker thread can be started at all (thread exhaustion, late
    interpreter shutdown), falls back to running serially in the calling
    thread — the caller still gets a full list and the route doesn't 500.

Python 3.9 compatible.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Iterable, List, Optional

log = logging.getLogger("augur.safe_executor")


def _looks_like_global_shutdown(exc: BaseException) -> bool:
    """RuntimeErrors that mean threads can't be spawned, so we should fall
    back to serial. Distinct conditions, same remedy:
      • interpreter-shutdown: thread creation refused while Python exits —
        "can't create new thread at interpreter shutdown" (and the legacy
        concurrent.futures phrasing, kept for callers that still raise it).
      • thread exhaustion: the OS/process can't allocate another thread —
        "can't start new thread" / "failed to create thread". Observed under
        concurrent load (warmer + many in-flight requests each spawning pools).
    Serial execution spawns no threads, so it works in both cases. String match
    because Python doesn't expose distinct exception types for these."""
    msg = str(exc).lower()
    return (
        "interpreter shutdown" in msg
        or "cannot schedule new futures" in msg
        or "can't start new thread" in msg
        or "can't create new thread" in msg
        or "failed to create thread" in msg
    )


def _spawn(target: Callable[[], None], name: str) -> threading.Thread:
    """Start one daemon worker. Separate seam so tests can simulate
    thread-exhaustion by monkeypatching."""
    t = threading.Thread(target=target, daemon=True, name=name)
    t.start()
    return t


def parallel_map(
    work_fn: Callable[[Any], Any],
    items: Iterable[Any],
    max_workers: int = 6,
    timeout_per_item: Optional[float] = None,
    thread_name_prefix: str = "augur-parallel",
) -> List[Any]:
    """Run `work_fn(item)` for each item, in parallel where possible.

    Always returns a list of results in the SAME ORDER as `items`. If a
    work_fn call raises, its slot is filled with None. Falls back to
    sequential execution if no worker thread can be started (see module
    docstring).
    """
    items_list = list(items)
    if not items_list:
        return []

    results: List[Any] = [None] * len(items_list)
    cursor = {"i": 0}
    lock = threading.Lock()

    def _worker():
        while True:
            with lock:
                i = cursor["i"]
                if i >= len(items_list):
                    return
                cursor["i"] = i + 1
            try:
                results[i] = work_fn(items_list[i])
            except Exception as e:
                log.debug("parallel_map work_fn raised at idx %d: %s", i, e)
                results[i] = None

    n_workers = max(1, min(int(max_workers), len(items_list)))
    threads: List[threading.Thread] = []
    for w in range(n_workers):
        try:
            threads.append(_spawn(_worker, "{}-{}".format(thread_name_prefix, w)))
        except RuntimeError as e:
            if _looks_like_global_shutdown(e):
                if threads:
                    break  # partial pool — the started workers will drain the queue
                log.warning(
                    "thread spawn unavailable (%s); falling back to serial for %d items",
                    e, len(items_list),
                )
                return _serial_map(work_fn, items_list, timeout_per_item)
            raise

    deadline = (time.time() + timeout_per_item) if timeout_per_item else None
    for t in threads:
        t.join(max(0.0, deadline - time.time()) if deadline is not None else None)

    if deadline is not None and any(t.is_alive() for t in threads):
        done = sum(1 for r in results if r is not None)
        log.warning(
            "parallel_map overall timeout (%ss); %d/%d items done, rest None",
            timeout_per_item, done, len(results),
        )

    # Snapshot: stragglers past the deadline keep running on daemon threads
    # but must not mutate the list the caller received.
    return list(results)


def _serial_map(
    work_fn: Callable[[Any], Any],
    items: List[Any],
    timeout_per_item: Optional[float] = None,
) -> List[Any]:
    """Sequential fallback used when worker threads are unavailable.
    `timeout_per_item` is accepted for API parity but ignored — without
    threads we can't enforce a per-item deadline without a heavyweight
    signal-based timer."""
    results: List[Any] = []
    for item in items:
        try:
            results.append(work_fn(item))
        except Exception as e:
            log.debug("serial work_fn raised on %r: %s", item, e)
            results.append(None)
    return results
