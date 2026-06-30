"""
crypto_exchanges.py — Cross-exchange crypto data via ccxt public endpoints.
No API keys required for read-only ticker/orderbook data.
"""

import logging
import threading
import time

log = logging.getLogger("augur.ccxt")

# Use a small, reliable set of major spot exchanges
DEFAULT_EXCHANGES = ["binance", "coinbase", "kraken", "bitstamp", "bitfinex"]

_cache = {}
_lock = threading.Lock()
_exchange_objs = {}
# Per-exchange construction locks so two threads can't BOTH build (and then
# both fetch_ticker on) a fresh ccxt instance for the same exchange. Building
# under a per-name lock — rather than the global _lock — means a slow ccxt
# constructor for one exchange doesn't serialize construction of the others.
_construct_locks = {}
_construct_locks_guard = threading.Lock()


def _name_lock(name):
    with _construct_locks_guard:
        lk = _construct_locks.get(name)
        if lk is None:
            lk = threading.Lock()
            _construct_locks[name] = lk
        return lk


def _get_exchange(name):
    # The previous version checked _exchange_objs under _lock, then RELEASED
    # the lock to construct, then re-acquired to store. In that gap N threads
    # all saw a miss, all constructed their own ccxt instance, and all issued
    # fetch_ticker on an UNREGISTERED instance before setdefault picked one
    # winner — defeating ccxt's per-instance enableRateLimit token bucket and
    # multiplying upstream request rate by N. Hold a per-name construction
    # lock across the whole check→construct→store so only ONE instance is ever
    # built (and used) per exchange.
    with _lock:
        ex = _exchange_objs.get(name)
    if ex is not None:
        return ex
    with _name_lock(name):
        # Re-check inside the construction lock — a racing thread may have
        # finished building while we waited.
        with _lock:
            ex = _exchange_objs.get(name)
        if ex is not None:
            return ex
        try:
            import ccxt
            cls = getattr(ccxt, name, None)
            if cls is None:
                return None
            ex = cls({"enableRateLimit": True, "timeout": 8000})
        except Exception as e:
            log.warning("ccxt init %s: %s", name, e)
            return None
        with _lock:
            _exchange_objs[name] = ex
        return ex


def _cached(key, ttl, builder):
    with _lock:
        v = _cache.get(key)
        if v and v[1] > time.time():
            return v[0]
    try:
        out = builder()
    except Exception as e:
        log.warning("ccxt %s: %s", key, e)
        return None
    with _lock:
        # Re-check: another caller may have finished the same builder while
        # we were running. Prefer their result so we don't churn the cache
        # entry (and so the TTL aligns with the first writer).
        v = _cache.get(key)
        if v and v[1] > time.time():
            return v[0]
        _cache[key] = (out, time.time() + ttl)
    return out


def cross_exchange_prices(symbol="BTC/USDT", exchanges=DEFAULT_EXCHANGES):
    """Return {exchange: {bid, ask, last, mid, volume}} for a pair."""
    def _one_exchange(ex_name):
        """Fetch one exchange's ticker. Returns (ex_name, dict) or None.
        Each exchange is an independent ~8s network round-trip, so fanning
        them out turns a serial 5×8s worst case into a single ~8s wall time."""
        ex = _get_exchange(ex_name)
        if not ex:
            return None
        sym = symbol
        # USDT pairs work on most; adjust for coinbase/kraken/bitstamp (USD)
        if ex_name in ("coinbase", "kraken", "bitstamp") and sym.endswith("/USDT"):
            sym = sym.replace("/USDT", "/USD")
        try:
            t = ex.fetch_ticker(sym)
        except Exception as e:
            log.debug("ticker %s/%s: %s", ex_name, sym, e)
            return None
        bid = t.get("bid")
        ask = t.get("ask")
        last = t.get("last") or t.get("close")
        vol = t.get("baseVolume")
        if last is None and bid and ask:
            last = (bid + ask) / 2
        mid = (bid + ask) / 2 if (bid and ask) else last
        return (ex_name, {
            "symbol": sym,
            "bid": bid, "ask": ask, "last": last,
            "mid": mid, "volume": vol,
        })

    def _build():
        out = {}
        try:
            import safe_executor
            results = safe_executor.parallel_map(
                _one_exchange, list(exchanges),
                max_workers=min(5, len(exchanges)) or 1,
                thread_name_prefix="ccxt-xex",
            )
        except Exception:
            # Fail-open to serial if the executor is unavailable.
            results = [_one_exchange(n) for n in exchanges]
        for res in results:
            if res:
                out[res[0]] = res[1]
        # spread metrics
        prices = [d["mid"] for d in out.values() if d.get("mid")]
        spread = (max(prices) - min(prices)) if len(prices) >= 2 else 0
        ref = sum(prices) / len(prices) if prices else 0
        return {
            "pair": symbol,
            "exchanges": out,
            "max_spread": spread,
            "max_spread_bps": (spread / ref * 10000) if ref else None,
        }
    return _cached(("xex", symbol, tuple(exchanges)), 60, _build)
