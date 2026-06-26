"""AJTA — AUGUR MCP READ service (AJTA-SPEC-1.0 §15, VERIFY-MCP-READ).

A localhost server exposing AUGUR/AJ **read** tools to the operator spine.
Mutating tools (place order, kill, set config) are NOT exposed here — execution
flows only through §12 (aj_execution) behind the risk gate. The exposed tool
list is FROZEN and covered by a contract test (test_aj_contract); adding a tool
requires updating FROZEN_TOOLS, which forces the test (and a review) to change.

The MCP transport (stdio) is optional and only loaded when `serve_stdio()` is
called with the `mcp` package installed; the registry + dispatch here are pure
Python and need no third-party package, so the contract is testable offline.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List

log = logging.getLogger("augur.aj_mcp_read")


# ── tool implementations (all READ-ONLY, fail-open to a neutral envelope) ─────

def _t_aj_status(args: Dict[str, Any]) -> Dict[str, Any]:
    import aj_metrics
    return aj_metrics.status()


def _t_aj_day_pnl(args: Dict[str, Any]) -> Dict[str, Any]:
    import aj_risk
    return aj_risk.compute_day_pnl()


def _t_aj_positions(args: Dict[str, Any]) -> Dict[str, Any]:
    import aj_positions
    return aj_positions.paper_book(str(args.get("mode") or "paper"))


def _t_aj_metrics(args: Dict[str, Any]) -> Dict[str, Any]:
    import aj_metrics
    return {"cumulative_pnl": aj_metrics.cumulative_pnl(),
            "orders": aj_metrics.order_stats(),
            "gate_today": aj_metrics.gate_stats(),
            "recon": aj_metrics.recon_stats(),
            "routing_today": aj_metrics.routing_stats(),
            "alerts": aj_metrics.alerts()}


def _t_quote(args: Dict[str, Any]) -> Dict[str, Any]:
    import fetcher
    sym = str(args.get("symbol") or "").upper()
    if not sym:
        return {"error": "symbol required"}
    try:
        return fetcher.get_quote(sym) or {"symbol": sym, "price": None}
    except Exception as e:
        return {"symbol": sym, "error": str(e)[:200]}


def _t_forecast(args: Dict[str, Any]) -> Dict[str, Any]:
    import forecast_ensemble
    sym = str(args.get("symbol") or "").upper()
    if not sym:
        return {"error": "symbol required"}
    try:
        horizon = int(args.get("horizon_days") or 20)
    except (TypeError, ValueError):
        horizon = 20
    try:
        return forecast_ensemble.ensemble_forecast(sym, horizon)
    except Exception as e:
        return {"symbol": sym, "error": str(e)[:200]}


def _t_policy_check(args: Dict[str, Any]) -> Dict[str, Any]:
    try:
        import jarvis_policy
        return jarvis_policy.check_portfolio()
    except Exception as e:
        return {"error": str(e)[:200], "violations": []}


def _clamp_limit(raw: Any, default: int = 20, hi: int = 200) -> int:
    """Bounded LIMIT for the frozen read surface. A negative LIMIT in SQLite
    means 'no limit' (returns ALL rows) and a huge one can OOM the response, so
    clamp caller-supplied values to [1, hi]."""
    try:
        n = int(raw) if raw is not None else default
    except (TypeError, ValueError):
        n = default
    return max(1, min(n, hi))


def _t_proposals(args: Dict[str, Any]) -> Dict[str, Any]:
    import aj_db
    limit = _clamp_limit(args.get("limit"))
    rows = aj_db.query(
        "SELECT * FROM aj_proposals ORDER BY id DESC LIMIT ?", (limit,))
    return {"proposals": rows}


def _t_orders(args: Dict[str, Any]) -> Dict[str, Any]:
    import aj_db
    limit = _clamp_limit(args.get("limit"))
    rows = aj_db.query("SELECT * FROM aj_orders ORDER BY id DESC LIMIT ?", (limit,))
    return {"orders": rows}


# Registry: name -> {description, fn}. READ-ONLY by construction.
TOOLS: Dict[str, Dict[str, Any]] = {
    "aj_status":      {"fn": _t_aj_status, "description": "Operator status snapshot (switches, P&L, gate, recon, alerts)."},
    "aj_day_pnl":     {"fn": _t_aj_day_pnl, "description": "Today's paper-book day P&L (realized + unrealized vs session open)."},
    "aj_positions":   {"fn": _t_aj_positions, "description": "Current paper position book (FIFO)."},
    "aj_metrics":     {"fn": _t_aj_metrics, "description": "Observability metrics: fill-rate, gate, recon, routing, alerts."},
    "aj_proposals":   {"fn": _t_proposals, "description": "Recent proposals (read-only)."},
    "aj_orders":      {"fn": _t_orders, "description": "Recent orders and their states (read-only)."},
    "quote":          {"fn": _t_quote, "description": "Latest quote for a symbol."},
    "forecast_ensemble": {"fn": _t_forecast, "description": "Calibrated directional forecast + return cone for a symbol."},
    "policy_check":   {"fn": _t_policy_check, "description": "Check the portfolio against standing IPS policy rules."},
}

# FROZEN contract (VERIFY-MCP-READ). Adding a tool MUST update this tuple, which
# breaks the contract test until reviewed. Order is part of the contract.
FROZEN_TOOLS = (
    "aj_status",
    "aj_day_pnl",
    "aj_positions",
    "aj_metrics",
    "aj_proposals",
    "aj_orders",
    "quote",
    "forecast_ensemble",
    "policy_check",
)


# Per-tool input parameters (name -> ordered list of arg names the tool reads).
# Used to give each MCP-registered tool an explicit signature so FastMCP can
# derive a proper input schema (a bare **kwargs would advertise NO parameters,
# making symbol/horizon/limit/mode impossible to pass over the wire).
TOOL_PARAMS: Dict[str, List[str]] = {
    "aj_positions":      ["mode"],
    "aj_proposals":      ["limit"],
    "aj_orders":         ["limit"],
    "quote":             ["symbol"],
    "forecast_ensemble": ["symbol", "horizon_days"],
}


def _make(n: str):
    """Build an MCP tool wrapper for tool `n` with an explicit parameter
    signature (so FastMCP advertises the correct input schema) that dispatches
    through invoke(). Hoisted to module level so `n` is captured per call and
    the late-binding bug cannot be reintroduced by inlining into a loop."""
    import inspect
    params = TOOL_PARAMS.get(n, [])

    def _call(**kwargs):
        return invoke(n, kwargs)

    if params:
        # Give _call a real signature with one keyword-or-positional param per
        # arg (all optional, default None) so FastMCP exposes them in the schema.
        sig = inspect.Signature(
            [inspect.Parameter(p, inspect.Parameter.POSITIONAL_OR_KEYWORD,
                               default=None) for p in params])
        _call.__signature__ = sig
    return _call


def tool_names() -> List[str]:
    return list(TOOLS.keys())


def contract_ok() -> bool:
    """The live registry MUST exactly equal the frozen contract (names + order)
    and expose NO mutating verbs."""
    # The REAL guarantee is this exact-set equality (names + order) plus the
    # contract test that pins FROZEN_TOOLS — adding any tool, mutating or not,
    # breaks this until reviewed. (A substring verb-denylist was removed: it was
    # dead code and would false-positive on read listings like 'aj_orders'.)
    if tuple(TOOLS.keys()) != FROZEN_TOOLS:
        return False
    # Belt-and-suspenders: pin each registered callable to the known set of
    # READ implementations. A name-based denylist gives false confidence (a
    # mutating tool added under a novel name like 'liquidate' would slip past
    # it); binding to an allow-list of read fns means ANY non-read callable —
    # whatever its name — fails the contract.
    read_fns = {
        _t_aj_status, _t_aj_day_pnl, _t_aj_positions, _t_aj_metrics,
        _t_quote, _t_forecast, _t_policy_check, _t_proposals, _t_orders,
    }
    for spec in TOOLS.values():
        if spec.get("fn") not in read_fns:
            return False
    return True


def invoke(name: str, args: Dict[str, Any] = None) -> Dict[str, Any]:
    """Dispatch a read tool. Unknown / non-registered names are rejected — the
    server can only ever do what the frozen registry defines."""
    spec = TOOLS.get(name)
    if not spec:
        return {"error": "unknown read tool: {}".format(name)}
    try:
        return spec["fn"](args or {})
    except Exception as e:
        log.exception("read tool %s failed", name)
        return {"error": str(e)[:200]}


def schemas() -> List[Dict[str, Any]]:
    return [{"name": n, "description": s["description"]} for n, s in TOOLS.items()]


def serve_stdio():  # pragma: no cover - optional transport
    """Optional MCP stdio server. Requires the `mcp` package. Exposes only the
    frozen read tools."""
    try:
        from mcp.server.fastmcp import FastMCP
    except Exception as e:
        raise RuntimeError("MCP transport requires the 'mcp' package: {}".format(e))
    server = FastMCP("augur-read")
    for name, spec in TOOLS.items():
        server.tool(name=name, description=spec["description"])(_make(name))
    server.run()
