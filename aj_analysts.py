"""AJTA — Analyst Council: analyst adapters (merge plan §5.1).

Four analysts (fundamentals / news / sentiment / technical), each a THIN adapter
that turns AUGUR's existing data engines into an evidence bundle, asks a
deep-think model to score it, and returns a typed AnalystReport. This is the
merge's core synergy: TradingAgents' analyst pattern fed by AUGUR's richer data
layer (sec_edgar, narrative_engine, alt_signals, forecast_ensemble, …) — we do
NOT vendor TradingAgents' dataflows.

Design for testability + safety:
  * Every analyst takes an injected `call(tier, role, system, prompt) -> str|None`
    so tests inject a deterministic fake and never touch the network/LLM.
  * Evidence gathering is fail-open: any engine error yields thinner evidence,
    never an exception. With zero evidence and no model the analyst returns a
    neutral, low-confidence report.
  * Analysts are DATA ONLY — they never size, trade, or touch the risk gate.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Callable, Dict, List, Optional

import aj_routing
from aj_schemas import AnalystReport

log = logging.getLogger("augur.aj_analysts")

# call(tier, role, system, prompt) -> Optional[str]
CallFn = Callable[[str, str, str, str], Optional[str]]


def default_call(tier: str, role: str, system: str, prompt: str) -> Optional[str]:
    """Real completion via the privacy-aware router. Public sensitivity: an
    analyst prompt is market evidence about ONE ticker — no holdings/P&L — so it
    may use a cloud model. (The operator's portfolio is never injected here.)"""
    try:
        r = aj_routing.complete_tiered(prompt, tier=tier, system=system, role=role,
                                       sensitivity=aj_routing.PUBLIC)
        return r.get("text") if r and r.get("ok") else None
    except Exception:
        log.exception("default_call failed for role=%s", role)
        return None


def _safe(fn, *a, **k):
    """Call an engine fail-open; return its result or None on any error."""
    try:
        return fn(*a, **k)
    except Exception as e:
        log.debug("evidence engine %s failed: %s", getattr(fn, "__name__", fn), e)
        return None


def _fmt(obj: Any, keep: Optional[List[str]] = None, limit: int = 1500) -> str:
    """Compact a dict/list of evidence into a bounded JSON snippet for a prompt."""
    if obj is None:
        return ""
    try:
        if isinstance(obj, dict) and keep:
            obj = {k: obj.get(k) for k in keep if obj.get(k) is not None}
        s = json.dumps(obj, default=str, separators=(",", ":"))
        return s[:limit]
    except Exception:
        return str(obj)[:limit]


_SYS = ("You are a {role} for a disciplined trading desk. Analyze ONLY the "
        "evidence provided about {symbol}. Be decisive and concise. Respond with "
        "STRICT JSON: {{\"band\":\"<one label>\",\"score\":<0-10, 10=most "
        "bullish>,\"confidence\":<0-1>,\"key_points\":[\"...\"],\"narrative\":"
        "\"<=80 words\"}}. No prose outside the JSON.")


def _neutral(analyst: str, why: str = "") -> AnalystReport:
    return AnalystReport(analyst=analyst, band="NEUTRAL", score=5.0, confidence=0.1,
                         key_points=([why] if why else []),
                         narrative=why or "insufficient evidence")


def _run(analyst: str, symbol: str, evidence: Dict[str, str], call: CallFn,
         band_is_sentiment: bool = False) -> AnalystReport:
    """Shared harness: assemble evidence → deep-think → parse AnalystReport."""
    ev = {k: v for k, v in evidence.items() if v}
    if not ev:
        # No evidence at all — still ask the model nothing; return neutral.
        return _neutral(analyst, "no evidence engines returned data")
    sys = _SYS.format(role=analyst + " analyst", symbol=symbol)
    prompt = "Symbol: {}\nEvidence:\n{}".format(
        symbol, "\n".join("[{}] {}".format(k, v) for k, v in ev.items()))
    text = call(aj_routing.DEEP, "council.analyst." + analyst, sys, prompt)
    if not text:
        return _neutral(analyst, "model unavailable; evidence gathered")
    refs = list(ev.keys())
    return AnalystReport.from_llm(analyst, text, band_is_sentiment=band_is_sentiment,
                                  evidence_refs=refs)


# ── the four analysts ─────────────────────────────────────────────────────────

def fundamentals(symbol: str, call: CallFn = default_call,
                 cfg: Optional[Dict] = None) -> AnalystReport:
    import fetcher
    ev: Dict[str, str] = {}
    f = _safe(fetcher.get_fundamentals, symbol)
    ev["fundamentals"] = _fmt(f, keep=[
        "pe_ratio", "forward_pe", "peg_ratio", "ps_ratio", "pb_ratio", "ev_ebitda",
        "debt_equity", "current_ratio", "return_on_equity", "profit_margin",
        "gross_margin", "operating_margin", "revenue_growth", "earnings_growth",
        "free_cashflow", "recommendation", "analyst_target"])
    try:
        import earnings
        ev["earnings"] = _fmt(_safe(earnings.get_earnings_dossier, symbol), keep=[
            "days_until", "beat_rate", "avg_surprise_pct", "implied_move_pct",
            "insider_activity"])
    except Exception:
        pass
    try:
        import smart_money
        sm = _safe(smart_money.compute_score, symbol)
        if isinstance(sm, dict):
            ev["smart_money"] = _fmt({"score": sm.get("score"), "signal": sm.get("signal")})
    except Exception:
        pass
    return _run("fundamentals", symbol, ev, call)


def news(symbol: str, call: CallFn = default_call,
         cfg: Optional[Dict] = None) -> AnalystReport:
    ev: Dict[str, str] = {}
    try:
        import narrative_engine
        n = _safe(narrative_engine.analyze_narrative, symbol)
        if isinstance(n, dict):
            ev["narrative"] = _fmt(n, keep=[
                "dominant_narrative", "narrative_phase", "velocity_score",
                "velocity_label", "contrarian_signal", "article_count"])
    except Exception:
        pass
    try:
        import fetcher
        headlines = _safe(fetcher.get_news, symbol, 8)
        if isinstance(headlines, list):
            ev["headlines"] = _fmt([h.get("title") for h in headlines if isinstance(h, dict)])
    except Exception:
        pass
    try:
        import fred_data
        ev["macro"] = _fmt(_safe(fred_data.snapshot), limit=900)
    except Exception:
        pass
    return _run("news", symbol, ev, call)


def sentiment(symbol: str, call: CallFn = default_call,
              cfg: Optional[Dict] = None) -> AnalystReport:
    ev: Dict[str, str] = {}
    try:
        import hn_sentiment
        hn = _safe(hn_sentiment.fetch_mentions, symbol)
        if isinstance(hn, dict):
            ev["hackernews"] = _fmt({"mentions": hn.get("mention_count"),
                                     "stats": hn.get("stats")})
    except Exception:
        pass
    try:
        import alt_signals
        st = _safe(alt_signals.stocktwits_symbol_sentiment, symbol)
        if isinstance(st, dict):
            ev["stocktwits"] = _fmt({k: st.get(k) for k in
                                     ("messages_total", "bullish", "bearish", "bull_ratio")})
    except Exception:
        pass
    # Optional FinGPT numeric prior (Phase 6) — only if explicitly enabled.
    if cfg and cfg.get("fingpt_sentiment_enabled"):
        try:
            import aj_personas
            ev["fingpt"] = _fmt(_safe(aj_personas.fingpt_sentiment, symbol))
        except Exception:
            pass
    return _run("sentiment", symbol, ev, call, band_is_sentiment=True)


def technical(symbol: str, call: CallFn = default_call,
              cfg: Optional[Dict] = None) -> AnalystReport:
    ev: Dict[str, str] = {}
    try:
        import fetcher
        bars = _safe(fetcher.get_chart_data, symbol, "6mo", "1d")
        if isinstance(bars, list) and bars:
            ind = _safe(fetcher.compute_indicators, bars)
            ev["indicators"] = _fmt(ind, keep=[
                "rsi", "macd", "macd_signal", "current_price", "price_vs_sma20",
                "price_vs_sma50", "price_vs_sma200", "atr"])
    except Exception:
        pass
    try:
        import forecast_ensemble
        horizon = int((cfg or {}).get("forecast_horizon_days", 20) or 20)
        fc = _safe(forecast_ensemble.ensemble_forecast, symbol, horizon)
        if isinstance(fc, dict) and isinstance(fc.get("ensemble"), dict):
            e = fc["ensemble"]
            ev["forecast"] = _fmt({k: e.get(k) for k in
                                   ("prob_up", "direction", "conviction", "edge",
                                    "disagreement", "consensus")})
    except Exception:
        pass
    try:
        import gex_engine
        g = _safe(gex_engine.compute_gex, symbol)
        if isinstance(g, dict):
            ev["gex"] = _fmt(g, keep=["regime", "flip_point", "net_gex"], limit=500)
    except Exception:
        pass
    return _run("technical", symbol, ev, call)


# Registry consumed by aj_council (config toggles select the subset).
ANALYSTS = {
    "fundamentals": fundamentals,
    "news": news,
    "sentiment": sentiment,
    "technical": technical,
}
