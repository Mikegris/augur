"""
Fama-French 5-factor + momentum exposure for any equity or portfolio.

Tells a user what their stock (or whole book) actually loads on in
risk-factor terms: market beta, size (SMB), value (HML), profitability
(RMW), investment (CMA), and momentum (MOM). Anything left over is
idiosyncratic alpha — the bet you're actually taking that isn't just
exposure to a known systematic factor.

Data source: Ken French's data library (Tuck/Dartmouth). Two daily CSVs
shipped inside zip files, free and keyless. We download, parse, merge,
and cache for 24h. The merged frame is also persisted to
`data/ff5_momentum_cache.csv` so subsequent runs work offline if the
Tuck site is unreachable.

Regression: OLS via numpy.linalg.lstsq on daily log returns minus the
daily risk-free rate. T-stats and p-values via the standard
(X'X)^-1 * sigma^2 formula. R² is computed against the demeaned
dependent variable. Annualized alpha = intercept * 252 * 100.

Python 3.9 compatible.
"""

from __future__ import annotations

import io
import logging
import math
import os
import time
import zipfile
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple
from urllib.request import Request, urlopen

import numpy as np

log = logging.getLogger("augur.factors")

try:
    import cache_store  # type: ignore
except Exception:  # pragma: no cover - cache_store should always be available
    cache_store = None  # type: ignore

try:
    import fetcher  # type: ignore
except Exception as _ferr:
    fetcher = None  # type: ignore
    log.warning("fetcher unavailable: %s", _ferr)


FF5_URL = (
    "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/"
    "F-F_Research_Data_5_Factors_2x3_daily_CSV.zip"
)
MOM_URL = (
    "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/"
    "F-F_Momentum_Factor_daily_CSV.zip"
)

_FACTOR_COLS = ["Mkt-RF", "SMB", "HML", "RMW", "CMA", "Mom"]
_CACHE_TTL = 24 * 3600
_HEADERS = {
    "User-Agent": (
        "AUGUR/1.0 wealth-tracker (research@augur-app.org)"
    ),
}

# Save raw merged CSV here for offline fallback.
_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
_CSV_PATH = os.path.join(_DATA_DIR, "ff5_momentum_cache.csv")

# Significance of t-stats — Student-t with large dof ~ normal. We compute
# two-sided p-values with a normal approximation rather than pulling scipy
# in just for that. With >250 observations the difference is <1%.
def _norm_sf(z: float) -> float:
    """Two-sided survival fn for a standard normal: P(|Z| > z)."""
    if z != z:  # NaN
        return float("nan")
    return math.erfc(abs(z) / math.sqrt(2.0))


# ───────────────────────── factor data ─────────────────────────


def _http_get(url: str, timeout: float = 20.0) -> bytes:
    req = Request(url, headers=_HEADERS)
    with urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _parse_french_csv(raw: bytes, value_cols: List[str]) -> Dict[str, List[float]]:
    """Parse one of Ken French's daily-factor CSVs.

    The files have a free-text preamble describing the methodology, then a
    blank line, then a header row of column names, then one daily row per
    trading day formatted as ``YYYYMMDD,float,float,...``. After the daily
    section some files include a second monthly/annual section preceded by
    another blank line — we stop at the first row whose first token isn't
    an 8-digit date.

    Returns ``{ "YYYY-MM-DD": [v1, v2, ...] }`` with values converted from
    percent to decimal.
    """
    text = raw.decode("latin-1", errors="replace")
    lines = text.splitlines()
    out: Dict[str, List[float]] = {}
    started = False
    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            if started:
                # Blank line after the daily block — French files often
                # append a monthly/annual section we don't want.
                break
            continue
        parts = [p.strip() for p in line.split(",")]
        first = parts[0]
        if len(first) == 8 and first.isdigit():
            try:
                d = datetime.strptime(first, "%Y%m%d").strftime("%Y-%m-%d")
                vals = [float(parts[i + 1]) / 100.0 for i in range(len(value_cols))]
                out[d] = vals
                started = True
            except (ValueError, IndexError):
                continue
        else:
            if started:
                break
    return out


def _unzip_single_csv(blob: bytes) -> bytes:
    """Return the single CSV file inside a Ken French ZIP."""
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        for name in zf.namelist():
            if name.lower().endswith(".csv"):
                return zf.read(name)
    raise RuntimeError("no CSV found inside French ZIP")


def _merge_frames(
    ff5: Dict[str, List[float]],
    mom: Dict[str, List[float]],
) -> Tuple[List[str], np.ndarray, np.ndarray]:
    """Intersect dates between the FF5 and momentum frames.

    Returns ``(dates, factor_matrix, rf_vector)`` where ``factor_matrix`` is
    shape (n, 6) with columns matching ``_FACTOR_COLS`` and ``rf_vector`` is
    the daily risk-free rate (already in decimal).
    """
    dates = sorted(d for d in ff5 if d in mom)
    n = len(dates)
    fac = np.zeros((n, 6), dtype=float)
    rf = np.zeros(n, dtype=float)
    for i, d in enumerate(dates):
        mkt, smb, hml, rmw, cma, r = ff5[d]
        # Defensive: take the first momentum column rather than unpacking
        # exactly one — if French ever appends a second column, `(m,) = ...`
        # would raise ValueError and abort the whole factor merge.
        m = mom[d][0]
        fac[i, 0] = mkt
        fac[i, 1] = smb
        fac[i, 2] = hml
        fac[i, 3] = rmw
        fac[i, 4] = cma
        fac[i, 5] = m
        rf[i] = r
    return dates, fac, rf


def _save_csv_cache(dates: List[str], fac: np.ndarray, rf: np.ndarray) -> None:
    try:
        os.makedirs(_DATA_DIR, exist_ok=True)
        with open(_CSV_PATH, "w", encoding="ascii") as fh:
            fh.write("date,Mkt-RF,SMB,HML,RMW,CMA,Mom,RF\n")
            for i, d in enumerate(dates):
                fh.write(
                    "%s,%.6f,%.6f,%.6f,%.6f,%.6f,%.6f,%.6f\n"
                    % (d, fac[i, 0], fac[i, 1], fac[i, 2],
                       fac[i, 3], fac[i, 4], fac[i, 5], rf[i])
                )
    except Exception as e:
        log.debug("ff5 csv cache write failed: %s", e)


def _load_csv_cache() -> Optional[Tuple[List[str], np.ndarray, np.ndarray]]:
    if not os.path.exists(_CSV_PATH):
        return None
    try:
        dates: List[str] = []
        rows: List[List[float]] = []
        rfs: List[float] = []
        with open(_CSV_PATH, "r", encoding="ascii") as fh:
            header = fh.readline()
            if not header.startswith("date,"):
                return None
            for line in fh:
                parts = line.strip().split(",")
                if len(parts) < 8:
                    continue
                dates.append(parts[0])
                rows.append([float(x) for x in parts[1:7]])
                rfs.append(float(parts[7]))
        if not dates:
            return None
        return dates, np.asarray(rows, dtype=float), np.asarray(rfs, dtype=float)
    except Exception as e:
        log.debug("ff5 csv cache load failed: %s", e)
        return None


def _load_factor_data() -> Tuple[List[str], np.ndarray, np.ndarray]:
    """Return ``(dates, factor_matrix, rf_vector)`` for all factors.

    Cached in ``cache_store`` for 24h. Falls back to the local CSV if the
    Tuck site is unreachable. Raises ``RuntimeError`` if neither path
    yields data.
    """
    ck = ("factors", "ff5_mom", "v1")

    def _fetch() -> Tuple[List[str], List[List[float]], List[float]]:
        try:
            ff5_zip = _http_get(FF5_URL)
            mom_zip = _http_get(MOM_URL)
            ff5_csv = _unzip_single_csv(ff5_zip)
            mom_csv = _unzip_single_csv(mom_zip)
            ff5 = _parse_french_csv(ff5_csv, ["Mkt-RF", "SMB", "HML", "RMW", "CMA", "RF"])
            mom = _parse_french_csv(mom_csv, ["Mom"])
            if not ff5 or not mom:
                raise RuntimeError("French CSV parse returned no rows")
            dates, fac, rf = _merge_frames(ff5, mom)
            _save_csv_cache(dates, fac, rf)
            # cache_store needs JSON-friendly payload; convert numpy → lists
            return dates, fac.tolist(), rf.tolist()
        except Exception as e:
            log.warning("French factor download failed: %s", e)
            local = _load_csv_cache()
            if local is None:
                return [], [], []
            d, f, r = local
            return d, f.tolist(), r.tolist()

    payload = None
    if cache_store is not None:
        try:
            payload = cache_store.coalesce(ck, _CACHE_TTL, _fetch)
        except Exception as e:
            log.warning("cache_store.coalesce failed for factors: %s", e)
            payload = _fetch()
    else:
        payload = _fetch()

    if not payload:
        raise RuntimeError("factor data unavailable")
    dates, fac_list, rf_list = payload
    if not dates:
        # Final fallback to local CSV — covers the case where coalesce
        # cached an empty failure for some other reason.
        local = _load_csv_cache()
        if local is None:
            raise RuntimeError("factor data unavailable (no local cache)")
        return local
    return dates, np.asarray(fac_list, dtype=float), np.asarray(rf_list, dtype=float)


# ───────────────────────── stock returns ─────────────────────────


def _stock_log_returns(symbol: str, period_years: int) -> Tuple[List[str], np.ndarray]:
    """Daily SIMPLE returns for a symbol over ~period_years.

    Returns ``(date_strings, simple_return_array)`` aligned so that
    ``return_array[i]`` corresponds to the trading day ``date_strings[i]``
    (i.e. close-to-close return ending on that day; the first calendar day
    is dropped because we have no prior close).

    WHY (Q7): this is the dependent variable in the FF5+MOM regression, and
    the Fama-French factor series are SIMPLE (arithmetic) returns. Regressing
    log stock returns on simple factor returns mixes units and biases the
    intercept (alpha) low by ~σ²/2 per day, i.e. ~σ²/2·252 annualized — a
    material drag for volatile names. Use simple returns to match the factors.
    (Name kept for call-site stability; it now returns simple returns.)
    """
    if fetcher is None:
        raise RuntimeError("fetcher module unavailable")
    period = f"{int(period_years)}y"
    bars = fetcher.get_chart_data(symbol, period, "1d")
    if not bars:
        return [], np.zeros(0, dtype=float)
    closes: List[float] = []
    dates: List[str] = []
    for b in bars:
        c = b.get("close")
        t = b.get("time")
        if c is None or t is None or c <= 0:
            continue
        try:
            d = datetime.fromtimestamp(int(t), tz=timezone.utc).strftime("%Y-%m-%d")
        except Exception:
            continue
        closes.append(float(c))
        dates.append(d)
    if len(closes) < 2:
        return [], np.zeros(0, dtype=float)
    arr = np.asarray(closes, dtype=float)
    # WHY (Q7): simple close-to-close returns (arr[i]/arr[i-1] - 1) to match
    # the Fama-French simple-return factors, not log returns.
    lr = arr[1:] / arr[:-1] - 1.0
    return dates[1:], lr


# ───────────────────────── OLS ─────────────────────────


def _ols(
    y: np.ndarray, X: np.ndarray
) -> Tuple[np.ndarray, np.ndarray, np.ndarray, float, np.ndarray]:
    """Plain OLS: returns (coef, t_stats, p_values, r_squared, residuals).

    ``X`` should already include an intercept column. With small (n,k) and
    well-behaved factor data, ``lstsq`` is more numerically stable than
    forming X'X directly.

    The numpy 2.0 BLAS path emits spurious ``divide by zero / overflow``
    RuntimeWarnings from inside ``matmul`` even on perfectly finite inputs;
    we silence them locally because they correspond to no real numerical
    issue (the returned ``coef`` and ``resid`` arrays are finite — we assert
    that below by checking ``ss_tot``/``sigma2``).
    """
    n, k = X.shape
    with np.errstate(divide="ignore", over="ignore", invalid="ignore"):
        coef, *_ = np.linalg.lstsq(X, y, rcond=None)
        resid = y - X @ coef
        dof = max(n - k, 1)
        sigma2 = float(resid @ resid) / dof
        try:
            XtX_inv = np.linalg.pinv(X.T @ X)
        except np.linalg.LinAlgError:
            XtX_inv = np.linalg.pinv(X.T @ X + 1e-10 * np.eye(k))
        se = np.sqrt(np.clip(np.diag(XtX_inv) * sigma2, 0.0, None))
        t = np.divide(coef, se, out=np.full_like(coef, np.nan), where=se > 0)
        p = np.asarray([_norm_sf(float(tt)) if tt == tt else float("nan") for tt in t])
        yc = y - float(np.mean(y))
        ss_tot = float(yc @ yc)
        ss_res = float(resid @ resid)
        r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
    return coef, t, p, r2, resid


# ───────────────────────── public API ─────────────────────────


def _empty_envelope(symbol: str, period_years: int, msg: str) -> dict:
    return {
        "symbol": symbol.upper(),
        "period_years": int(period_years),
        "error": msg,
    }


def _exposures_dict(coef: np.ndarray, t: np.ndarray, p: np.ndarray) -> dict:
    """Build the per-factor dict, skipping the intercept column (coef[0])."""
    out: dict = {}
    for i, name in enumerate(_FACTOR_COLS):
        b = float(coef[i + 1])
        tt = float(t[i + 1])
        pp = float(p[i + 1])
        out[name] = {
            "beta": round(b, 4),
            "t_stat": round(tt, 3) if tt == tt else None,
            "p_value": round(pp, 4) if pp == pp else None,
        }
    return out


def _regress(
    stock_dates: List[str],
    stock_ret: np.ndarray,
    fac_dates: List[str],
    fac: np.ndarray,
    rf: np.ndarray,
) -> Optional[Tuple[np.ndarray, np.ndarray, np.ndarray, float, int, str, np.ndarray]]:
    """Align stock + factor frames on date, run OLS, return (coef,t,p,R2,n,as_of,resid)."""
    fac_index = {d: i for i, d in enumerate(fac_dates)}
    aligned_y: List[float] = []
    aligned_X: List[List[float]] = []
    last_date = ""
    for d, r in zip(stock_dates, stock_ret):
        idx = fac_index.get(d)
        if idx is None:
            continue
        excess = float(r) - float(rf[idx])
        aligned_y.append(excess)
        aligned_X.append(list(fac[idx]))
        last_date = d
    if len(aligned_y) < 60:
        return None
    y = np.asarray(aligned_y, dtype=float)
    X = np.hstack([np.ones((len(aligned_y), 1)), np.asarray(aligned_X, dtype=float)])
    coef, t, p, r2, resid = _ols(y, X)
    # idiosyncratic vol: annualised residual standard deviation
    n = len(aligned_y)
    return coef, t, p, r2, n, last_date, resid


def factor_exposure(symbol: str, period_years: int = 5) -> dict:
    """Regress one stock's excess returns on the FF5 + momentum factors.

    Returns a dict with per-factor beta/t/p, annualised alpha, R², and
    idiosyncratic vol. Cached in ``cache_store`` for 24h per (symbol,
    period_years) pair so the regression itself isn't re-run repeatedly.
    """
    sym = (symbol or "").upper().strip()
    if not sym:
        return _empty_envelope(symbol or "", period_years, "missing symbol")
    period_years = int(period_years) if period_years else 5
    period_years = max(1, min(period_years, 30))

    def _compute() -> dict:
        try:
            fac_dates, fac, rf = _load_factor_data()
        except Exception as e:
            return _empty_envelope(sym, period_years, f"factor data error: {e}")
        try:
            stock_dates, stock_ret = _stock_log_returns(sym, period_years)
        except Exception as e:
            return _empty_envelope(sym, period_years, f"price history error: {e}")
        if not stock_dates:
            return _empty_envelope(sym, period_years, "no price history")
        res = _regress(stock_dates, stock_ret, list(fac_dates), fac, rf)
        if res is None:
            return _empty_envelope(sym, period_years, "insufficient overlap with factor history")
        coef, t, p, r2, n, last_date, resid_arr = res
        alpha_daily = float(coef[0])
        # residual stdev for idiosyncratic vol — reuse the regression's own
        # residuals (same alignment) so idio vol is exactly consistent with
        # the reported R²/coefficients.
        idio_vol_pct = float(np.std(resid_arr, ddof=1) * math.sqrt(252.0) * 100.0)
        return {
            "symbol": sym,
            "period_years": period_years,
            "n_obs": int(n),
            "exposures": _exposures_dict(coef, t, p),
            "alpha_annual_pct": round(alpha_daily * 252.0 * 100.0, 3),
            "alpha_t_stat": round(float(t[0]), 3) if t[0] == t[0] else None,
            "alpha_p_value": round(float(p[0]), 4) if p[0] == p[0] else None,
            "r_squared": round(float(r2), 4),
            "idiosyncratic_vol_pct": round(idio_vol_pct, 3),
            "as_of": last_date,
        }

    if cache_store is None:
        return _compute()
    try:
        return cache_store.coalesce(("factors", sym, period_years), _CACHE_TTL, _compute)
    except Exception as e:
        log.warning("cache_store.coalesce(factor_exposure) failed: %s", e)
        return _compute()


def portfolio_factor_exposure(holdings: List[dict], period_years: int = 5) -> dict:
    """Factor decomposition for a multi-position portfolio.

    ``holdings`` is a list of ``{"symbol": str, "market_value": number}``.
    We compute exposures two complementary ways:

    1. Position-weighted average of each holding's individual factor betas
       (with their respective alphas blended the same way).
    2. A direct OLS on the position-weighted return series — this gives a
       true portfolio-level R² and idiosyncratic vol (the weighted average
       of individual residual vols would understate it whenever residuals
       are correlated).

    The per-factor block in the response uses the direct-regression
    estimates so betas, t-stats, and p-values are consistent with the
    portfolio R². ``weighted_exposures`` is also returned so the UI can
    show the simpler decomposition for sanity-checking.
    """
    if not holdings:
        return {"error": "no holdings", "period_years": int(period_years)}
    period_years = int(period_years) if period_years else 5
    period_years = max(1, min(period_years, 30))

    # Normalise weights (drop non-positive market values).
    cleaned: List[Tuple[str, float]] = []
    for h in holdings:
        try:
            sym = str(h.get("symbol", "")).upper().strip()
            mv = float(h.get("market_value") or 0.0)
        except Exception:
            continue
        if not sym or mv <= 0:
            continue
        cleaned.append((sym, mv))
    if not cleaned:
        return {"error": "no positive-value holdings", "period_years": period_years}
    total = sum(mv for _, mv in cleaned)
    weights = {sym: mv / total for sym, mv in cleaned}

    try:
        fac_dates, fac, rf = _load_factor_data()
    except Exception as e:
        return {"error": f"factor data error: {e}", "period_years": period_years}
    fac_index = {d: i for i, d in enumerate(fac_dates)}

    # Per-holding exposures + per-symbol return series for the direct regression.
    per_holding: List[dict] = []
    return_series: Dict[str, Dict[str, float]] = {}  # symbol → {date: log_return}
    weighted = {name: 0.0 for name in _FACTOR_COLS}
    weighted_alpha = 0.0
    weight_sum = 0.0
    for sym, _mv in cleaned:
        w = weights[sym]
        single = factor_exposure(sym, period_years)
        per_holding.append({"symbol": sym, "weight": round(w, 6), "result": single})
        if "error" in single:
            continue
        weight_sum += w
        for name in _FACTOR_COLS:
            beta = single.get("exposures", {}).get(name, {}).get("beta")
            if beta is not None:
                weighted[name] += w * float(beta)
        a = single.get("alpha_annual_pct")
        if a is not None:
            weighted_alpha += w * float(a)
        # Pull the daily return series so we can build a weighted portfolio series.
        try:
            sd, sr = _stock_log_returns(sym, period_years)
            return_series[sym] = {d: float(r) for d, r in zip(sd, sr)}
        except Exception as e:
            log.debug("portfolio: returns failed for %s: %s", sym, e)

    # Build the weighted-portfolio excess return series on dates common to
    # the factor frame AND every available holding's history.
    if return_series:
        all_dates = set(fac_index.keys())
        for sym in return_series:
            all_dates &= set(return_series[sym].keys())
        ordered = sorted(all_dates)
    else:
        ordered = []

    direct_block: dict = {}
    if len(ordered) >= 60 and weight_sum > 0:
        port_excess: List[float] = []
        X_rows: List[List[float]] = []
        # Re-normalize across symbols that actually have history (drops
        # holdings whose chart fetch failed — otherwise the portfolio
        # series silently understates weight).
        eff_syms = list(return_series.keys())
        eff_total = sum(weights[s] for s in eff_syms) or 1.0
        eff_weights = {s: weights[s] / eff_total for s in eff_syms}
        for d in ordered:
            idx = fac_index[d]
            pr = sum(eff_weights[s] * return_series[s][d] for s in eff_syms)
            port_excess.append(pr - float(rf[idx]))
            X_rows.append(list(fac[idx]))
        y = np.asarray(port_excess, dtype=float)
        X = np.hstack([np.ones((len(port_excess), 1)), np.asarray(X_rows, dtype=float)])
        coef, t, p, r2, resid = _ols(y, X)
        idio_vol_pct = float(np.std(resid, ddof=1) * math.sqrt(252.0) * 100.0)
        direct_block = {
            "n_obs": int(len(port_excess)),
            "exposures": _exposures_dict(coef, t, p),
            "alpha_annual_pct": round(float(coef[0]) * 252.0 * 100.0, 3),
            "alpha_t_stat": round(float(t[0]), 3) if t[0] == t[0] else None,
            "alpha_p_value": round(float(p[0]), 4) if p[0] == p[0] else None,
            "r_squared": round(float(r2), 4),
            "idiosyncratic_vol_pct": round(idio_vol_pct, 3),
            "as_of": ordered[-1],
            "effective_weights": {s: round(w, 6) for s, w in eff_weights.items()},
        }

    weighted_block = {
        "exposures": {name: round(weighted[name], 4) for name in _FACTOR_COLS},
        "alpha_annual_pct": round(weighted_alpha, 3),
        "coverage_weight": round(weight_sum, 6),
    }

    if direct_block:
        out = {
            "period_years": period_years,
            "n_holdings": len(cleaned),
            **direct_block,
            "weighted_exposures": weighted_block,
            "holdings": per_holding,
        }
    else:
        # No direct regression possible — at least return the weighted view
        # so the UI has something to show.
        if weight_sum == 0:
            return {
                "error": "no holding had a successful factor regression",
                "period_years": period_years,
                "n_holdings": len(cleaned),
                "holdings": per_holding,
            }
        out = {
            "period_years": period_years,
            "n_holdings": len(cleaned),
            "exposures": {
                name: {"beta": round(weighted[name], 4), "t_stat": None, "p_value": None}
                for name in _FACTOR_COLS
            },
            "alpha_annual_pct": round(weighted_alpha, 3),
            "alpha_t_stat": None,
            "alpha_p_value": None,
            "r_squared": None,
            "idiosyncratic_vol_pct": None,
            "as_of": "",
            "weighted_exposures": weighted_block,
            "holdings": per_holding,
            "note": "direct portfolio regression unavailable; showing weighted-average exposures only",
        }
    return out


__all__ = [
    "factor_exposure",
    "portfolio_factor_exposure",
]
