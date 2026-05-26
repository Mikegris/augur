"""synth_groundhyp.py — Pattern-Grounded Hypotheses for AUGUR.

Retrieval-augmented hypothesis generation: instead of asking the LLM cold,
we first extract the *current* signal pattern as a feature vector, search
the historical ``signal_forecasts`` ledger (owned by :mod:`research_tracker`)
for similar patterns, retrieve their realized outcomes, and only THEN ask
gpt-4o-mini to refine a hypothesis grounded in those analogs.

Public entry point:

    grounded_hypothesis(symbol) -> dict
        {
          "symbol", "current_pattern",
          "anchor_cases": [...],           # up to 5 closest historical analogs
          "base_rate_summary": {...},      # hit_rate / avg / IQR over analogs
          "hypothesis": {...} | None,      # LLM output (same schema as
                                           # research_hypothesis.generate_hypothesis)
          "as_of": ISO-8601,
        }

If the OpenAI key is missing or the shared daily cap is exceeded, the
``hypothesis`` field is ``None`` and the rest of the payload still
contains the analog-retrieval view (which is useful on its own).

Schema extension: this module needs an issue-time pattern vector for each
``signal_forecasts`` row to do cosine retrieval.  Rather than touch
``database.py`` or ``research_tracker.py``, we self-init an extra column
``pattern_vector_json`` via an idempotent ``ALTER TABLE`` in
:func:`_ensure_pattern_column`.  Older rows have ``NULL`` and we fall back
to parsing whatever lives in the existing ``metadata`` JSON blob.

Python 3.9 compatible — no PEP 604 unions, no ``dict[…]`` style hints.
"""

from __future__ import annotations

import json
import logging
import math
import os
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Soft dependencies — every upstream is guarded.  Missing one only weakens
# the pattern vector; the rest of the pipeline still runs.
# ---------------------------------------------------------------------------
try:
    import cache_store  # type: ignore
except Exception:  # pragma: no cover
    cache_store = None

try:
    import fetcher  # type: ignore
except Exception:  # pragma: no cover
    fetcher = None

try:
    import ml_forecast  # type: ignore
except Exception:  # pragma: no cover
    ml_forecast = None

try:
    import smart_money  # type: ignore
except Exception:  # pragma: no cover
    smart_money = None

try:
    import gex_engine  # type: ignore
except Exception:  # pragma: no cover
    gex_engine = None

try:
    import narrative_engine  # type: ignore
except Exception:  # pragma: no cover
    narrative_engine = None

try:
    import sec_edgar  # type: ignore
except Exception:  # pragma: no cover
    sec_edgar = None

try:
    import research_factors  # type: ignore
except Exception:  # pragma: no cover
    research_factors = None

try:
    import research_tracker  # type: ignore
except Exception:  # pragma: no cover
    research_tracker = None

try:
    import ai_summarizer  # type: ignore
except Exception:  # pragma: no cover
    ai_summarizer = None


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Ordered list of feature names — keeps the vector layout stable across
# pattern extraction, retrieval, and the LLM prompt. Each feature is
# normalised to [-1, +1].
FEATURE_NAMES: Tuple[str, ...] = (
    "ml_prob_up",          # ml_forecast prob_up_20d, mapped (p - .5) * 2
    "smart_money",         # smart_money composite 0..100, mapped (s-50)/50
    "gex_regime",          # +0.3 LONG/POSITIVE, -0.3 SHORT/NEGATIVE, 0 NEUT
    "narrative_phase",     # phase map ∈ {-0.5, …, +0.5}
    "insider_form4_net",   # (buy - sell) / (buy + sell), already [-1, +1]
    "factor_alpha",        # alpha_annual_pct, saturated at ±15% → ±1
    "price_momentum",      # blend of intraday change_pct and 52w position
)

ANALOG_K = 5      # top-k closest historical analogs
ANALOG_POOL = 200  # candidates we read from the DB before ranking
_CACHE_TTL = 3 * 3600  # 3 hours

# Phase-to-scalar mapping (matches synth_consensus's recipe so analogs
# computed via either path align).
_PHASE_MAP: Dict[str, float] = {
    "ACCELERATION": +0.5,
    "EMERGENCE":    +0.3,
    "BREAKOUT":     +0.5,
    "REVERSAL":     +0.2,
    "DEVELOPING":   0.0,
    "CONSENSUS":   -0.3,
    "EXHAUSTION":  -0.5,
}


# ---------------------------------------------------------------------------
# Storage helpers — we share the same DB file as research_tracker but never
# call into its internals; the schema extension is self-managed.
# ---------------------------------------------------------------------------

def _db_path() -> str:
    return os.environ.get("AUGUR_DB_PATH", "wealth.db")


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(_db_path(), check_same_thread=False, timeout=10.0)
    c.row_factory = sqlite3.Row
    return c


_pattern_col_checked = False


def _ensure_pattern_column() -> None:
    """Idempotently add ``pattern_vector_json`` to signal_forecasts.

    Older rows logged by research_tracker won't have a column at all (the
    table is owned by that module).  We add it ourselves so future
    pattern-aware logging can populate it without touching research_tracker
    or database.py.  Safe to call repeatedly; we cache the success flag in
    a module-level boolean.
    """
    global _pattern_col_checked
    if _pattern_col_checked:
        return
    try:
        with _conn() as c:
            # Confirm the parent table exists; if not, research_tracker
            # hasn't initialised yet — try to coax it.
            row = c.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='signal_forecasts'"
            ).fetchone()
            if row is None and research_tracker is not None:
                try:
                    research_tracker.init_tracker_db()
                except Exception as e:
                    log.debug("research_tracker.init_tracker_db failed: %s", e)
                row = c.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='signal_forecasts'"
                ).fetchone()
            if row is None:
                # No table at all — nothing to alter.  Don't latch; next
                # call may find it after the tracker boots.
                return
            cols = {r["name"] for r in c.execute("PRAGMA table_info(signal_forecasts)")}
            if "pattern_vector_json" not in cols:
                try:
                    c.execute("ALTER TABLE signal_forecasts ADD COLUMN pattern_vector_json TEXT")
                    c.commit()
                except sqlite3.OperationalError as e:
                    # Race with another process that just added it — fine.
                    log.debug("ALTER TABLE benign failure: %s", e)
            _pattern_col_checked = True
    except Exception as e:  # pragma: no cover
        log.debug("_ensure_pattern_column failed: %s", e)


# ---------------------------------------------------------------------------
# Feature extraction — all guarded; each contributor returns ``float | None``.
# ---------------------------------------------------------------------------

def _clip(x: float, lo: float, hi: float) -> float:
    if x is None:
        return lo
    if x != x:  # NaN
        return 0.0
    return max(lo, min(hi, x))


def _safe(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Exception as e:
        log.debug("safe-call %s failed: %s", getattr(fn, "__name__", fn), e)
        return None


def _f_ml_prob_up(symbol: str, ctx: Dict[str, Any]) -> Optional[float]:
    if ml_forecast is None:
        return None
    res = _safe(ml_forecast.ml_forecast, symbol)
    if not isinstance(res, dict) or res.get("error"):
        return None
    rf = res.get("rf_classifier") or {}
    p = rf.get("prob_up_20d")
    if p is None:
        return None
    try:
        p = float(p)
    except Exception:
        return None
    ctx["_raw"]["ml_prob_up_20d"] = round(p, 4)
    return _clip((p - 0.5) * 2.0, -1.0, 1.0)


def _f_smart_money(symbol: str, ctx: Dict[str, Any]) -> Optional[float]:
    if smart_money is None:
        return None
    fn = getattr(smart_money, "compute_score", None)
    if not callable(fn):
        return None
    res = _safe(fn, symbol)
    if not isinstance(res, dict) or res.get("error"):
        return None
    s = res.get("score")
    if s is None:
        return None
    try:
        s = float(s)
    except Exception:
        return None
    ctx["_raw"]["smart_money_score"] = int(s)
    return _clip((s - 50.0) / 50.0, -1.0, 1.0)


def _f_gex_regime(symbol: str, ctx: Dict[str, Any]) -> Optional[float]:
    if gex_engine is None:
        return None
    fn = getattr(gex_engine, "get_gex_summary", None) or getattr(gex_engine, "compute_gex", None)
    if not callable(fn):
        return None
    res = _safe(fn, symbol)
    if not isinstance(res, dict) or res.get("error"):
        return None
    regime = res.get("gamma_regime") or res.get("regime")
    if not regime:
        return None
    r = str(regime).upper()
    ctx["_raw"]["gex_regime"] = regime
    if "LONG" in r or "POSITIVE" in r:
        return +0.3
    if "SHORT" in r or "NEGATIVE" in r:
        return -0.3
    return 0.0


def _f_narrative(symbol: str, ctx: Dict[str, Any]) -> Optional[float]:
    if narrative_engine is None:
        return None
    fn = getattr(narrative_engine, "analyze_narrative", None)
    if not callable(fn):
        return None
    res = _safe(fn, symbol)
    if not isinstance(res, dict) or res.get("error"):
        return None
    phase = res.get("narrative_phase") or res.get("phase")
    if not phase:
        return None
    p = str(phase).upper()
    ctx["_raw"]["narrative_phase"] = phase
    n = _PHASE_MAP.get(p, 0.0)
    velocity = res.get("velocity_score")
    try:
        if velocity is not None:
            v = float(velocity)
            n = n + max(-0.2, min(0.2, v / 100.0))
    except Exception:
        pass
    return _clip(n, -1.0, 1.0)


def _f_insider_form4(symbol: str, ctx: Dict[str, Any]) -> Optional[float]:
    if sec_edgar is None:
        return None
    fn = getattr(sec_edgar, "get_form4_transactions", None)
    if not callable(fn):
        return None
    txns = _safe(fn, symbol, limit=20)
    if not isinstance(txns, list) or not txns:
        return None
    buy = sell = 0.0
    for t in txns:
        if not isinstance(t, dict):
            continue
        try:
            v = float(t.get("value") or 0.0)
        except Exception:
            v = 0.0
        ttype = (t.get("transaction_type") or "").upper()
        if ttype == "BUY":
            buy += v
        elif ttype == "SELL":
            sell += v
    total = buy + sell
    if total <= 0:
        return 0.0
    ratio = (buy - sell) / total
    ctx["_raw"]["insider_form4_net"] = round(ratio, 3)
    return _clip(ratio, -1.0, 1.0)


def _f_factor_alpha(symbol: str, ctx: Dict[str, Any]) -> Optional[float]:
    if research_factors is None:
        return None
    fn = getattr(research_factors, "factor_exposure", None)
    if not callable(fn):
        return None
    res = _safe(fn, symbol, period_years=3)
    if not isinstance(res, dict) or res.get("error"):
        return None
    a = res.get("alpha_annual_pct")
    if a is None:
        return None
    try:
        a = float(a)
    except Exception:
        return None
    ctx["_raw"]["factor_alpha_pct"] = round(a, 2)
    return _clip(a / 15.0, -1.0, 1.0)


def _f_price_momentum(symbol: str, ctx: Dict[str, Any]) -> Optional[float]:
    if fetcher is None:
        return None
    q = _safe(fetcher.get_quote, symbol)
    if not isinstance(q, dict) or q.get("error"):
        return None
    pct = q.get("change_pct")
    hi = q.get("fifty_two_week_high") or q.get("52w_high")
    lo = q.get("fifty_two_week_low") or q.get("52w_low")
    price = q.get("price")
    if price is not None:
        ctx["_raw"]["price"] = price
    pieces: List[float] = []
    try:
        if pct is not None:
            pieces.append(_clip(float(pct) / 5.0, -1.0, 1.0))
            ctx["_raw"]["intraday_change_pct"] = float(pct)
    except Exception:
        pass
    try:
        if price is not None and hi is not None and lo is not None and float(hi) > float(lo):
            pos = (float(price) - float(lo)) / (float(hi) - float(lo))
            pieces.append(_clip((pos - 0.5) * 2.0, -1.0, 1.0))
    except Exception:
        pass
    if not pieces:
        return None
    return sum(pieces) / len(pieces)


_EXTRACTORS = (
    ("ml_prob_up",        _f_ml_prob_up),
    ("smart_money",       _f_smart_money),
    ("gex_regime",        _f_gex_regime),
    ("narrative_phase",   _f_narrative),
    ("insider_form4_net", _f_insider_form4),
    ("factor_alpha",      _f_factor_alpha),
    ("price_momentum",    _f_price_momentum),
)


def _extract_pattern(symbol: str) -> Tuple[Dict[str, Optional[float]], Dict[str, Any]]:
    """Compute the current pattern vector for ``symbol``.

    Returns (vector_dict, raw_values_dict).  Missing features are present
    as ``None`` so the LLM prompt and the UI see them explicitly.
    """
    sym = (symbol or "").upper().strip()
    vector: Dict[str, Optional[float]] = {n: None for n in FEATURE_NAMES}
    ctx: Dict[str, Any] = {"_raw": {}}
    for name, fn in _EXTRACTORS:
        try:
            v = fn(sym, ctx)
        except Exception as e:
            log.debug("extractor %s failed: %s", name, e)
            v = None
        if v is None:
            continue
        try:
            vf = float(v)
        except Exception:
            continue
        if vf != vf or math.isinf(vf):
            continue
        vector[name] = round(_clip(vf, -1.0, 1.0), 4)
    return vector, ctx["_raw"]


# ---------------------------------------------------------------------------
# Historical pattern reconstruction (from existing signal_forecasts rows)
# ---------------------------------------------------------------------------

def _vec_from_metadata(signal_name: str, metadata: Dict[str, Any]) -> Dict[str, Optional[float]]:
    """Reconstruct what we can of a feature vector from a tracker row's
    ``metadata`` blob.  Different signal_names log different fields; we
    map the ones we recognise into our standard feature layout.

    Missing features remain ``None``; cosine distance ignores them.
    """
    vec: Dict[str, Optional[float]] = {n: None for n in FEATURE_NAMES}
    if not isinstance(metadata, dict):
        return vec
    name = (signal_name or "").lower()

    if name == "ml_forecast":
        p = metadata.get("prob_up_20d")
        try:
            if p is not None:
                vec["ml_prob_up"] = _clip((float(p) - 0.5) * 2.0, -1.0, 1.0)
        except Exception:
            pass
    if name == "smart_money":
        s = metadata.get("score")
        try:
            if s is not None:
                vec["smart_money"] = _clip((float(s) - 50.0) / 50.0, -1.0, 1.0)
        except Exception:
            pass
    if name == "gex":
        regime = metadata.get("regime")
        if regime:
            r = str(regime).upper()
            if "LONG" in r or "POSITIVE" in r:
                vec["gex_regime"] = +0.3
            elif "SHORT" in r or "NEGATIVE" in r:
                vec["gex_regime"] = -0.3
            else:
                vec["gex_regime"] = 0.0
    if name == "narrative":
        phase = metadata.get("phase")
        if phase:
            vec["narrative_phase"] = _PHASE_MAP.get(str(phase).upper(), 0.0)

    return vec


def _vec_from_pattern_json(raw_json: str) -> Dict[str, Optional[float]]:
    """Parse the (newer) ``pattern_vector_json`` column, if populated."""
    out: Dict[str, Optional[float]] = {n: None for n in FEATURE_NAMES}
    if not raw_json:
        return out
    try:
        data = json.loads(raw_json)
    except Exception:
        return out
    if not isinstance(data, dict):
        return out
    for k in FEATURE_NAMES:
        v = data.get(k)
        if v is None:
            continue
        try:
            f = float(v)
            if f == f and not math.isinf(f):
                out[k] = round(_clip(f, -1.0, 1.0), 4)
        except Exception:
            continue
    return out


# ---------------------------------------------------------------------------
# Cosine distance over partially-missing vectors
# ---------------------------------------------------------------------------

def _cosine_distance(a: Dict[str, Optional[float]], b: Dict[str, Optional[float]]) -> Optional[float]:
    """Cosine distance in [0, 2] over features present in BOTH vectors.

    Returns ``None`` if there is no shared dimension (so the caller can
    drop the analog).
    """
    dot = 0.0
    na = 0.0
    nb = 0.0
    shared = 0
    for k in FEATURE_NAMES:
        av = a.get(k)
        bv = b.get(k)
        if av is None or bv is None:
            continue
        try:
            af = float(av)
            bf = float(bv)
        except Exception:
            continue
        dot += af * bf
        na += af * af
        nb += bf * bf
        shared += 1
    if shared == 0 or na <= 0 or nb <= 0:
        return None
    sim = dot / math.sqrt(na * nb)
    # Numerical safety
    sim = max(-1.0, min(1.0, sim))
    return 1.0 - sim


# ---------------------------------------------------------------------------
# Analog retrieval
# ---------------------------------------------------------------------------

def _load_candidate_rows(pool_size: int) -> List[sqlite3.Row]:
    """Pull the most recent ``pool_size`` scored forecast rows."""
    _ensure_pattern_column()
    try:
        with _conn() as c:
            # Defensive: confirm the columns we need exist.  If the parent
            # table is missing (no tracker init), return empty.
            tbl = c.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='signal_forecasts'"
            ).fetchone()
            if tbl is None:
                return []
            cols = {r["name"] for r in c.execute("PRAGMA table_info(signal_forecasts)")}
            select_cols = [
                "id", "signal_name", "symbol", "issued_at", "horizon_days",
                "predicted_direction", "metadata", "scored_at",
                "realized_return", "hit",
            ]
            if "pattern_vector_json" in cols:
                select_cols.append("pattern_vector_json")
            sql = (
                "SELECT " + ", ".join(select_cols) +
                " FROM signal_forecasts WHERE scored_at IS NOT NULL "
                "ORDER BY datetime(scored_at) DESC LIMIT ?"
            )
            return list(c.execute(sql, (int(pool_size),)).fetchall())
    except Exception as e:
        log.debug("_load_candidate_rows failed: %s", e)
        return []


def _row_to_vector(row: sqlite3.Row) -> Dict[str, Optional[float]]:
    """Reconstruct a feature vector for one historical row.

    Prefers the explicit ``pattern_vector_json`` column when present,
    falling back to whatever can be derived from ``metadata``.
    """
    keys = row.keys() if hasattr(row, "keys") else []
    pj = None
    if "pattern_vector_json" in keys:
        pj = row["pattern_vector_json"]
    vec = _vec_from_pattern_json(pj) if pj else {n: None for n in FEATURE_NAMES}
    # If pattern_vector_json was empty / missing, fall back to metadata.
    if all(v is None for v in vec.values()):
        meta_raw = row["metadata"]
        meta_obj: Dict[str, Any] = {}
        if meta_raw:
            try:
                meta_obj = json.loads(meta_raw)
            except Exception:
                meta_obj = {}
        vec = _vec_from_metadata(row["signal_name"], meta_obj)
    return vec


def _find_analogs(current: Dict[str, Optional[float]], k: int) -> List[Dict[str, Any]]:
    """Top-k analogs ranked by cosine distance to ``current``.

    Each analog dict has: symbol, signal_name, issued_at, horizon_days,
    pattern_distance, outcome { direction, realized_return, hit }.
    """
    rows = _load_candidate_rows(ANALOG_POOL)
    scored: List[Tuple[float, Dict[str, Any]]] = []
    for r in rows:
        vec = _row_to_vector(r)
        d = _cosine_distance(current, vec)
        if d is None:
            continue
        realized = r["realized_return"]
        try:
            realized_pct = float(realized) * 100.0 if realized is not None else None
        except Exception:
            realized_pct = None
        hit = r["hit"]
        try:
            hit_bool: Optional[bool] = bool(int(hit)) if hit is not None else None
        except Exception:
            hit_bool = None
        scored.append((d, {
            "id": int(r["id"]),
            "symbol": r["symbol"],
            "signal_name": r["signal_name"],
            "issued_at": r["issued_at"],
            "horizon_days": int(r["horizon_days"]) if r["horizon_days"] is not None else None,
            "pattern_distance": round(d, 4),
            "outcome": {
                "direction": r["predicted_direction"],
                "realized_return_pct": round(realized_pct, 4) if realized_pct is not None else None,
                "hit": hit_bool,
            },
        }))
    scored.sort(key=lambda t: t[0])
    return [a for _, a in scored[: max(1, int(k))]]


# ---------------------------------------------------------------------------
# Base-rate stats over a set of analogs
# ---------------------------------------------------------------------------

def _quantile(values: List[float], q: float) -> Optional[float]:
    if not values:
        return None
    v = sorted(values)
    if len(v) == 1:
        return v[0]
    idx = q * (len(v) - 1)
    lo = int(math.floor(idx))
    hi = int(math.ceil(idx))
    if lo == hi:
        return v[lo]
    frac = idx - lo
    return v[lo] + (v[hi] - v[lo]) * frac


def _base_rate(analogs: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not analogs:
        return {
            "n_analogs": 0,
            "hit_rate": None,
            "avg_realized_return_pct": None,
            "iqr_realized": None,
        }
    hits = [a["outcome"]["hit"] for a in analogs if a["outcome"]["hit"] is not None]
    rets = [a["outcome"]["realized_return_pct"]
            for a in analogs if a["outcome"]["realized_return_pct"] is not None]
    hit_rate = (sum(1 for h in hits if h) / len(hits)) if hits else None
    avg = (sum(rets) / len(rets)) if rets else None
    q25 = _quantile(rets, 0.25)
    q75 = _quantile(rets, 0.75)
    iqr = [round(q25, 3), round(q75, 3)] if (q25 is not None and q75 is not None) else None
    return {
        "n_analogs": len(analogs),
        "hit_rate": round(hit_rate, 4) if hit_rate is not None else None,
        "avg_realized_return_pct": round(avg, 3) if avg is not None else None,
        "iqr_realized": iqr,
    }


# ---------------------------------------------------------------------------
# LLM call — schema mirrors research_hypothesis._SCHEMA_HINT exactly so
# the saved hypothesis is interchangeable.
# ---------------------------------------------------------------------------

_SCHEMA_HINT = """\
Return ONLY a JSON object with this exact shape:
{
  "title": "<short headline, <=120 chars>",
  "conditions": {
      "ml_forecast_prob_up_20d_above": <0..1 float or null>,
      "smart_money_score_above":       <0..100 int or null>,
      "gex_regime":                    "POSITIVE_GAMMA" | "NEGATIVE_GAMMA" | null,
      "narrative_phase_not":           "REVERSAL" | "DEVELOPING" | "MOMENTUM" | "EXHAUSTION" | null,
      "custom":                        "<optional plain-English extra clause>"
  },
  "prediction": {
      "direction":     "UP" | "DOWN" | "RANGE",
      "magnitude_pct": <float, expected move size in percent>,
      "horizon_days":  <int days, typically 5..60>,
      "confidence":    <0..1 float>
  },
  "rationale": "<2-3 sentence chain-of-reasoning that EXPLICITLY references the historical analogs and base rate>",
  "tags": ["<short tag>", "..."]
}
Anchor every claim in the supplied analog cases and base-rate stats.
If the base rate disagrees with the current pattern's apparent direction,
say so — calibrated honesty beats false confidence.
"""


def _build_prompt(symbol: str,
                  pattern: Dict[str, Optional[float]],
                  raw: Dict[str, Any],
                  analogs: List[Dict[str, Any]],
                  base_rate: Dict[str, Any]) -> str:
    payload = {
        "symbol": symbol,
        "current_pattern_normalized": pattern,
        "current_pattern_raw": raw,
        "anchor_cases": analogs,
        "base_rate_summary": base_rate,
    }
    return (
        "You are grounding a falsifiable trading hypothesis for {sym} in a set "
        "of historical analogs retrieved from this app's own signal-forecast "
        "ledger.  Use the analogs' realized outcomes as your evidence base; "
        "do NOT invent numbers that aren't supplied.\n\n"
        "DATA:\n{data}\n\n{schema}\n"
    ).format(
        sym=symbol,
        data=json.dumps(payload, default=str, indent=2),
        schema=_SCHEMA_HINT,
    )


def _openai_key() -> str:
    """Resolve the OpenAI key via ai_summarizer if available, else env."""
    if ai_summarizer is not None:
        try:
            k = ai_summarizer.get_openai_key()
            if k:
                return k
        except Exception:
            pass
    return (os.environ.get("OPENAI_API_KEY") or "").strip()


def _cap_ok() -> bool:
    if ai_summarizer is None:
        return True
    try:
        return not ai_summarizer._cap_exceeded()
    except Exception:
        return True


def _record_call() -> None:
    if ai_summarizer is None:
        return
    try:
        ai_summarizer._record_ai_call()
    except Exception:
        pass


def _validate_hypothesis(h: Any) -> Optional[Dict[str, Any]]:
    """Coerce / sanity-check the LLM payload.

    Mirrors research_hypothesis._validate_hypothesis so a grounded
    hypothesis can be persisted via the existing save-hypothesis API.
    """
    if not isinstance(h, dict):
        return None
    pred = h.get("prediction") or {}
    if not isinstance(pred, dict):
        return None

    direction = (pred.get("direction") or "").upper()
    if direction not in ("UP", "DOWN", "RANGE"):
        direction = "UP"

    try:
        mag = float(pred.get("magnitude_pct") or 0.0)
    except (TypeError, ValueError):
        mag = 0.0
    if mag < 0:
        mag = abs(mag)
    if mag == 0.0:
        mag = 3.0

    try:
        horizon = int(pred.get("horizon_days") or 20)
    except (TypeError, ValueError):
        horizon = 20
    horizon = max(1, min(365, horizon))

    try:
        conf = float(pred.get("confidence") or 0.5)
    except (TypeError, ValueError):
        conf = 0.5
    conf = max(0.0, min(1.0, conf))

    conditions = h.get("conditions") or {}
    if not isinstance(conditions, dict):
        conditions = {}

    tags = h.get("tags") or []
    if not isinstance(tags, list):
        tags = []
    tags = [str(t)[:40] for t in tags][:8]

    return {
        "title": str(h.get("title") or "Untitled grounded hypothesis")[:200],
        "conditions": conditions,
        "prediction": {
            "direction": direction,
            "magnitude_pct": round(mag, 3),
            "horizon_days": horizon,
            "confidence": round(conf, 3),
        },
        "rationale": str(h.get("rationale") or "")[:2000],
        "tags": tags,
    }


def _call_llm(prompt: str, key: str) -> Dict[str, Any]:
    if not _cap_ok():
        return {"error": "Daily AI request cap reached"}
    try:
        from openai import OpenAI  # type: ignore
    except Exception as e:
        return {"error": "openai package not installed: {}".format(e)}
    try:
        client = OpenAI(api_key=key)
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a quantitative research analyst. You produce "
                        "concise, falsifiable, multi-factor trading hypotheses "
                        "in pure JSON, grounded in historical analog cases. "
                        "Never include prose outside the JSON."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            response_format={"type": "json_object"},
            temperature=0.3,
            max_tokens=700,
        )
        _record_call()
        content = resp.choices[0].message.content or "{}"
        return json.loads(content)
    except Exception as e:
        return {"error": "OpenAI call failed: {}".format(e)}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _compute(symbol: str) -> Dict[str, Any]:
    sym = (symbol or "").upper().strip()
    if not sym:
        return {"error": "missing symbol"}
    t0 = time.time()

    vector, raw = _extract_pattern(sym)
    analogs = _find_analogs(vector, ANALOG_K)
    base_rate = _base_rate(analogs)

    hypothesis: Optional[Dict[str, Any]] = None
    llm_status: str
    key = _openai_key()
    if not key:
        llm_status = "no_api_key"
    elif not _cap_ok():
        llm_status = "daily_cap_exceeded"
    else:
        prompt = _build_prompt(sym, vector, raw, analogs, base_rate)
        raw_out = _call_llm(prompt, key)
        if "error" in raw_out:
            llm_status = raw_out["error"]
        else:
            cooked = _validate_hypothesis(raw_out)
            if cooked is None:
                llm_status = "llm_returned_unparseable"
            else:
                hypothesis = cooked
                hypothesis["symbol"] = sym
                llm_status = "ok"

    return {
        "symbol": sym,
        "current_pattern": vector,
        "current_pattern_raw": raw,
        "anchor_cases": analogs,
        "base_rate_summary": base_rate,
        "hypothesis": hypothesis,
        "llm_status": llm_status,
        "as_of": _now_iso(),
        "computed_in_ms": round((time.time() - t0) * 1000),
    }


def grounded_hypothesis(symbol: str) -> Dict[str, Any]:
    """Top-level entry: pattern-grounded hypothesis for ``symbol``.

    Cached via ``cache_store.coalesce`` for 3 hours.
    """
    sym = (symbol or "").upper().strip()
    if not sym:
        return {"error": "missing symbol"}

    if cache_store is None:
        return _compute(sym)

    try:
        return cache_store.coalesce(
            ("groundhyp", sym),
            _CACHE_TTL,
            lambda: _compute(sym),
        )
    except Exception as e:
        log.debug("cache coalesce failed, computing directly: %s", e)
        return _compute(sym)


# ---------------------------------------------------------------------------
# Helper: log a pattern alongside a tracker forecast (optional integration
# point — callers may pass the vector at log time so retrieval works even
# when ``metadata`` doesn't carry the full feature set).
# ---------------------------------------------------------------------------

def attach_pattern_to_forecast(forecast_id: int, vector: Dict[str, Any]) -> bool:
    """Write a pattern vector to an existing signal_forecasts row.

    Returns True on success.  Idempotent; safe to call multiple times.
    Used by external callers that want richer retrieval keys than what
    the existing per-signal metadata blobs carry.
    """
    if forecast_id is None:
        return False
    _ensure_pattern_column()
    try:
        with _conn() as c:
            cols = {r["name"] for r in c.execute("PRAGMA table_info(signal_forecasts)")}
            if "pattern_vector_json" not in cols:
                return False
            payload = {k: vector.get(k) for k in FEATURE_NAMES if k in vector}
            c.execute(
                "UPDATE signal_forecasts SET pattern_vector_json = ? WHERE id = ?",
                (json.dumps(payload), int(forecast_id)),
            )
            c.commit()
            return True
    except Exception as e:
        log.debug("attach_pattern_to_forecast failed: %s", e)
        return False


# Auto-init schema extension on import (best effort; won't crash callers).
try:
    _ensure_pattern_column()
except Exception as _e:  # pragma: no cover
    log.debug("synth_groundhyp init deferred: %s", _e)


if __name__ == "__main__":  # pragma: no cover
    # Tiny smoke test.
    logging.basicConfig(level=logging.INFO)
    out = grounded_hypothesis("AAPL")
    print(json.dumps(
        {k: v for k, v in out.items() if k != "hypothesis"},
        indent=2, default=str,
    ))
    print("hypothesis:", out.get("hypothesis"))
