"""AJTA — trade-control configuration (AJTA-SPEC-1.0 §11.1, Appendix D).

The risk/trade switches and caps. EVERY default is the safest, blocking value
(fail-closed): trading off, live off, robinhood off, empty allowlist, zero
caps. A config that has never been touched can place NO order, paper or live.

Authoritative store is AUGUR `settings` (string KV), keyed `aj_*`; this module
owns typing/validation. A `risk.yaml` template ships in the repo as docs, but
settings is the source of truth (spec: "risk.yaml, persisted to settings").
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List

import database as db

log = logging.getLogger("augur.aj_config")

# ── safe defaults (§11.1) ─────────────────────────────────────────────────────
DEFAULTS: Dict[str, Any] = {
    # master + venue switches — ALL false
    "trading_enabled":        False,
    "live_trading_enabled":   False,
    "robinhood_enabled":      False,
    # caps — ALL zero = block (must be set > 0 to trade)
    "symbol_allowlist":       [],     # EMPTY = nothing tradable
    "allow_any_symbol":       False,  # OPT-IN open universe: trade off-allowlist
    "max_order_notional_usd": 0.0,
    "max_trades_per_day":     0,
    "max_daily_loss_usd":     0.0,
    # semantics
    "daily_loss_basis":       "realized_plus_unrealized",  # or "realized"
    "halt_rearm":             "manual",                    # or "session_open"
    "session_whitelist":      ["regular"],
    # execution defaults (paper-first)
    "default_broker":         "paper",
    "auto_approve_paper":     True,    # paper MAY auto-approve; live NEVER does
    # paper fill model (§12.5)
    "paper_slippage_bps":     2.0,
    "paper_spread_fraction":  0.5,
    "fee_bps":                0.0,     # commission-free equities default
    "min_fee_usd":            0.0,
    "crypto_fee_bps":         10.0,    # exchange-typical for crypto venues
    # operator decision tunables (§19 scan→judge→size)
    "forecast_horizon_days":  20,
    "buy_prob_threshold":     0.55,    # prob_up at/above => buy candidate
    "sell_prob_threshold":    0.45,    # prob_up at/below => sell held name
    "min_edge_pct_pts":       3.0,     # |edge| floor; below => no trade
    "order_notional_target_usd": 0.0,  # 0 => half of max_order_notional_usd
    "use_llm_synthesis":      False,   # off => deterministic rule-based thesis
    "scan_universe_max":      25,      # cap symbols/cycle in open-universe mode
}

_BOOL_KEYS = {"trading_enabled", "live_trading_enabled", "robinhood_enabled",
              "auto_approve_paper", "use_llm_synthesis", "allow_any_symbol"}
_LIST_KEYS = {"symbol_allowlist", "session_whitelist"}
_FLOAT_KEYS = {"max_order_notional_usd", "max_daily_loss_usd",
               "paper_slippage_bps", "paper_spread_fraction", "fee_bps",
               "min_fee_usd", "crypto_fee_bps", "buy_prob_threshold",
               "sell_prob_threshold", "min_edge_pct_pts",
               "order_notional_target_usd"}
_INT_KEYS = {"max_trades_per_day", "forecast_horizon_days", "scan_universe_max"}
_STR_KEYS = {"daily_loss_basis", "halt_rearm", "default_broker"}

_PREFIX = "aj_"
_VALID_LOSS_BASIS = ("realized_plus_unrealized", "realized")
_VALID_REARM = ("manual", "session_open")
_VALID_SESSIONS = ("premarket", "regular", "afterhours", "closed")


def _coerce_bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def _coerce_list(v: Any) -> List[str]:
    if isinstance(v, list):
        items = v
    else:
        s = str(v or "").strip()
        if not s:
            return []
        try:
            parsed = json.loads(s)
            items = parsed if isinstance(parsed, list) else [parsed]
        except Exception:
            items = [x for x in s.replace(",", " ").split() if x]
    out: List[str] = []
    for it in items:
        t = str(it).strip().upper()
        if t and t not in out:
            out.append(t)
    return out


def _coerce_float(v: Any, default: float = 0.0) -> float:
    try:
        f = float(str(v).replace(",", "").replace("$", "").strip())
        return f if f == f and f not in (float("inf"), float("-inf")) else default
    except Exception:
        return default


def get_config() -> Dict[str, Any]:
    """Typed config = DEFAULTS overlaid with any stored `aj_*` settings."""
    raw = {}
    try:
        raw = db.get_settings()
    except Exception:
        log.debug("get_config: settings unavailable, using defaults")
    cfg = dict(DEFAULTS)
    for key in DEFAULTS:
        sk = _PREFIX + key
        if sk not in raw:
            continue
        val = raw[sk]
        if key in _BOOL_KEYS:
            cfg[key] = _coerce_bool(val)
        elif key in _LIST_KEYS:
            cfg[key] = _coerce_list(val)
        elif key in _FLOAT_KEYS:
            cfg[key] = _coerce_float(val, DEFAULTS[key])
        elif key in _INT_KEYS:
            cfg[key] = int(_coerce_float(val, DEFAULTS[key]))
        else:
            cfg[key] = str(val)
    # sanitize enums (an out-of-band stored value must not weaken the gate)
    if cfg["daily_loss_basis"] not in _VALID_LOSS_BASIS:
        cfg["daily_loss_basis"] = "realized_plus_unrealized"
    if cfg["halt_rearm"] not in _VALID_REARM:
        cfg["halt_rearm"] = "manual"
    cfg["session_whitelist"] = [s.lower() for s in cfg["session_whitelist"]
                                if s.lower() in _VALID_SESSIONS] or ["regular"]
    return cfg


def _serialize(key: str, value: Any) -> str:
    if key in _BOOL_KEYS:
        return "true" if _coerce_bool(value) else "false"
    if key in _LIST_KEYS:
        return json.dumps(_coerce_list(value))
    if key in _FLOAT_KEYS:
        return str(_coerce_float(value, DEFAULTS[key]))
    if key in _INT_KEYS:
        return str(int(_coerce_float(value, DEFAULTS[key])))
    return str(value)


def set_config(partial: Dict[str, Any]) -> Dict[str, Any]:
    """Persist a partial config update. Unknown keys are ignored. Returns the
    full typed config after the update. Validation rejects nonsensical values
    by clamping to the safe default rather than storing them."""
    if not isinstance(partial, dict):
        raise ValueError("config update must be a dict")
    for key, value in partial.items():
        if key not in DEFAULTS:
            log.warning("set_config: ignoring unknown key %r", key)
            continue
        db.set_setting(_PREFIX + key, _serialize(key, value))
    return get_config()


def get(key: str) -> Any:
    return get_config().get(key)


def is_trade_path_enabled() -> bool:
    """True only when the master switch is on. Convenience for callers that
    want to short-circuit before building a proposal."""
    return bool(get_config().get("trading_enabled"))
