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


def _get_exchange(name):
    # Must be locked: cross_exchange_prices() runs builder() under no lock
    # (intentional, so a slow ccxt.fetch_ticker doesn't block other pairs),
    # but multiple Flask request threads racing to populate _exchange_objs
    # would otherwise construct duplicate ccxt instances. Each instance has
    # its own enableRateLimit token bucket, so duplicates defeat ccxt's
    # built-in throttle and we end up making 2x-Nx the upstream requests
    # the exchange's rate limit allows.
    with _lock:
        if name in _exchange_objs:
            return _exchange_objs[name]
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
        # Another thread may have raced us — return the winner so we share
        # a single per-exchange rate-limit budget.
        return _exchange_objs.setdefault(name, ex)


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
    def _build():
        out = {}
        # USDT pairs work on most; adjust for coinbase/kraken (USD)
        for ex_name in exchanges:
            ex = _get_exchange(ex_name)
            if not ex:
                continue
            sym = symbol
            if ex_name in ("coinbase", "kraken", "bitstamp") and sym.endswith("/USDT"):
                sym = sym.replace("/USDT", "/USD")
            try:
                t = ex.fetch_ticker(sym)
                bid = t.get("bid")
                ask = t.get("ask")
                last = t.get("last") or t.get("close")
                vol = t.get("baseVolume")
                if last is None and bid and ask:
                    last = (bid + ask) / 2
                mid = (bid + ask) / 2 if (bid and ask) else last
                out[ex_name] = {
                    "symbol": sym,
                    "bid": bid, "ask": ask, "last": last,
                    "mid": mid, "volume": vol,
                }
            except Exception as e:
                log.debug("ticker %s/%s: %s", ex_name, sym, e)
                continue
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
