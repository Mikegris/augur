"""
AI Hypothesis Generator + Research Lab.

Synthesises every other research module (smart_money, ml_forecast, narrative,
gex, recent filings, quote) into a testable hypothesis via an OpenAI call,
then persists it in SQLite so the warmer can score it once the horizon ends.

Python 3.9 compatible — no `dict[str, X]` style hints, no PEP 604 unions.

Schema:
    hypotheses (
        id, created_at, symbol, title, conditions_json, prediction_json,
        rationale, status, horizon_days, issued_price, closed_at,
        realized_return, confidence, tags_json, source
    )
status values: OPEN | CONFIRMED | REJECTED | INVALIDATED
source values: "ai" | "user"

This module intentionally does NOT use database.py — it has its own
init_hypothesis_db() that uses the same DB file (AUGUR_DB_PATH / wealth.db)
so all rows live in one place but the migration path is independent.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def _db_path() -> str:
    """Honour the same env override database.py uses."""
    return os.environ.get("AUGUR_DB_PATH", "wealth.db")


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path(), check_same_thread=False, timeout=10.0)
    conn.row_factory = sqlite3.Row
    return conn


def init_hypothesis_db() -> None:
    """Create the hypotheses table + indexes. Idempotent."""
    with _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS hypotheses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                symbol TEXT NOT NULL,
                title TEXT NOT NULL,
                conditions_json TEXT NOT NULL,
                prediction_json TEXT NOT NULL,
                rationale TEXT,
                status TEXT NOT NULL DEFAULT 'OPEN',
                horizon_days INTEGER NOT NULL,
                issued_price REAL,
                closed_at TEXT,
                realized_return REAL,
                confidence REAL,
                tags_json TEXT,
                source TEXT
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_hyp_symbol ON hypotheses(symbol, status)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_hyp_status_horizon ON hypotheses(status, created_at)")
        c.commit()


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Context gathering — every step is best-effort
# ---------------------------------------------------------------------------

def _safe_call(label: str, fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Exception as e:
        log.debug("hypothesis context fetch %s failed: %s", label, e)
        return None


def _gather_context(symbol: str) -> Dict[str, Any]:
    """Best-effort collation of every research signal for ``symbol``.

    Each block is wrapped in try/except so a single broken upstream doesn't
    poison the whole context dict.
    """
    symbol = (symbol or "").upper().strip()
    ctx: Dict[str, Any] = {"symbol": symbol}

    # --- Quote / price -----------------------------------------------------
    try:
        import fetcher
        q = _safe_call("quote", fetcher.get_quote, symbol)
        if q and "error" not in q:
            ctx["price"] = q.get("price")
            ctx["change_pct"] = q.get("change_pct")
            ctx["currency"] = q.get("currency")
            ctx["market_cap"] = q.get("market_cap")
    except Exception:
        pass

    # --- ML forecast -------------------------------------------------------
    try:
        import ml_forecast
        ml = _safe_call("ml_forecast", ml_forecast.ml_forecast, symbol)
        if ml and "error" not in ml:
            rf = ml.get("rf_classifier") or {}
            trend = ml.get("trend_forecast") or {}
            regime = ml.get("regime") or {}
            mr = ml.get("mean_reversion") or {}
            # ml_forecast's trend_forecast surfaces "trend_short" / "trend_mid"
            # (not "direction"), and regime surfaces "current_regime" (not
            # "regime"). Reading the wrong key just returned None for every
            # call, so the synthesised hypothesis context was always blank
            # on those two fields.
            ctx["ml_forecast"] = {
                "prob_up_20d": rf.get("prob_up_20d"),
                "trend_direction": trend.get("trend_short"),
                "trend_forecast_pct": trend.get("forecast_pct"),
                "regime": regime.get("current_regime") if isinstance(regime, dict) else None,
                "mean_reversion_signal": mr.get("signal") if isinstance(mr, dict) else None,
            }
    except Exception:
        pass

    # --- Smart money -------------------------------------------------------
    try:
        import smart_money
        sm = _safe_call("smart_money", smart_money.compute_score, symbol)
        if sm and "error" not in sm:
            ctx["smart_money"] = {
                "score": sm.get("score"),
                "signal": sm.get("signal"),
                "insider": (sm.get("components") or {}).get("insider_activity", {}).get("score"),
                "institutional": (sm.get("components") or {}).get("institutional", {}).get("score"),
                "momentum": (sm.get("components") or {}).get("momentum", {}).get("score"),
            }
    except Exception:
        pass

    # --- Narrative --------------------------------------------------------
    try:
        import narrative_engine
        nar = _safe_call("narrative", narrative_engine.analyze_narrative, symbol)
        if nar and "error" not in nar:
            ctx["narrative"] = {
                "phase": nar.get("narrative_phase"),
                "dominant": nar.get("dominant_narrative"),
                "velocity": nar.get("velocity_score"),
                "velocity_label": nar.get("velocity_label"),
                "contrarian": nar.get("contrarian_signal"),
                "article_count": nar.get("article_count"),
            }
    except Exception:
        pass

    # --- GEX --------------------------------------------------------------
    try:
        import gex_engine
        gx = _safe_call("gex", gex_engine.get_gex_summary, symbol)
        if gx and "error" not in gx:
            ctx["gex"] = {
                "regime": gx.get("gamma_regime"),
                "flip_price": gx.get("gamma_flip_price"),
                "spot": gx.get("spot_price"),
                "max_pain": gx.get("max_pain"),
                "put_call_oi_ratio": gx.get("put_call_oi_ratio"),
            }
    except Exception:
        pass

    # --- Recent SEC filings (titles only) --------------------------------
    try:
        import sec_edgar
        filings = _safe_call("filings", sec_edgar.get_recent_filings, symbol, None, 5) or []
        if filings:
            ctx["recent_filings"] = [
                {
                    "form": f.get("form"),
                    "date": f.get("filing_date") or f.get("date"),
                    "description": (f.get("description") or "")[:160],
                }
                for f in filings[:5]
            ]
    except Exception:
        pass

    return ctx


# ---------------------------------------------------------------------------
# OpenAI call
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
  "rationale": "<2-3 sentence chain-of-reasoning grounded in the context>",
  "tags": ["<short tag>", "..."]
}
Only fields you can justify from the context should be filled; set the
others to null. Do not invent numbers that aren't in the context.
"""


def _build_prompt(ctx: Dict[str, Any]) -> str:
    return (
        "Given the multi-factor research context below for {sym}, generate "
        "ONE concrete, falsifiable trading hypothesis the user can save and "
        "verify in {horizon_default} days or less.\n\n"
        "CONTEXT:\n{ctx_json}\n\n"
        "{schema}\n"
    ).format(
        sym=ctx.get("symbol", "?"),
        horizon_default=20,
        ctx_json=json.dumps(ctx, default=str, indent=2),
        schema=_SCHEMA_HINT,
    )


def _validate_hypothesis(h: Any) -> Optional[Dict[str, Any]]:
    """Coerce / sanity-check the LLM payload. Returns None if unusable."""
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
        mag = 3.0  # sensible default

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
        "title": str(h.get("title") or "Untitled hypothesis")[:200],
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


def _call_openai(prompt: str, api_key: str) -> Dict[str, Any]:
    """Single OpenAI completion. Returns parsed JSON or {error}."""
    # Respect the same daily cap the other AI modules use.
    try:
        import ai_summarizer
        if getattr(ai_summarizer, "_cap_exceeded", lambda: False)():
            return {"error": "Daily AI request cap reached"}
    except Exception:
        pass

    try:
        from openai import OpenAI
    except Exception as e:
        return {"error": "openai package not installed: {}".format(e)}

    try:
        client = OpenAI(api_key=api_key)
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a quantitative research analyst. You produce "
                        "concise, falsifiable, multi-factor trading hypotheses "
                        "in pure JSON. Never include prose outside the JSON."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            response_format={"type": "json_object"},
            temperature=0.3,
            max_tokens=700,
        )
        # Bump the shared daily counter so this module shares the cap budget.
        try:
            import ai_summarizer
            ai_summarizer._record_ai_call()
        except Exception:
            pass

        content = resp.choices[0].message.content or "{}"
        return json.loads(content)
    except Exception as e:
        return {"error": "OpenAI call failed: {}".format(e)}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate_hypothesis(symbol: str) -> Dict[str, Any]:
    """Generate a single hypothesis for ``symbol`` using the LLM.

    Returns the hypothesis dict (schema documented above) on success, or
    ``{"error": ...}`` on failure (missing key, network error, bad JSON).

    Never raises.
    """
    symbol = (symbol or "").upper().strip()
    if not symbol:
        return {"error": "No symbol provided"}

    # API key — defer to ai_summarizer's resolution so we honour env + DB.
    try:
        import ai_summarizer
        key = ai_summarizer.get_openai_key()
    except Exception:
        key = (os.environ.get("OPENAI_API_KEY") or "").strip()

    if not key:
        return {"error": "OpenAI key not configured"}

    ctx = _gather_context(symbol)
    prompt = _build_prompt(ctx)
    raw = _call_openai(prompt, key)
    if "error" in raw:
        return raw

    hyp = _validate_hypothesis(raw)
    if hyp is None:
        return {"error": "LLM returned an unparseable hypothesis"}

    # Attach the context snapshot so the UI can show "what we knew when".
    hyp["_context_snapshot"] = ctx
    hyp["symbol"] = symbol
    return hyp


def save_hypothesis(hypothesis: Dict[str, Any], symbol: str, source: str = "ai") -> int:
    """Persist a hypothesis dict. Returns the new row id.

    Captures ``issued_price`` from the current quote (best-effort).
    Raises ``ValueError`` if the dict is missing required fields.
    """
    if not isinstance(hypothesis, dict):
        raise ValueError("hypothesis must be a dict")

    symbol = (symbol or hypothesis.get("symbol") or "").upper().strip()
    if not symbol:
        raise ValueError("symbol required")

    pred = hypothesis.get("prediction") or {}
    if "horizon_days" not in pred:
        raise ValueError("prediction.horizon_days required")
    try:
        horizon = int(pred.get("horizon_days") or 20)
    except (TypeError, ValueError):
        horizon = 20
    horizon = max(1, min(365, horizon))

    try:
        confidence = float(pred.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0

    # Best-effort issued price
    issued_price: Optional[float] = None
    try:
        import fetcher
        q = fetcher.get_quote(symbol) or {}
        if "error" not in q and q.get("price") is not None:
            issued_price = float(q["price"])
    except Exception:
        issued_price = None

    init_hypothesis_db()
    with _conn() as c:
        cur = c.execute(
            """
            INSERT INTO hypotheses (
                created_at, symbol, title, conditions_json, prediction_json,
                rationale, status, horizon_days, issued_price, confidence,
                tags_json, source
            ) VALUES (?, ?, ?, ?, ?, ?, 'OPEN', ?, ?, ?, ?, ?)
            """,
            (
                _now_iso(),
                symbol,
                str(hypothesis.get("title") or "Untitled hypothesis")[:200],
                json.dumps(hypothesis.get("conditions") or {}),
                json.dumps(hypothesis.get("prediction") or {}),
                str(hypothesis.get("rationale") or "")[:2000],
                horizon,
                issued_price,
                confidence,
                json.dumps(hypothesis.get("tags") or []),
                (source or "ai"),
            ),
        )
        c.commit()
        return int(cur.lastrowid)


def _row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    d = dict(row)
    for k in ("conditions_json", "prediction_json", "tags_json"):
        raw = d.get(k)
        if raw:
            try:
                d[k.replace("_json", "")] = json.loads(raw)
            except Exception:
                d[k.replace("_json", "")] = None
    return d


def list_hypotheses(status: Optional[str] = None,
                    symbol: Optional[str] = None,
                    limit: int = 50) -> List[Dict[str, Any]]:
    """List hypotheses, newest first."""
    init_hypothesis_db()
    clauses: List[str] = []
    params: List[Any] = []
    if status:
        clauses.append("status = ?")
        params.append(status.upper())
    if symbol:
        clauses.append("symbol = ?")
        params.append(symbol.upper())
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    try:
        limit = max(1, min(500, int(limit)))
    except (TypeError, ValueError):
        limit = 50
    params.append(limit)

    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM hypotheses {w} ORDER BY datetime(created_at) DESC LIMIT ?"
            .format(w=where),
            params,
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def get_hypothesis(hypothesis_id: int) -> Optional[Dict[str, Any]]:
    init_hypothesis_db()
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM hypotheses WHERE id = ?", (int(hypothesis_id),)
        ).fetchone()
    return _row_to_dict(row) if row else None


def update_hypothesis_status(hypothesis_id: int, status: str) -> bool:
    """Manual override. Returns True on success."""
    status = (status or "").upper().strip()
    if status not in ("OPEN", "CONFIRMED", "REJECTED", "INVALIDATED"):
        raise ValueError("invalid status: {}".format(status))
    init_hypothesis_db()
    closed_at = None if status == "OPEN" else _now_iso()
    with _conn() as c:
        cur = c.execute(
            "UPDATE hypotheses SET status = ?, closed_at = ? WHERE id = ?",
            (status, closed_at, int(hypothesis_id)),
        )
        c.commit()
        return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Scoring (resolution at horizon end)
# ---------------------------------------------------------------------------

def _parse_dt(iso_str: str) -> Optional[datetime]:
    if not iso_str:
        return None
    try:
        # Python 3.9 fromisoformat handles "+00:00" but not trailing "Z".
        s = iso_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _current_price(symbol: str) -> Optional[float]:
    try:
        import fetcher
        q = fetcher.get_quote(symbol) or {}
        if "error" not in q and q.get("price") is not None:
            return float(q["price"])
    except Exception:
        return None
    return None


def _resolve(direction: str, magnitude_pct: float, realized_pct: float) -> str:
    """Return CONFIRMED / REJECTED for the given outcome."""
    direction = (direction or "UP").upper()
    threshold = float(magnitude_pct) / 100.0
    realized = float(realized_pct) / 100.0
    if direction == "UP":
        return "CONFIRMED" if realized >= threshold else "REJECTED"
    if direction == "DOWN":
        return "CONFIRMED" if realized <= -threshold else "REJECTED"
    # RANGE: confirmed if absolute realized stays under magnitude
    return "CONFIRMED" if abs(realized) < threshold else "REJECTED"


def score_hypothesis(hypothesis_id: int) -> Dict[str, Any]:
    """Resolve a single hypothesis if it has passed its horizon.

    Returns a dict describing what happened:
        {"id": <id>, "status": <new status>, "realized_return": <pct or None>}
    or {"id": <id>, "status": "OPEN", "reason": "still in horizon"}.
    """
    h = get_hypothesis(hypothesis_id)
    if not h:
        return {"error": "not found"}

    if h.get("status") and h["status"] != "OPEN":
        return {"id": hypothesis_id, "status": h["status"], "reason": "already closed"}

    created = _parse_dt(h.get("created_at"))
    horizon = int(h.get("horizon_days") or 0)
    if not created or horizon <= 0:
        return {"id": hypothesis_id, "status": "OPEN", "reason": "bad metadata"}

    due = created + timedelta(days=horizon)
    now = datetime.now(tz=timezone.utc)
    if now < due:
        return {"id": hypothesis_id, "status": "OPEN", "reason": "still in horizon"}

    issued = h.get("issued_price")
    if issued is None:
        # Without an entry price we can't measure return → invalidate.
        with _conn() as c:
            c.execute(
                "UPDATE hypotheses SET status='INVALIDATED', closed_at=? WHERE id=?",
                (_now_iso(), hypothesis_id),
            )
            c.commit()
        return {"id": hypothesis_id, "status": "INVALIDATED", "reason": "no issued price"}

    current = _current_price(h["symbol"])
    if current is None or float(issued) == 0:
        return {"id": hypothesis_id, "status": "OPEN", "reason": "no current price"}

    realized_pct = ((current - float(issued)) / float(issued)) * 100.0
    pred = h.get("prediction") or {}
    new_status = _resolve(
        pred.get("direction", "UP"),
        float(pred.get("magnitude_pct") or 0.0),
        realized_pct,
    )

    with _conn() as c:
        c.execute(
            "UPDATE hypotheses SET status=?, closed_at=?, realized_return=? WHERE id=?",
            (new_status, _now_iso(), round(realized_pct, 4), hypothesis_id),
        )
        c.commit()

    return {
        "id": hypothesis_id,
        "status": new_status,
        "realized_return": round(realized_pct, 4),
    }


def score_due_hypotheses() -> Dict[str, Any]:
    """Batch-resolve every OPEN hypothesis whose horizon has elapsed.

    Intended to be called by the cache_warmer once per day.
    Returns a summary dict.
    """
    init_hypothesis_db()
    with _conn() as c:
        rows = c.execute(
            "SELECT id, created_at, horizon_days FROM hypotheses WHERE status='OPEN'"
        ).fetchall()

    now = datetime.now(tz=timezone.utc)
    due_ids: List[int] = []
    for r in rows:
        created = _parse_dt(r["created_at"])
        if not created:
            continue
        if now >= created + timedelta(days=int(r["horizon_days"] or 0)):
            due_ids.append(int(r["id"]))

    results = []
    for hid in due_ids:
        try:
            results.append(score_hypothesis(hid))
        except Exception as e:
            log.warning("score_hypothesis(%s) failed: %s", hid, e)
            results.append({"id": hid, "error": str(e)})

    summary = {
        "open_total": len(rows),
        "scored": len(results),
        "results": results,
    }
    return summary


# ---------------------------------------------------------------------------
# Convenience: stats for the lab UI
# ---------------------------------------------------------------------------

def stats() -> Dict[str, Any]:
    """Aggregate counts by status + simple hit rate."""
    init_hypothesis_db()
    with _conn() as c:
        rows = c.execute(
            "SELECT status, COUNT(*) AS n FROM hypotheses GROUP BY status"
        ).fetchall()
    counts = {r["status"]: r["n"] for r in rows}
    confirmed = counts.get("CONFIRMED", 0)
    rejected = counts.get("REJECTED", 0)
    closed = confirmed + rejected
    hit_rate = (confirmed / closed) if closed else None
    return {
        "by_status": counts,
        "hit_rate": round(hit_rate, 4) if hit_rate is not None else None,
        "total": sum(counts.values()),
    }


# Auto-init on import so callers don't need to remember it.
try:
    init_hypothesis_db()
except Exception as _e:  # pragma: no cover
    log.debug("hypothesis init deferred: %s", _e)
