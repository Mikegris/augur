"""
research_optimizer.py — Portfolio optimization (Markowitz, Black-Litterman, Risk Parity).

Solves classical mean-variance problems plus a Bayesian Black-Litterman blend
and an equal risk-contribution (ERC / risk parity) allocation. All math is done
with scipy.optimize.minimize and numpy — we deliberately avoid cvxpy so the
desktop bundle stays slim (cvxpy pulls 30+ MB of ECOS/SCS/OSQP binaries).

Daily returns come from `fetcher.get_chart_data`; we resample to daily,
compute log-returns, and annualize with the conventional 252 trading days.

Public surface used by the Flask route:
    markowitz_optimize(symbols, objective, constraints)
    risk_parity(symbols, period)
    black_litterman(symbols, mkt_caps, views, view_confidence, period, risk_aversion, tau)
    compare_to_current(current_weights, optimal_weights)

Each returns a JSON-serializable dict.
"""

from __future__ import annotations

import logging
import math
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np

log = logging.getLogger(__name__)

try:
    from scipy.optimize import minimize
except Exception as _e:  # pragma: no cover - scipy is a hard dep already
    minimize = None
    log.warning("scipy.optimize unavailable: %s", _e)

try:
    import fetcher  # local module
except Exception as _e:  # pragma: no cover
    fetcher = None
    log.warning("fetcher unavailable: %s", _e)


TRADING_DAYS = 252
DEFAULT_RF = 0.045
MIN_HISTORY_DAYS = 60  # need at least ~3 months of bars to estimate stats
EPS = 1e-10


# ─────────────────────────────────────────────────────────────────────────────
# Data plumbing
# ─────────────────────────────────────────────────────────────────────────────

def _daily_log_returns(symbol: str, period: str = "2y") -> Optional[np.ndarray]:
    """Return a 1-D numpy array of daily log-returns (length N-1), or None
    if the chart endpoint can't give us enough history.

    Kept for backward compat / single-asset callers; the portfolio estimator
    now uses `_dated_log_returns` so the covariance correlates returns from
    the SAME calendar dates, not merely the same trailing index."""
    dated = _dated_log_returns(symbol, period=period)
    if dated is None:
        return None
    return dated[1]


def _dated_log_returns(symbol: str, period: str = "2y"):
    """Return (dates, returns) where dates[i] is the bar date of returns[i],
    or None. Dates let the estimator inner-join on common trading days — a
    single missing/halted bar in one name otherwise shifts every later return
    relative to its peers, silently corrupting the covariance."""
    if fetcher is None:
        return None
    try:
        bars = fetcher.get_chart_data(symbol, period=period, interval="1d") or []
    except Exception as e:
        log.warning("get_chart_data(%s) failed: %s", symbol, e)
        return None
    rows = [(b.get("date") or b.get("time") or b.get("timestamp"), b.get("close"))
            for b in bars if b and b.get("close") is not None]
    if len(rows) < MIN_HISTORY_DAYS:
        return None
    dates = [str(d) for d, _ in rows]
    arr = np.asarray([c for _, c in rows], dtype=float)
    arr = np.where(arr > 0, arr, np.nan)
    rets = np.diff(np.log(arr))                      # return[i] spans dates[i]→dates[i+1]
    ret_dates = np.asarray(dates[1:], dtype=object)  # label each return by its END bar
    finite = np.isfinite(rets)
    rets, ret_dates = rets[finite], ret_dates[finite]
    if rets.size < MIN_HISTORY_DAYS - 1:
        return None
    return ret_dates, rets


def _estimate_cov_and_mean(
    symbols: Sequence[str],
    period: str = "2y",
) -> Tuple[np.ndarray, np.ndarray, List[str]]:
    """Pull daily returns for each symbol; align on the shortest series; return
    (annualized mean vector, annualized covariance, usable symbol list).

    Symbols whose history is too short or whose fetch failed are silently
    dropped — callers should check the returned symbol list."""
    usable: List[str] = []
    maps: List[Dict[str, float]] = []  # date -> return, per asset
    for s in symbols:
        dr = _dated_log_returns(s, period=period)
        if dr is None:
            continue
        dates, rets = dr
        usable.append(s.upper())
        maps.append({d: float(v) for d, v in zip(dates, rets)})

    if not maps:
        return np.zeros(0), np.zeros((0, 0)), []

    # Inner-join on the dates ALL usable assets share, then take the most
    # recent window. This guarantees column t of every row is the same
    # trading day — the precondition np.cov assumes and the old trailing-index
    # stack silently violated whenever one name had a missing/halted bar.
    common = set(maps[0])
    for m in maps[1:]:
        common &= set(m)
    if len(common) < MIN_HISTORY_DAYS - 1:
        return np.zeros(0), np.zeros((0, 0)), []
    common_dates = sorted(common)
    matrix = np.vstack([[m[d] for d in common_dates] for m in maps])  # (n_assets, T)

    mean_daily = matrix.mean(axis=1)
    cov_daily = np.cov(matrix, ddof=1)
    if cov_daily.ndim == 0:  # single-asset corner case
        cov_daily = np.array([[float(cov_daily)]])

    mean_annual = mean_daily * TRADING_DAYS
    cov_annual = cov_daily * TRADING_DAYS
    # Symmetrize to fight numerical drift from np.cov.
    cov_annual = 0.5 * (cov_annual + cov_annual.T)
    return mean_annual, cov_annual, usable


# ─────────────────────────────────────────────────────────────────────────────
# Portfolio math helpers
# ─────────────────────────────────────────────────────────────────────────────

def _portfolio_stats(
    w: np.ndarray,
    mean: np.ndarray,
    cov: np.ndarray,
    rf: float = DEFAULT_RF,
) -> Tuple[float, float, float]:
    ret = float(np.dot(w, mean))
    var = float(np.dot(w, cov @ w))
    vol = math.sqrt(max(var, 0.0))
    sharpe = (ret - rf) / vol if vol > EPS else 0.0
    return ret, vol, sharpe


def _build_bounds(
    n: int,
    min_w: float,
    max_w: float,
    long_only: bool,
) -> List[Tuple[float, float]]:
    lo = max(min_w, 0.0) if long_only else min_w
    hi = max_w
    if hi < lo:
        hi = lo
    return [(lo, hi)] * n


def _sum_to_one_constraint() -> Dict:
    return {"type": "eq", "fun": lambda w: np.sum(w) - 1.0}


def _initial_weights(n: int, bounds: List[Tuple[float, float]]) -> np.ndarray:
    # Equal weight, then clip into bounds. If the clipped vector doesn't sum
    # to 1 (e.g. n=2 with max_weight=0.3 — equal weights of 0.5 get clipped
    # to 0.3 each, summing to 0.6) we distribute the residual into the slack
    # of each component instead of normalising. Plain renormalisation would
    # scale the clipped values back up above max_weight, handing SLSQP a
    # starting point that already violates the box constraints — the
    # optimiser then often fails ("Inequality constraints incompatible").
    w = np.full(n, 1.0 / n)
    lo = np.array([b[0] for b in bounds])
    hi = np.array([b[1] for b in bounds])
    w = np.clip(w, lo, hi)
    s = w.sum()
    # Push residual into headroom (hi - w_i) or pull from cushion (w_i - lo)
    # until sum ≈ 1 or no slack remains in any direction.
    for _ in range(64):
        diff = 1.0 - float(w.sum())
        if abs(diff) < 1e-9:
            break
        if diff > 0:
            slack = hi - w
            tot = float(slack.sum())
            if tot <= EPS:
                break
            w = w + slack * (diff / tot)
        else:
            cushion = w - lo
            tot = float(cushion.sum())
            if tot <= EPS:
                break
            w = w + cushion * (diff / tot)  # diff < 0 shrinks
        w = np.clip(w, lo, hi)
    return w


# ─────────────────────────────────────────────────────────────────────────────
# Markowitz mean-variance
# ─────────────────────────────────────────────────────────────────────────────

def markowitz_optimize(
    symbols: Sequence[str],
    objective: str = "max_sharpe",
    constraints: Optional[Dict] = None,
) -> Dict:
    """Classical Markowitz optimizer.

    `objective`: max_sharpe | min_vol | target_return | target_vol
    `constraints` keys: min_weight, max_weight, long_only, target_return,
                       target_vol, risk_free_rate, period.
    """
    if minimize is None:
        return {"error": "scipy.optimize not available"}
    cons = dict(constraints or {})
    period = cons.get("period", "2y")
    mean, cov, used = _estimate_cov_and_mean(symbols, period=period)
    if not used:
        return {"error": "Insufficient price history for any of the requested symbols"}

    n = len(used)
    rf = float(cons.get("risk_free_rate", DEFAULT_RF))
    min_w = float(cons.get("min_weight", 0.0))
    max_w = float(cons.get("max_weight", 1.0))
    long_only = bool(cons.get("long_only", True))
    bounds = _build_bounds(n, min_w, max_w, long_only)
    base_constraints: List[Dict] = [_sum_to_one_constraint()]

    # Define the objective function per mode.
    if objective == "max_sharpe":
        def fn(w: np.ndarray) -> float:
            ret, vol, _ = _portfolio_stats(w, mean, cov, rf)
            if vol < EPS:
                return 1e6
            return -(ret - rf) / vol
    elif objective == "min_vol":
        def fn(w: np.ndarray) -> float:
            return float(np.dot(w, cov @ w))
    elif objective == "target_return":
        target = float(cons.get("target_return", float(np.mean(mean))))
        base_constraints.append({"type": "eq", "fun": lambda w, t=target: float(np.dot(w, mean)) - t})

        def fn(w: np.ndarray) -> float:
            return float(np.dot(w, cov @ w))
    elif objective == "target_vol":
        target_v = float(cons.get("target_vol", 0.15))
        base_constraints.append(
            {"type": "eq", "fun": lambda w, t=target_v: math.sqrt(max(float(np.dot(w, cov @ w)), 0.0)) - t}
        )

        def fn(w: np.ndarray) -> float:
            return -float(np.dot(w, mean))
    else:
        return {"error": f"Unknown objective: {objective}"}

    w0 = _initial_weights(n, bounds)
    res = minimize(
        fn,
        w0,
        method="SLSQP",
        bounds=bounds,
        constraints=base_constraints,
        options={"maxiter": 500, "ftol": 1e-9},
    )
    if not res.success:
        # Fall back to the start point so the UI always sees a coherent payload.
        w_opt = w0
    else:
        w_opt = np.array(res.x, dtype=float)
        # Numerical cleanup: clip tiny negatives, renormalize.
        if long_only:
            w_opt = np.where(w_opt < 0, 0.0, w_opt)
        s = w_opt.sum()
        if s > EPS:
            w_opt = w_opt / s

    ret, vol, sharpe = _portfolio_stats(w_opt, mean, cov, rf)
    frontier = _efficient_frontier(mean, cov, bounds, rf)

    out = {
        "symbols": used,
        "weights": {s: float(round(w_opt[i], 6)) for i, s in enumerate(used)},
        "expected_return_pct": round(ret * 100, 4),
        "expected_vol_pct": round(vol * 100, 4),
        "sharpe": round(sharpe, 4),
        "risk_free_rate": rf,
        "objective": objective,
        "efficient_frontier": frontier,
        "converged": bool(res.success),
    }
    # For the equality-constrained objectives a failed solve usually means the
    # target is infeasible under the box constraints; we returned the feasible
    # start point. Surface that explicitly so the UI can explain the fallback
    # rather than presenting the naive start as an "optimal" portfolio.
    if not res.success and objective in ("target_return", "target_vol"):
        out["warning"] = "target infeasible under constraints; returning feasible start"
    return out


def _efficient_frontier(
    mean: np.ndarray,
    cov: np.ndarray,
    bounds: List[Tuple[float, float]],
    rf: float,
    n_points: int = 30,
) -> List[Dict]:
    """Sample the efficient frontier at ~n_points return levels and return
    [{ret, vol, sharpe}, ...]. Done by solving min_vol s.t. expected return
    equal to a grid value between min(mean) and max(mean)."""
    if mean.size == 0:
        return []
    lo_r, hi_r = float(mean.min()), float(mean.max())
    if hi_r <= lo_r:
        return []
    grid = np.linspace(lo_r, hi_r, n_points)
    n = mean.size
    out: List[Dict] = []
    for target in grid:
        cons = [
            _sum_to_one_constraint(),
            {"type": "eq", "fun": lambda w, t=target: float(np.dot(w, mean)) - t},
        ]
        w0 = _initial_weights(n, bounds)
        try:
            res = minimize(
                lambda w: float(np.dot(w, cov @ w)),
                w0,
                method="SLSQP",
                bounds=bounds,
                constraints=cons,
                options={"maxiter": 200, "ftol": 1e-8},
            )
            if not res.success:
                continue
            w = np.array(res.x)
            ret, vol, sharpe = _portfolio_stats(w, mean, cov, rf)
            out.append({"ret": round(ret * 100, 4), "vol": round(vol * 100, 4), "sharpe": round(sharpe, 4)})
        except Exception:
            continue
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Risk parity (equal risk contribution)
# ─────────────────────────────────────────────────────────────────────────────

def risk_parity(symbols: Sequence[str], period: str = "2y") -> Dict:
    """Solve for weights such that each asset contributes equal marginal
    volatility. We minimize the sum of squared deviations of contributions
    from their average (a smooth surrogate for the ERC condition)."""
    if minimize is None:
        return {"error": "scipy.optimize not available"}
    mean, cov, used = _estimate_cov_and_mean(symbols, period=period)
    if not used:
        return {"error": "Insufficient price history"}
    n = len(used)
    bounds = [(1e-4, 1.0)] * n  # strictly positive long-only weights

    def objective(w: np.ndarray) -> float:
        port_var = float(np.dot(w, cov @ w))
        if port_var < EPS:
            return 1e6
        mrc = cov @ w  # marginal risk contribution
        rc = w * mrc   # risk contribution per asset
        avg = rc.mean()
        return float(np.sum((rc - avg) ** 2))

    w0 = np.full(n, 1.0 / n)
    res = minimize(
        objective,
        w0,
        method="SLSQP",
        bounds=bounds,
        constraints=[_sum_to_one_constraint()],
        options={"maxiter": 1000, "ftol": 1e-12},
    )
    w = np.array(res.x if res.success else w0, dtype=float)
    # A diverged SLSQP can hand back NaN/inf in res.x; NaN survives the
    # (w < 0) clip below and poisons the whole weight vector. Sanitize first
    # and fall back to equal weights if nothing finite remains.
    w = np.where(np.isfinite(w), w, 0.0)
    w = np.where(w < 0, 0.0, w)
    s = w.sum()
    if s > EPS:
        w = w / s
    else:
        w = np.full(n, 1.0 / n)

    ret, vol, sharpe = _portfolio_stats(w, mean, cov, DEFAULT_RF)
    port_var = float(np.dot(w, cov @ w))
    if port_var > EPS:
        rc = w * (cov @ w) / port_var  # fraction of total variance
    else:
        rc = np.full(n, 1.0 / n)

    return {
        "symbols": used,
        "weights": {sym: float(round(w[i], 6)) for i, sym in enumerate(used)},
        "expected_return_pct": round(ret * 100, 4),
        "expected_vol_pct": round(vol * 100, 4),
        "sharpe": round(sharpe, 4),
        "marginal_contributions": {sym: float(round(rc[i] * 100, 4)) for i, sym in enumerate(used)},
        "converged": bool(res.success),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Black-Litterman
# ─────────────────────────────────────────────────────────────────────────────

def black_litterman(
    symbols: Sequence[str],
    mkt_caps: Dict[str, float],
    views: List[Dict],
    view_confidence: List[float],
    period: str = "2y",
    risk_aversion: float = 2.5,
    tau: float = 0.05,
) -> Dict:
    """Classical Black-Litterman blend.

    Steps:
      1. Implied equilibrium returns:  Pi = lambda * Sigma * w_mkt
      2. Encode views as P (k x n picking matrix), Q (k vector of view returns)
      3. Build Omega = diag( ((1-c)/c) * (P tau Sigma P').diag() ) per view
      4. Posterior mean:
            mu_bar = inv( inv(tau*Sigma) + P' inv(Omega) P )
                     * ( inv(tau*Sigma) Pi + P' inv(Omega) Q )
      5. Re-run mean-variance with the blended returns to land on weights.
    """
    mean, cov, used = _estimate_cov_and_mean(symbols, period=period)
    if not used:
        return {"error": "Insufficient price history"}
    n = len(used)
    sym_to_idx = {s: i for i, s in enumerate(used)}

    # ── Market weights from supplied caps ──
    # `used` symbols are upper-cased by _estimate_cov_and_mean; the caller may
    # key mkt_caps in original/lower case. Normalize so lookups don't all miss
    # and silently collapse to equal weights, discarding the supplied caps.
    caps_norm = {str(k).upper(): v for k, v in (mkt_caps or {}).items()}
    caps = np.array([float(caps_norm.get(s, 0.0)) for s in used], dtype=float)
    if caps.sum() <= 0:
        # Fallback to equal weights so we still return something useful.
        w_mkt = np.full(n, 1.0 / n)
    else:
        w_mkt = caps / caps.sum()

    Sigma = cov
    pi = risk_aversion * (Sigma @ w_mkt)  # implied excess returns (decimal)

    # ── Build P, Q, Omega from views ──
    P_rows: List[np.ndarray] = []
    Q_vals: List[float] = []
    confs: List[float] = []
    for v, c in zip(views or [], view_confidence or []):
        if not isinstance(v, dict):
            continue
        vtype = v.get("type", "absolute")
        v_syms = [s.upper() for s in v.get("symbols", [])]
        if not v_syms or any(s not in sym_to_idx for s in v_syms):
            continue
        try:
            q = float(v.get("expected_excess_return_pct", 0.0)) / 100.0
        except (TypeError, ValueError):
            continue
        row = np.zeros(n)
        if vtype == "absolute":
            row[sym_to_idx[v_syms[0]]] = 1.0
        elif vtype == "relative" and len(v_syms) >= 2:
            row[sym_to_idx[v_syms[0]]] = 1.0
            row[sym_to_idx[v_syms[1]]] = -1.0
        else:
            continue
        try:
            conf = float(c)
        except (TypeError, ValueError):
            conf = 0.5
        conf = min(max(conf, 1e-3), 0.9999)
        P_rows.append(row)
        Q_vals.append(q)
        confs.append(conf)

    if P_rows:
        P = np.vstack(P_rows)
        Q = np.array(Q_vals)
        # Omega: per-view uncertainty. The "(1-c)/c" trick scales the prior
        # view-variance (P tau Sigma P').diag() by the user's confidence so
        # 100% confidence collapses Omega and 0% confidence inflates it.
        ptau = P @ (tau * Sigma) @ P.T
        omega_diag = np.array([
            max(((1.0 - confs[i]) / confs[i]) * max(ptau[i, i], EPS), EPS)
            for i in range(len(confs))
        ])
        Omega = np.diag(omega_diag)

        inv_tau_sigma = np.linalg.pinv(tau * Sigma)
        inv_omega = np.linalg.pinv(Omega)
        A = inv_tau_sigma + P.T @ inv_omega @ P
        b = inv_tau_sigma @ pi + P.T @ inv_omega @ Q
        mu_bar = np.linalg.pinv(A) @ b
    else:
        mu_bar = pi.copy()

    # ── Solve for unconstrained optimal weights given blended returns ──
    # w* = (1 / lambda) * inv(Sigma) * mu_bar, then clip/normalize.
    try:
        w_star = np.linalg.solve(risk_aversion * Sigma, mu_bar)
    except np.linalg.LinAlgError:
        w_star = np.linalg.pinv(risk_aversion * Sigma) @ mu_bar
    w_star = np.where(w_star < 0, 0.0, w_star)  # long-only projection
    s = w_star.sum()
    if s > EPS:
        w_star = w_star / s
    else:
        w_star = np.full(n, 1.0 / n)

    ret, vol, sharpe = _portfolio_stats(w_star, mean, Sigma, DEFAULT_RF)

    return {
        "symbols": used,
        "weights": {sym: float(round(w_star[i], 6)) for i, sym in enumerate(used)},
        "blended_returns": {sym: float(round(mu_bar[i] * 100, 4)) for i, sym in enumerate(used)},
        "prior_returns": {sym: float(round(pi[i] * 100, 4)) for i, sym in enumerate(used)},
        "market_weights": {sym: float(round(w_mkt[i], 6)) for i, sym in enumerate(used)},
        "expected_return_pct": round(ret * 100, 4),
        "expected_vol_pct": round(vol * 100, 4),
        "sharpe": round(sharpe, 4),
        "risk_aversion": risk_aversion,
        "tau": tau,
        "n_views_applied": len(P_rows),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Comparison utility
# ─────────────────────────────────────────────────────────────────────────────

def compare_to_current(
    current_weights: Dict[str, float],
    optimal_weights: Dict[str, float],
    period: str = "2y",
) -> Dict:
    """Return per-symbol delta and an estimated tracking error in annualized
    volatility units. Tracking error uses the covariance of the *union* of
    symbols (symbols missing from one side count as zero-weight)."""
    syms = sorted(set(current_weights) | set(optimal_weights))
    delta = {s: float(round(optimal_weights.get(s, 0.0) - current_weights.get(s, 0.0), 6)) for s in syms}

    te_pct: Optional[float] = None
    try:
        _, cov, used = _estimate_cov_and_mean(syms, period=period)
        if used:
            d = np.array([delta.get(s, delta.get(s.upper(), 0.0)) for s in used])
            te = math.sqrt(max(float(d @ cov @ d), 0.0))
            te_pct = round(te * 100, 4)
    except Exception as e:
        log.warning("tracking-error estimate failed: %s", e)

    return {
        "delta": delta,
        "tracking_error_pct": te_pct,
        "symbols": syms,
    }


__all__ = [
    "markowitz_optimize",
    "risk_parity",
    "black_litterman",
    "compare_to_current",
    "_estimate_cov_and_mean",
]
