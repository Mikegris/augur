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
    try:
        import idea_generator as ig
        top = int(cfg.get("crypto_universe_top", 60) or 60)
        try:
            full = ig.get_full_crypto_universe(top) or []
            out = [(c.get("symbol") if isinstance(c, dict) else c) for c in full]
        except Exception:
            out = list(getattr(ig, "CRYPTO_UNIVERSE", []) or [])
    except Exception:
        log.debug("crypto universe unavailable", exc_info=True)
    norm = []
    for s in out:
        if not s:
            continue
        s = str(s).upper()
        norm.append(s if s.endswith("-USD") else s + "-USD")
    return norm[:int(cfg.get("crypto_universe_top", 60) or 60)]


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


def screen(cfg: Dict[str, Any] = None) -> List[str]:
    """Return the screened candidate shortlist for this cycle. Never raises."""
    try:
        cfg = cfg or aj_config.get_config()
        batch = max(1, int(cfg.get("screen_scan_batch", 400) or 400))
        smax = max(1, int(cfg.get("screen_max", 150) or 150))
        min_price = _num(cfg.get("screen_min_price", 1.0))
        min_dvol = _num(cfg.get("screen_min_dollar_volume", 1_000_000))
        min_mcap = _num(cfg.get("screen_min_market_cap", 0))

        equities = _rotate(_equity_population(cfg), batch)
        cryptos = _crypto_population(cfg)
        if not equities and not cryptos:
            return []

        # cheap batch quote of this cycle's slice (+ crypto, for momentum rank)
        quotes: Dict[str, Any] = {}
        try:
            import fetcher
            quotes = fetcher.get_quotes_batch(equities + cryptos) or {}
        except Exception:
            log.exception("screen: batch quote failed")

        scored = []
        for sym in equities:
            q = quotes.get(sym) or quotes.get(sym.upper())
            if not isinstance(q, dict):
                continue
            px = _num(q.get("price"))
            if px < min_price:
                continue
            dvol = px * _num(q.get("volume"))
            if dvol < min_dvol:
                continue
            mcap = _num(q.get("market_cap"))
            if min_mcap > 0 and 0 < mcap < min_mcap:
                continue
            momentum = abs(_num(q.get("change_pct")))
            scored.append((sym, momentum, dvol))
        scored.sort(key=lambda x: (-x[1], -x[2]))
        out = [s for s, _, _ in scored[:smax]]

        # crypto is the inherently-liquid top set → always include (capped),
        # so crypto trading is guaranteed even when a slice has thin equity flow.
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
