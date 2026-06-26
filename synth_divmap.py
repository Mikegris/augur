"""
Divergence Map — find symbols where normally-correlated signals disagree.

Big idea: AUGUR ships nine independent signal stacks (insider flow,
congressional flow, smart-money composite, ML forecast, factor alpha,
narrative phase + velocity, options put-call skew, reflexivity loop
strength). Most days, on most names, these tell roughly the same story:
either everything points up or everything points down. The interesting
moments are when they don't.

A heavy bullish smart-money composite while insiders are dumping is a
classic distribution top. A bullish narrative while puts are bid is the
market telling you the story is priced in. The view this module powers
turns those quiet contradictions into a sortable list.

Implementation:

  - 5 pair definitions (see PAIR_DEFS). Each pair specifies how to extract
    two scalar signals for a symbol, how to normalize each to [-1, +1],
    a magnitude formula (just |a − b|), and a templated interpretation.

  - For each symbol in the universe, fetch the per-pair signals in
    parallel (ThreadPoolExecutor(max_workers=6)). If either signal is
    missing or the source raises, skip that pair for that symbol — never
    crash the scan.

  - Magnitude threshold: 0.8 on a [-1,+1] axis (so the signals are at
    least 80% opposed before counting as divergence).

  - Sort all surfaced divergences by magnitude desc and return the top_n.

The whole scan is wrapped in cache_store.coalesce(("divmap", …), 3600,
…) so concurrent UI calls don't fan out duplicate scans across the
30-symbol universe.

Python 3.9 compatible.
"""

from __future__ import annotations

import hashlib
import logging
import math
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

import safe_executor

log = logging.getLogger("augur.divmap")

try:
    import cache_store  # type: ignore
except Exception:  # pragma: no cover
    cache_store = None  # type: ignore

# Optional source modules — every import is best-effort so the scan still
# runs even if one of them is mid-refactor or fails to load on a stripped
# desktop bundle. Missing modules simply cause their pairs to be skipped
# (rather than the whole scan to bail).
try:
    import sec_edgar  # type: ignore
except Exception as _e:
    sec_edgar = None  # type: ignore
    log.warning("sec_edgar unavailable: %s", _e)

try:
    import congress  # type: ignore
except Exception as _e:
    congress = None  # type: ignore
    log.warning("congress unavailable: %s", _e)

try:
    import smart_money  # type: ignore
except Exception as _e:
    smart_money = None  # type: ignore
    log.warning("smart_money unavailable: %s", _e)

try:
    import ml_forecast  # type: ignore
except Exception as _e:
    ml_forecast = None  # type: ignore
    log.warning("ml_forecast unavailable: %s", _e)

try:
    import research_factors  # type: ignore
except Exception as _e:
    research_factors = None  # type: ignore
    log.warning("research_factors unavailable: %s", _e)

try:
    import narrative_engine  # type: ignore
except Exception as _e:
    narrative_engine = None  # type: ignore
    log.warning("narrative_engine unavailable: %s", _e)

try:
    import reflexivity_detector  # type: ignore
except Exception as _e:
    reflexivity_detector = None  # type: ignore
    log.warning("reflexivity_detector unavailable: %s", _e)

try:
    import fetcher  # type: ignore
except Exception as _e:
    fetcher = None  # type: ignore
    log.warning("fetcher unavailable: %s", _e)


# ───────────────────────── tunables ─────────────────────────

_CACHE_TTL = 3600                # 1h scan TTL — fits the 6h warmer cadence with margin
_MAX_WORKERS = 6                 # heavy parallel per-symbol fetch
_PER_SYMBOL_TIMEOUT_S = 45.0     # absolute cap so one slow symbol doesn't stall the scan
_DIVERGENCE_THRESHOLD = 0.8      # magnitude must exceed this to surface

# Map of narrative buckets to a directional axis. The narrative engine
# emits topic buckets (GROWTH, PROFITABILITY, REGULATORY, SCANDAL, …)
# rather than BULLISH/BEARISH labels — translate them here so the pair
# logic doesn't need to know the implementation detail.
_NARRATIVE_BULL = {"GROWTH", "PROFITABILITY", "TURNAROUND", "M_AND_A"}
_NARRATIVE_BEAR = {"REGULATORY", "SCANDAL"}

# Default universe — S&P 500 top-100 by market cap (rough as of 2026-05).
# Same pattern as the parallel synth_cluster build: a static list rather
# than a live cap-table lookup keeps the scan deterministic and offline-
# usable. Drift over a quarter doesn't materially change the divergences
# anyone cares about.
SP500_TOP100: Tuple[str, ...] = (
    "AAPL", "MSFT", "NVDA", "GOOGL", "GOOG", "AMZN", "META", "TSLA",
    "BRK-B", "LLY", "AVGO", "JPM", "V", "WMT", "XOM", "UNH", "MA",
    "PG", "HD", "JNJ", "ORCL", "COST", "ABBV", "BAC", "NFLX", "KO",
    "CRM", "CVX", "MRK", "AMD", "TMO", "PEP", "ACN", "ADBE", "LIN",
    "MCD", "CSCO", "ABT", "WFC", "DHR", "TXN", "CAT", "INTU", "GE",
    "VZ", "DIS", "PFE", "IBM", "QCOM", "AMAT", "AXP", "T", "AMGN",
    "PM", "GS", "ISRG", "NOW", "MS", "RTX", "UBER", "BLK", "SPGI",
    "PLTR", "HON", "NEE", "BKNG", "LOW", "ELV", "C", "DE", "SCHW",
    "BSX", "ETN", "SYK", "TJX", "VRTX", "MDT", "ADP", "PGR", "GILD",
    "ADI", "MMC", "PANW", "SBUX", "BX", "CB", "FI", "MU", "LMT",
    "REGN", "TMUS", "BMY", "MO", "MDLZ", "INTC", "SO", "ZTS", "DUK",
    "AMT", "ICE", "EOG",
)


# ───────────────────────── per-signal extractors ─────────────────────────
#
# Each helper returns a scalar (already in [-1, +1] where applicable), the
# raw value to surface to the UI, and a short human interpretation. If the
# signal isn't available, return None and the pair gets skipped.


def _clamp(v: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, v))


def _insider_form4_net_60d(symbol: str) -> Optional[Dict[str, Any]]:
    """Net dollar value of insider Form-4 buys minus sells over ~60 days.

    Returns a dict with:
      norm:   value in [-1, +1] (signed buy/sell pressure)
      raw:    the actual net $ flow (millions)
      label:  short text for the UI
    """
    if sec_edgar is None:
        return None
    try:
        txns = sec_edgar.get_form4_transactions(symbol, limit=40) or []
    except Exception as e:
        log.debug("form4 fetch failed for %s: %s", symbol, e)
        return None
    if not txns:
        return None

    # Compare in the same naive frame as the parsed filing date; using
    # .timestamp() on a naive datetime would interpret it in the host's local
    # timezone and drift the 60-day boundary on non-UTC hosts.
    cutoff_dt = datetime.utcnow() - timedelta(days=60)
    net_value = 0.0
    n = 0
    for t in txns:
        try:
            dt_str = t.get("date") or ""
            if not dt_str:
                continue
            # ISO YYYY-MM-DD comparison is fine lexicographically.
            try:
                dt_obj = datetime.strptime(dt_str[:10], "%Y-%m-%d")
            except ValueError:
                continue
            if dt_obj < cutoff_dt:
                continue
            val = float(t.get("value") or 0.0)
            if val <= 0:
                continue
            if t.get("transaction_type") == "BUY":
                net_value += val
            else:
                net_value -= val
            n += 1
        except Exception:
            continue

    if n == 0:
        return None

    # Normalize: $5M net flow ≈ saturation. Could refine per market-cap
    # but constant works fine for divergence detection across mega-caps.
    norm = _clamp(net_value / 5_000_000.0)
    net_m = round(net_value / 1_000_000.0, 2)
    if norm > 0.2:
        label = f"Insiders net BUYING ~${net_m}M (60d)"
    elif norm < -0.2:
        label = f"Insiders net SELLING ~${abs(net_m)}M (60d)"
    else:
        label = f"Insider flow ~flat (${net_m}M net)"
    return {"norm": norm, "raw": net_m, "label": label}


def _smart_money_score_signed(symbol: str) -> Optional[Dict[str, Any]]:
    """Convert smart-money composite (0-100) to a [-1, +1] axis around 50."""
    if smart_money is None:
        return None
    try:
        result = smart_money.compute_score(symbol) or {}
    except Exception as e:
        log.debug("smart_money score failed for %s: %s", symbol, e)
        return None
    score = result.get("score")
    if score is None or "error" in result:
        return None
    # 50 = neutral, 100 = max bullish, 0 = max bearish.
    norm = _clamp((float(score) - 50.0) / 50.0)
    signal = result.get("signal", "")
    label = f"Smart Money {int(score)}/100 ({signal})" if signal else f"Smart Money {int(score)}/100"
    return {"norm": norm, "raw": int(score), "label": label}


def _ml_prob_up_signed(symbol: str) -> Optional[Dict[str, Any]]:
    """Composite ML forecast probability mapped from [0,1] → [-1, +1]."""
    if ml_forecast is None:
        return None
    try:
        result = ml_forecast.ml_forecast(symbol) or {}
    except Exception as e:
        log.debug("ml_forecast failed for %s: %s", symbol, e)
        return None
    if "error" in result:
        return None
    composite = result.get("composite_ml_score")
    if composite is None:
        return None
    # composite is roughly P(up over 20d).
    norm = _clamp((float(composite) - 0.5) * 2.0)
    sig = result.get("composite_signal", "")
    label = f"ML composite {composite:.2f} ({sig})" if sig else f"ML composite {composite:.2f}"
    return {"norm": norm, "raw": round(float(composite), 3), "label": label}


def _factor_alpha_signed(symbol: str) -> Optional[Dict[str, Any]]:
    """Annualized factor alpha → [-1, +1] (saturates at ±20% annual)."""
    if research_factors is None:
        return None
    try:
        result = research_factors.factor_exposure(symbol, 3) or {}
    except Exception as e:
        log.debug("factor_exposure failed for %s: %s", symbol, e)
        return None
    if "error" in result:
        return None
    alpha = result.get("alpha_annual_pct")
    if alpha is None:
        return None
    norm = _clamp(float(alpha) / 20.0)
    label = f"Factor alpha {alpha:+.1f}%/yr"
    return {"norm": norm, "raw": round(float(alpha), 2), "label": label}


def _narrative_direction(symbol: str) -> Optional[Dict[str, Any]]:
    """Translate dominant narrative bucket + velocity into a directional value.

    Bullish topics (growth, profitability, M&A, turnaround) → +1
    Bearish topics (regulatory, scandal) → -1
    Velocity reinforces direction; macro is treated as neutral.
    """
    if narrative_engine is None:
        return None
    try:
        result = narrative_engine.analyze_narrative(symbol) or {}
    except Exception as e:
        log.debug("narrative failed for %s: %s", symbol, e)
        return None
    if "error" in result:
        return None
    dom = result.get("dominant_narrative") or ""
    velocity = result.get("velocity_score")
    article_count = result.get("article_count", 0)
    if article_count < 3:
        return None
    if dom in _NARRATIVE_BULL:
        base = 1.0
        direction = "BULLISH"
    elif dom in _NARRATIVE_BEAR:
        base = -1.0
        direction = "BEARISH"
    else:
        # Use velocity as a weak signal when topic itself is neutral.
        if velocity is not None and abs(velocity) > 25:
            base = 0.4 if velocity > 0 else -0.4
            direction = "ACCEL+" if velocity > 0 else "ACCEL-"
        else:
            return None
    # Velocity damps or amplifies (cap effect to keep within axis).
    # WHY (S7): velocity is the ACCELERATION of news volume, not a directional
    # bullishness. Adding it raw made accelerating BAD news (base<0, velocity>0)
    # read LESS bearish — exactly backwards. Acceleration should intensify the
    # narrative in whatever direction it already points, so push it along the
    # sign of `base`.
    if velocity is not None:
        base = _clamp(base + math.copysign(abs(velocity) / 200.0, base))
    label = f"Narrative {direction} ({dom}, vel={velocity})"
    return {"norm": base, "raw": direction, "label": label}


def _options_pcr(symbol: str) -> Optional[Dict[str, Any]]:
    """Compute put-call ratio from the front-month chain.

    Bullish skew = more call volume → norm > 0.
    Bearish skew = more put volume → norm < 0.

    PCR > 1.0 means more puts than calls, classically bearish; we flip the
    sign so a higher *put* count maps to a more *negative* norm — that way
    the divergence math reads naturally ("narrative+, options−").
    """
    if fetcher is None:
        return None
    try:
        chain = fetcher.get_option_chain(symbol) or {}
    except Exception as e:
        log.debug("option chain failed for %s: %s", symbol, e)
        return None
    if "error" in chain:
        return None
    calls = chain.get("calls") or []
    puts = chain.get("puts") or []
    call_vol = sum(float(c.get("volume") or 0) for c in calls)
    put_vol = sum(float(p.get("volume") or 0) for p in puts)
    if call_vol + put_vol < 100:  # too thin to read
        return None
    if call_vol <= 0:
        # No call volume at all: a PCR is undefined, and the old 5.0 sentinel
        # forced a fixed -1 (max bearish) norm regardless of how little put
        # volume there was. Skip the pair rather than emit a spurious extreme.
        return None
    pcr = put_vol / call_vol
    # Map PCR centered at 1.0 (parity): 0.5 → +1, 1.5 → -1.
    # We use a simple anchored-linear formula clamped to the axis.
    norm = _clamp((1.0 - pcr) / 0.5)
    if pcr > 1.2:
        interp = "Heavy put buying"
    elif pcr < 0.7:
        interp = "Heavy call buying"
    else:
        interp = "Balanced"
    label = f"Options PCR {pcr:.2f} ({interp})"
    return {"norm": norm, "raw": round(pcr, 2), "label": label}


def _congress_net_60d(symbol: str) -> Optional[Dict[str, Any]]:
    """Net buy/sell dollars from congressional PTRs for this ticker over ~60d."""
    if congress is None:
        return None
    try:
        trades = congress.get_trades_for_ticker(symbol, days=60) or []
    except Exception as e:
        log.debug("congress trades failed for %s: %s", symbol, e)
        return None
    if not trades:
        return None
    net = 0.0
    n_buys = 0
    n_sells = 0
    for t in trades:
        # congress.py emits amount_val (number) and txn_type / txn_type_raw
        # (label + raw P/S code). The old field names (amount_max/type) never
        # existed, so this congressional signal was silently dead for every
        # symbol — n_buys+n_sells was always 0 and the function returned None.
        amt = t.get("amount_val") or 0
        try:
            amt = float(amt)
        except (ValueError, TypeError):
            continue
        ttype = "{} {}".format(t.get("txn_type") or "", t.get("txn_type_raw") or "").lower().strip()
        # Branch on the RAW P/S code (mirrors synth_sectorflow/_consensus), not
        # the combined label's first char: ttype starts with the human label
        # ("sale s", "purchase p"), so a future label like "split"/"pending"
        # would be miscounted by ttype.startswith("p"/"s").
        raw = (t.get("txn_type_raw") or "").upper().strip()
        if "purchase" in ttype or "buy" in ttype or raw.startswith("P"):
            net += amt
            n_buys += 1
        elif "sale" in ttype or "sell" in ttype or raw.startswith("S"):
            net -= amt
            n_sells += 1
    if n_buys + n_sells == 0:
        return None
    # Saturates at $250k each side (PTRs report ranges, not exact figures).
    norm = _clamp(net / 250_000.0)
    label = f"Congress: {n_buys}B/{n_sells}S, net ~${net/1000:.0f}k (60d)"
    return {"norm": norm, "raw": round(net / 1000, 1), "label": label}


def _reflexivity_strength_signed(symbol: str) -> Optional[Dict[str, Any]]:
    """Strongest reflexivity loop → directional [-1, +1]."""
    if reflexivity_detector is None:
        return None
    try:
        result = reflexivity_detector.detect_loops(symbol) or {}
    except Exception as e:
        log.debug("reflexivity failed for %s: %s", symbol, e)
        return None
    if "error" in result:
        return None
    loops = result.get("active_loops") or []
    if not loops:
        return None
    best = max(loops, key=lambda lp: lp.get("strength", 0) or 0)
    strength = best.get("strength") or 0
    if not strength or strength < 20:  # weak loops aren't worth comparing
        return None
    direction = (best.get("direction") or "NEUTRAL").upper()
    if direction in ("UP", "POSITIVE", "BULLISH"):
        sign = 1.0
    elif direction in ("DOWN", "NEGATIVE", "BEARISH"):
        sign = -1.0
    else:
        # equity_feedback loops are intrinsically reinforcing — treat as
        # sign-from-the-loop-type when no explicit direction is set.
        ltype = (best.get("type") or "").lower()
        if "squeeze" in ltype:
            sign = 1.0
        elif "dilution" in ltype:
            sign = -1.0
        else:
            return None
    norm = _clamp(sign * (strength / 100.0))
    loop_type = best.get("type") or "loop"
    label = f"Reflexivity {loop_type} strength={strength}"
    return {"norm": norm, "raw": int(strength), "label": label}


def _narrative_velocity_signed(symbol: str) -> Optional[Dict[str, Any]]:
    """Narrative velocity → [-1, +1] axis (acceleration sign)."""
    if narrative_engine is None:
        return None
    try:
        result = narrative_engine.analyze_narrative(symbol) or {}
    except Exception as e:
        log.debug("narrative velocity failed for %s: %s", symbol, e)
        return None
    if "error" in result:
        return None
    vel = result.get("velocity_score")
    if vel is None:
        return None
    if abs(vel) < 10:  # essentially stable, no signal
        return None
    norm = _clamp(float(vel) / 60.0)
    vlabel = result.get("velocity_label") or ""
    label = f"Narrative velocity {vel:+d} ({vlabel})"
    return {"norm": norm, "raw": int(vel), "label": label}


# ───────────────────────── pair definitions ─────────────────────────
#
# Each pair: (name, signal_a_fn, signal_a_display_name,
#             signal_b_fn, signal_b_display_name, interpretation_template)
#
# The interpretation template uses .format() with named keys: a_label,
# b_label, a_dir, b_dir.

PAIR_DEFS = [
    {
        "name": "insider_form4_vs_smart_money",
        "signal_a_fn": _insider_form4_net_60d,
        "signal_a_name": "insider_form4_net_60d",
        "signal_b_fn": _smart_money_score_signed,
        "signal_b_name": "smart_money_score",
        "template": (
            "Smart Money composite {b_dir} but insiders are {a_dir} — "
            "possible {topbot}."
        ),
    },
    {
        "name": "ml_forecast_vs_factor_alpha",
        "signal_a_fn": _ml_prob_up_signed,
        "signal_a_name": "ml_forecast_prob_up_20d",
        "signal_b_fn": _factor_alpha_signed,
        "signal_b_name": "factor_alpha_annual",
        "template": (
            "ML forecast {a_dir} but factor model says alpha is {b_dir} — "
            "model and risk-attribution disagree."
        ),
    },
    {
        "name": "narrative_vs_options_pcr",
        "signal_a_fn": _narrative_direction,
        "signal_a_name": "narrative_phase",
        "signal_b_fn": _options_pcr,
        "signal_b_name": "options_pcr",
        "template": (
            "Narrative {a_dir} but options skewed {b_dir} — "
            "story may be priced in (or fading)."
        ),
    },
    {
        "name": "congress_vs_smart_money",
        "signal_a_fn": _congress_net_60d,
        "signal_a_name": "congress_net_60d",
        "signal_b_fn": _smart_money_score_signed,
        "signal_b_name": "smart_money_score",
        "template": (
            "Smart Money composite {b_dir} but Congress is {a_dir} — "
            "follow-the-power vs follow-the-math diverge."
        ),
    },
    {
        "name": "reflexivity_vs_narrative_velocity",
        "signal_a_fn": _reflexivity_strength_signed,
        "signal_a_name": "reflexivity_strength",
        "signal_b_fn": _narrative_velocity_signed,
        "signal_b_name": "narrative_velocity",
        "template": (
            "Reflexivity loop {a_dir} but narrative velocity {b_dir} — "
            "price feedback and story momentum out of phase."
        ),
    },
]


def _dir_word(norm: float) -> str:
    """Convert a [-1,+1] norm to a directional adjective for the template."""
    if norm > 0.5:
        return "strongly bullish"
    if norm > 0.1:
        return "bullish"
    if norm < -0.5:
        return "strongly bearish"
    if norm < -0.1:
        return "bearish"
    return "neutral"


def _verb_word(norm: float, bullish_verb: str, bearish_verb: str) -> str:
    """Variant for verbs ('buying' vs 'selling') — used for flow signals."""
    if norm > 0.1:
        return bullish_verb
    if norm < -0.1:
        return bearish_verb
    return "flat"


def _build_interpretation(pair: dict, a: dict, b: dict) -> str:
    """Compose human-readable text for a divergence row."""
    a_norm = a["norm"]
    b_norm = b["norm"]
    name = pair["name"]
    # Pair-specific phrasing so the directional words make sense in context.
    if name == "insider_form4_vs_smart_money":
        a_dir = _verb_word(a_norm, "buying heavily", "selling heavily")
        b_dir = _dir_word(b_norm)
        topbot = "top" if (b_norm > 0 and a_norm < 0) else "bottom"
        return pair["template"].format(a_dir=a_dir, b_dir=b_dir, topbot=topbot)
    if name == "ml_forecast_vs_factor_alpha":
        a_dir = _dir_word(a_norm)
        b_dir = "positive" if b_norm > 0 else "negative"
        return pair["template"].format(a_dir=a_dir, b_dir=b_dir)
    if name == "narrative_vs_options_pcr":
        a_dir = _dir_word(a_norm)
        b_dir = "to the put side" if b_norm < 0 else "to the call side"
        return pair["template"].format(a_dir=a_dir, b_dir=b_dir)
    if name == "congress_vs_smart_money":
        a_dir = _verb_word(a_norm, "buying", "selling")
        b_dir = _dir_word(b_norm)
        return pair["template"].format(a_dir=a_dir, b_dir=b_dir)
    if name == "reflexivity_vs_narrative_velocity":
        a_dir = "reinforcing up" if a_norm > 0 else "reinforcing down"
        b_dir = "accelerating up" if b_norm > 0 else "accelerating down"
        return pair["template"].format(a_dir=a_dir, b_dir=b_dir)
    # Fallback
    return f"{pair['name']}: a={a_norm:+.2f}, b={b_norm:+.2f}"


# ───────────────────────── per-symbol scan ─────────────────────────
#
# The five pairs draw on only NINE distinct signals, and two of them
# (smart_money, via two pairs) used to be fetched twice per symbol. The scan
# now fetches each distinct signal at most once per symbol, in two phases:
#
#   phase 1 — the "a-side" signals, all of which sit on self-caching
#             upstreams (EDGAR/congress via cache_store, ml_forecast 1h,
#             narrative 30min, reflexivity 30min);
#   phase 2 — each pair's partner signal, fetched ONLY when the phase-1 side
#             produced a value. A pair with a missing side can never yield a
#             row, so skipping the partner is free — and the skipped partners
#             are the expensive ones (smart_money composite ≈6 upstream
#             calls, options chain = uncached Yahoo HTTP).

_SIGNAL_FNS: Dict[str, Callable[[str], Optional[Dict[str, Any]]]] = {
    "insider":       _insider_form4_net_60d,
    "smart_money":   _smart_money_score_signed,
    "ml":            _ml_prob_up_signed,
    "factor":        _factor_alpha_signed,
    "narrative_dir": _narrative_direction,
    "options":       _options_pcr,
    "congress":      _congress_net_60d,
    "reflexivity":   _reflexivity_strength_signed,
    "narrative_vel": _narrative_velocity_signed,
}

# pair name -> (phase-1 signal key, phase-2 signal key)
_PAIR_SIGNALS: Dict[str, Tuple[str, str]] = {
    "insider_form4_vs_smart_money":      ("insider", "smart_money"),
    "ml_forecast_vs_factor_alpha":       ("ml", "factor"),
    "narrative_vs_options_pcr":          ("narrative_dir", "options"),
    "congress_vs_smart_money":           ("congress", "smart_money"),
    "reflexivity_vs_narrative_velocity": ("reflexivity", "narrative_vel"),
}

# phase-1 (gating) keys, in PAIR_DEFS order, deduped
_PHASE1_KEYS = ["insider", "ml", "narrative_dir", "congress", "reflexivity"]

# phase-2 key -> the phase-1 keys that justify fetching it
_PHASE2_DEPS = {
    "smart_money":   ("insider", "congress"),
    "factor":        ("ml",),
    "options":       ("narrative_dir",),
    "narrative_vel": ("reflexivity",),
}


def _fetch_signal(symbol: str, key: str) -> Optional[Dict[str, Any]]:
    """Run one signal extractor; exceptions degrade to None (pair skipped)."""
    try:
        return _SIGNAL_FNS[key](symbol)
    except Exception as e:
        log.debug("signal %s failed for %s: %s", key, symbol, e)
        return None


def _pair_row(symbol: str, pair: dict,
              a: Optional[Dict[str, Any]],
              b: Optional[Dict[str, Any]],
              norm_stats: Optional[Dict[str, Tuple[float, float]]] = None) -> Optional[Dict[str, Any]]:
    """Build a divergence row from two already-fetched signals.

    The raw per-signal ``norm`` values saturate at DIFFERENT scales (e.g.
    insider flow hits ±1 at $5M net, smart-money at ±50pts), so a single
    global 0.8 magnitude threshold on raw norms compared apples to oranges.
    When ``norm_stats`` (a per-signal-key {key: (mean, std)} map computed
    cross-sectionally over the scanned universe) is supplied, we z-score each
    leg against its OWN distribution first, then compare the standardized
    values — so "0.8 apart" means the same thing for every pair. We keep the
    opposite-sign opposition check on the (sign-preserving) z-scores.

    Falls back to raw-norm comparison when ``norm_stats`` is absent (e.g. a
    direct ``_evaluate_pair_for_symbol`` call outside a full scan).

    Returns None on:
      - either signal missing
      - standardized magnitude <= threshold
      - same-sign signals (big and small, but not opposed)
    """
    if a is None or b is None:
        return None

    key_a, key_b = _PAIR_SIGNALS[pair["name"]]

    def _z(key: str, norm: float) -> float:
        # z-score against the signal's own cross-sectional distribution;
        # mean-center then scale by std. Std<=0 (degenerate) → fall back to
        # the centered value so we never divide by ~0 and manufacture a blowout.
        if not norm_stats:
            return norm
        stats = norm_stats.get(key)
        if not stats:
            return norm
        mean, std = stats
        if std and std > 1e-9:
            return (norm - mean) / std
        return norm - mean

    za = _z(key_a, a["norm"])
    zb = _z(key_b, b["norm"])

    magnitude = abs(za - zb)
    if magnitude <= _DIVERGENCE_THRESHOLD:
        return None
    # Require actual *opposition*, not just one big and one small same-sign.
    # Use the standardized (sign-preserving) values for the sign test.
    if (za > 0) == (zb > 0) and abs(za) > 0.05 and abs(zb) > 0.05:
        # Same sign — both bullish or both bearish, just different magnitudes.
        # Not a divergence; skip.
        return None

    interpretation = _build_interpretation(pair, a, b)
    return {
        "symbol": symbol,
        "pair": pair["name"],
        "signal_a": {
            "name": pair["signal_a_name"],
            "value": a.get("raw"),
            "norm": round(a["norm"], 3),
            "z_score": round(za, 3),
            "interpretation": a.get("label", ""),
        },
        "signal_b": {
            "name": pair["signal_b_name"],
            "value": b.get("raw"),
            "norm": round(b["norm"], 3),
            "z_score": round(zb, 3),
            "interpretation": b.get("label", ""),
        },
        "magnitude": round(magnitude, 3),
        "interpretation": interpretation,
    }


def _evaluate_pair_for_symbol(symbol: str, pair: dict) -> Optional[Dict[str, Any]]:
    """Run one pair for one symbol and return a divergence row if it
    qualifies. Kept as a public-ish seam (fetches both signals fresh);
    the scan itself goes through _scan_symbol so shared signals are fetched
    once per symbol instead of once per pair."""
    key_a, key_b = _PAIR_SIGNALS[pair["name"]]
    a = _fetch_signal(symbol, key_a)
    b = _fetch_signal(symbol, key_b)
    return _pair_row(symbol, pair, a, b)


def _gather_symbol_signals(symbol: str) -> Dict[str, Optional[Dict[str, Any]]]:
    """Fetch every distinct signal for one symbol (each at most ONCE), with
    the two-phase cheap-then-expensive gating. Returns the {key: signal|None}
    map without building rows — so the caller can z-score norms
    cross-sectionally across the universe before thresholding."""
    t0 = time.time()
    signals: Dict[str, Optional[Dict[str, Any]]] = {}

    res1 = safe_executor.parallel_map(
        lambda key: _fetch_signal(symbol, key),
        _PHASE1_KEYS,
        max_workers=5,
        timeout_per_item=_PER_SYMBOL_TIMEOUT_S,
        thread_name_prefix="divmap-p1",
    )
    signals.update(zip(_PHASE1_KEYS, res1))

    phase2 = [key for key, deps in _PHASE2_DEPS.items()
              if any(signals.get(d) is not None for d in deps)]
    if phase2:
        remaining = max(5.0, _PER_SYMBOL_TIMEOUT_S - (time.time() - t0))
        res2 = safe_executor.parallel_map(
            lambda key: _fetch_signal(symbol, key),
            phase2,
            max_workers=4,
            timeout_per_item=remaining,
            thread_name_prefix="divmap-p2",
        )
        signals.update(zip(phase2, res2))
    return signals


def _rows_from_signals(symbol: str,
                       signals: Dict[str, Optional[Dict[str, Any]]],
                       norm_stats: Optional[Dict[str, Tuple[float, float]]] = None
                       ) -> List[Dict[str, Any]]:
    """Build divergence rows for one symbol from its already-fetched signals,
    standardizing each leg by ``norm_stats`` when supplied."""
    rows: List[Dict[str, Any]] = []
    for pair in PAIR_DEFS:
        key_a, key_b = _PAIR_SIGNALS[pair["name"]]
        try:
            row = _pair_row(symbol, pair, signals.get(key_a), signals.get(key_b), norm_stats)
        except Exception as e:
            log.debug("scan %s pair %s failed: %s", symbol, pair["name"], e)
            continue
        if row is not None:
            rows.append(row)
    return rows


def _scan_symbol(symbol: str) -> List[Dict[str, Any]]:
    """Evaluate every pair for one symbol. Returns list of divergence rows.

    Standalone seam (no universe context, so norms are compared raw — the
    full-universe scan in ``_compute`` z-scores them cross-sectionally first).
    Fetches each distinct signal at most ONCE and skips a pair's expensive
    partner signal when the cheap gating side returned nothing.
    """
    signals = _gather_symbol_signals(symbol)
    return _rows_from_signals(symbol, signals, norm_stats=None)


def _compute_norm_stats(
    per_symbol_signals: Dict[str, Dict[str, Optional[Dict[str, Any]]]]
) -> Dict[str, Tuple[float, float]]:
    """Cross-sectional (mean, std) of each signal key's ``norm`` over the
    universe. Keys with <3 observations are omitted (insufficient to z-score),
    causing _pair_row to fall back to the raw norm for that leg."""
    by_key: Dict[str, List[float]] = {k: [] for k in _SIGNAL_FNS}
    for signals in per_symbol_signals.values():
        for key, sig in signals.items():
            if sig is None:
                continue
            v = sig.get("norm")
            if v is None:
                continue
            try:
                fv = float(v)
            except (TypeError, ValueError):
                continue
            if fv == fv and not math.isinf(fv):
                by_key[key].append(fv)
    stats: Dict[str, Tuple[float, float]] = {}
    for key, vals in by_key.items():
        n = len(vals)
        if n < 3:
            continue
        mean = sum(vals) / n
        var = sum((x - mean) ** 2 for x in vals) / (n - 1)
        std = math.sqrt(var) if var > 0 else 0.0
        stats[key] = (mean, std)
    return stats


# ───────────────────────── public API ─────────────────────────


def _universe_to_list(universe) -> Tuple[str, List[str]]:
    """Resolve the universe argument into (label, symbols).

    universe can be:
      None            → default sp500_top100
      "sp500_top100"  → same
      list of str     → custom universe; the label becomes a hash for caching
    """
    if universe is None or universe == "sp500_top100":
        return ("sp500_top100", list(SP500_TOP100))
    if isinstance(universe, str):
        # Unknown string label — fall back to default but preserve the name
        # so the response echoes what was asked.
        return (universe, list(SP500_TOP100))
    if isinstance(universe, (list, tuple)):
        syms = [str(s).strip().upper() for s in universe if str(s).strip()]
        # Dedup while preserving order.
        seen = set()
        deduped: List[str] = []
        for s in syms:
            if s not in seen:
                seen.add(s)
                deduped.append(s)
        if not deduped:
            return ("sp500_top100", list(SP500_TOP100))
        h = hashlib.md5("|".join(sorted(deduped)).encode("utf-8")).hexdigest()[:8]
        return (f"custom_{h}", deduped)
    return ("sp500_top100", list(SP500_TOP100))


def divergence_map(universe=None, top_n: int = 20) -> dict:
    """Scan a universe for cross-signal divergences.

    See module docstring for the high-level logic. Returns:

        {
          "universe": "sp500_top100" | "<custom>",
          "n_symbols_scanned": int,
          "as_of": iso-utc string,
          "divergences": [ {...}, … ]  # sorted by magnitude desc, len ≤ top_n
        }
    """
    try:
        top_n = int(top_n)
    except (TypeError, ValueError):
        top_n = 20
    top_n = max(1, min(top_n, 200))

    label, symbols = _universe_to_list(universe)

    def _compute() -> dict:
        t0 = time.time()
        all_rows: List[Dict[str, Any]] = []

        # Heavy parallelism — _MAX_WORKERS symbols at a time on
        # safe_executor's daemon threads; each symbol fans its signal fetches
        # out on a short-lived inner pool (see _scan_symbol). The unit of
        # work is the SYMBOL (not (symbol, pair)) so signals shared by
        # several pairs — smart_money feeds two — are fetched once, and the
        # expensive partner fetches are skipped when their gating signal is
        # missing. parallel_map fills a slot with None when the work fn
        # raises (matching the old skip-on-error behavior) and falls back to
        # serial by itself when threads can't spawn. The overall batch
        # deadline of _PER_SYMBOL_TIMEOUT_S × 4 is a hard cap so one slow
        # upstream can't stall the scan forever, with headroom for symbols
        # queued behind the _MAX_WORKERS limit.
        # Pass 1: gather every symbol's signals (no thresholding yet) so we
        # can z-score each signal against its own cross-sectional distribution.
        raw_signals = safe_executor.parallel_map(
            _gather_symbol_signals,
            symbols,
            max_workers=_MAX_WORKERS,
            timeout_per_item=_PER_SYMBOL_TIMEOUT_S * 4,
            thread_name_prefix="divmap",
        )
        per_symbol_signals: Dict[str, Dict[str, Optional[Dict[str, Any]]]] = {}
        for sym, sigs in zip(symbols, raw_signals):
            per_symbol_signals[sym] = sigs if isinstance(sigs, dict) else {}

        # Per-signal cross-sectional (mean,std) → puts every pair's two legs
        # on a common z-scale before the magnitude threshold.
        norm_stats = _compute_norm_stats(per_symbol_signals)

        # Pass 2: build rows with standardized norms.
        for sym in symbols:
            rows = _rows_from_signals(sym, per_symbol_signals.get(sym, {}), norm_stats)
            if rows:
                all_rows.extend(rows)

        # Sort by magnitude desc, then take top_n.
        all_rows.sort(key=lambda r: r.get("magnitude", 0), reverse=True)
        top = all_rows[:top_n]

        elapsed_ms = int((time.time() - t0) * 1000)
        log.info(
            "divmap scan: %d symbols × %d pairs → %d divergences (top %d) in %dms",
            len(symbols), len(PAIR_DEFS), len(all_rows), len(top), elapsed_ms,
        )
        return {
            "universe": label,
            "n_symbols_scanned": len(symbols),
            "as_of": datetime.now(tz=timezone.utc).isoformat(),
            "divergences": top,
            "total_found": len(all_rows),
            "elapsed_ms": elapsed_ms,
        }

    if cache_store is None:
        return _compute()
    try:
        return cache_store.coalesce(("divmap", label, top_n), _CACHE_TTL, _compute)
    except Exception as e:
        log.warning("cache_store.coalesce(divergence_map) failed: %s", e)
        return _compute()


def warm_default_scan() -> None:
    """Pre-warm the default scan. Used by cache_warmer."""
    try:
        divergence_map(universe=None, top_n=20)
    except Exception as e:
        log.debug("divmap warm failed: %s", e)


# ───────────────────────── self-test ─────────────────────────

if __name__ == "__main__":  # pragma: no cover
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    import json as _json
    test_universe = ["AAPL", "MSFT", "NVDA", "TSLA", "META", "GOOGL", "SPY"]
    out = divergence_map(universe=test_universe, top_n=20)
    print(_json.dumps({
        "universe": out["universe"],
        "n_symbols_scanned": out["n_symbols_scanned"],
        "total_found": out.get("total_found"),
        "elapsed_ms": out.get("elapsed_ms"),
        "top_5": out["divergences"][:5],
    }, indent=2, default=str))
