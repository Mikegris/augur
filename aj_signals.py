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

from typing import Dict, Optional


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
    """
    return _clamp(0.5 + (float(score) - 50.0) / 100.0, 0.05, 0.95)


def _score_to_confidence(score: float) -> float:
    """Confidence grows with distance from the neutral midpoint (50).

    A score sitting at 50 carries no directional conviction (confidence 0); a
    score pinned at 0 or 100 carries full conviction (confidence 1).
    """
    return _clamp(abs(float(score) - 50.0) / 50.0, 0.0, 1.0)


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
    composite onto the shared score->prob curve and DAMPEN both confidence and
    the directional tilt when coverage is poor (few live channels) — a composite
    built from one live channel shouldn't speak as loudly as one built from six.
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
        # Poor coverage -> pull prob back toward neutral and cut confidence.
        prob = 0.5 + (_score_to_prob(score) - 0.5) * coverage
        prob = _clamp(prob, 0.05, 0.95)
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
        buy_amt = sell_amt = 0.0
        n_buy = n_sell = 0
        for t in trades:
            if not isinstance(t, dict):
                continue
            if (t.get("ticker") or "").upper() != sym:
                continue
            txn = (t.get("txn_type") or "").lower()
            # Dollar weight: fall back to a nominal $1 when the band is missing
            # so a trade still counts toward direction even without an amount.
            amt = t.get("amount_val")
            try:
                amt = float(amt) if amt is not None else 0.0
            except (TypeError, ValueError):
                amt = 0.0
            weight = amt if amt > 0 else 1.0
            if "buy" in txn or "purchase" in txn:
                buy_amt += weight
                n_buy += 1
            elif "sell" in txn or "sale" in txn:
                sell_amt += weight
                n_sell += 1
            # exchanges / other types are direction-neutral -> ignored

        total = buy_amt + sell_amt
        n = n_buy + n_sell
        if n == 0 or total <= 0:
            return None  # no directional trades for this symbol

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
# 5. Aggregator
# --------------------------------------------------------------------------- #

# (name, adapter) pairs. Order is stable for deterministic output/tests.
_ADAPTERS = (
    ("smart_money", smart_money_signal),
    ("insider", insider_signal),
    ("congress", congress_signal),
    ("social", social_signal),
)


def all_signals(symbol: str) -> Dict[str, dict]:
    """Run every orthogonal adapter resiliently and collect the live ones.

    Returns {name: sigdict} for each adapter that produced a non-None result.
    One adapter raising (despite the fail-open contract) must not kill the
    others, so each call is individually guarded here too.
    """
    out: Dict[str, dict] = {}
    for name, fn in _ADAPTERS:
        try:
            sig = fn(symbol)
        except Exception:
            sig = None
        if sig is not None:
            out[name] = sig
    return out
