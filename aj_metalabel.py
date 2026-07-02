"""AJTA — meta-labeling layer (López-de-Prado-style P(profitable | setup)).

A calibrated probability model trained on the agent's OWN realized CLOSED trades.
It learns "given this setup, how often did I actually make money?" and exposes a
single number — P(profit) — that the operator/decision path MAY consult to FILTER
or SHRINK an entry. It NEVER places, sizes-up, or unblocks a trade on its own: the
fail-closed risk gate is always upstream.

Design choices (all deliberate, see MEMORY v3.2 / aj_config metalabel_* block):

  * Walk-forward PROMOTION GATE. The model only influences live decisions after it
    beats a baseline OUT-OF-SAMPLE. We split the labeled trades by TIME (older 70%
    train, newer 30% holdout), fit on the train slice, and measure holdout ROC-AUC.
    `promoted = (n >= min_samples) and (oos_auc >= min_auc)`. A random split would
    leak future information into the past, so the split is strictly time-ordered.

  * JSON persistence, NOT pickle. The deployed model is a plain standardizer
    (mean/std) + logistic (coef/intercept) — a handful of floats. We store them as
    JSON via aj_db.set_setting_raw("__aj_metalabel_model", ...). This is
    interpretable, version-safe across sklearn upgrades, and bundle-safe for the
    py2app build (no pickle code-execution surface, no class-path coupling).

  * sklearn for TRAINING only. Inference (predict_proba) is pure-numpy from the
    stored coefficients — 1/(1+exp(-(coef·z + b))). A model trained elsewhere (or
    on an older sklearn) still scores correctly with no sklearn import at inference.

  * Fail-OPEN everywhere. Every public function is wrapped and never raises:
    train -> {error}, predict_proba -> None, is_promoted/status -> promoted=False.
    A broken meta-label model can only ever DECLINE to add signal; it can never
    crash the cycle nor weaken a guard.

Contract against the sibling `aj_features` (built in parallel — we code to this
public API only, importing nothing of its internals):

    aj_features.training_set() -> (X, y, feature_names)
        X: list[list[float]] (equal length rows, already imputed)
        y: list[int] in {0,1}
        feature_names: list[str] (ordered, matches each row's columns)
    aj_features.build_labels(lookback_days=365) -> dict
    aj_features.label_stats() -> dict   # carries at least {'n': int}
"""
from __future__ import annotations

import json
import logging
import math
import time
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("augur.aj_metalabel")

# Persisted-model setting key. `__`-prefixed so get_settings() hides it from the
# typed-config surface; we round-trip it through aj_db.{get,set}_setting_raw.
_MODEL_KEY = "__aj_metalabel_model"
# Tracks the label count observed at the last successful train(), so maybe_retrain
# can cheaply decide whether enough NEW closed trades have accrued to retrain.
_LASTN_KEY = "__aj_metalabel_last_train_n"

# Time-ordered walk-forward split: oldest TRAIN_FRACTION used to fit, the rest
# (newest) held out for the honest out-of-sample AUC.
_TRAIN_FRACTION = 0.70

# Neutral imputation value for a feature absent at inference. After standardization
# a stored-mean value maps to 0.0; we impute the stored MEAN (pre-standardization)
# so a missing feature contributes nothing to the logit — matching aj_features'
# "missing already imputed to neutral" convention.


# ── config / persistence helpers ───────────────────────────────────────────────

def _cfg(cfg: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Resolve the typed config, fail-open to bare defaults if config is broken."""
    if cfg is not None:
        return cfg
    try:
        import aj_config
        return aj_config.get_config()
    except Exception:
        return {
            "metalabel_min_samples": 50,
            "metalabel_min_auc": 0.55,
            "metalabel_prob_threshold": 0.5,
            "metalabel_retrain_min_new": 10,
            "metalabel_enabled": False,
        }


def _load_model() -> Optional[Dict[str, Any]]:
    """Load + parse the persisted JSON model. None on absence or any parse error."""
    try:
        import aj_db
        raw = aj_db.get_setting_raw(_MODEL_KEY)
        if not raw:
            return None
        m = json.loads(raw)
        return m if isinstance(m, dict) else None
    except Exception:
        log.debug("metalabel: model load failed", exc_info=True)
        return None


def _save_model(model: Dict[str, Any]) -> None:
    try:
        import aj_db
        aj_db.set_setting_raw(_MODEL_KEY, json.dumps(model))
    except Exception:
        log.debug("metalabel: model save failed", exc_info=True)


def _get_last_train_n() -> int:
    try:
        import aj_db
        raw = aj_db.get_setting_raw(_LASTN_KEY)
        return int(float(raw)) if raw is not None else -1
    except Exception:
        return -1


def _set_last_train_n(n: int) -> None:
    try:
        import aj_db
        aj_db.set_setting_raw(_LASTN_KEY, str(int(n)))
    except Exception:
        pass


def _trade_windows(target: str, n: int) -> Optional[List[Tuple[Any, Any]]]:
    """Per-row (opened_at, closed_at) holding windows, aligned to training_set().

    aj_features.training_set() reads aj_trade_labels ORDER BY closed_at ASC,
    id ASC (and, for the 'alpha' target, skips rows without a usable
    beat_benchmark). We replay the SAME query + skip rule here so windows[i]
    describes X[i]. Fail-open: returns None (→ purging is skipped) whenever the
    query fails or the row count doesn't match the matrix — never guess an
    alignment."""
    try:
        import aj_db
        rows = aj_db.query(
            "SELECT opened_at, closed_at, beat_benchmark FROM aj_trade_labels "
            "ORDER BY closed_at ASC, id ASC")
    except Exception:
        log.debug("metalabel: trade-window query failed", exc_info=True)
        return None
    use_alpha = str(target or "profit").lower() == "alpha"
    wins: List[Tuple[Any, Any]] = []
    for r in rows:
        if use_alpha:
            bb = r.get("beat_benchmark")
            if bb is None:
                continue
            try:
                int(bb)
            except Exception:
                continue
        wins.append((r.get("opened_at"), r.get("closed_at")))
    return wins if len(wins) == n else None


# ── training ────────────────────────────────────────────────────────────────────

def train(cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Train (or re-train) the meta-label model from the agent's realized trades.

    Pulls aj_features.training_set(); if there are too few labels or only one
    class is present, persists a NON-promoted stub (with a reason) and returns.
    Otherwise: time-orders the data, fits StandardScaler+LogisticRegression on the
    OLDER 70%, scores ROC-AUC on the NEWER 30% holdout (the honest walk-forward
    out-of-sample metric), then REFITS on ALL data for the deployed coefficients
    (standard practice — you ship the model trained on everything but report the
    held-out skill). Promotes iff n >= min_samples AND oos_auc >= min_auc.

    Returns {trained, n, oos_auc, promoted, base_rate, reason}. Never raises.
    """
    c = _cfg(cfg)
    min_samples = int(c.get("metalabel_min_samples", 50))
    min_auc = float(c.get("metalabel_min_auc", 0.55))

    target = str(c.get("metalabel_target", "profit") or "profit").lower()
    if target not in ("profit", "alpha"):
        target = "profit"
    try:
        import aj_features
        X, y, feature_names = aj_features.training_set(target=target)
    except Exception as e:
        log.debug("metalabel.train: training_set unavailable", exc_info=True)
        return {"trained": False, "n": 0, "oos_auc": None, "promoted": False,
                "base_rate": None, "target": target,
                "reason": "training_set error: {}".format(e), "error": str(e)}

    try:
        X = [[float(v) for v in row] for row in (X or [])]
        y = [int(v) for v in (y or [])]
        feature_names = [str(f) for f in (feature_names or [])]
        n = len(X)
        base_rate = (sum(y) / n) if n else None

        def _persist_stub(reason: str) -> Dict[str, Any]:
            model = {
                "feature_names": feature_names, "mean": [], "std": [],
                "coef": [], "intercept": 0.0, "oos_auc": None,
                "n_train": n, "base_rate": base_rate, "target": target,
                "trained_at": time.time(), "promoted": False, "reason": reason,
            }
            _save_model(model)
            _set_last_train_n(n)
            return {"trained": True, "n": n, "oos_auc": None, "promoted": False,
                    "base_rate": base_rate, "target": target, "reason": reason}

        if n < min_samples:
            return _persist_stub(
                "insufficient samples: {} < min {}".format(n, min_samples))
        classes = set(y)
        if len(classes) < 2:
            return _persist_stub(
                "single class present ({}): cannot train".format(
                    next(iter(classes)) if classes else "none"))
        if not feature_names or any(len(row) != len(feature_names) for row in X):
            return _persist_stub("malformed feature matrix")

        import numpy as np
        from sklearn.linear_model import LogisticRegression
        from sklearn.preprocessing import StandardScaler
        from sklearn.metrics import roc_auc_score

        Xa = np.asarray(X, dtype=float)
        ya = np.asarray(y, dtype=int)

        # ── walk-forward split: rows are assumed time-ordered (oldest first) by
        # aj_features. Train on the older head, hold out the newer tail. ──────────
        split = int(math.floor(n * _TRAIN_FRACTION))
        split = max(1, min(split, n - 1))   # guarantee both slices non-empty
        X_tr, X_ho = Xa[:split], Xa[split:]
        y_tr, y_ho = ya[:split], ya[split:]

        # ── PURGE at the split boundary (López-de-Prado-style). Ordering is by
        # closed_at, so every train trade CLOSES on/before the boundary — but a
        # holdout trade that OPENED before the last train trade closed has a
        # holding window overlapping the train data: its features/label share
        # information the model was fit on, inflating the "out-of-sample" AUC.
        # Drop those straddling holdout rows before scoring. Fail-open: if the
        # windows can't be aligned to the matrix (or timestamps don't parse),
        # skip purging rather than break training. ────────────────────────────────
        n_purged = 0
        windows = _trade_windows(target, n)
        if windows is not None:
            try:
                import aj_db
                boundary = aj_db.parse_iso(windows[split - 1][1])
                if boundary is not None:
                    keep = []
                    for i in range(split, n):
                        opened = aj_db.parse_iso(windows[i][0])
                        # unparsable opened_at → keep (can't prove a straddle)
                        if opened is not None and opened < boundary:
                            n_purged += 1
                        else:
                            keep.append(i - split)
                    if n_purged:
                        X_ho, y_ho = X_ho[keep], y_ho[keep]
                        log.debug("metalabel: purged %d boundary-straddling "
                                  "holdout trades", n_purged)
            except Exception:
                log.debug("metalabel: purge failed; scoring unpurged holdout",
                          exc_info=True)

        oos_auc: Optional[float] = None
        if len(set(y_tr.tolist())) >= 2 and len(set(y_ho.tolist())) >= 2:
            try:
                sc = StandardScaler().fit(X_tr)
                clf = LogisticRegression(max_iter=1000)
                clf.fit(sc.transform(X_tr), y_tr)
                proba_ho = clf.predict_proba(sc.transform(X_ho))[:, 1]
                oos_auc = float(roc_auc_score(y_ho, proba_ho))
            except Exception:
                log.debug("metalabel.train: holdout scoring failed", exc_info=True)
                oos_auc = None
        # If the holdout (or train) slice was single-class we cannot measure OOS
        # skill honestly — leave oos_auc None, which fails the promotion gate.

        # ── deployed model: refit StandardScaler + LogisticRegression on ALL data ─
        scaler = StandardScaler().fit(Xa)
        model_clf = LogisticRegression(max_iter=1000)
        model_clf.fit(scaler.transform(Xa), ya)

        mean = [float(v) for v in scaler.mean_.tolist()]
        # StandardScaler stores scale_ (==std, with zeros replaced by 1.0); persist
        # it directly so inference divides by the identical denominator.
        std = [float(v) for v in scaler.scale_.tolist()]
        coef = [float(v) for v in model_clf.coef_[0].tolist()]
        intercept = float(model_clf.intercept_[0])

        promoted = bool(n >= min_samples and oos_auc is not None
                        and oos_auc >= min_auc)
        if oos_auc is None:
            reason = "no honest out-of-sample AUC (single-class holdout)"
        elif promoted:
            reason = "promoted: oos_auc {:.3f} >= min {:.3f}".format(oos_auc, min_auc)
        else:
            reason = "oos_auc {:.3f} < min {:.3f}".format(oos_auc, min_auc)

        model = {
            "feature_names": feature_names,
            "mean": mean, "std": std,
            "coef": coef, "intercept": intercept,
            "oos_auc": oos_auc, "n_train": n, "base_rate": base_rate, "target": target,
            "trained_at": time.time(), "promoted": promoted, "reason": reason,
        }
        _save_model(model)
        _set_last_train_n(n)
        return {"trained": True, "n": n, "oos_auc": oos_auc, "promoted": promoted,
                "base_rate": base_rate, "target": target, "reason": reason}
    except Exception as e:
        log.debug("metalabel.train: unexpected failure", exc_info=True)
        return {"trained": False, "n": 0, "oos_auc": None, "promoted": False,
                "base_rate": None, "reason": "train error: {}".format(e),
                "error": str(e)}


# ── inference (pure numpy, no sklearn) ──────────────────────────────────────────

def predict_proba(features: Dict[str, Any],
                  cfg: Optional[Dict[str, Any]] = None) -> Optional[float]:
    """P(profit | setup) for a single setup, in pure numpy from the stored coef.

    Returns None when there is no PROMOTED model (the gate hasn't been cleared) or
    on ANY error — the caller treats None as "no meta-label opinion" and proceeds
    on its other signals. Features are aligned to the stored feature_names; a
    missing feature is imputed to its stored MEAN (neutral — contributes 0 to the
    standardized logit). The result is clamped to [0, 1].
    """
    try:
        model = _load_model()
        if not model or not model.get("promoted"):
            return None
        feature_names = model.get("feature_names") or []
        mean = model.get("mean") or []
        std = model.get("std") or []
        coef = model.get("coef") or []
        intercept = float(model.get("intercept", 0.0))
        if not (len(feature_names) == len(mean) == len(std) == len(coef)) \
                or not feature_names:
            return None

        feats = features if isinstance(features, dict) else {}
        import numpy as np
        # Build the aligned, imputed row.
        x = np.empty(len(feature_names), dtype=float)
        for i, name in enumerate(feature_names):
            v = feats.get(name)
            if v is None:
                x[i] = float(mean[i])          # neutral impute
            else:
                try:
                    fv = float(v)
                    x[i] = fv if fv == fv else float(mean[i])  # NaN -> neutral
                except Exception:
                    x[i] = float(mean[i])
        m = np.asarray(mean, dtype=float)
        s = np.asarray(std, dtype=float)
        s = np.where(s == 0.0, 1.0, s)         # guard divide-by-zero (matches scaler)
        z = (x - m) / s
        logit = float(np.dot(np.asarray(coef, dtype=float), z) + intercept)
        # numerically stable logistic
        if logit >= 0:
            p = 1.0 / (1.0 + math.exp(-logit))
        else:
            ez = math.exp(logit)
            p = ez / (1.0 + ez)
        return float(min(1.0, max(0.0, p)))
    except Exception:
        log.debug("metalabel.predict_proba failed", exc_info=True)
        return None


# ── promotion status / retrain / status ─────────────────────────────────────────

def is_promoted(cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Whether a model has cleared the walk-forward promotion gate.
    Fail-open: any error reports promoted=False."""
    try:
        model = _load_model()
        if not model:
            return {"promoted": False, "oos_auc": None, "n": 0,
                    "reason": "no model trained yet"}
        return {
            "promoted": bool(model.get("promoted")),
            "oos_auc": model.get("oos_auc"),
            "n": int(model.get("n_train", 0) or 0),
            "reason": str(model.get("reason", "")),
        }
    except Exception:
        log.debug("metalabel.is_promoted failed", exc_info=True)
        return {"promoted": False, "oos_auc": None, "n": 0,
                "reason": "is_promoted error"}


def maybe_retrain(cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Cheap incremental-retrain check. Re-labels best-effort, then retrains only
    if the labeled-trade count has grown by >= metalabel_retrain_min_new since the
    last train. Otherwise a no-op. Never raises."""
    c = _cfg(cfg)
    min_new = int(c.get("metalabel_retrain_min_new", 10))
    try:
        import aj_features
        # Best-effort: label any newly-closed trades before counting.
        try:
            aj_features.build_labels()
        except Exception:
            log.debug("metalabel.maybe_retrain: build_labels failed", exc_info=True)

        try:
            stats = aj_features.label_stats() or {}
            cur_n = int(stats.get("n", 0) or 0)
        except Exception:
            return {"retrained": False, "reason": "label_stats unavailable",
                    "n": None, "last_n": _get_last_train_n()}

        last_n = _get_last_train_n()
        if last_n < 0:
            # Never trained: train now if there's any data at all.
            res = train(c)
            return {"retrained": True, "reason": "first train", "n": cur_n,
                    "last_n": last_n, "train": res}
        if cur_n - last_n >= min_new:
            res = train(c)
            return {"retrained": True,
                    "reason": "{} new labels >= {}".format(cur_n - last_n, min_new),
                    "n": cur_n, "last_n": last_n, "train": res}
        return {"retrained": False,
                "reason": "only {} new labels (< {})".format(
                    max(0, cur_n - last_n), min_new),
                "n": cur_n, "last_n": last_n}
    except Exception as e:
        log.debug("metalabel.maybe_retrain failed", exc_info=True)
        return {"retrained": False, "reason": "maybe_retrain error: {}".format(e),
                "n": None, "last_n": _get_last_train_n()}


def status(cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Combined view for the status endpoint / Insights panel: the persisted
    model's promotion state + AUC + n, the configured gate thresholds, the live
    enabled flag, and the current label_stats. Fail-open to a safe empty view."""
    c = _cfg(cfg)
    out: Dict[str, Any] = {
        "enabled": bool(c.get("metalabel_enabled", False)),
        "min_samples": int(c.get("metalabel_min_samples", 50)),
        "min_auc": float(c.get("metalabel_min_auc", 0.55)),
        "prob_threshold": float(c.get("metalabel_prob_threshold", 0.5)),
        "target": str(c.get("metalabel_target", "profit") or "profit").lower(),
        "benchmark": str(c.get("metalabel_benchmark", "SPY") or "SPY").upper(),
        "promoted": False, "oos_auc": None, "n_train": 0,
        "base_rate": None, "trained_at": None, "reason": "no model trained yet",
        "label_stats": {},
    }
    try:
        model = _load_model()
        if model:
            out.update({
                "promoted": bool(model.get("promoted")),
                "oos_auc": model.get("oos_auc"),
                "n_train": int(model.get("n_train", 0) or 0),
                "base_rate": model.get("base_rate"),
                "trained_at": model.get("trained_at"),
                "reason": str(model.get("reason", "")),
                "n_features": len(model.get("feature_names") or []),
            })
    except Exception:
        log.debug("metalabel.status: model read failed", exc_info=True)
    try:
        import aj_features
        out["label_stats"] = aj_features.label_stats() or {}
    except Exception:
        log.debug("metalabel.status: label_stats failed", exc_info=True)
    return out
