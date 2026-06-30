"""AJTA — market-screener universe (the candidate population).

Replaces the allowlist/portfolio-seeded scan with a funnel over the FULL
investable population:

    full equity universe (≈10k SEC tickers) + top crypto
      → rotating slice batch-quoted each cycle (quoting all 10k/cycle is
        infeasible at ~0.45s/name, so we sweep the population over successive
        cycles via a persisted cursor)
      → cheap liquidity/price/momentum screen + rank
      → top `screen_max` shortlist  (+ the top-crypto set, always included)

The agent then deep-analyzes only the shortlist. No allowlist; portfolio is NOT
a seed. Fail-closed: any error / empty screen → [] → the cycle proposes nothing.

This module is READ-ONLY market data; it never trades, sizes, or gates.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Tuple

import aj_config

log = logging.getLogger("augur.aj_universe")

_CURSOR_KEY = "__aj_screen_cursor"   # control-plane setting (rotation offset)


# ── population sources ────────────────────────────────────────────────────────

def _equity_population(cfg: Dict[str, Any]) -> List[str]:
    """The equity name pool. Full SEC universe by default; falls back to the
    curated liquid universe if the full load fails or is disabled."""
    syms: List[str] = []
    try:
        import idea_generator as ig
        if cfg.get("screen_full_equities", True):
            try:
                syms = list(ig.get_full_equity_universe() or [])
            except Exception:
                log.debug("full equity universe load failed; using curated", exc_info=True)
        if not syms:
            syms = list(getattr(ig, "EQUITY_UNIVERSE", []) or [])
    except Exception:
        log.debug("idea_generator unavailable", exc_info=True)
    return [str(s).upper() for s in syms if s]


def _crypto_population(cfg: Dict[str, Any]) -> List[str]:
    """Top crypto by market cap (inherently liquid → bypasses the screen).
    Symbols carry the -USD suffix so infer_asset_type/quote conventions match."""
    if not cfg.get("include_crypto", True):
        return []
    out: List[str] = []
    top = int(cfg.get("crypto_universe_top", 60) or 60)
    try:
        import idea_generator as ig
        full = ig.get_full_crypto_universe(top) or []
        out = [(c.get("symbol") if isinstance(c, dict) else c) for c in full if (
            c.get("symbol") if isinstance(c, dict) else c)]
    except Exception:
        log.debug("crypto universe fetch failed; using last-good/curated", exc_info=True)
    norm = []
    for s in out:
        if not s:
            continue
        s = str(s).upper()
        norm.append(s if s.endswith("-USD") else s + "-USD")
    norm = norm[:top]
    # Robustness: persist the last GOOD list; on a fetch failure (e.g. CoinGecko
    # 429) reuse it instead of collapsing to the tiny curated fallback.
    try:
        import aj_db
        import json as _json
        if len(norm) >= 10:
            aj_db.set_setting_raw("__aj_crypto_lastgood", _json.dumps(norm))
        else:
            raw = aj_db.get_setting_raw("__aj_crypto_lastgood")
            cached = _json.loads(raw) if raw else []
            if len(cached) > len(norm):
                norm = cached[:top]
    except Exception:
        pass
    if not norm:
        try:
            import idea_generator as ig
            norm = [(s if str(s).upper().endswith("-USD") else str(s).upper() + "-USD")
                    for s in (getattr(ig, "CRYPTO_UNIVERSE", []) or [])][:top]
        except Exception:
            pass
    return norm


# ── rotation (sweep the full population across cycles) ─────────────────────────

def _rotate(pop: List[str], batch: int) -> List[str]:
    """Return the next `batch` names, advancing a persisted wrap-around cursor."""
    n = len(pop)
    if n == 0:
        return []
    if n <= batch:
        return pop
    import aj_db
    try:
        raw = aj_db.get_setting_raw(_CURSOR_KEY)
        off = int(raw) % n if raw else 0
    except Exception:
        off = 0
    sl = (pop + pop)[off:off + batch]
    try:
        aj_db.set_setting_raw(_CURSOR_KEY, str((off + batch) % n))
    except Exception:
        log.debug("could not advance screen cursor", exc_info=True)
    return sl


# ── screen ────────────────────────────────────────────────────────────────────

def _num(v: Any) -> float:
    try:
        f = float(v)
        return f if f == f else 0.0          # NaN → 0
    except (TypeError, ValueError):
        return 0.0


def _cache_upsert(rows: Dict[str, Any]) -> None:
    """Store this cycle's slice quotes so future cycles can rank across them."""
    try:
        import aj_db
        import database as db
        ts = aj_db.utc_now_iso()
        with db._write_lock:
            conn = db.get_conn()
            for sym, q in rows.items():
                if not isinstance(q, dict):
                    continue
                conn.execute(
                    "INSERT OR REPLACE INTO aj_screen_cache "
                    "(symbol, ts, price, volume, market_cap, change_pct) VALUES (?,?,?,?,?,?)",
                    (sym, ts, _num(q.get("price")), _num(q.get("volume")),
                     _num(q.get("market_cap")), _num(q.get("change_pct"))))
            conn.commit()
    except Exception:
        log.debug("screen cache upsert failed", exc_info=True)


def _cache_fresh(ttl_min: int) -> List[Dict[str, Any]]:
    """All cache rows seen within ttl_min (the global-best view), pruning older."""
    try:
        import aj_db
        import database as db
        from datetime import timedelta
        cutoff = (aj_db.utc_now() - timedelta(minutes=max(1, ttl_min))).replace(
            microsecond=0).isoformat()
        with db._write_lock:
            conn = db.get_conn()
            conn.execute("DELETE FROM aj_screen_cache WHERE ts < ?", (cutoff,))
            conn.commit()
            rows = conn.execute(
                "SELECT symbol, price, volume, market_cap, change_pct "
                "FROM aj_screen_cache WHERE ts >= ?", (cutoff,)).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        log.debug("screen cache read failed", exc_info=True)
        return []


def screen(cfg: Dict[str, Any] = None) -> List[str]:
    """Return the screened candidate shortlist for this cycle. Never raises.

    Quotes this cycle's rotating slice, merges it into a TTL cache, then ranks
    across EVERY recently-seen name (not just the slice) so a strong setup seen a
    few cycles ago is still selectable. Crypto (inherently liquid) is always
    included so crypto trading is guaranteed."""
    try:
        cfg = cfg or aj_config.get_config()
        batch = max(1, int(cfg.get("screen_scan_batch", 400) or 400))
        smax = max(1, int(cfg.get("screen_max", 150) or 150))
        ttl = int(cfg.get("screen_cache_ttl_min", 45) or 45)
        min_price = _num(cfg.get("screen_min_price", 1.0))
        min_dvol = _num(cfg.get("screen_min_dollar_volume", 1_000_000))
        min_mcap = _num(cfg.get("screen_min_market_cap", 0))

        equities = _rotate(_equity_population(cfg), batch)
        cryptos = _crypto_population(cfg)
        if not equities and not cryptos:
            return []

        # quote this cycle's slice, fold into the global cache
        quotes: Dict[str, Any] = {}
        try:
            import fetcher
            quotes = fetcher.get_quotes_batch(equities + cryptos) or {}
        except Exception:
            log.exception("screen: batch quote failed")
        if quotes:
            _cache_upsert({s: quotes[s] for s in equities if s in quotes})

        # rank across the whole fresh cache (global-best), else this slice only
        pool = _cache_fresh(ttl)
        if not pool:
            pool = [dict(symbol=s, price=_num((quotes.get(s) or {}).get("price")),
                         volume=_num((quotes.get(s) or {}).get("volume")),
                         market_cap=_num((quotes.get(s) or {}).get("market_cap")),
                         change_pct=_num((quotes.get(s) or {}).get("change_pct")))
                    for s in equities if s in quotes]

        scored = []
        for q in pool:
            sym = q.get("symbol")
            if not sym or sym.endswith("-USD"):
                continue
            px = _num(q.get("price"))
            if px < min_price:
                continue
            dvol = px * _num(q.get("volume"))
            if dvol < min_dvol:
                continue
            mcap = _num(q.get("market_cap"))
            # When a min-market-cap floor is set, an UNKNOWN cap (mcap<=0, since
            # _num coerces missing/NaN to 0) must FAIL the screen rather than
            # bypass it — otherwise every name with no cap data slips through the
            # large-cap filter.
            if min_mcap > 0 and mcap < min_mcap:
                continue
            momentum = abs(_num(q.get("change_pct")))
            scored.append((sym, momentum, dvol))
        scored.sort(key=lambda x: (-x[1], -x[2]))
        out = [s for s, _, _ in scored[:smax]]
        out.extend(c for c in cryptos if c not in out)
        return out
    except Exception:
        log.exception("screen failed; returning empty (fail-closed)")
        return []


def population_size(cfg: Dict[str, Any] = None) -> Dict[str, int]:
    """Diagnostics for the UI/CLI: how big the swept population is."""
    cfg = cfg or aj_config.get_config()
    return {"equities": len(_equity_population(cfg)),
            "crypto": len(_crypto_population(cfg)),
            "scan_batch": int(cfg.get("screen_scan_batch", 400) or 400),
            "screen_max": int(cfg.get("screen_max", 150) or 150)}
