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
from aj_schemas import AnalystReport, ResearchPlan

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
    transcript = "\n".join("{}: {}".format(t["role"].upper(), t["content"])
                           for t in turns)
    sys = _MGR_SYS.format(symbol=symbol)
    prompt = "Analyst reports:\n{r}\n\nDebate:\n{t}\n\nYour decision (JSON):".format(
        r=_reports_md(reports), t=transcript)
    text = call(aj_routing.DEEP, "council.research_manager", sys, prompt)
    if not text:
        return None
    return ResearchPlan.from_llm(text)
