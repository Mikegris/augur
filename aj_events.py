"""AJTA — the event-alpha engine.

Turns TEXT the market reads — news headlines and SEC filings — into
calibrated, accountable, point-in-time trading signals. This is the AI used
as a measured feature factory, never as a trader:

  * INGEST  — headlines (fetcher.get_news) + filings (sec_edgar) land in the
    aj_events table with their SOURCE publication timestamps and a natural
    dedup key, unscored. Idempotent per event.
  * SCORE   — an LLM (aj_routing, budget-capped per cycle) converts each
    event into a structured verdict: direction [-1..+1], magnitude,
    confidence [0..1], half_life_days, one-line rationale. Strict JSON,
    clamped, fail-open: an unparseable response leaves the event unscored.
  * SIGNAL  — event_signal(symbol) aggregates the scored, UNEXPIRED events
    (exponential decay at each event's own half-life) into the standard
    adapter shape {prob_up, confidence}. It reads through aj_db.utc_now(),
    so it is SIM-CLOCK AWARE: replayed against a populated events table it
    sees only events published on/before the simulated day — no look-ahead.
  * ACCOUNT — every scored event stores the price at scoring time; after its
    half-life elapses the realized return is measured against the predicted
    direction (hit/miss). The adapter itself flows through the existing
    multi-factor fusion, IC promotion gate, and adapter scorecard — the LLM
    earns ensemble weight the same way every other source does: by realized
    skill, never by fiat.

Opt-in (event_alpha_enabled, default OFF) and fail-open everywhere: nothing
here can break a cycle. Python 3.9 compatible.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
from datetime import timedelta
from typing import Any, Dict, List, Optional

import aj_db

log = logging.getLogger("augur.aj_events")

# LLM tiers by event weight: headlines are cheap/quick; filings get the
# deeper read. Both flow through aj_routing (cost telemetry + fallbacks).
_NEWS_MAX_TOKENS = 220
_FILING_MAX_TOKENS = 300
_SCORE_SYS = (
    "You are a trading-event analyst. Given one market event for a symbol, "
    "return STRICT JSON only: {\"direction\": <-1.0..1.0, sign = expected "
    "price impact>, \"magnitude\": \"small|medium|large\", \"confidence\": "
    "<0.0..1.0>, \"half_life_days\": <1..30, how long the edge should last>, "
    "\"rationale\": \"<=25 words\"}. Judge ONLY price-relevant impact; "
    "routine/neutral events get direction 0 and low confidence.")

# The interesting filing forms: current events (8-K), the big periodic
# reports, and new registrations. 13F/SC 13G are covered by other adapters.
_FILING_FORMS = ("8-K", "10-Q", "10-K", "S-1")


def _cfg(cfg: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if cfg is not None:
        return cfg
    try:
        import aj_config
        return aj_config.get_config()
    except Exception:
        return {}


def _now_iso() -> str:
    return aj_db.utc_now_iso()


def _source_id(*parts: Any) -> str:
    return hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:20]


# ── ingestion ──────────────────────────────────────────────────────────────────

def _ingest_news(symbol: str) -> int:
    """Headlines -> unscored aj_events rows. Idempotent (UNIQUE source_id)."""
    n = 0
    try:
        import fetcher
        for it in (fetcher.get_news(symbol, 8) or []):
            if not isinstance(it, dict):
                continue
            title = str(it.get("title") or "").strip()
            if not title:
                continue
            pub = str(it.get("pub_date") or it.get("published") or
                      it.get("providerPublishTime") or "").strip()
            # An event with no source timestamp cannot be point-in-time —
            # stamp ingestion time as the conservative LOWER bound (an event
            # is never visible before we ingested it, so no look-ahead).
            published = pub or _now_iso()
            sid = _source_id("news", symbol,
                             it.get("url") or it.get("link") or title)
            try:
                aj_db.insert("aj_events", symbol=symbol.upper(),
                             event_type="news", source_id=sid,
                             published_at=published, ingested_at=_now_iso(),
                             title=title[:300],
                             summary=str(it.get("summary") or "")[:600],
                             url=str(it.get("url") or it.get("link") or "")[:400])
                n += 1
            except Exception:
                pass   # duplicate (UNIQUE) — already ingested
    except Exception:
        log.debug("news ingest failed for %s", symbol, exc_info=True)
    return n


def _ingest_filings(symbol: str) -> int:
    """Recent SEC filings -> unscored aj_events rows. Idempotent."""
    n = 0
    try:
        import sec_edgar
        for f in (sec_edgar.get_recent_filings(symbol, forms=list(_FILING_FORMS),
                                               limit=6) or []):
            if not isinstance(f, dict):
                continue
            form = str(f.get("form") or f.get("form_type") or "").strip()
            if not form:
                continue
            acc = str(f.get("accession") or f.get("accessionNumber") or "")
            fdate = str(f.get("filing_date") or f.get("filingDate") or "")
            title = "{} filing".format(form)
            items = f.get("items") or f.get("item_labels") or ""
            desc = str(f.get("description") or
                       f.get("primaryDocDescription") or "")
            summary = "; ".join(str(x) for x in
                                ([items] if isinstance(items, str) else items)
                                if x) or desc
            sid = _source_id("filing", symbol, acc or (form + fdate))
            try:
                aj_db.insert("aj_events", symbol=symbol.upper(),
                             event_type="filing", source_id=sid,
                             published_at=(fdate or _now_iso()),
                             ingested_at=_now_iso(), title=title[:300],
                             summary=str(summary)[:600],
                             url=str(f.get("url") or "")[:400])
                n += 1
            except Exception:
                pass
    except Exception:
        log.debug("filing ingest failed for %s", symbol, exc_info=True)
    return n


# ── LLM scoring (budget-capped) ────────────────────────────────────────────────

def _score_event(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """One event -> validated verdict dict, or None (leaves it unscored)."""
    try:
        import aj_routing
        from aj_schemas import extract_json
        is_filing = row.get("event_type") == "filing"
        prompt = ("Symbol {s}. Event ({t}, published {p}): {title}. {summary}"
                  .format(s=row.get("symbol"), t=row.get("event_type"),
                          p=str(row.get("published_at"))[:10],
                          title=row.get("title") or "",
                          summary=(row.get("summary") or "")[:500]))
        r = aj_routing.complete(
            prompt, system=_SCORE_SYS, role="events.score",
            sensitivity="public",
            max_tokens=_FILING_MAX_TOKENS if is_filing else _NEWS_MAX_TOKENS)
        if not (r and r.get("ok") and r.get("text")):
            return None
        d = extract_json(r["text"])
        if not d:
            return None
        try:
            direction = max(-1.0, min(1.0, float(d.get("direction", 0.0))))
            confidence = max(0.0, min(1.0, float(d.get("confidence", 0.0))))
            half_life = max(1.0, min(30.0, float(d.get("half_life_days", 5.0))))
        except (TypeError, ValueError):
            return None
        if not all(math.isfinite(v) for v in (direction, confidence, half_life)):
            return None
        mag = str(d.get("magnitude") or "small").lower()
        if mag not in ("small", "medium", "large"):
            mag = "small"
        return {"direction": round(direction, 3),
                "confidence": round(confidence, 3),
                "half_life_days": round(half_life, 1), "magnitude": mag,
                "rationale": str(d.get("rationale") or "")[:200],
                "model": str(r.get("chosen_model") or r.get("model") or ""),
                "cost_usd": float(r.get("cost_usd") or 0.0)}
    except Exception:
        log.debug("event scoring failed", exc_info=True)
        return None


def ingest_and_score(symbols: List[str],
                     cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The per-cycle entry point: ingest fresh events for `symbols` (capped),
    then LLM-score the newest unscored rows within the per-cycle budget.
    Gated by event_alpha_enabled; never raises."""
    out = {"ingested": 0, "scored": 0, "cost_usd": 0.0}
    try:
        c = _cfg(cfg)
        if not c.get("event_alpha_enabled"):
            return out
        max_syms = max(1, int(c.get("event_symbols_per_cycle", 10) or 10))
        budget = max(0, int(c.get("event_max_llm_per_cycle", 8) or 8))
        for sym in list(dict.fromkeys(s.upper() for s in symbols if s))[:max_syms]:
            if sym.startswith("OPT:") or sym.endswith("-USD"):
                continue          # no headlines/filings for options; crypto later
            out["ingested"] += _ingest_news(sym)
            out["ingested"] += _ingest_filings(sym)
        if budget <= 0:
            return out
        rows = aj_db.query(
            "SELECT * FROM aj_events WHERE scored_at IS NULL "
            "ORDER BY published_at DESC, id DESC LIMIT ?", (budget,))
        for row in rows:
            v = _score_event(row)
            if v is None:
                # Mark the attempt so a permanently-unparseable event doesn't
                # eat the whole budget every cycle: retry only twice.
                tries = int(row.get("score_tries") or 0) + 1
                cols = {"score_tries": tries}
                if tries >= 3:
                    cols.update({"scored_at": _now_iso(), "direction": 0.0,
                                 "confidence": 0.0, "half_life_days": 1.0,
                                 "magnitude": "small",
                                 "rationale": "unscorable after retries"})
                aj_db.update("aj_events", row["id"], **cols)
                continue
            price = None
            try:
                import fetcher
                q = fetcher.get_quote(row["symbol"]) or {}
                p = q.get("price")
                if isinstance(p, (int, float)) and math.isfinite(float(p)) and p > 0:
                    price = float(p)
            except Exception:
                pass
            aj_db.update("aj_events", row["id"], scored_at=_now_iso(),
                         direction=v["direction"], confidence=v["confidence"],
                         half_life_days=v["half_life_days"],
                         magnitude=v["magnitude"], rationale=v["rationale"],
                         model=v["model"], cost_usd=v["cost_usd"],
                         price_at=price)
            out["scored"] += 1
            out["cost_usd"] = round(out["cost_usd"] + v["cost_usd"], 6)
    except Exception:
        log.debug("ingest_and_score failed", exc_info=True)
    return out


# ── the signal (adapter contract; sim-clock aware; no look-ahead) ─────────────

_MAG_W = {"small": 0.5, "medium": 1.0, "large": 1.5}


def event_signal(symbol: str) -> Optional[Dict[str, Any]]:
    """Aggregate the scored, unexpired events for `symbol` into the standard
    adapter shape. Reads through aj_db.utc_now() and filters on published_at
    <= now, so a replay with an imported events table sees ONLY the past.
    None when there is no live event evidence (adapter contract)."""
    try:
        now = aj_db.utc_now()
        # events older than 3x the LONGEST half-life can't contribute; bound
        # the query window instead of scanning the whole table.
        floor = (now - timedelta(days=90)).isoformat()
        rows = aj_db.query(
            "SELECT direction, confidence, half_life_days, magnitude, "
            "published_at, event_type FROM aj_events WHERE symbol=? "
            "AND scored_at IS NOT NULL AND published_at <= ? "
            "AND published_at >= ? ORDER BY published_at DESC LIMIT 40",
            (symbol.upper(), now.isoformat(), floor))
        agg = 0.0
        n_live = 0
        conf_sum = 0.0
        for r in rows:
            try:
                direction = float(r.get("direction") or 0.0)
                conf = float(r.get("confidence") or 0.0)
                hl = max(1.0, float(r.get("half_life_days") or 5.0))
                pub = aj_db.parse_iso(str(r.get("published_at")))
                if pub is None or conf <= 0.0 or direction == 0.0:
                    continue
                age_days = max(0.0, (now - pub).total_seconds() / 86400.0)
                if age_days > 3.0 * hl:
                    continue          # fully decayed
                decay = math.exp(-math.log(2.0) * age_days / hl)
                w = direction * conf * _MAG_W.get(str(r.get("magnitude")), 0.5)
                agg += w * decay
                conf_sum += conf * decay
                n_live += 1
            except Exception:
                continue
        if n_live == 0:
            return None
        prob_up = 0.5 + 0.35 * math.tanh(1.5 * agg)
        prob_up = min(0.95, max(0.05, prob_up))
        # confidence grows with corroborating events, capped; scaled by the
        # decayed average confidence so stale weak events can't shout.
        confidence = min(0.9, (0.25 + 0.1 * min(n_live, 5))
                         * min(1.0, conf_sum / n_live * 1.5))
        return {"prob_up": round(prob_up, 4),
                "confidence": round(confidence, 3),
                "detail": "event-alpha: {} live event(s), agg {:+.2f}".format(
                    n_live, agg),
                "source": "events"}
    except Exception:
        log.debug("event_signal failed for %s", symbol, exc_info=True)
        return None


# ── outcome accountability ────────────────────────────────────────────────────

def score_due_outcomes(max_rows: int = 100) -> Dict[str, Any]:
    """Grade matured events: once ~1.5x the half-life has elapsed (calendar
    approximation of trading days), realized return vs predicted direction.
    Neutral calls (|direction| < 0.15) are not graded — there is nothing to
    be right about. Never raises."""
    out = {"graded": 0}
    try:
        now = aj_db.utc_now()
        rows = aj_db.query(
            "SELECT id, symbol, direction, half_life_days, price_at, scored_at "
            "FROM aj_events WHERE scored_at IS NOT NULL AND outcome_at IS NULL "
            "AND price_at IS NOT NULL ORDER BY scored_at ASC LIMIT ?",
            (max_rows,))
        due = []
        for r in rows:
            ts = aj_db.parse_iso(str(r.get("scored_at")))
            hl = max(1.0, float(r.get("half_life_days") or 5.0))
            if ts is not None and (now - ts) >= timedelta(days=hl * 1.5):
                due.append(r)
        if not due:
            return out
        import fetcher
        quotes = fetcher.get_quotes_batch(
            list(dict.fromkeys(r["symbol"] for r in due))) or {}
        for r in due:
            q = quotes.get(r["symbol"]) or {}
            p = q.get("price")
            if not (isinstance(p, (int, float)) and math.isfinite(float(p)) and p > 0):
                continue          # no quote yet — stays due
            ret = (float(p) / float(r["price_at"]) - 1.0) * 100.0
            direction = float(r.get("direction") or 0.0)
            hit = None
            if abs(direction) >= 0.15:
                hit = 1 if (ret > 0) == (direction > 0) else 0
            aj_db.update("aj_events", r["id"], outcome_at=_now_iso(),
                         realized_return_pct=round(ret, 3), hit=hit)
            out["graded"] += 1
    except Exception:
        log.debug("score_due_outcomes failed", exc_info=True)
    return out


def event_skill(limit: int = 500) -> Dict[str, Any]:
    """Per-event-type realized skill report for the CLI/status panel."""
    try:
        rows = aj_db.query(
            "SELECT event_type, hit FROM aj_events WHERE hit IS NOT NULL "
            "ORDER BY id DESC LIMIT ?", (limit,))
        by_type: Dict[str, List[int]] = {}
        for r in rows:
            by_type.setdefault(str(r["event_type"]), []).append(int(r["hit"]))
        skill = {t: {"n": len(v), "hit_rate": round(sum(v) / len(v), 3)}
                 for t, v in by_type.items() if v}
        pend = aj_db.query("SELECT COUNT(*) AS n FROM aj_events "
                           "WHERE scored_at IS NULL")[0]["n"]
        scored = aj_db.query("SELECT COUNT(*) AS n FROM aj_events "
                             "WHERE scored_at IS NOT NULL")[0]["n"]
        cost = aj_db.query("SELECT COALESCE(SUM(cost_usd),0) AS c "
                           "FROM aj_events")[0]["c"]
        return {"skill": skill, "scored": int(scored), "unscored": int(pend),
                "total_llm_cost_usd": round(float(cost or 0), 4)}
    except Exception as e:
        return {"error": str(e)[:120]}
