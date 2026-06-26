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
from typing import Any, Dict, List, Optional

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
    # ── 25 enhancements (all opt-in; 0/false = disabled) ─────────────────────
    # sizing & entry
    "conviction_sizing":      False,   # scale order size by edge strength
    "min_order_notional_usd": 0.0,     # skip dust orders below this
    "entry_order_type":       "market",  # or "limit"
    "entry_limit_offset_bps": 0.0,     # limit placed this far through the quote
    "order_ttl_cycles":       0,       # expire resting limit orders after N cycles
    # exit rules
    "take_profit_pct":        0.0,     # auto-sell a paper long up this %
    "stop_loss_pct":          0.0,     # auto-sell a paper long down this %
    "trailing_stop_pct":      0.0,     # exit on drawdown from peak mark
    "exit_cooldown_min":      0,       # no re-entry of a symbol for N min after exit
    # extra gates
    "max_open_positions":     0,       # cap simultaneous open paper positions
    "max_symbol_weight_pct":  0.0,     # cap any one name's % of the paper book
    "max_trades_per_symbol_per_day": 0,
    "trade_skip_open_min":    0,       # skip first N min of the regular session
    "trade_skip_close_min":   0,       # skip last N min of the regular session
    "max_slippage_bps":       0.0,     # reject a paper fill exceeding this slippage
    "risk_off_vix":           0.0,     # skip NEW buys when VIX is above this
    "dry_run":                False,   # propose + gate but NEVER execute (preview)
    "notify_fills":           False,   # macOS notification on a fill
    # ── 100x layer (aj_alpha / aj_autonomy) — all opt-in, fail-closed ────────
    # sizing intelligence (1-5)
    "kelly_sizing":           False,   # 1: fractional-Kelly sizing from realized win-rate/edge
    "kelly_fraction":         0.5,     #    fraction of full Kelly (half-Kelly default)
    "volatility_target_pct":  0.0,     # 2: target per-position daily-vol %; 0=off
    "compound_sizing":        False,   # 3: scale order size with the agent's equity
    "compound_base_equity_usd": 10000.0,  #  equity baseline for compounding ratio
    "symbol_performance_weighting": False,  # 4: up/down-size by per-symbol realized record
    "drawdown_throttle_pct":  0.0,     # 5: equity-drawdown % at which size is fully throttled; 0=off
    # entry alpha (6-11)
    "momentum_filter_days":   0,       # 6: only buy if price > N-day SMA; 0=off
    "mean_reversion_rsi_max": 0.0,     # 7: only buy if RSI(14) <= this; 0=off
    "relative_strength_filter": False, # 8: only buy names outperforming SPY over the lookback
    "relative_strength_lookback_days": 20,
    "max_book_correlation":   0.0,     # 9: block a buy whose avg corr to the book exceeds this; 0=off
    "earnings_blackout_days": 0,       # 10: skip buys within N days of earnings; 0=off
    "max_sector_weight_pct":  0.0,     # 11: cap any one GICS sector's % of book; 0=off
    # exit intelligence (12-15)
    "max_holding_days":       0,       # 12: force-exit a position older than N days; 0=off
    "profit_ratchet_pct":     0.0,     # 13: once up this %, lock in a floor gain; 0=off
    "profit_ratchet_lock_pct": 0.0,    #     floor gain % the ratchet protects
    "tp_ladder":              False,   # 14: scale out in thirds at take_profit_pct, 1.5x, 2x
    "atr_stop_mult":          0.0,     # 15: stop distance = mult x ATR(14); 0=off
    "atr_period":             14,
    # adaptive brain (16-20)
    "adaptive_thresholds":    False,   # 16: nudge buy/sell thresholds from recent realized hit-rate
    "regime_adaptive":        False,   # 17: switch threshold/size profile on bull/bear/chop regime
    "pyramiding":             False,   # 18: allow adding to a winning position
    "pyramid_max_adds":       2,       #     max additional adds per position
    "pyramid_min_gain_pct":   3.0,     #     position must be up this % to pyramid
    "signal_scorecard":       False,   # 19: track per-signal realized accuracy (analytics/adaptive)
    "opportunity_radar":      False,   # 20: rank the scan universe, trade only the top-K
    "opportunity_radar_top_k": 5,
    # autonomous operation (21-25)
    "auto_run_enabled":       False,   # 21: run cycles automatically during market hours
    "auto_run_interval_min":  30,      #     minutes between automatic cycles
    "health_autohalt":        False,   # 22: self-halt on fill-rate collapse / divergence / unknown orders
    "auto_preset_escalation": False,   # 23: auto conservative<->moderate<->aggressive on performance
    "daily_reflection":       False,   # 24: write an end-of-day self-review journal entry
    "premarket_briefing":     False,   # 25: build a pre-market ranked opportunity briefing
    # ── Analyst Council layer (merge plan) — ALL opt-in, fail-closed ─────────
    # Advisory multi-agent reasoning. The council can VETO/REDUCE a signal or
    # (in coequal mode) raise conviction within existing caps; it can NEVER
    # create an order the risk gate would block nor bypass any gate. Requires
    # both council_enabled AND the VERIFY-COUNCIL gate (aj_verify_council=pass).
    "council_enabled":        False,   # master switch for the council layer
    "council_policy":         "advisory",  # advisory | confirm | coequal
    "council_topk":           3,       # run council only for top-K scan candidates
    "max_research_rounds":    1,       # bull/bear debate rounds (hard-terminated)
    "max_risk_rounds":        1,       # aggressive/conservative/neutral rounds
    "council_max_calls_per_cycle": 40, # hard cap on LLM calls/cycle (cost guard)
    "council_cache_ttl_min":  360,     # memoize a decision per (symbol,date,hash)
    "council_deep_max_tokens": 1024,   # token budget for deep-think analysis
    "council_quick_max_tokens": 512,   # token budget for quick-think summarization
    "council_deep_model":     "",      # model for the deep tier ("" = provider default)
    "council_quick_model":    "",      # model for the quick tier ("" = provider default)
    "council_analyst_fundamentals": True,
    "council_analyst_news":         True,
    "council_analyst_sentiment":    True,
    "council_analyst_technical":    True,
    "personas_enabled":       False,   # Phase 6: investor-persona analysts (opt-in)
    "fingpt_sentiment_enabled": False, # Phase 6: FinGPT numeric sentiment prior (opt-in)
    # coequal-policy track-record gate (Phase 5): coequal may BOOST size only
    # after the council's realized alpha track record clears these bars.
    "coequal_min_samples":    20,      # min resolved reflections before unlock
    "coequal_min_alpha":      0.0,     # mean alpha (fraction) required to unlock
    "coequal_max_boost":      0.5,     # max extra size fraction (0.5 => up to 1.5x)
    # ── universe / market screener (aj_universe) ─────────────────────────────
    # How the cycle picks its candidate universe:
    #   'allowlist'     — only symbol_allowlist (fail-closed, fully controlled)
    #   'open'          — allowlist ∪ watchlist ∪ portfolio ∪ idea-pool (legacy)
    #   'market_screen' — sweep the FULL investable population (SEC equities +
    #                     top crypto), screened to a shortlist each cycle. DEFAULT.
    "universe_mode":          "market_screen",
    "screen_full_equities":   True,    # use the full ~10k SEC list (else curated)
    "include_crypto":         True,    # include crypto in the screened universe
    "crypto_universe_top":    60,      # top-N crypto by market cap to include
    "screen_scan_batch":      400,     # names batch-quoted per cycle (rotating sweep)
    "screen_max":             150,     # shortlist size handed to the forecaster
    "screen_min_price":       1.0,     # drop sub-$1 names
    "screen_min_dollar_volume": 1000000.0,  # min price*volume (liquidity floor)
    "screen_min_market_cap":  0.0,     # 0 = no market-cap floor
    # ── options trading (aj_options) — single-leg LONG calls, paper ───────────
    # When on, a BUY signal on an underlying becomes a long CALL (ATM, ~N DTE),
    # sized by premium, paper-filled at the chain mid. Live options gated like
    # live equities. Default OFF (fail-closed).
    "trade_options":          False,
    "option_target_dte":      35,      # target days-to-expiry for picked contracts
    "option_moneyness":       0.0,     # 0 = ATM; +0.05 = 5% OTM call
    "option_contract_multiplier": 100,
    "option_min_open_interest": 50,    # skip illiquid contracts below this OI
    "option_max_spread_pct":  0.30,    # skip contracts whose bid-ask > this × mid
    "option_fee_per_contract": 0.65,   # paper option commission per contract
    "option_trade_puts":      True,    # also buy long PUTS on bearish signals
    # screener global-best cache: rank across recently-seen names, not just the
    # current rotating slice (closes the "rolling sweep only sees 400/cycle" gap).
    "screen_cache_ttl_min":   45,      # how long a screened quote stays rank-eligible
}

_BOOL_KEYS = {"trading_enabled", "live_trading_enabled", "robinhood_enabled",
              "auto_approve_paper", "use_llm_synthesis", "allow_any_symbol",
              "conviction_sizing", "dry_run", "notify_fills",
              # 100x layer
              "kelly_sizing", "compound_sizing", "symbol_performance_weighting",
              "relative_strength_filter", "tp_ladder", "adaptive_thresholds",
              "regime_adaptive", "pyramiding", "signal_scorecard",
              "opportunity_radar", "auto_run_enabled", "health_autohalt",
              "auto_preset_escalation", "daily_reflection", "premarket_briefing",
              # council layer
              "council_enabled", "council_analyst_fundamentals",
              "council_analyst_news", "council_analyst_sentiment",
              "council_analyst_technical", "personas_enabled",
              "fingpt_sentiment_enabled",
              # universe screener
              "screen_full_equities", "include_crypto",
              # options
              "trade_options", "option_trade_puts"}
_LIST_KEYS = {"symbol_allowlist", "session_whitelist"}
_FLOAT_KEYS = {"max_order_notional_usd", "max_daily_loss_usd",
               "paper_slippage_bps", "paper_spread_fraction", "fee_bps",
               "min_fee_usd", "crypto_fee_bps", "buy_prob_threshold",
               "sell_prob_threshold", "min_edge_pct_pts",
               "order_notional_target_usd",
               "min_order_notional_usd", "entry_limit_offset_bps",
               "take_profit_pct", "stop_loss_pct", "trailing_stop_pct",
               "max_symbol_weight_pct", "max_slippage_bps", "risk_off_vix",
               # 100x layer
               "kelly_fraction", "volatility_target_pct",
               "compound_base_equity_usd", "drawdown_throttle_pct",
               "mean_reversion_rsi_max", "max_book_correlation",
               "max_sector_weight_pct", "profit_ratchet_pct",
               "profit_ratchet_lock_pct", "atr_stop_mult",
               "pyramid_min_gain_pct",
               # council coequal gate
               "coequal_min_alpha", "coequal_max_boost",
               # universe screener
               "screen_min_price", "screen_min_dollar_volume",
               "screen_min_market_cap", "option_moneyness",
               "option_max_spread_pct", "option_fee_per_contract"}
_INT_KEYS = {"max_trades_per_day", "forecast_horizon_days", "scan_universe_max",
             "order_ttl_cycles", "exit_cooldown_min", "max_open_positions",
             "max_trades_per_symbol_per_day", "trade_skip_open_min",
             "trade_skip_close_min",
             # 100x layer
             "momentum_filter_days", "relative_strength_lookback_days",
             "earnings_blackout_days", "max_holding_days", "atr_period",
             "pyramid_max_adds", "opportunity_radar_top_k",
             "auto_run_interval_min",
             # council layer
             "council_topk", "max_research_rounds", "max_risk_rounds",
             "council_max_calls_per_cycle", "council_cache_ttl_min",
             "council_deep_max_tokens", "council_quick_max_tokens",
             "coequal_min_samples",
             # universe screener
             "crypto_universe_top", "screen_scan_batch", "screen_max"}
_STR_KEYS = {"daily_loss_basis", "halt_rearm", "default_broker",
             "entry_order_type", "council_policy",
             "council_deep_model", "council_quick_model",
             "universe_mode"}

_VALID_COUNCIL_POLICY = ("advisory", "confirm", "coequal")
_VALID_UNIVERSE_MODE = ("allowlist", "open", "market_screen")

_PREFIX = "aj_"
_VALID_LOSS_BASIS = ("realized_plus_unrealized", "realized")
_VALID_REARM = ("manual", "session_open")
# 'closed' is a market STATE, never a tradable session — it must not be
# whitelist-able or the gate's session check would treat market-closed as OK.
_TRADABLE_SESSIONS = ("premarket", "regular", "afterhours")
_BASE_BROKERS = {"paper", "alpaca", "ccxt", "robinhood"}


def _valid_brokers() -> set:
    """Allowed default_broker values = the known venues PLUS anything actually
    registered in aj_broker (custom/test brokers). Lazy import avoids a circular
    dependency. A typo still falls outside this set and resets to paper."""
    brokers = set(_BASE_BROKERS)
    try:
        import aj_broker
        brokers |= set(aj_broker._BROKERS.keys())
    except Exception:
        pass
    return brokers
# Config keys that are probabilities — clamped to [0,1] so a typo can't make
# the agent buy/sell on every signal.
_PROB_KEYS = ("buy_prob_threshold", "sell_prob_threshold")


def _coerce_bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def _coerce_list(v: Any, upper: bool = True) -> List[str]:
    """Normalize a value into a de-duped list of strings. `upper` uppercases
    each entry (correct for ticker symbols); session names must use upper=False
    so the persisted value stays case-consistent with _TRADABLE_SESSIONS."""
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
        t = str(it).strip()
        t = t.upper() if upper else t.lower()
        if t and t not in out:
            out.append(t)
    return out


import re as _re

# A number with optional sign, optional '$', and commas ONLY as proper
# thousands separators (e.g. 1,234,567.89). '1,2' / '12,5' / '1,2,3' do NOT
# match and fall back to the safe default rather than being silently
# re-interpreted as a larger value — critical for cap inputs.
_THOUSANDS_RE = _re.compile(r"^[+-]?\$?(\d{1,3}(,\d{3})+|\d+)(\.\d+)?$")


def _coerce_float(v: Any, default: float = 0.0) -> float:
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        f = float(v)
        return f if f == f and f not in (float("inf"), float("-inf")) else default
    s = str(v).strip()
    try:
        if "," in s:
            # commas allowed only as well-formed thousands separators
            if not _THOUSANDS_RE.match(s):
                return default
            s = s.replace(",", "")
        f = float(s.replace("$", ""))
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
            cfg[key] = _coerce_list(val, upper=(key != "session_whitelist"))
        elif key in _FLOAT_KEYS:
            cfg[key] = _coerce_float(val, DEFAULTS[key])
        elif key in _INT_KEYS:
            # round (not truncate): a fractional '0.6' must not silently collapse
            # to 0 and disable a guard the operator believed they enabled.
            cfg[key] = int(round(_coerce_float(val, DEFAULTS[key])))
        else:
            cfg[key] = str(val)
    # sanitize enums (an out-of-band stored value must not weaken the gate)
    if cfg["daily_loss_basis"] not in _VALID_LOSS_BASIS:
        cfg["daily_loss_basis"] = "realized_plus_unrealized"
    if cfg["halt_rearm"] not in _VALID_REARM:
        cfg["halt_rearm"] = "manual"
    if str(cfg["default_broker"]).lower() not in {b.lower() for b in _valid_brokers()}:
        cfg["default_broker"] = "paper"   # unknown venue => safe internal paper
    if str(cfg.get("entry_order_type")).lower() not in ("market", "limit"):
        cfg["entry_order_type"] = "market"
    if str(cfg.get("council_policy")).lower() not in _VALID_COUNCIL_POLICY:
        cfg["council_policy"] = "advisory"   # unknown policy => safest advisory
    else:
        cfg["council_policy"] = str(cfg["council_policy"]).lower()
    um = str(cfg.get("universe_mode") or "").lower()
    cfg["universe_mode"] = um if um in _VALID_UNIVERSE_MODE else "market_screen"
    cfg["session_whitelist"] = [s.lower() for s in cfg["session_whitelist"]
                                if s.lower() in _TRADABLE_SESSIONS] or ["regular"]
    # numeric guards: caps/counts can never be negative; probs clamp to [0,1].
    # A negative is an invalid/typo'd value — reset it to the DEFAULT rather than
    # to 0.0. For protective limits where 0 means 'disabled' (e.g. max_slippage_bps,
    # take_profit_pct), forcing 0.0 would silently turn the guard OFF; resetting to
    # the default preserves the intended posture instead of weakening it.
    for k in _FLOAT_KEYS:
        if cfg.get(k, 0) < 0:
            cfg[k] = float(DEFAULTS[k])
    for k in _INT_KEYS:
        if cfg.get(k, 0) < 0:
            cfg[k] = int(DEFAULTS[k])
    if cfg.get("scan_universe_max", 0) < 1:
        cfg["scan_universe_max"] = 1
    for k in _PROB_KEYS:
        v = cfg.get(k, 0.5)
        cfg[k] = min(1.0, max(0.0, v))
    # Gate sanity: buy threshold must sit at/above the sell threshold. An inverted
    # pair (buy < sell) makes almost every prob both a buy AND a sell candidate —
    # reset both to their safe defaults so the gate can't be silently inverted.
    if cfg["sell_prob_threshold"] > cfg["buy_prob_threshold"]:
        cfg["buy_prob_threshold"] = float(DEFAULTS["buy_prob_threshold"])
        cfg["sell_prob_threshold"] = float(DEFAULTS["sell_prob_threshold"])
    return cfg


def _serialize(key: str, value: Any) -> str:
    # Validate/clamp to the SAME rules get_config applies on read, so the
    # persisted setting is never out-of-range or an invalid enum/session. This
    # keeps the round-trip lossless and protects any direct reader of the raw
    # setting (get_config still re-sanitizes defensively on read).
    if key in _BOOL_KEYS:
        return "true" if _coerce_bool(value) else "false"
    if key in _LIST_KEYS:
        items = _coerce_list(value, upper=(key != "session_whitelist"))
        if key == "session_whitelist":
            items = [s for s in items if s in _TRADABLE_SESSIONS] or ["regular"]
        return json.dumps(items)
    if key in _FLOAT_KEYS:
        f = _coerce_float(value, DEFAULTS[key])
        if f < 0:
            f = float(DEFAULTS[key])
        if key in _PROB_KEYS:
            f = min(1.0, max(0.0, f))
        return str(f)
    if key in _INT_KEYS:
        i = int(round(_coerce_float(value, DEFAULTS[key])))
        if i < 0:
            i = int(DEFAULTS[key])
        return str(i)
    if key == "daily_loss_basis" and str(value) not in _VALID_LOSS_BASIS:
        return "realized_plus_unrealized"
    if key == "halt_rearm" and str(value) not in _VALID_REARM:
        return "manual"
    if key == "entry_order_type" and str(value).lower() not in ("market", "limit"):
        return "market"
    if key == "council_policy":
        v = str(value).lower()
        return v if v in _VALID_COUNCIL_POLICY else "advisory"
    if key == "universe_mode":
        v = str(value).lower()
        return v if v in _VALID_UNIVERSE_MODE else "market_screen"
    if key == "default_broker" and \
            str(value).lower() not in {b.lower() for b in _valid_brokers()}:
        return "paper"
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


# ── ㉓ config presets ─────────────────────────────────────────────────────────
# Risk + strategy bundles. Deliberately DO NOT touch trading_enabled,
# live_trading_enabled, robinhood_enabled, or symbol_allowlist — the operator
# always controls those explicitly.
PRESETS: Dict[str, Dict[str, Any]] = {
    "conservative": {
        "max_order_notional_usd": 500, "max_trades_per_day": 3,
        "max_daily_loss_usd": 200, "buy_prob_threshold": 0.62,
        "sell_prob_threshold": 0.40, "min_edge_pct_pts": 6.0,
        "max_open_positions": 3, "stop_loss_pct": 5.0, "take_profit_pct": 10.0,
        "trailing_stop_pct": 4.0, "exit_cooldown_min": 60,
        "conviction_sizing": True, "min_order_notional_usd": 50.0,
    },
    "moderate": {
        "max_order_notional_usd": 1000, "max_trades_per_day": 5,
        "max_daily_loss_usd": 500, "buy_prob_threshold": 0.55,
        "sell_prob_threshold": 0.45, "min_edge_pct_pts": 3.0,
        "max_open_positions": 6, "stop_loss_pct": 8.0, "take_profit_pct": 15.0,
        "trailing_stop_pct": 6.0, "exit_cooldown_min": 30,
        "conviction_sizing": True, "min_order_notional_usd": 50.0,
    },
    "aggressive": {
        "max_order_notional_usd": 2500, "max_trades_per_day": 10,
        "max_daily_loss_usd": 1500, "buy_prob_threshold": 0.52,
        "sell_prob_threshold": 0.48, "min_edge_pct_pts": 1.5,
        "max_open_positions": 12, "stop_loss_pct": 12.0, "take_profit_pct": 25.0,
        "trailing_stop_pct": 10.0, "exit_cooldown_min": 0,
        "conviction_sizing": True, "min_order_notional_usd": 0.0,
    },
}


def apply_preset(name: str) -> Optional[Dict[str, Any]]:
    """Apply a risk/strategy preset. Returns the full config, or None if the
    preset name is unknown.

    NOTE: presets are ADDITIVE OVERLAYS — they set only the keys listed in
    PRESETS and leave every other tunable (incl. advanced 100x guards such as
    max_book_correlation, atr_stop_mult, kelly_sizing) at its current value.
    Switching presets therefore does NOT reset those untouched guards; the
    operator manages them explicitly. This is deliberate so a preset switch can
    never silently weaken a guard the operator set by hand."""
    p = PRESETS.get(str(name or "").lower())
    if not p:
        return None
    return set_config(dict(p))


def get(key: str) -> Any:
    return get_config().get(key)


def is_trade_path_enabled() -> bool:
    """True only when the master switch is on. Convenience for callers that
    want to short-circuit before building a proposal."""
    return bool(get_config().get("trading_enabled"))


def is_open_universe(cfg: Optional[Dict[str, Any]] = None) -> bool:
    """True when the cycle may trade OFF the allowlist — i.e. universe_mode is
    'open' or 'market_screen', or the legacy allow_any_symbol flag is set. Both
    the scan (which symbols to consider) and the risk gate (which symbols may
    pass the allowlist check) MUST agree on this, so they both call here."""
    c = cfg if cfg is not None else get_config()
    mode = str(c.get("universe_mode") or "").lower()
    return mode in ("open", "market_screen") or bool(c.get("allow_any_symbol"))


# ── Analyst Council gating ────────────────────────────────────────────────────
# The council is doubly gated: the config flag AND a one-time operator
# acknowledgement (VERIFY-COUNCIL) that the layer is costly + non-deterministic.
# This mirrors VERIFY-ALPACA / VERIFY-OPENCODE / VERIFY-MCP-READ.

def council_verify_passed() -> bool:
    """True iff the operator has closed the VERIFY-COUNCIL gate."""
    try:
        return str(db.get_settings().get("aj_verify_council")) == "pass"
    except Exception:
        return False


def council_active(cfg: Optional[Dict[str, Any]] = None) -> bool:
    """True only when the council may run: enabled in config AND VERIFY-COUNCIL
    passed. Any read error => False (fail-closed: council stays off)."""
    try:
        c = cfg if cfg is not None else get_config()
        return bool(c.get("council_enabled")) and council_verify_passed()
    except Exception:
        return False
