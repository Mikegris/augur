"""
safe_executor — ThreadPoolExecutor that survives Python's interpreter-shutdown flag.

Problem observed in the v0.3.7 bundle: after a long-running session, some
endpoints (smart-money/scores, synth/cluster-scan, synth/divergence-map)
started returning HTTP 500 with `RuntimeError: cannot schedule new futures
after interpreter shutdown`. The error comes from `concurrent.futures.thread`'s
global `_shutdown` flag, which is set by `_python_exit` (an atexit handler).
Once the flag is True, every `ThreadPoolExecutor.submit()` raises immediately.

Root cause is hard to pin down inside a py2app bundle — pywebview, sklearn's
joblib, or some other library's atexit handler can flip the flag while Flask
is still serving. Restarting the bundle clears it; that's the user's
workaround today. This module provides the long-term fix:

`parallel_map(work_fn, items, max_workers, ...) -> list`

Tries ThreadPoolExecutor first. If `submit()` raises the
`cannot schedule new futures` RuntimeError, falls back to running the work
items sequentially in the calling thread. Either way, the caller gets a
list of results in input order, and the route doesn't 500.

Python 3.9 compatible.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Iterable, List, Optional

log = logging.getLogger("augur.safe_executor")


def _looks_like_global_shutdown(exc: BaseException) -> bool:
    """The exact message thrown by concurrent.futures.thread.submit when
    the module-level `_shutdown` flag is set (atexit fired). We use a
    string check because Python doesn't expose a distinct exception type."""
    msg = str(exc).lower()
    return "interpreter shutdown" in msg or "cannot schedule new futures" in msg


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
    sequential execution if ThreadPoolExecutor.submit() reports the
    interpreter-shutdown condition (see module docstring).
    """
    items_list = list(items)
    if not items_list:
        return []

    results: List[Any] = [None] * len(items_list)

    # First attempt: ThreadPoolExecutor
    try:
        with ThreadPoolExecutor(
            max_workers=max(1, int(max_workers)),
            thread_name_prefix=thread_name_prefix,
        ) as pool:
            future_to_idx = {}
            try:
                for idx, item in enumerate(items_list):
                    future_to_idx[pool.submit(work_fn, item)] = idx
            except RuntimeError as e:
                if _looks_like_global_shutdown(e):
                    log.warning(
                        "ThreadPool unavailable (%s); falling back to serial for %d items",
                        e, len(items_list),
                    )
                    return _serial_map(work_fn, items_list, timeout_per_item)
                raise

            for fut in as_completed(future_to_idx, timeout=timeout_per_item):
                idx = future_to_idx[fut]
                try:
                    results[idx] = fut.result(timeout=timeout_per_item)
                except Exception as e:
                    log.debug("parallel_map work_fn raised at idx %d: %s", idx, e)
                    results[idx] = None
        return results
    except RuntimeError as e:
        # Entering `with ThreadPoolExecutor(...)` can itself raise on
        # newer Python versions when the global flag is set; catch here too.
        if _looks_like_global_shutdown(e):
            log.warning(
                "ThreadPool ctor raised global-shutdown (%s); falling back to serial",
                e,
            )
            return _serial_map(work_fn, items_list, timeout_per_item)
        raise


def _serial_map(
    work_fn: Callable[[Any], Any],
    items: List[Any],
    timeout_per_item: Optional[float] = None,
) -> List[Any]:
    """Sequential fallback used when ThreadPoolExecutor is unavailable.
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
