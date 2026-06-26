"""AJTA — Analyst Council: investor personas + optional FinGPT prior + reports
(merge plan §6, Phase 6). ALL opt-in.

Persona analysts (pattern from virattt/ai-hedge-fund, MIT — see
THIRD_PARTY_NOTICES.md) are extra analysts that view the SAME evidence through a
distinct investing lens (value / growth / contrarian / quality / macro). They
plug into the exact analyst interface (symbol, call, cfg) -> AnalystReport, so
the council can fan them out alongside the four base analysts when
personas_enabled.

fingpt_sentiment() is an OPTIONAL numeric sentiment prior (pattern from
AI4Finance/FinGPT, MIT). It is lazy + fail-open: if the optional model stack
isn't installed it returns None, adding ZERO required dependency to the bundle.

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


# ── optional FinGPT numeric sentiment prior (lazy, fail-open) ──────────────────

def fingpt_sentiment(symbol: str) -> Optional[Dict[str, Any]]:
    """Optional numeric sentiment prior via a FinGPT-style local model. Returns
    {score:0-10, label, source} or None when the optional model stack isn't
    installed (the bundle never requires it). Never raises."""
    try:
        # Lazy: only import if the operator installed the optional stack.
        import importlib
        import importlib.util
        if importlib.util.find_spec("transformers") is None:
            return None
        import fetcher
        headlines = _safe(fetcher.get_news, symbol, 10) or []
        titles = [h.get("title") for h in headlines if isinstance(h, dict) and h.get("title")]
        if not titles:
            return None
        # Placeholder scoring hook — a real FinGPT pipeline would classify each
        # headline. We keep the integration point without bundling the weights:
        # if a project-local finbert/fingpt helper exists, use it; else bail.
        helper = None
        for modname in ("fingpt_local", "finbert_local"):
            if importlib.util.find_spec(modname) is not None:
                helper = importlib.import_module(modname)
                break
        if helper is None or not hasattr(helper, "score_headlines"):
            return None
        score = float(helper.score_headlines(titles))   # 0..10
        if score != score:                               # reject NaN
            return None
        score = max(0.0, min(10.0, score))
        label = "BULLISH" if score >= 6 else ("BEARISH" if score <= 4 else "NEUTRAL")
        return {"score": score, "label": label, "source": "fingpt"}
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
