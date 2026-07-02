"""AJTA — Analyst Council debate engine (merge plan §5.2).

Bull vs Bear research debate with HARD round-count termination (no runaway
loops — a TradingAgents `conditional_logic` semantics, reimplemented in compact
pure-Python), followed by a Research Manager that picks a definitive stance.

Every model turn goes through the injected `call(tier, role, system, prompt)`
(budget-capped, privacy-routed by the council). On any empty/None completion the
debate degrades gracefully (stops early) and the council falls back to the
analyst consensus (fail-closed: no fabricated debate).
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

import aj_routing
from aj_schemas import AnalystReport, ResearchPlan, TraderProposal, extract_json

log = logging.getLogger("augur.aj_debate")

CallFn = Callable[[str, str, str, str], Optional[str]]


def _reports_md(reports: List[AnalystReport]) -> str:
    return "\n\n".join(r.render() for r in reports) or "(no analyst reports)"


_BULL_SYS = ("You are the BULL researcher. Argue the strongest evidence-based "
             "case to BUY {symbol}. Directly rebut the bear's last point. Be "
             "concise (<=120 words). Do not invent facts beyond the evidence.")
_BEAR_SYS = ("You are the BEAR researcher. Argue the strongest evidence-based "
             "case to AVOID/SELL {symbol}. Directly rebut the bull's last point. "
             "Be concise (<=120 words). Do not invent facts beyond the evidence.")


def research_debate(symbol: str, reports: List[AnalystReport], memory_text: str,
                    call: CallFn, rounds: int = 1) -> List[Dict[str, Any]]:
    """Alternating bull/bear debate. Returns a list of turn dicts
    {debate:'research', role:'bull'|'bear', round:int, content:str}. Hard-
    terminates after 2*rounds turns; stops early on a None completion."""
    rounds = max(0, int(rounds or 0))
    if rounds == 0 or not reports:
        return []
    reports_md = _reports_md(reports)
    mem = ("\nPrior lessons:\n" + memory_text) if memory_text else ""
    turns: List[Dict[str, Any]] = []
    last = {"bull": "", "bear": ""}
    for i in range(2 * rounds):
        side = "bull" if i % 2 == 0 else "bear"
        opp = "bear" if side == "bull" else "bull"
        sys = (_BULL_SYS if side == "bull" else _BEAR_SYS).format(symbol=symbol)
        prompt = ("Evidence:\n{ev}{mem}\n\nOpponent's last argument:\n{opp}\n\n"
                  "Your argument:").format(ev=reports_md, mem=mem,
                                           opp=(last[opp] or "(none yet)"))
        text = call(aj_routing.DEEP, "council.research." + side, sys, prompt)
        if not text:
            break   # degrade: stop the debate, council falls back to consensus
        text = str(text).strip()
        last[side] = text
        turns.append({"debate": "research", "role": side, "round": i // 2,
                      "content": text})
    return turns


_MGR_SYS = (
    "You are the Research Manager. Read the analyst reports and the full "
    "bull/bear debate about {symbol} and commit to a DEFINITIVE stance — do not "
    "default to a lazy Hold unless the evidence is genuinely balanced. Respond "
    "with STRICT JSON: {{\"recommendation\":\"BUY|OVERWEIGHT|HOLD|UNDERWEIGHT|"
    "SELL\",\"rationale\":\"<=100 words\",\"strategic_actions\":[\"...\"]}}.")


def research_manager(symbol: str, reports: List[AnalystReport],
                     turns: List[Dict[str, Any]], call: CallFn) -> Optional[ResearchPlan]:
    """Synthesize the debate into a ResearchPlan. Returns None when there is no
    debate to judge or the model is unavailable (council falls back)."""
    if not turns:
        return None
    # .get with defaults so a malformed/partial turn record (e.g. reconstructed
    # from persisted audit data on replay) degrades rather than KeyError-ing out
    # of the manager synthesis.
    transcript = "\n".join(
        "{}: {}".format(str(t.get("role", "?")).upper(), t.get("content", ""))
        for t in turns)
    sys = _MGR_SYS.format(symbol=symbol)
    prompt = "Analyst reports:\n{r}\n\nDebate:\n{t}\n\nYour decision (JSON):".format(
        r=_reports_md(reports), t=transcript)
    text = call(aj_routing.DEEP, "council.research_manager", sys, prompt)
    if not text:
        return None
    d = extract_json(text)
    # Prose/unparseable output must fall back to the analyst consensus (None),
    # not silently replace it with a parse-failure HOLD plan marked status "ok".
    if not (d.get("recommendation") or d.get("rating")):
        return None
    return ResearchPlan.from_llm(d)


# ── trader (plan → executable proposal) ───────────────────────────────────────

_TRADER_SYS = (
    "You are the Trader. Convert the research plan for {symbol} into a concrete "
    "executable proposal. Respond with STRICT JSON: {{\"action\":\"BUY|HOLD|"
    "SELL\",\"reasoning\":\"<=60 words\",\"entry_price\":<num or null>,"
    "\"stop_loss\":<num or null>,\"position_sizing\":\"<small|normal|large>\"}}.")


def trader(symbol: str, plan: ResearchPlan, call: CallFn) -> Optional[TraderProposal]:
    """Turn the research plan into a 3-tier TraderProposal. Returns None when the
    model is unavailable (the council then derives the action from the plan)."""
    if plan is None:
        return None
    sys = _TRADER_SYS.format(symbol=symbol)
    prompt = "Research plan:\n{}\n\nYour proposal (JSON):".format(plan.render())
    text = call(aj_routing.DEEP, "council.trader", sys, prompt)
    if not text:
        return None
    d = extract_json(text)
    # Unparseable trader output must NOT default to a HOLD proposal: the
    # decision layer takes proposal.action over the rating-implied action, so a
    # silent HOLD would override a SELL plan with the 0.5 hold-trim factor.
    # None lets the council derive the action from the plan (documented path).
    if not (d.get("action") or d.get("decision")):
        return None
    return TraderProposal.from_llm(d)


# ── risk debate (3 perspectives, hard-terminated) ─────────────────────────────

_RISK_PERSPECTIVES = ("aggressive", "conservative", "neutral")
_RISK_SYS = (
    "You are the {persp} risk manager reviewing a proposed trade in {symbol}. "
    "From your risk perspective, assess the proposal and the portfolio context. "
    "Be concise (<=90 words). Respond with STRICT JSON: {{\"assessment\":"
    "\"...\",\"recommended_action\":\"BUY|HOLD|SELL\"}}.")


def risk_debate(symbol: str, proposal: TraderProposal, portfolio_text: str,
                call: CallFn, rounds: int = 1) -> List[Dict[str, Any]]:
    """Aggressive → Conservative → Neutral rotation, hard-terminated after
    3*rounds turns. ADVISORY — informs the arbiter; never the execution gate.
    Stops early on a None completion."""
    rounds = max(0, int(rounds or 0))
    if rounds == 0 or proposal is None:
        return []
    prop_md = proposal.render()
    ctx = ("\nPortfolio context:\n" + portfolio_text) if portfolio_text else ""
    turns: List[Dict[str, Any]] = []
    history = ""
    total = 3 * rounds
    for i in range(total):
        persp = _RISK_PERSPECTIVES[i % 3]
        sys = _RISK_SYS.format(persp=persp, symbol=symbol)
        prompt = ("Proposal:\n{p}{c}\n\nDebate so far:\n{h}\n\nYour view:").format(
            p=prop_md, c=ctx, h=history or "(none yet)")
        text = call(aj_routing.DEEP, "council.risk." + persp, sys, prompt)
        if not text:
            break
        text = str(text).strip()
        # Bound the carried transcript so the prompt doesn't grow quadratically
        # across turns (re-sending every prior turn at full length would inflate
        # token cost and risk blowing the council's per-cycle budget cap). The
        # full turns are still returned below for persistence/UI.
        history = (history + "\n{}: {}".format(persp.upper(), text))[-2000:]
        turns.append({"debate": "risk", "role": persp, "round": i // 3,
                      "content": text})
    return turns
