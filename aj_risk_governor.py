"""AJTA — portfolio-level Risk Governor + alpha-decay circuit breaker.

Computes ONE global exposure multiplier `G` applied to NEW-entry sizing, from
three arms:
  1. Drawdown — shrink as the portfolio drawdown grows; breaker (G=0) at a hard
     threshold.
  2. Regime / volatility — halve exposure in high-VIX, trim in a bear regime.
  3. Realized alpha-decay — shrink on weak realized expectancy; breaker (G=0) on
     NEGATIVE realized trailing expectancy (the live edge is gone).

Design rules (mirror the rest of the agent):
  * OPT-IN (risk_governor_enabled, default OFF) and FAIL-OPEN to G=1.0 — any
    error leaves sizing untouched.
  * It can only SHRINK exposure autonomously (or pause via the breaker, G=0).
    G>1 (levering up) requires risk_governor_max>1 AND a PROVEN realized edge.
  * It NEVER touches a sell (exits always close the full position) and NEVER
    bypasses the fail-closed risk gate — it's a multiplier on the entry size,
    applied upstream of the gate which remains the sole execution authority.
  * Memoized for a short TTL so the per-entry call is cheap (the VIX/equity/DB
    reads run once per ~cycle, not once per candidate).

Python 3.9 compatible.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional, Tuple

import aj_config

log = logging.getLogger("augur.aj_risk_governor")

_MEMO_TTL = 90.0          # seconds — one compute per ~cycle, not per candidate
_memo: Dict[str, Any] = {"ts": 0.0, "val": None}
_memo_lock_note = "single-threaded operator loop; benign if recomputed"


def _f(cfg: Dict[str, Any], key: str, default: float) -> float:
    try:
        v = cfg.get(key, default)
        return float(v) if v is not None else float(default)
    except (TypeError, ValueError):
        return float(default)


def _realized_expectancy(window: int) -> Tuple[Optional[float], int]:
    """Mean realized return % over the most recent `window` CLOSED trades from
    the meta-label store (aj_trade_labels). (None, 0) if unavailable."""
    try:
        import aj_db
        rows = aj_db.query(
            "SELECT realized_return_pct FROM aj_trade_labels "
            "WHERE realized_return_pct IS NOT NULL ORDER BY id DESC LIMIT ?",
            (max(1, int(window)),))
        vals = []
        for r in rows:
            v = r.get("realized_return_pct")
            if v is None:
                continue
            try:
                fv = float(v)
            except (TypeError, ValueError):
                continue
            if fv == fv:          # drop NaN
                vals.append(fv)
        if not vals:
            return (None, 0)
        return (sum(vals) / len(vals), len(vals))
    except Exception:
        log.debug("realized expectancy read failed", exc_info=True)
        return (None, 0)


def _compute(cfg: Dict[str, Any]) -> Dict[str, Any]:
    reasons = []
    comps: Dict[str, Any] = {}
    gmax = _f(cfg, "risk_governor_max", 1.0)
    gmin = _f(cfg, "risk_governor_min", 0.0)
    G = min(1.0, gmax)            # never START above 1.0; lever-up handled below
    breaker = False

    # ── 1. drawdown arm ──────────────────────────────────────────────────────
    dd = 0.0
    try:
        import aj_alpha
        dd = float(aj_alpha.current_drawdown_pct() or 0.0)
    except Exception:
        log.debug("governor drawdown read failed", exc_info=True)
    comps["drawdown_pct"] = round(dd, 1)
    dd_derisk = _f(cfg, "rg_drawdown_derisk_pct", 10.0)
    dd_break = _f(cfg, "rg_drawdown_breaker_pct", 20.0)
    if dd >= dd_break > 0:
        breaker = True
        reasons.append("drawdown {:.0f}% >= breaker {:.0f}%".format(dd, dd_break))
    elif dd > dd_derisk > 0:
        span = max(1e-9, dd_break - dd_derisk)
        f = max(0.25, 1.0 - 0.75 * (dd - dd_derisk) / span)   # 1.0 -> 0.25 across the band
        if f < G:
            G = f
            reasons.append("drawdown {:.0f}% -> x{:.2f}".format(dd, f))

    # ── 2. regime / volatility arm ───────────────────────────────────────────
    vix = None
    try:
        import aj_rules
        vix = aj_rules.current_vix()
    except Exception:
        log.debug("governor vix read failed", exc_info=True)
    comps["vix"] = vix
    vix_derisk = _f(cfg, "rg_vix_derisk", 30.0)
    if vix is not None and vix_derisk > 0 and float(vix) >= vix_derisk:
        G = min(G, 0.5)
        reasons.append("VIX {:.0f} >= {:.0f} -> x0.50".format(float(vix), vix_derisk))
    regime = None
    try:
        import aj_alpha
        regime = aj_alpha.detect_regime()
    except Exception:
        log.debug("governor regime read failed", exc_info=True)
    comps["regime"] = regime
    if regime == "bear":
        G = min(G, 0.7)
        reasons.append("bear regime -> x0.70")

    # ── 3. realized alpha-decay arm ──────────────────────────────────────────
    min_tr = int(_f(cfg, "rg_alpha_decay_min_trades", 20))
    exp, n = _realized_expectancy(max(min_tr, 30))
    comps["realized_expectancy_pct"] = round(exp, 3) if exp is not None else None
    comps["realized_n"] = n
    if exp is not None and n >= min_tr:
        if exp < 0:
            breaker = True
            reasons.append("realized expectancy {:.2f}% < 0 over {} trades".format(exp, n))
        elif exp < _f(cfg, "rg_alpha_decay_floor_pct", 0.1):
            G = min(G, 0.5)
            reasons.append("weak realized expectancy {:.2f}% -> x0.50".format(exp))

    # ── lever up ONLY on proven edge (and only if max>1) ─────────────────────
    if not breaker and gmax > 1.0 and exp is not None \
            and n >= int(_f(cfg, "rg_lever_unlock_trades", 50)) \
            and exp >= _f(cfg, "rg_lever_min_expectancy_pct", 0.5):
        lever = min(gmax, 1.0 + (gmax - 1.0) * min(1.0, exp / 1.0))
        if lever > G:
            G = lever
            reasons.append("proven edge ({:.2f}% over {}) -> lever to x{:.2f}".format(exp, n, lever))

    if breaker:
        G = 0.0
    else:
        G = max(gmin, min(gmax, G))

    return {"G": round(G, 3), "enabled": True, "breaker": breaker,
            "reasons": reasons, "components": comps}


def exposure_multiplier(cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The global new-entry exposure multiplier. Returns {G, enabled, breaker,
    reasons, components}. Fail-open to G=1.0. Memoized for _MEMO_TTL seconds."""
    try:
        cfg = cfg or aj_config.get_config()
        if not cfg.get("risk_governor_enabled"):
            return {"G": 1.0, "enabled": False, "breaker": False, "reasons": [], "components": {}}
        now = time.time()
        cached = _memo.get("val")
        if cached is not None and (now - float(_memo.get("ts") or 0.0)) < _MEMO_TTL:
            return cached
        out = _compute(cfg)
        _memo["ts"] = now
        _memo["val"] = out
        return out
    except Exception:
        log.debug("exposure_multiplier failed; G=1.0", exc_info=True)
        return {"G": 1.0, "enabled": False, "breaker": False, "reasons": ["error"], "components": {}}


def status(cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Current governor state for the status endpoint / Insights panel."""
    cfg = cfg or aj_config.get_config()
    out = exposure_multiplier(cfg)
    out = dict(out)
    out["max"] = _f(cfg, "risk_governor_max", 1.0)
    out["min"] = _f(cfg, "risk_governor_min", 0.0)
    return out


def reset_memo() -> None:
    """Clear the memo (tests / immediate re-evaluation after a config change)."""
    _memo["ts"] = 0.0
    _memo["val"] = None
