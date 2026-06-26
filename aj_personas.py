"""AJTA — Analyst Council: investor personas + optional FinGPT prior + reports
(merge plan §6, Phase 6). ALL opt-in.

Persona analysts (pattern from virattt/ai-hedge-fund, MIT — see
THIRD_PARTY_NOTICES.md) are extra analysts that view the SAME evidence through a
distinct investing lens (value / growth / contrarian / quality / macro). They
plug into the exact analyst interface (symbol, call, cfg) -> AnalystReport, so
the council can fan them out alongside the four base analysts when
personas_enabled.

fingpt_sentiment() is an OPTIONAL numeric sentiment prior. Its FUNCTIONAL
default is a lightweight, dependency-free finance lexicon scorer (a curated
bullish/bearish term bag-of-words with negation handling) over the fetched
headlines — NOT FinGPT. It keeps an upgrade hook that prefers a project-local
FinBERT/FinGPT helper (`fingpt_local`/`finbert_local`) when one is installed,
but never requires it: pure-stdlib lexicon path runs in the bundle as-is. It is
lazy + fail-open: any error returns None and it never fabricates a number.

council_report() renders a FinRobot-style (Apache-2.0) markdown brief from a set
of council decisions.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

import aj_routing
from aj_analyst_util import CallFn, default_call, _safe, _fmt, _neutral
from aj_schemas import AnalystReport

log = logging.getLogger("augur.aj_personas")


# persona lens system prompts (generic archetypes — not impersonations).
_PERSONAS: Dict[str, str] = {
    "value": ("a deep-value investor (margin of safety, low valuation multiples, "
              "durable cash flows). Penalize rich valuations even on good companies."),
    "growth": ("a growth investor (revenue/earnings acceleration, expanding TAM, "
               "momentum). Tolerate high multiples when growth justifies them."),
    "contrarian": ("a contrarian (fade crowded positioning and over-extended "
                   "sentiment; buy capitulation, sell euphoria)."),
    "quality": ("a quality investor (high ROE/margins, low leverage, consistent "
                "compounding). Avoid low-quality balance sheets."),
    "macro": ("a top-down macro investor (rates, liquidity, regime). Weigh the "
              "macro backdrop heavily over single-name specifics."),
}

_SYS = ("You are {lens} Analyze ONLY the evidence about {symbol} through this "
        "lens. Respond with STRICT JSON: {{\"band\":\"<label>\",\"score\":<0-10, "
        "10=most bullish>,\"confidence\":<0-1>,\"key_points\":[\"...\"],"
        "\"narrative\":\"<=70 words\"}}. No prose outside the JSON.")


def _evidence(symbol: str) -> Dict[str, str]:
    """Compact, shared evidence bundle (fundamentals + indicators), fail-open."""
    ev: Dict[str, str] = {}
    try:
        import fetcher
        ev["fundamentals"] = _fmt(_safe(fetcher.get_fundamentals, symbol), keep=[
            "pe_ratio", "forward_pe", "peg_ratio", "ps_ratio", "pb_ratio",
            "return_on_equity", "profit_margin", "debt_equity", "revenue_growth",
            "earnings_growth", "free_cashflow", "beta"])
        bars = _safe(fetcher.get_chart_data, symbol, "6mo", "1d")
        if isinstance(bars, list) and bars:
            ev["indicators"] = _fmt(_safe(fetcher.compute_indicators, bars), keep=[
                "rsi", "macd", "current_price", "price_vs_sma50", "price_vs_sma200"])
    except Exception:
        pass
    return {k: v for k, v in ev.items() if v}


def _persona_fn(name: str, lens: str):
    def fn(symbol: str, call: CallFn = default_call,
           cfg: Optional[Dict] = None) -> AnalystReport:
        ev = _evidence(symbol)
        if not ev:
            return _neutral("persona:" + name, "no evidence")
        sys = _SYS.format(lens=lens, symbol=symbol)
        prompt = "Symbol: {}\nEvidence:\n{}".format(
            symbol, "\n".join("[{}] {}".format(k, v) for k, v in ev.items()))
        text = call(aj_routing.DEEP, "council.persona." + name, sys, prompt)
        if not text:
            return _neutral("persona:" + name, "model unavailable")
        rep = AnalystReport.from_llm("persona:" + name, text, evidence_refs=list(ev.keys()))
        return rep
    fn.__name__ = "persona_" + name
    return fn


def persona_analysts(cfg: Optional[Dict] = None) -> Dict[str, Callable]:
    """Analyst-compatible callables for each persona, keyed 'persona:<name>'.
    Empty unless personas_enabled."""
    if not (cfg or {}).get("personas_enabled"):
        return {}
    return {"persona:" + n: _persona_fn(n, lens) for n, lens in _PERSONAS.items()}


# ── optional numeric sentiment prior (lexicon default + FinGPT hook) ───────────

# Curated finance polarity lexicon. Terms are lower-cased; the scorer matches on
# word boundaries so "beat" won't fire inside "beaten path". Weights are small
# integers (strength of the cue), summed per headline. This is a transparent
# bag-of-words VIBE check, NOT a trained model — it exists so the prior actually
# scores something rather than always returning None.
_BULLISH_TERMS: Dict[str, int] = {
    "beat": 2, "beats": 2, "surge": 2, "surges": 2, "soar": 2, "soars": 2,
    "rally": 2, "rallies": 2, "jump": 1, "jumps": 1, "gain": 1, "gains": 1,
    "rise": 1, "rises": 1, "climb": 1, "climbs": 1, "upgrade": 2, "upgraded": 2,
    "outperform": 2, "bullish": 2, "record": 1, "profit": 1, "profits": 1,
    "growth": 1, "strong": 1, "boost": 1, "boosts": 1, "raises": 1, "raised": 1,
    "topped": 2, "tops": 2, "buyback": 1, "dividend": 1, "expand": 1,
    "expands": 1, "optimistic": 2, "momentum": 1, "breakout": 2, "wins": 1,
    "approval": 1, "approved": 1, "exceeds": 2, "exceed": 2, "rebound": 2,
    "recovery": 1, "upbeat": 2, "accelerate": 1, "accelerates": 1,
}
_BEARISH_TERMS: Dict[str, int] = {
    "miss": 2, "misses": 2, "missed": 2, "plunge": 2, "plunges": 2, "drop": 1,
    "drops": 1, "fall": 1, "falls": 1, "slump": 2, "slumps": 2, "tumble": 2,
    "tumbles": 2, "decline": 1, "declines": 1, "downgrade": 2, "downgraded": 2,
    "underperform": 2, "bearish": 2, "loss": 1, "losses": 1, "weak": 1,
    "warning": 2, "warns": 2, "cut": 1, "cuts": 1, "slash": 2, "slashes": 2,
    "lawsuit": 2, "probe": 1, "investigation": 1, "fraud": 3, "bankruptcy": 3,
    "default": 2, "recall": 2, "layoffs": 2, "layoff": 2, "selloff": 2,
    "crash": 3, "crashes": 3, "fears": 1, "concern": 1, "concerns": 1,
    "pessimistic": 2, "halt": 2, "halts": 2, "delays": 1, "delay": 1,
    "shortfall": 2, "disappointing": 2, "disappoints": 2, "sinks": 2,
}
# A negator immediately before a polarity term flips its sign (e.g. "not strong",
# "no growth", "fails to beat").
_NEGATORS = frozenset({"not", "no", "never", "without", "fails", "fail",
                       "failed", "lacks", "lack", "lacking", "isn't", "wasn't",
                       "won't", "didn't", "doesn't"})

import re as _re_sent
_TOKEN_RE = _re_sent.compile(r"[a-z']+")


def _lexicon_score_headlines(titles: List[str]) -> Optional[float]:
    """Lexicon bag-of-words polarity over headlines -> 0..10 sentiment prior
    (5 = neutral). Negation-aware (a negator immediately before a polarity term
    flips it). Returns None if no polarity cue fired at all. Never raises."""
    net = 0
    hits = 0
    for title in titles:
        toks = _TOKEN_RE.findall(str(title).lower())
        for i, tok in enumerate(toks):
            w = _BULLISH_TERMS.get(tok)
            sign = 1
            if w is None:
                w = _BEARISH_TERMS.get(tok)
                sign = -1
            if w is None:
                continue
            if i > 0 and toks[i - 1] in _NEGATORS:
                sign = -sign
            net += sign * w
            hits += 1
    if hits == 0:
        return None
    # Squash the net polarity into 0..10 around a neutral 5. Divide by hits so a
    # long news day doesn't saturate; scale so a couple of clear cues move ~2 pts.
    import math
    score = 5.0 + 5.0 * math.tanh(net / max(2.0, hits))
    return max(0.0, min(10.0, score))


def fingpt_sentiment(symbol: str) -> Optional[Dict[str, Any]]:
    """Optional numeric sentiment prior over recent headlines. Returns
    {score:0-10, label, source} or None.

    Scoring path (in order of preference):
      1. A project-local FinBERT/FinGPT helper (`fingpt_local`/`finbert_local`
         exposing `score_headlines(titles)->0..10`) IF the operator installed
         one — the optional model-based upgrade.
      2. Otherwise, the built-in dependency-free finance LEXICON scorer (the
         functional default; pure stdlib, always available in the bundle).

    Lazy + fail-open: any error returns None and it NEVER fabricates a number.
    `source` is "fingpt" (helper) or "lexicon" (default) so callers can tell
    which path produced the prior."""
    try:
        import fetcher
        headlines = _safe(fetcher.get_news, symbol, 10) or []
        titles = [h.get("title") for h in headlines if isinstance(h, dict) and h.get("title")]
        if not titles:
            return None
        # Optional upgrade hook: prefer a project-local FinBERT/FinGPT helper if
        # the operator installed one. We keep the integration point without
        # bundling any weights — absence simply falls through to the lexicon.
        import importlib.util
        score = None
        source = "lexicon"
        for modname in ("fingpt_local", "finbert_local"):
            try:
                if importlib.util.find_spec(modname) is not None:
                    import importlib
                    helper = importlib.import_module(modname)
                    if hasattr(helper, "score_headlines"):
                        score = float(helper.score_headlines(titles))
                        source = "fingpt"
                        break
            except Exception:
                continue  # bad helper => fall back to lexicon, never raise
        if score is None:
            score = _lexicon_score_headlines(titles)
        if score is None or score != score:   # no cue fired, or NaN
            return None
        score = max(0.0, min(10.0, float(score)))
        label = "BULLISH" if score >= 6 else ("BEARISH" if score <= 4 else "NEUTRAL")
        return {"score": score, "label": label, "source": source}
    except Exception:
        log.debug("fingpt_sentiment unavailable", exc_info=True)
        return None


# ── FinRobot-style report ─────────────────────────────────────────────────────

def council_report(decisions: List[Any]) -> str:
    """Render a concise markdown brief from council decisions (objects with
    .symbol/.rating/.action/.conviction/.thesis/.dissent or equivalent dicts)."""
    lines = ["# Analyst Council — Decision Brief", ""]
    if not decisions:
        return "\n".join(lines + ["_no council decisions_"])
    for d in decisions:
        g = (lambda k: getattr(d, k, None) if not isinstance(d, dict) else d.get(k))
        rating = g("rating")
        rating = getattr(rating, "value", rating)
        action = g("action")
        action = getattr(action, "value", action)
        conv = g("conviction") or 0.0
        try:
            convf = float(conv)
        except (TypeError, ValueError):
            convf = 0.0
        lines.append("## {sym} — {r}/{a} (conviction {c:.0%})".format(
            sym=g("symbol"), r=(rating if rating is not None else "?"),
            a=(action if action is not None else "?"), c=convf))
        if g("thesis"):
            lines.append("- **Thesis:** {}".format(str(g("thesis"))[:400]))
        if g("dissent"):
            lines.append("- **Dissent:** {}".format(str(g("dissent"))[:300]))
        lines.append("")
    return "\n".join(lines)
