"""aj_signals.py — orthogonal alpha-signal adapters for the forecast ensemble.

These adapters wrap the app's already-computed *alt-data* sources (insider,
institutional, political, social) into the SAME signal-dict shape the forecast
ensemble consumes, so they can later be fused alongside the price/technical
signals. They are deliberately ORTHOGONAL to price — they carry information the
price/technical channels don't already encode.

THE CONTRACT (matches how forecast_ensemble.py consumes signals): each adapter
returns either None (source unavailable / no coverage) or a dict::

    {
      "prob_up":    <float in [0,1]>,   # directional read; 0.5 == neutral
      "detail":     <short str>,         # human-readable one-liner
      "confidence": <float in [0,1]>,    # optional; how much to trust it
      "source":     <str>,               # adapter id
    }

Guardrails:
  * No new dependencies — everything is lazily imported from existing modules.
  * Fail-OPEN: any error inside an adapter returns None, never raises.
  * Pure read — these never trade, never mutate state.
  * Each adapter is independently testable (source module monkeypatchable).

NOTE: there is intentionally NO GEX adapter here. Gamma exposure is
direction-symmetric (a volatility/timing signal, not a directional one) and is
consumed for entry TIMING elsewhere, not as a prob_up signal.
"""

import logging
import math
import threading
import time
from typing import Any, Dict, Optional

log = logging.getLogger("augur.aj_signals")


# --------------------------------------------------------------------------- #
# Shared mapping helpers
# --------------------------------------------------------------------------- #

def _clamp(x: float, lo: float, hi: float) -> float:
    """Clamp x into [lo, hi]."""
    return max(lo, min(hi, x))


def _score_to_prob(score: float) -> float:
    """Map a 0-100 convergence/conviction score to a prob_up in [0.05, 0.95].

    Calibrated-ish monotone map centered so that the neutral score (50) lands
    exactly on 0.5, and a +/-50 swing in score moves prob by +/-0.50 before
    clamping. The clamp keeps any single orthogonal signal from claiming
    certainty (no 0.0 / 1.0 reads):

        prob = clamp(0.5 + (score - 50) / 100, 0.05, 0.95)

    score=50  -> 0.50   (neutral)
    score=100 -> 0.95   (clamped from 1.00)
    score=0   -> 0.05   (clamped from 0.00)
    score=75  -> 0.75
    score=25  -> 0.25

    A NON-FINITE score (NaN/inf from a corrupt upstream composite) maps to the
    NEUTRAL 0.5, never conviction: adapters only None-check, and an unguarded
    NaN slid through the clamp asymmetry to a 0.95 max-conviction read.
    """
    s = float(score)
    if not math.isfinite(s):
        return 0.5
    return _clamp(0.5 + (s - 50.0) / 100.0, 0.05, 0.95)


def _score_to_confidence(score: float) -> float:
    """Confidence grows with distance from the neutral midpoint (50).

    A score sitting at 50 carries no directional conviction (confidence 0); a
    score pinned at 0 or 100 carries full conviction (confidence 1). A
    NON-FINITE score carries NO conviction (0.0) — a NaN used to clamp to the
    MAX (1.0), full trust in corrupt data.
    """
    s = float(score)
    if not math.isfinite(s):
        return 0.0
    return _clamp(abs(s - 50.0) / 50.0, 0.0, 1.0)


# --------------------------------------------------------------------------- #
# 1. Smart-money convergence  (institutional + insider + options + technical)
# --------------------------------------------------------------------------- #

def smart_money_signal(symbol: str) -> Optional[dict]:
    """Directional read from smart_money.compute_score (0-100 conviction).

    compute_score returns {"score": <0-100 or None>, "signal": str, ...}; a
    None score means the source had no price data. We map the score onto the
    shared score->prob curve. Returns None on any failure or missing score.
    """
    try:
        import smart_money
        res = smart_money.compute_score(symbol)
        if not isinstance(res, dict):
            return None
        score = res.get("score")
        if score is None:
            return None
        score = float(score)
        sig = res.get("signal") or ""
        return {
            "prob_up": _score_to_prob(score),
            "confidence": _score_to_confidence(score),
            "detail": "smart-money score {:.0f}/100{}".format(
                score, " ({})".format(sig) if sig else ""),
            "source": "smart_money",
        }
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# 2. Synthetic insider convergence  (6-channel composite)
# --------------------------------------------------------------------------- #

def insider_signal(symbol: str) -> Optional[dict]:
    """Directional read from synthetic_insider.compute_composite.

    compute_composite returns {"composite_score": <0-100>, "coverage": <0-1>,
    "convergence_count": int, "signal": str, ...} or {"error": str}. We map the
    composite onto the shared score->prob curve and DAMPEN the CONFIDENCE channel
    ONLY when coverage is poor (few live channels) — a composite built from one
    live channel shouldn't be TRUSTED as much as one built from six. We do NOT
    also pull the prob tilt toward neutral: damping both channels double-penalizes
    the same coverage gap (the fusion layer already down-weights by confidence).
    Returns None on error or missing data.
    """
    try:
        import synthetic_insider
        res = synthetic_insider.compute_composite(symbol)
        if not isinstance(res, dict) or "error" in res:
            return None
        score = res.get("composite_score")
        if score is None:
            return None
        score = float(score)
        # coverage in [0,1] = fraction of channels with live data.
        coverage = res.get("coverage")
        coverage = float(coverage) if coverage is not None else 1.0
        coverage = _clamp(coverage, 0.0, 1.0)
        # Poor coverage -> cut CONFIDENCE only (single channel). The prob tilt is
        # left at full strength; the ensemble already scales each signal by its
        # confidence, so damping prob too would penalize the same gap twice.
        prob = _clamp(_score_to_prob(score), 0.05, 0.95)
        confidence = _score_to_confidence(score) * coverage
        conv = res.get("convergence_count")
        sig = res.get("signal") or ""
        return {
            "prob_up": prob,
            "confidence": _clamp(confidence, 0.0, 1.0),
            "detail": "insider composite {:.0f}/100, conv={}, cover={:.0%}{}".format(
                score, conv if conv is not None else "?", coverage,
                " ({})".format(sig) if sig else ""),
            "source": "insider",
        }
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# 3. Congressional trades  ($-weighted net buy/sell pressure)
# --------------------------------------------------------------------------- #

def congress_signal(symbol: str, days: int = 90) -> Optional[dict]:
    """Directional read from congressional trading pressure.

    Uses congress.get_recent_trades(days, tickers=[symbol]) which returns
    {"trades": [ {ticker, txn_type, amount_val, ...}, ... ], ...}. We compute a
    $-weighted net pressure = (buy$ - sell$) / (buy$ + sell$) in [-1, 1] and map
    it to a SMALL prob tilt around 0.5 (this is sparse, lagged disclosure data —
    informative but not a strong directional edge). Returns None when there are
    no qualifying trades for the symbol.
    """
    try:
        import congress
        res = congress.get_recent_trades(days=days, tickers=[symbol])
        # get_recent_trades returns a dict {"trades": [...]}; tolerate a bare
        # list too in case a caller/monkeypatch hands one back directly.
        if isinstance(res, dict):
            trades = res.get("trades") or []
        elif isinstance(res, list):
            trades = res
        else:
            return None

        sym = (symbol or "").upper()
        # First pass: collect this symbol's directional trades with their dollar
        # amounts (None when the band is missing). We must NOT mix $-weighted and
        # unit-$1 trades in one net — a single $50M-banded buy alongside three
        # amountless sells would be wildly miscalibrated. So if ANY qualifying
        # trade lacks an amount we fall back to pure TRADE-COUNT netting for ALL
        # of this symbol's trades; otherwise we use the $-weighted net.
        dir_trades = []   # (is_buy, amount_or_None)
        any_missing_amt = False
        for t in trades:
            if not isinstance(t, dict):
                continue
            if (t.get("ticker") or "").upper() != sym:
                continue
            txn = (t.get("txn_type") or "").lower()
            if "buy" in txn or "purchase" in txn:
                is_buy = True
            elif "sell" in txn or "sale" in txn:
                is_buy = False
            else:
                continue  # exchanges / other types are direction-neutral
            amt = t.get("amount_val")
            try:
                amt = float(amt) if amt is not None else None
            except (TypeError, ValueError):
                amt = None
            if amt is None or amt <= 0:
                any_missing_amt = True
                amt = None
            dir_trades.append((is_buy, amt))

        n_buy = sum(1 for is_buy, _ in dir_trades if is_buy)
        n_sell = sum(1 for is_buy, _ in dir_trades if not is_buy)
        n = n_buy + n_sell
        if n == 0:
            return None  # no directional trades for this symbol

        if any_missing_amt:
            # Pure trade-count netting (every trade weighted 1) so we never blend
            # a $-banded trade with an amountless one.
            buy_amt, sell_amt = float(n_buy), float(n_sell)
        else:
            buy_amt = sum(a for is_buy, a in dir_trades if is_buy)
            sell_amt = sum(a for is_buy, a in dir_trades if not is_buy)
        total = buy_amt + sell_amt
        if total <= 0:
            return None

        net = (buy_amt - sell_amt) / total  # in [-1, 1]
        # Small tilt: a fully one-sided book moves prob by at most +/-0.25.
        prob = _clamp(0.5 + 0.25 * net, 0.05, 0.95)
        # Confidence scales with how lopsided the book is AND how many trades
        # back it (a single trade shouldn't speak as confidently as ten).
        count_factor = _clamp(n / 5.0, 0.0, 1.0)
        confidence = _clamp(abs(net) * count_factor, 0.0, 1.0)
        return {
            "prob_up": prob,
            "confidence": confidence,
            "detail": "congress {} buy / {} sell, net {:+.0%}".format(
                n_buy, n_sell, net),
            "source": "congress",
        }
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# 4. Social sentiment  (StockTwits bull/bear ratio)  — noisy, low confidence
# --------------------------------------------------------------------------- #

def social_signal(symbol: str) -> Optional[dict]:
    """Directional read from social sentiment (StockTwits bull/bear ratio).

    alt_signals.stocktwits_symbol_sentiment(symbol) returns
    {"bull_ratio": <0-1 or None>, "bullish": int, "bearish": int, ...} or None.
    Retail social sentiment is NOISY and weakly predictive, so we map the
    bull_ratio onto a SMALL prob tilt and keep the natural confidence low
    (capped well below 1). Returns None when there's no tagged sentiment.
    """
    try:
        import alt_signals
        res = alt_signals.stocktwits_symbol_sentiment(symbol)
        if not isinstance(res, dict):
            return None
        ratio = res.get("bull_ratio")
        bull = res.get("bullish") or 0
        bear = res.get("bearish") or 0
        tagged = (bull or 0) + (bear or 0)
        if ratio is None or tagged == 0:
            return None  # no tagged sentiment to read

        ratio = _clamp(float(ratio), 0.0, 1.0)
        # bull_ratio is in [0,1] (0.5 == balanced). Map to a SMALL tilt:
        # a fully bullish board moves prob by at most +/-0.20.
        prob = _clamp(0.5 + 0.20 * (ratio - 0.5) * 2.0, 0.05, 0.95)
        # Low confidence by design: scale with sample size but cap at 0.4 so
        # this never out-shouts the harder signals during fusion.
        size_factor = _clamp(tagged / 30.0, 0.0, 1.0)
        confidence = _clamp(abs(ratio - 0.5) * 2.0 * size_factor * 0.4, 0.0, 0.4)
        return {
            "prob_up": prob,
            "confidence": confidence,
            "detail": "social bull_ratio {:.0%} ({}B/{}S)".format(ratio, bull, bear),
            "source": "social",
        }
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# 4b. Event alpha (LLM-scored news/filings, point-in-time)
# --------------------------------------------------------------------------- #

def events_signal(symbol: str) -> Optional[dict]:
    """Decay-weighted aggregate of LLM-scored market events (aj_events).
    Sim-clock aware and published_at-filtered, so it is replay-safe. Like
    every adapter it earns ensemble weight only through the IC promotion
    gate + adapter scorecard — the LLM's opinion is measured, not trusted."""
    try:
        import aj_events
        return aj_events.event_signal(symbol)
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# 5. Aggregator
# --------------------------------------------------------------------------- #

# (name, adapter) pairs. Order is stable for deterministic output/tests.
_ADAPTERS = (
    ("smart_money", smart_money_signal),
    ("insider", insider_signal),
    ("congress", congress_signal),
    ("social", social_signal),
    ("events", events_signal),
)


def all_signals(symbol: str, cfg: Optional[dict] = None,
                horizon_days: Optional[int] = None) -> Dict[str, dict]:
    """Run every orthogonal adapter resiliently and collect the live ones.

    Returns {name: sigdict} for each adapter that produced a non-None result.
    One adapter raising (despite the fail-open contract) must not kill the
    others, so each call is individually guarded here too.

    This is THE fusion seam: forecast_ensemble consumes exactly this dict when
    multi_factor_signals is on. So the opt-in adapter scorecard (section 6)
    hooks here — behind cfg["adapter_scorecard"] (default OFF) it (a) logs each
    adapter's forecast into aj_signal_scores for later scoring, and (b) decays
    each adapter's CONFIDENCE by its realized hit-rate weight. Flag OFF (the
    default — the key isn't even in aj_config.DEFAULTS yet) leaves the output
    byte-identical to the pre-scorecard behavior. `cfg`/`horizon_days` are
    optional so the existing all_signals(symbol) call sites need no change.
    """
    out: Dict[str, dict] = {}
    for name, fn in _ADAPTERS:
        try:
            sig = fn(symbol)
        except Exception:
            sig = None
        if sig is not None:
            out[name] = sig

    # ── opt-in adapter scorecard (section 6) — fail-open, default OFF ────────
    try:
        if cfg is None:
            import aj_config
            cfg = aj_config.get_config() or {}
    except Exception:
        cfg = cfg or {}
    if out and cfg.get("adapter_scorecard"):
        # Log FIRST (the raw adapter output — the ledger scores each adapter's
        # native forecast, not the decayed one), then apply the decay weights.
        try:
            if horizon_days is None:
                # Match the ensemble's own horizon so the scorecard grades the
                # same bet the fusion actually made.
                horizon_days = int(cfg.get("forecast_horizon_days") or 20)
            import fetcher
            q = (fetcher.get_quotes_batch([symbol]) or {}).get(
                (symbol or "").upper()) or {}
            price_at = q.get("price")
            # No entry price -> the row could never be scored; skip logging
            # rather than filling the ledger with permanently-unscorable rows.
            if price_at is not None:
                log_adapter_signals(symbol, out, horizon_days, float(price_at))
        except Exception:
            log.debug("adapter scorecard logging skipped", exc_info=True)
        try:
            w = adapter_weights()
            for name, sig in out.items():
                conf = sig.get("confidence")
                if conf is None:
                    continue
                sig["confidence"] = _clamp(
                    float(conf) * float(w.get(name, 1.0)), 0.0, 1.0)
        except Exception:
            log.debug("adapter scorecard weighting skipped", exc_info=True)
    return out


# --------------------------------------------------------------------------- #
# 6. Adapter scorecard — forecast ledger + confidence decay (opt-in)
# --------------------------------------------------------------------------- #
#
# Every adapter forecast is a testable claim: "prob_up > 0.5 within N trading
# days". The scorecard records each claim in aj_signal_scores (migration v10)
# at fusion time, scores it against the realized price once the horizon has
# elapsed, and DECAYS the confidence of adapters whose realized hit-rate is
# poor — so a chronically wrong adapter loses its voice in the ensemble
# without any manual tuning, while a cold-start adapter keeps a neutral 1.0.
#
# Flag: cfg["adapter_scorecard"] (bool, default OFF). NOTE for the config
# owner: the key is not yet in aj_config.DEFAULTS, so it must be added there
# (default False) before the UI/settings path can enable it; until then only
# explicit-cfg callers (tests) can turn it on.
#
# aj_signal_scores is deliberately NOT in aj_db._ALLOWED_TABLES (that
# whitelist is for the trading tables), so writes go directly through the
# shared `database` write lock, mirroring aj_autonomy's settings helpers.

_SCORE_BATCH_CAP = 200        # max rows scored per score_due_adapter_signals()
_WEIGHT_WINDOW = 200          # trailing scored rows per adapter for hit-rate
_WEIGHT_MIN_SAMPLES = 30      # below this: cold start -> neutral weight 1.0
_WEIGHTS_TTL_S = 600.0        # memoize adapter_weights for 10 min per process

_weights_lock = threading.Lock()
_weights_memo: Dict[str, Any] = {"at": 0.0, "val": None}


def log_adapter_signals(symbol: str, signals: Dict[str, dict],
                        horizon_days: int, price_at: float) -> int:
    """Insert one aj_signal_scores row per adapter signal (ts=now, scored_at
    NULL — an open forecast awaiting scoring). Returns rows written; fail-open
    (0 on any error — the ledger must never break a forecast)."""
    if not signals:
        return 0
    try:
        import aj_db
        import database as _db
        now = aj_db.utc_now_iso()
        rows = []
        for name, sig in signals.items():
            if not isinstance(sig, dict) or sig.get("prob_up") is None:
                continue
            rows.append((now, (symbol or "").upper(), name,
                         float(sig["prob_up"]),
                         float(sig.get("confidence") or 0.0),
                         int(horizon_days),
                         float(price_at) if price_at is not None else None))
        if not rows:
            return 0
        with _db._write_lock:
            conn = _db.get_conn()
            conn.executemany(
                "INSERT INTO aj_signal_scores "
                "(ts, symbol, adapter, prob_up, confidence, horizon_days, price_at) "
                "VALUES (?,?,?,?,?,?,?)", rows)
            conn.commit()
        return len(rows)
    except Exception:
        log.debug("log_adapter_signals failed", exc_info=True)
        return 0


def score_due_adapter_signals() -> Dict[str, Any]:
    """Score matured forecasts: rows with scored_at NULL whose ts is older than
    the horizon. horizon_days is TRADING days but ts arithmetic is calendar
    time, so we approximate with horizon_days * 1.5 calendar days (5 trading
    days ≈ 7 calendar days; 1.5 over-waits slightly, which only makes the
    grade fairer to the adapter). Sets realized_up = price_now > price_at and
    hit = (prob_up > 0.5) == realized_up. Batches ALL symbols into one
    fetcher.get_quotes_batch call; caps _SCORE_BATCH_CAP rows per call so a
    huge backlog can't stall a cycle. Fail-open."""
    try:
        import aj_db
        import database as _db
        now = aj_db.utc_now()
        # Over-fetch beyond the cap (oldest first): recent rows may not be due
        # yet, and the due filter needs Python-side ISO parsing (SQLite's date
        # functions choke on the microsecond+offset ISO format we store).
        rows = aj_db.query(
            "SELECT id, ts, symbol, prob_up, horizon_days, price_at "
            "FROM aj_signal_scores WHERE scored_at IS NULL "
            "ORDER BY ts ASC LIMIT ?", (_SCORE_BATCH_CAP * 5,))
        due = []
        for r in rows:
            ts = aj_db.parse_iso(r.get("ts"))
            h = int(r.get("horizon_days") or 5)
            if ts is None or (now - ts).total_seconds() < h * 1.5 * 86400.0:
                continue
            due.append(r)
            if len(due) >= _SCORE_BATCH_CAP:
                break
        if not due:
            return {"scored": 0, "skipped": 0}
        import fetcher
        quotes = fetcher.get_quotes_batch(sorted({r["symbol"] for r in due})) or {}
        now_iso = aj_db.utc_now_iso()
        updates, skipped = [], 0
        for r in due:
            price_at = r.get("price_at")
            if price_at is None:
                # Permanently unscorable (no entry price): retire the row
                # (scored_at set, realized/hit NULL) so it stops re-surfacing.
                updates.append((now_iso, None, None, r["id"]))
                continue
            price_now = (quotes.get(r["symbol"]) or {}).get("price")
            if price_now is None:
                skipped += 1   # quote miss — leave open for the next pass
                continue
            realized = 1 if float(price_now) > float(price_at) else 0
            prob_up = float(r.get("prob_up") or 0.5)
            hit = 1 if ((prob_up > 0.5) == bool(realized)) else 0
            updates.append((now_iso, realized, hit, r["id"]))
        if updates:
            with _db._write_lock:
                conn = _db.get_conn()
                # scored_at IS NULL guard: a concurrent scorer that already
                # graded the row wins; we never overwrite a landed grade.
                conn.executemany(
                    "UPDATE aj_signal_scores SET scored_at=?, realized_up=?, hit=? "
                    "WHERE id=? AND scored_at IS NULL", updates)
                conn.commit()
        return {"scored": len(updates), "skipped": skipped}
    except Exception:
        log.debug("score_due_adapter_signals failed", exc_info=True)
        return {"scored": 0, "skipped": 0, "error": True}


def _hit_rate_weight(hit_rate: float) -> float:
    """Map a realized hit-rate onto a confidence multiplier in [0.25, 1.0]:

        weight = clamp((hit_rate - 0.35) / 0.15, 0.25, 1.0)

    A smooth, monotone linear ramp: hit_rate >= 0.50 (coin-flip or better —
    remember these are CONFIDENCE-weighted votes, so ~0.5 is not damning)
    keeps the full 1.0 voice; below 0.50 the weight slides linearly down,
    reaching the 0.25 floor at hit_rate <= 0.3875. The floor is deliberately
    non-zero: a cold streak quiets an adapter but never permanently silences
    it, so it can still earn its voice back as new scored rows roll into the
    trailing window."""
    return _clamp((float(hit_rate) - 0.35) / 0.15, 0.25, 1.0)


def adapter_weights() -> Dict[str, float]:
    """{adapter: confidence multiplier} from realized performance — hit-rate
    over the trailing _WEIGHT_WINDOW scored rows per adapter, mapped through
    _hit_rate_weight. Fewer than _WEIGHT_MIN_SAMPLES scored rows -> neutral
    1.0 (cold start must not punish an adapter for being new). Memoized for
    _WEIGHTS_TTL_S per process (10 min): weights move on the scoring cadence
    (days), so per-symbol recomputes inside one cycle are pure waste."""
    now = time.time()
    with _weights_lock:
        if (_weights_memo["val"] is not None
                and now - _weights_memo["at"] < _WEIGHTS_TTL_S):
            return dict(_weights_memo["val"])
    out: Dict[str, float] = {}
    try:
        import aj_db
        for name, _fn in _ADAPTERS:
            rows = aj_db.query(
                "SELECT hit FROM aj_signal_scores "
                "WHERE adapter=? AND scored_at IS NOT NULL AND hit IS NOT NULL "
                "ORDER BY id DESC LIMIT ?", (name, _WEIGHT_WINDOW))
            n = len(rows)
            if n < _WEIGHT_MIN_SAMPLES:
                out[name] = 1.0
            else:
                hit_rate = sum(1 for r in rows if r.get("hit")) / float(n)
                out[name] = round(_hit_rate_weight(hit_rate), 4)
    except Exception:
        log.debug("adapter_weights failed; neutral weights", exc_info=True)
        out = {name: 1.0 for name, _fn in _ADAPTERS}
    with _weights_lock:
        _weights_memo["at"] = now
        _weights_memo["val"] = dict(out)
    return out


def _invalidate_adapter_weights() -> None:
    """Drop the adapter_weights memo (tests; or after a scoring pass when an
    immediate refresh is wanted)."""
    with _weights_lock:
        _weights_memo["at"] = 0.0
        _weights_memo["val"] = None
