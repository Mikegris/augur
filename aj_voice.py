"""AJTA — voice command gateway (AJTA-SPEC-1.0 §18).

Optional. Spoken commands inherit the operator's identity trust but STILL pass
the risk gate (§11); high-risk actions (any trade, kill, config change) require
explicit approval and NEVER auto-execute from voice. Read commands (status,
P&L, positions) run directly through the frozen read path (§15).

Untrusted text is treated as DATA, never instructions — voice text is parsed by
a small rule classifier here, not handed to a tool-calling model with authority.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict

log = logging.getLogger("augur.aj_voice")

# Read intents → frozen MCP read tools (no authority).
_READ_INTENTS = [
    (re.compile(r"\b(status|how are we|where do we stand)\b", re.I), "aj_status"),
    (re.compile(r"\b(p\W*n\W*l|pnl|profit|loss|how much)\b", re.I), "aj_day_pnl"),
    (re.compile(r"\b(positions?|book|holdings?)\b", re.I), "aj_positions"),
    (re.compile(r"\b(orders?|fills?)\b", re.I), "aj_orders"),
    (re.compile(r"\b(alerts?|anything wrong)\b", re.I), "aj_metrics"),
]
# High-risk verbs that may NEVER auto-execute from voice.
_HIGH_RISK = re.compile(r"\b(buy|sell|trade|kill|halt|enable|disable|set|rearm|"
                        r"re-arm|approve|live)\b", re.I)


def handle_command(text: str) -> Dict[str, Any]:
    """Route a spoken command. Read intents return data; anything high-risk
    returns a CONFIRMATION REQUEST (requires_approval=True) and is never
    executed here."""
    t = str(text or "").strip()
    if not t:
        return {"ok": False, "spoken": "I didn't catch that."}

    if _HIGH_RISK.search(t):
        # High-risk: surface for explicit human approval; do NOT act.
        return {"ok": True, "requires_approval": True, "command": t,
                "spoken": "That's a high-risk action — confirm it on screen "
                          "and it'll pass the risk gate before anything happens.",
                "executed": False}

    for pat, tool in _READ_INTENTS:
        if pat.search(t):
            try:
                import aj_mcp_read
                data = aj_mcp_read.invoke(tool, {})
                return {"ok": True, "requires_approval": False, "tool": tool,
                        "data": data, "executed": True,
                        "spoken": _summarize(tool, data)}
            except Exception as e:
                log.debug("voice read failed", exc_info=True)
                return {"ok": False, "spoken": "I couldn't read that just now."}

    return {"ok": True, "requires_approval": False, "executed": False,
            "spoken": "I can report status, P&L, positions, orders or alerts — "
                      "trades need on-screen confirmation."}


def _summarize(tool: str, data: Any) -> str:
    try:
        if tool == "aj_day_pnl" and isinstance(data, dict):
            return "Day P&L is ${:,.2f}.".format(data.get("day_pnl") or 0)
        if tool == "aj_status" and isinstance(data, dict):
            on = data.get("trading_enabled")
            return "Trading is {}; session {}.".format(
                "on (paper)" if on else "off", data.get("session"))
        if tool == "aj_positions" and isinstance(data, dict):
            n = len(data.get("positions") or {})
            return "{} open paper position(s).".format(n)
    except Exception:
        pass
    return "Here's what I found."
