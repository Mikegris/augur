"""Offline tests for aj_metalabel — the meta-labeling P(profit | setup) layer.

Fully isolated: a fresh temp DB, the sibling `aj_features` contract replaced by a
fake module emitting a SEPARABLE synthetic dataset (high feature x -> profit), no
network and no real trade history. Standalone runner: prints PASS/FAILED per check
and sys.exit(1) on any failure.

What we assert:
  * train() PROMOTES a separable dataset (oos_auc >= threshold) and persists the
    JSON model with the documented shape.
  * predict_proba scores a profit-leaning setup higher than a loss-leaning one.
  * pure-numpy inference matches sklearn's predict_proba within tolerance (the core
    "inference works without sklearn" guarantee).
  * < min_samples -> not promoted; single-class -> not promoted.
  * predict_proba returns None when no promoted model exists.
  * is_promoted / status fail open.
"""
import json
import os
import sys
import tempfile

os.environ["AUGUR_DB_PATH"] = tempfile.mktemp(suffix=".db")

import database  # noqa: E402
database.init_db()

# ── fake aj_features (the sibling contract), injected BEFORE aj_metalabel loads ──
import types  # noqa: E402

_fake = types.ModuleType("aj_features")
# mutable container the tests rewrite per-case
_fake._STATE = {"X": [], "y": [], "names": ["x", "noise"], "label_n": 0,
                "build_calls": 0}


def _training_set(target="profit"):
    s = _fake._STATE
    return (list(s["X"]), list(s["y"]), list(s["names"]))


def _build_labels(lookback_days=365):
    _fake._STATE["build_calls"] += 1
    return {"labeled": _fake._STATE["label_n"]}


def _label_stats():
    return {"n": _fake._STATE["label_n"]}


_fake.training_set = _training_set
_fake.build_labels = _build_labels
_fake.label_stats = _label_stats
sys.modules["aj_features"] = _fake

import aj_db  # noqa: E402
import aj_metalabel as ml  # noqa: E402

PASS = []
FAIL = []


def ok(msg):
    PASS.append(msg)
    print("  [OK] " + msg)


def check(cond, msg):
    if cond:
        ok(msg)
    else:
        FAIL.append(msg)
        print("  [XX] " + msg)


# ── synthetic dataset builders ──────────────────────────────────────────────────

def _separable(n=120):
    """Time-ordered, linearly separable: feature x high -> profit (y=1), low ->
    loss (y=0). A second 'noise' feature carries no signal. Interleaved so both
    the older-70% train slice and newer-30% holdout contain both classes."""
    X, y = [], []
    for i in range(n):
        profit = (i % 2 == 0)
        x = 2.0 + (i % 5) * 0.1 if profit else -2.0 - (i % 5) * 0.1
        noise = ((i * 7) % 11) - 5.0
        X.append([x, noise])
        y.append(1 if profit else 0)
    return X, y


def _set_dataset(X, y, names=None, label_n=None):
    s = _fake._STATE
    s["X"], s["y"] = list(X), list(y)
    if names is not None:
        s["names"] = list(names)
    s["label_n"] = len(X) if label_n is None else label_n


def _reset_model():
    aj_db.set_setting_raw(ml._MODEL_KEY, "")
    aj_db.set_setting_raw(ml._LASTN_KEY, "")


CFG = {"metalabel_min_samples": 50, "metalabel_min_auc": 0.55,
       "metalabel_prob_threshold": 0.5, "metalabel_retrain_min_new": 10,
       "metalabel_enabled": True}


# ── tests ────────────────────────────────────────────────────────────────────────

def test_train_promotes_separable_and_persists_json():
    _reset_model()
    X, y = _separable(120)
    _set_dataset(X, y)
    res = ml.train(CFG)
    check(res["trained"] is True, "train: trained=True on separable data")
    check(res["promoted"] is True, "train: promoted on separable data")
    check(res["oos_auc"] is not None and res["oos_auc"] >= CFG["metalabel_min_auc"],
          "train: oos_auc >= min_auc ({})".format(res["oos_auc"]))
    check(res["n"] == 120, "train: reports n=120")
    check(abs(res["base_rate"] - 0.5) < 1e-9, "train: base_rate ~0.5")

    # persisted model JSON has the documented shape
    raw = aj_db.get_setting_raw(ml._MODEL_KEY)
    check(bool(raw), "train: persisted a non-empty model string")
    m = json.loads(raw)
    expected = {"feature_names", "mean", "std", "coef", "intercept", "oos_auc",
                "n_train", "base_rate", "trained_at", "promoted"}
    check(expected.issubset(set(m.keys())),
          "model JSON has all documented keys")
    check(m["feature_names"] == ["x", "noise"], "model: feature_names preserved")
    check(len(m["mean"]) == 2 and len(m["std"]) == 2 and len(m["coef"]) == 2,
          "model: mean/std/coef length match feature count")
    check(m["promoted"] is True, "model: promoted flag persisted True")
    # JSON, not pickle: it must parse as JSON and contain only plain types
    check(isinstance(m["coef"][0], float) and isinstance(m["intercept"], float),
          "model: coef/intercept are plain floats (JSON-serializable)")


def test_predict_proba_ranks_profit_over_loss():
    # relies on the promoted model from the previous test (separable)
    _set_dataset(*_separable(120))
    ml.train(CFG)
    p_profit = ml.predict_proba({"x": 2.5, "noise": 0.0}, CFG)
    p_loss = ml.predict_proba({"x": -2.5, "noise": 0.0}, CFG)
    check(p_profit is not None and p_loss is not None,
          "predict_proba returns floats for both setups")
    check(0.0 <= p_loss <= 1.0 and 0.0 <= p_profit <= 1.0,
          "predict_proba clamped to [0,1]")
    check(p_profit > p_loss,
          "predict_proba: profit-leaning ({:.3f}) > loss-leaning ({:.3f})".format(
              p_profit, p_loss))
    check(p_profit > 0.5 > p_loss, "predict_proba: straddles 0.5 sensibly")


def test_pure_numpy_matches_sklearn():
    """The deployed coef is refit on ALL data; rebuild the same sklearn pipeline on
    ALL data and confirm pure-numpy inference matches predict_proba within tol."""
    _reset_model()
    X, y = _separable(120)
    _set_dataset(X, y)
    ml.train(CFG)

    import numpy as np
    from sklearn.linear_model import LogisticRegression
    from sklearn.preprocessing import StandardScaler
    Xa = np.asarray(X, dtype=float)
    ya = np.asarray(y, dtype=int)
    sc = StandardScaler().fit(Xa)
    clf = LogisticRegression(max_iter=1000).fit(sc.transform(Xa), ya)

    max_err = 0.0
    for row in [[2.5, 0.0], [-2.5, 1.0], [0.3, -3.0], [1.1, 4.0]]:
        sk = float(clf.predict_proba(sc.transform([row]))[0, 1])
        ours = ml.predict_proba({"x": row[0], "noise": row[1]}, CFG)
        max_err = max(max_err, abs(sk - ours))
    check(max_err < 1e-6,
          "pure-numpy inference matches sklearn predict_proba (max err {:.2e})".format(
              max_err))


def test_missing_feature_imputed_neutral():
    _set_dataset(*_separable(120))
    ml.train(CFG)
    # omit 'noise' -> imputed to its mean (neutral). Should still score, near the
    # value with noise at its mean.
    p_full = ml.predict_proba({"x": 2.0, "noise": 0.0}, CFG)
    p_missing = ml.predict_proba({"x": 2.0}, CFG)
    check(p_missing is not None, "predict_proba handles a missing feature")
    # noise mean is ~0 in this fixture so the two should be close
    check(abs(p_full - p_missing) < 0.2,
          "missing feature imputed neutral (close to noise-at-mean)")


def test_below_min_samples_not_promoted():
    _reset_model()
    X, y = _separable(40)   # 40 < 50
    _set_dataset(X, y)
    res = ml.train(CFG)
    check(res["promoted"] is False, "n<min_samples -> not promoted")
    check("insufficient samples" in res["reason"],
          "n<min reason mentions insufficient samples")
    m = json.loads(aj_db.get_setting_raw(ml._MODEL_KEY))
    check(m["promoted"] is False, "n<min: persisted model not promoted")


def test_single_class_not_promoted():
    _reset_model()
    X = [[float(i), 0.0] for i in range(80)]
    y = [1] * 80   # only one class
    _set_dataset(X, y)
    res = ml.train(CFG)
    check(res["promoted"] is False, "single class -> not promoted")
    check("single class" in res["reason"], "single-class reason mentions single class")


def test_predict_proba_none_without_promoted_model():
    _reset_model()
    # below-min train => persisted but NOT promoted
    _set_dataset(*_separable(40))
    ml.train(CFG)
    p = ml.predict_proba({"x": 2.5, "noise": 0.0}, CFG)
    check(p is None, "predict_proba returns None when no promoted model")
    # and with no model at all
    _reset_model()
    check(ml.predict_proba({"x": 2.5}, CFG) is None,
          "predict_proba returns None when no model persisted")


def test_is_promoted_status_fail_open():
    _reset_model()
    ip = ml.is_promoted(CFG)
    check(ip["promoted"] is False, "is_promoted fail-open: False with no model")
    check(ip["n"] == 0, "is_promoted: n=0 with no model")

    st = ml.status(CFG)
    check(st["promoted"] is False, "status fail-open: promoted False with no model")
    check(st["enabled"] is True, "status: reflects enabled flag")
    check("label_stats" in st, "status: includes label_stats")

    # corrupt the persisted model -> still fails open, no raise
    aj_db.set_setting_raw(ml._MODEL_KEY, "{not valid json")
    check(ml.is_promoted(CFG)["promoted"] is False,
          "is_promoted: corrupt model -> not promoted (no raise)")
    check(ml.predict_proba({"x": 1.0}, CFG) is None,
          "predict_proba: corrupt model -> None (no raise)")
    check(ml.status(CFG)["promoted"] is False,
          "status: corrupt model -> promoted False (no raise)")


def test_maybe_retrain_threshold():
    _reset_model()
    # first call ever: trains
    _set_dataset(*_separable(120))
    r1 = ml.maybe_retrain(CFG)
    check(r1["retrained"] is True, "maybe_retrain: first call trains")
    check(_fake._STATE["build_calls"] >= 1, "maybe_retrain: called build_labels")

    # no growth -> no-op
    r2 = ml.maybe_retrain(CFG)
    check(r2["retrained"] is False, "maybe_retrain: no new labels -> no-op")

    # grow by fewer than min_new -> still no-op
    _fake._STATE["label_n"] += 5   # < 10
    r3 = ml.maybe_retrain(CFG)
    check(r3["retrained"] is False, "maybe_retrain: <min_new growth -> no-op")

    # grow past the threshold -> retrains
    X, y = _separable(140)
    _set_dataset(X, y)   # label_n becomes 140 (>= +min_new over last train n=120)
    r4 = ml.maybe_retrain(CFG)
    check(r4["retrained"] is True, "maybe_retrain: >=min_new new labels -> retrains")


def test_train_fail_open_on_features_error():
    _reset_model()

    def _boom(target="profit"):
        raise RuntimeError("features exploded")
    orig = _fake.training_set
    _fake.training_set = _boom
    try:
        res = ml.train(CFG)
        check(res["trained"] is False, "train fail-open: trained False on error")
        check("error" in res, "train fail-open: carries an error key")
    finally:
        _fake.training_set = orig


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    print("aj_metalabel — {} test groups\n".format(len(tests)))
    for fn in tests:
        try:
            fn()
        except Exception as e:
            FAIL.append(fn.__name__)
            print("  [XX] {}: unexpected {}: {}".format(
                fn.__name__, type(e).__name__, e))
    print("\n{} passed, {} failed".format(len(PASS), len(FAIL)))
    if FAIL:
        print("FAILED:")
        for f in FAIL:
            print("  - " + f)
    sys.exit(1 if FAIL else 0)
