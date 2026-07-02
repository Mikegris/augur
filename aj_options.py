"""AJTA — options engine (single-leg, long-only, paper).

Turns a bullish equity signal into a long CALL on that underlying (the agent
never writes options or shorts). Picks a near-dated, near-the-money contract,
prices it from the live chain, and marks held option positions.

Design:
  * Option symbol is a compact, parseable string  "OPT:{UND}:{YYYYMMDD}:{C|P}:{strike}"
    e.g. "OPT:AAPL:20260717:C:150.0". is_option() detects it; the risk gate
    skips its equity-symbol regex for these (proposals carry instrument_type).
  * PRICES ARE PER-CONTRACT (premium-per-share × 100). Storing per-contract keeps
    the asset-agnostic FIFO book + notional + P&L math correct with NO multiplier
    awareness elsewhere: notional = qty(contracts) × premium_contract;
    P&L = (mark_contract − avg_contract) × contracts.
  * Data sources (chain / expirations / spot) are injectable for tests; the
    defaults use yfinance (same path gex_engine uses). Everything is fail-open:
    any error → None → the cycle simply skips the option.

Paper-only here; live options would route through a gated broker like equities.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

log = logging.getLogger("augur.aj_options")

DEFAULT_MULTIPLIER = 100
_PREFIX = "OPT:"


# ── symbol format ─────────────────────────────────────────────────────────────

def is_option(symbol: Any) -> bool:
    return isinstance(symbol, str) and symbol.startswith(_PREFIX)


def format_symbol(underlying: str, expiry: str, opt_type: str, strike: float) -> str:
    ymd = str(expiry).replace("-", "")[:8]
    t = "C" if str(opt_type).lower().startswith("c") else "P"
    return "{}{}:{}:{}:{}".format(_PREFIX, str(underlying).upper(), ymd, t, float(strike))


def parse_symbol(symbol: str) -> Optional[Dict[str, Any]]:
    """OPT:AAPL:20260717:C:150.0 -> {underlying, expiry(YYYY-MM-DD), option_type, strike}."""
    if not is_option(symbol):
        return None
    try:
        _, und, ymd, t, strike = symbol.split(":", 4)
        expiry = "{}-{}-{}".format(ymd[0:4], ymd[4:6], ymd[6:8])
        return {"underlying": und.upper(), "expiry": expiry,
                "option_type": "call" if t.upper() == "C" else "put",
                "strike": float(strike)}
    except Exception:
        return None


# ── default (network) data sources — injectable ───────────────────────────────

def _default_spot(underlying: str) -> Optional[float]:
    try:
        import fetcher
        q = fetcher.get_quote(underlying) or {}
        px = q.get("price")
        return float(px) if isinstance(px, (int, float)) and px > 0 else None
    except Exception:
        return None


def _default_expirations(underlying: str) -> List[str]:
    try:
        import yfinance as yf
        return list(yf.Ticker(underlying).options or [])
    except Exception:
        log.debug("expirations fetch failed for %s", underlying, exc_info=True)
        return []


def _default_chain(underlying: str, expiry: str) -> Dict[str, List[Dict[str, Any]]]:
    """Return {'calls': [...], 'puts': [...]} of dict rows for one expiry."""
    try:
        import yfinance as yf
        oc = yf.Ticker(underlying).option_chain(expiry)
        return {"calls": oc.calls.to_dict("records"),
                "puts": oc.puts.to_dict("records")}
    except Exception:
        log.debug("chain fetch failed for %s %s", underlying, expiry, exc_info=True)
        return {"calls": [], "puts": []}


# ── pricing helpers ───────────────────────────────────────────────────────────

def _row_premium_contract(row: Dict[str, Any], mult: int) -> Optional[float]:
    """Per-contract premium = mid(bid,ask) (else lastPrice) × multiplier."""
    def f(k):
        v = row.get(k)
        try:
            v = float(v)
            return v if v == v and v > 0 else None
        except (TypeError, ValueError):
            return None
    bid, ask, last = f("bid"), f("ask"), f("lastPrice")
    per_share = (bid + ask) / 2.0 if (bid and ask) else (last or bid or ask)
    if not per_share or per_share <= 0:
        return None
    return round(per_share * mult, 2)


def _days_to(expiry: str) -> Optional[int]:
    """Whole calendar days to expiry BY DATE (ET, where listed options expire).
    The old datetime subtraction measured from midnight of the expiry date, so
    dte was -1 for the entire expiry day and a still-tradeable contract was
    treated as already expired (marked 0.0 and force-closed at a fake loss).
    dte == 0 means 'expires today' — alive."""
    try:
        d = datetime.strptime(expiry[:10], "%Y-%m-%d").date()
        try:
            from zoneinfo import ZoneInfo
            today = datetime.now(timezone.utc).astimezone(
                ZoneInfo("America/New_York")).date()
        except Exception:
            today = datetime.now(timezone.utc).date()
        return (d - today).days
    except Exception:
        return None


# ── contract selection ────────────────────────────────────────────────────────

def _liquid_enough(row: Dict[str, Any], min_oi: int, max_spread_pct: float) -> bool:
    """Reject illiquid contracts: low open interest, or a bid-ask spread wider
    than max_spread_pct of the mid (you can't realistically trade those)."""
    try:
        oi = int(row.get("openInterest") or 0)
        vol = int(row.get("volume") or 0)
    except (TypeError, ValueError):
        oi, vol = 0, 0
    if min_oi > 0 and oi < min_oi and vol < min_oi:
        return False
    try:
        bid, ask = float(row.get("bid") or 0), float(row.get("ask") or 0)
    except (TypeError, ValueError):
        return True   # no bid/ask to judge — don't reject on spread alone
    if bid > 0 and ask > 0:
        mid = (bid + ask) / 2.0
        if mid > 0 and (ask - bid) / mid > max_spread_pct:
            return False
    return True


def pick_contract(underlying: str, side: str, cfg: Dict[str, Any], *,
                  direction: str = "bullish",
                  spot: Optional[float] = None,
                  expirations: Optional[List[str]] = None,
                  chain_fn: Optional[Callable[[str, str], Dict]] = None) -> Optional[Dict[str, Any]]:
    """Pick a near-dated, near-the-money LONG option for a directional signal:
    direction 'bullish' -> long CALL, 'bearish' -> long PUT. side must be 'buy'
    (we're always BUYING the option / long premium — never writing). A 'sell'
    returns None (exits close the held position, not via pick)."""
    if str(side).lower() != "buy":
        return None
    underlying = str(underlying or "").upper()
    if not underlying:
        return None
    opt_type = "put" if str(direction).lower() == "bearish" else "call"
    mult = int(cfg.get("option_contract_multiplier", DEFAULT_MULTIPLIER) or DEFAULT_MULTIPLIER)
    target_dte = int(cfg.get("option_target_dte", 35) or 35)
    moneyness = float(cfg.get("option_moneyness", 0.0) or 0.0)
    min_oi = int(cfg.get("option_min_open_interest", 50) or 0)
    max_spread = float(cfg.get("option_max_spread_pct", 0.30) or 1.0)

    if spot is None:
        spot = _default_spot(underlying)
    if not spot or spot <= 0:
        return None
    exps = expirations if expirations is not None else _default_expirations(underlying)
    if not exps:
        return None
    dated = sorted([(e, _days_to(e)) for e in exps if _days_to(e) is not None],
                   key=lambda x: x[1])
    if not dated:
        return None
    expiry = next((e for e, d in dated if d >= target_dte), dated[-1][0])

    chain = (chain_fn or _default_chain)(underlying, expiry)
    rows = (chain or {}).get("calls" if opt_type == "call" else "puts") or []
    if not rows:
        return None
    # ATM by default; a call OTM is ABOVE spot, a put OTM is BELOW spot.
    sign = 1.0 if opt_type == "call" else -1.0
    target_strike = spot * (1.0 + sign * abs(moneyness))
    best = None
    fallback = None   # nearest contract ignoring liquidity, if none pass the filter
    for row in rows:
        try:
            strike = float(row.get("strike"))
        except (TypeError, ValueError):
            continue
        prem = _row_premium_contract(row, mult)
        if prem is None:
            continue
        dist = abs(strike - target_strike)
        if fallback is None or dist < fallback[0]:
            fallback = (dist, strike, prem)
        if not _liquid_enough(row, min_oi, max_spread):
            continue
        if best is None or dist < best[0]:
            best = (dist, strike, prem)
    chosen = best or fallback
    if chosen is None:
        return None
    _, strike, prem = chosen
    return {
        "symbol": format_symbol(underlying, expiry, opt_type, strike),
        "underlying": underlying, "option_type": opt_type, "strike": strike,
        "expiry": expiry, "premium_contract": prem, "contract_multiplier": mult,
        "spot": spot, "instrument_type": "option",
    }


# ── marking held options ──────────────────────────────────────────────────────

def _config_multiplier() -> int:
    """The configured option contract multiplier (option_contract_multiplier),
    falling back to DEFAULT_MULTIPLIER. Fail-open: any error → DEFAULT."""
    try:
        import aj_config
        cfg = aj_config.get_config() or {}
        return int(cfg.get("option_contract_multiplier", DEFAULT_MULTIPLIER)
                   or DEFAULT_MULTIPLIER)
    except Exception:
        return DEFAULT_MULTIPLIER


def mark(option_symbol: str, *,
         multiplier: Optional[int] = None,
         chain_fn: Optional[Callable[[str, str], Dict]] = None) -> Optional[float]:
    """Per-contract mark for a held option position. None if unpriceable
    (expired / no chain / no quote) — callers treat None as 'no mark'.

    The per-contract premium = per-share mid × multiplier. The multiplier MUST
    match the one pick_contract stored at entry (option_contract_multiplier),
    otherwise P&L is corrupted for any non-100 contract. Pass the held position's
    stored `contract_multiplier` when available; otherwise we read the configured
    option_contract_multiplier (NOT the hardcoded 100)."""
    meta = parse_symbol(option_symbol)
    if not meta:
        return None
    dte = _days_to(meta["expiry"])
    if dte is not None and dte < 0:
        return 0.0   # expired → worthless for marking (paper)
    chain = (chain_fn or _default_chain)(meta["underlying"], meta["expiry"])
    rows = (chain or {}).get("calls" if meta["option_type"] == "call" else "puts") or []
    mult = int(multiplier) if multiplier else _config_multiplier()
    for row in rows:
        try:
            if abs(float(row.get("strike")) - meta["strike"]) < 1e-6:
                return _row_premium_contract(row, mult)
        except (TypeError, ValueError):
            continue
    return None
