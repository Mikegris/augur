"""aj_ic — the Information-Coefficient / signal-promotion GATE.

A new orthogonal forecast signal does NOT get to influence live decisions just
because it was added. It must first EARN that right by demonstrating *realized*
predictive skill on the forecast ledger. This module is that validation gate.

The posture is deliberately CONSERVATIVE and fail-OPEN-to-NOT-PROMOTED:
when we're unsure — no history, thin sample, a read error, anything — a signal
is **not** promoted. An unproven signal can only be ignored, never trusted.

What "skill" means here (all pure reads off the existing forecast ledger):

  • hit_rate    — realized directional accuracy (from the track record).
  • brier_skill — 1 - brier/base_rate_reference; >0 beats always-guessing the
                  base rate. Computed by forecast_accountability._brier_from_rows
                  (we import and reuse it — no duplicated Brier math).
  • ic          — Information Coefficient: correlation between the signal's
                  predicted EDGE (prob_up - 0.5) and the realized SIGNED return,
                  when both are present in the ledger metadata; else None.

Sourcing of realized data
--------------------------
Component signals are logged to research_tracker.signal_forecasts under the
name ``ens:<key>`` (see forecast_accountability._component_signal). Once a
forecast's horizon elapses the scorer prices it and fills `hit` +
`realized_return`. We read those scored rows via
``forecast_accountability`` helpers / ``research_tracker.get_scored_rows`` and
compute skill from them. A regime filter (aj_alpha.detect_regime) can restrict
the population to rows whose stored metadata regime matches.

Everything degrades to {"available": False} / promoted=False on a fresh install.

Python 3.9 compatible. No new dependencies.
"""
from __future__ import annotations

import logging
import math
from typing import Any, Dict, List, Optional

log = logging.getLogger("augur.aj_ic")

# Config-key defaults (the integrator registers these in aj_config; we only
# READ them, with conservative fallbacks).
_DEFAULT_MIN_SAMPLES = 20      # ic_min_samples     — need this much history
_DEFAULT_MIN_BRIER_SKILL = 0.0  # ic_min_brier_skill — must beat the base rate
_DEFAULT_MIN_IC = 0.0          # ic_min_ic          — IC must be non-negative


# ── data sourcing (monkeypatchable in tests) ──────────────────────────────────

def _scored_rows_for(name: str, since: Optional[str] = None) -> List[Dict[str, Any]]:
    """Realized, scored ledger rows for a signal name, chronological, with the
    parsed metadata dict (carries prob_up, optional regime). [] on any failure
    or a fresh install. The component leaderboard logs under ``ens:<key>``; we
    accept either the raw signal name or a bare component key.

    Reuses research_tracker.get_scored_rows — the same source
    forecast_accountability reads — so skill here matches the leaderboard.
    """
    candidates = []
    n = (name or "").strip()
    if not n:
        return []
    candidates.append(n)
    # Allow callers to pass a bare component key (e.g. "narrative"); the ledger
    # stores it prefixed. Try the prefixed form too.
    if not n.startswith("ens:"):
        try:
            import forecast_accountability as fa
            candidates.append(fa._component_signal(n))
        except Exception:
            candidates.append("ens:" + n)
    try:
        import research_tracker
    except Exception:
        return []
    for cand in candidates:
        try:
            rows = research_tracker.get_scored_rows(cand, since=since)
        except Exception:
            rows = []
        if rows:
            return list(rows)
    return []


def _detect_regime() -> Optional[str]:
    """Current market regime string, or None on failure."""
    try:
        import aj_alpha
        return aj_alpha.detect_regime()
    except Exception:
        return None


# ── helpers ───────────────────────────────────────────────────────────────────

def _filter_regime(rows: List[Dict[str, Any]], regime: Optional[str]) -> List[Dict[str, Any]]:
    """Keep only rows whose stored metadata regime matches `regime`. Rows that
    carry no regime tag are DROPPED when a regime is requested (we can't claim
    regime-specific skill from untagged history). No filter when regime is None.
    """
    if not regime:
        return rows
    want = str(regime).strip().lower()
    out = []
    for r in rows:
        md = r.get("metadata") or {}
        rg = md.get("regime") or md.get("_regime")
        if rg is not None and str(rg).strip().lower() == want:
            out.append(r)
    return out


def _hit_rate_from_rows(rows: List[Dict[str, Any]]) -> Optional[float]:
    """Realized directional hit-rate over rows with a non-None `hit`."""
    hits = [r for r in rows if r.get("hit") is not None]
    if not hits:
        return None
    won = sum(1 for r in hits if r.get("hit"))
    return won / float(len(hits))


def _ic_from_rows(rows: List[Dict[str, Any]]) -> Optional[float]:
    """Information Coefficient: Pearson correlation between predicted edge
    (prob_up - 0.5) and the realized SIGNED return. None when the ledger lacks
    the data (no prob_up / no realized_return) or there's no spread / <3 points.
    """
    edges: List[float] = []
    rets: List[float] = []
    for r in rows:
        md = r.get("metadata") or {}
        p = md.get("prob_up")
        rr = r.get("realized_return")
        if p is None or rr is None:
            continue
        try:
            edge = float(p) - 0.5
            ret = float(rr)
        except (TypeError, ValueError):
            continue
        edges.append(edge)
        rets.append(ret)
    n = len(edges)
    if n < 3:
        return None
    me = sum(edges) / n
    mr = sum(rets) / n
    ve = sum((e - me) ** 2 for e in edges)
    vr = sum((x - mr) ** 2 for x in rets)
    if ve <= 1e-12 or vr <= 1e-12:
        return None  # no spread in predictions or outcomes -> IC undefined
    cov = sum((edges[i] - me) * (rets[i] - mr) for i in range(n))
    ic = cov / math.sqrt(ve * vr)
    if not math.isfinite(ic):
        return None
    return max(-1.0, min(1.0, ic))


# ── 1. signal_skill ────────────────────────────────────────────────────────────

def signal_skill(name: str,
                 regime: Optional[str] = None,
                 since: Optional[str] = None) -> Dict[str, Any]:
    """Realized predictive skill for a forecast component.

    Returns {"n", "hit_rate", "brier_skill", "ic", "available"}. Skill is read
    from the scored forecast ledger, optionally restricted to `regime`. Brier
    math is delegated to forecast_accountability._brier_from_rows (not
    duplicated). `n` is the directional sample size the Brier/hit-rate are
    computed over. available=False means there is no usable scored history.

    Never raises — on any error returns an unavailable, neutral skill dict so
    the gate above it fails to NOT-PROMOTED.
    """
    empty = {"n": 0, "hit_rate": None, "brier_skill": None, "ic": None,
             "available": False}
    try:
        rows = _scored_rows_for(name, since=since)
        rows = _filter_regime(rows, regime)
        if not rows:
            return dict(empty)

        # Reuse the canonical Brier computation (also gives us the directional n).
        try:
            import forecast_accountability as fa
            cal = fa._brier_from_rows(rows)
        except Exception:
            cal = {"n": 0, "brier_skill": None}

        n = int(cal.get("n") or 0)
        brier_skill = cal.get("brier_skill")
        hit_rate = _hit_rate_from_rows(rows)
        ic = _ic_from_rows(rows)

        return {
            "n": n,
            "hit_rate": round(hit_rate, 4) if hit_rate is not None else None,
            "brier_skill": brier_skill,
            "ic": round(ic, 4) if ic is not None else None,
            "available": n > 0,
        }
    except Exception:
        log.debug("signal_skill failed for %s -> unavailable", name, exc_info=True)
        return dict(empty)


# ── 2. signal_promoted — THE GATE ───────────────────────────────────────────────

def signal_promoted(name: str,
                    cfg: Dict[str, Any],
                    regime: Optional[str] = None) -> Dict[str, Any]:
    """Decide whether `name` has earned the right to influence live decisions.

    Promotion requires ALL of:
      • n          >= cfg["ic_min_samples"]      (default 20)
      • brier_skill >= cfg["ic_min_brier_skill"] (default 0.0 — beats base rate)
      • ic         >= cfg["ic_min_ic"]           (default 0.0) — ONLY when IC is
                     available in the ledger; an absent IC does not block.

    Returns {"promoted", "reason", "skill"}. A signal with insufficient history
    is NOT promoted. Fail-open: any error -> promoted=False, reason "ic gate
    error" (the safe default is to not trust an unproven signal).
    """
    try:
        cfg = cfg or {}
        min_n = int(cfg.get("ic_min_samples", _DEFAULT_MIN_SAMPLES) or 0)
        min_bs = float(cfg.get("ic_min_brier_skill", _DEFAULT_MIN_BRIER_SKILL) or 0.0)
        min_ic = float(cfg.get("ic_min_ic", _DEFAULT_MIN_IC) or 0.0)

        skill = signal_skill(name, regime=regime)
        n = int(skill.get("n") or 0)

        if not skill.get("available") or n < min_n:
            return {
                "promoted": False,
                "reason": "insufficient history (n<{})".format(min_n),
                "skill": skill,
            }

        bs = skill.get("brier_skill")
        if bs is None:
            return {
                "promoted": False,
                "reason": "no brier skill (insufficient spread)",
                "skill": skill,
            }
        if float(bs) < min_bs:
            return {
                "promoted": False,
                "reason": "brier_skill {:.4f} < {:.4f} (does not beat base rate)".format(float(bs), min_bs),
                "skill": skill,
            }

        ic = skill.get("ic")
        if ic is not None and float(ic) < min_ic:
            return {
                "promoted": False,
                "reason": "ic {:.4f} < {:.4f}".format(float(ic), min_ic),
                "skill": skill,
            }

        reason = "promoted (n={}, brier_skill={:.4f}".format(n, float(bs))
        reason += ", ic={:.4f})".format(float(ic)) if ic is not None else ", ic=n/a)"
        return {"promoted": True, "reason": reason, "skill": skill}
    except Exception:
        log.debug("signal_promoted failed for %s -> not promoted", name, exc_info=True)
        return {
            "promoted": False,
            "reason": "ic gate error",
            "skill": {"n": 0, "hit_rate": None, "brier_skill": None,
                      "ic": None, "available": False},
        }


# ── 3. promotion_status — status endpoint ───────────────────────────────────────

def promotion_status(names: List[str],
                     cfg: Dict[str, Any],
                     regime: Optional[str] = None) -> Dict[str, Any]:
    """Map each signal name -> its signal_promoted result. For the status
    endpoint / UI panel. Never raises."""
    out: Dict[str, Any] = {}
    try:
        for name in (names or []):
            try:
                out[name] = signal_promoted(name, cfg, regime=regime)
            except Exception:
                out[name] = {"promoted": False, "reason": "ic gate error",
                             "skill": {"n": 0, "hit_rate": None,
                                       "brier_skill": None, "ic": None,
                                       "available": False}}
    except Exception:
        log.debug("promotion_status failed", exc_info=True)
    return out


# ── 4. walk_forward_validate — OPTIONAL offline validator ────────────────────────

def walk_forward_validate(symbol_or_signal: str,
                          cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Thin out-of-sample validator that delegates to research_backtest's
    walk-forward harness. This is a CLI / offline promotion path, SEPARATE from
    the live-ledger gate above (signal_promoted), which is what governs live
    decisions.

    Calling convention (best-effort wrapper around research_backtest):
      cfg["backtest_signal"] — adapter name (momentum|mean_reversion|
                               ml_forecast); defaults to "momentum".
      cfg["backtest_symbol"] — symbol; defaults to `symbol_or_signal` itself
                               (so a single positional arg can be the symbol).
      cfg["backtest_start"] / ["backtest_end"] — optional window.
      cfg["wf_min_signals"], cfg["wf_min_hit_rate"], cfg["wf_min_sharpe"] —
                               promotion thresholds (defaults 20 / 0.5 / 0.0).

    Returns {"validated": bool, "metrics": {...}}. Never raises; fail-OPEN to
    not-validated.

    LIMITATION: research_backtest runs a *price-based adapter strategy* over a
    symbol, not an arbitrary ledger signal. There is no clean API to backtest a
    single bespoke forecast signal that isn't one of the registered ADAPTERS,
    so `backtest_signal` must name one of those adapters. For ledger signals
    that have no backtest adapter, use the live gate (signal_promoted) instead.
    """
    metrics: Dict[str, Any] = {}
    try:
        cfg = cfg or {}
        import research_backtest as rb

        sig_name = (cfg.get("backtest_signal") or "momentum")
        adapter = rb.get_adapter(sig_name)
        if adapter is None:
            return {"validated": False,
                    "metrics": {"error": "no backtest adapter for '{}'".format(sig_name)}}

        symbol = (cfg.get("backtest_symbol") or symbol_or_signal or "").strip()
        if not symbol:
            return {"validated": False, "metrics": {"error": "no symbol"}}

        result = rb.run_backtest(
            adapter, symbol,
            start=cfg.get("backtest_start"),
            end=cfg.get("backtest_end"),
        )
        if not isinstance(result, dict) or result.get("error"):
            return {"validated": False,
                    "metrics": result if isinstance(result, dict) else {"error": "backtest failed"}}

        n = int(result.get("n_signals") or 0)
        hit_rate = float(result.get("hit_rate") or 0.0)
        sharpe = float(result.get("sharpe") or 0.0)
        metrics = {
            "n_signals": n,
            "hit_rate": round(hit_rate, 4),
            "sharpe": round(sharpe, 4),
            "total_return_pct": result.get("total_return_pct"),
            "max_drawdown_pct": result.get("max_drawdown_pct"),
            "signal": sig_name,
            "symbol": symbol,
        }

        min_n = int(cfg.get("wf_min_signals", 20) or 0)
        min_hr = float(cfg.get("wf_min_hit_rate", 0.5) or 0.0)
        min_sharpe = float(cfg.get("wf_min_sharpe", 0.0) or 0.0)
        validated = (n >= min_n and hit_rate >= min_hr and sharpe >= min_sharpe)
        return {"validated": bool(validated), "metrics": metrics}
    except Exception:
        log.debug("walk_forward_validate failed -> not validated", exc_info=True)
        return {"validated": False, "metrics": {"error": "walk-forward error"}}


__all__ = [
    "signal_skill",
    "signal_promoted",
    "promotion_status",
    "walk_forward_validate",
]


if __name__ == "__main__":  # manual smoke
    import json
    import sys
    name = sys.argv[1] if len(sys.argv) > 1 else "narrative"
    print(json.dumps({
        "skill": signal_skill(name),
        "promoted": signal_promoted(name, {}),
    }, indent=2, default=str))
