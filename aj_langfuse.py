"""AJTA — Langfuse trace exporter (AJTA-SPEC-1.0 §21.1).

Optional observability overlay. Emits the spec's cycle trace schema to a
self-hosted Langfuse when configured (env LANGFUSE_PUBLIC_KEY / SECRET_KEY /
HOST). FAIL-OPEN: observability is a non-trading read, so any error (missing
package, unreachable host, no keys) degrades to a silent no-op — it NEVER blocks
or alters a trading cycle. The append-only `aj_audit` hash chain remains the
authoritative local record regardless.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict

log = logging.getLogger("augur.aj_langfuse")

# Lazy module-level singleton. The Langfuse SDK spins up a background flush
# thread + HTTP session per client instance, so building a fresh one per cycle
# (as emit_cycle_trace runs every cycle of a long-running auto_run) would leak
# threads and sockets. Reuse one client across cycles instead.
_LF_CLIENT = None


def enabled() -> bool:
    return bool(os.environ.get("LANGFUSE_PUBLIC_KEY")
                and os.environ.get("LANGFUSE_SECRET_KEY"))


def _client():
    global _LF_CLIENT
    if _LF_CLIENT is None:
        from langfuse import Langfuse
        _LF_CLIENT = Langfuse(
            public_key=os.environ.get("LANGFUSE_PUBLIC_KEY"),
            secret_key=os.environ.get("LANGFUSE_SECRET_KEY"),
            host=os.environ.get("LANGFUSE_HOST", "http://localhost:3000"))
    return _LF_CLIENT


def emit_cycle_trace(summary: Dict[str, Any]) -> Dict[str, Any]:
    """Emit one `aj.cycle` trace (§21.1 schema) with spans for the gate, fills,
    reconcile and scoring. No-op + {emitted:False} when not configured."""
    if not enabled():
        return {"emitted": False, "reason": "langfuse not configured"}
    try:
        lf = _client()
        trace = lf.trace(name="aj.cycle",
                         metadata={"cycle_id": summary.get("cycle_id"),
                                   "mode": summary.get("mode"),
                                   "session": summary.get("session")})
        for p in (summary.get("proposals") or []):
            # Redact: emit only non-sensitive fields. A proposal's thesis/qty
            # and the cycle's P&L are portfolio data and must NOT leave the box
            # to a (possibly remote) LANGFUSE_HOST. 'result' is an open-ended
            # object that can carry qty/avg_fill_price/fees/pnl, so emit only a
            # coarse status string from it, never the whole object.
            if isinstance(p, dict):
                safe = {"symbol": p.get("symbol"), "side": p.get("side")}
                res = p.get("result")
                if isinstance(res, dict):
                    safe["result"] = res.get("state") or res.get("status")
                elif isinstance(res, str):
                    safe["result"] = res
            else:
                safe = {}
            trace.span(name="proposal", metadata=safe)
        trace.span(name="reconcile", metadata={"status": summary.get("reconcile")})
        trace.span(name="score", metadata=summary.get("scored") or {})
        try:
            lf.flush()
        except Exception:
            pass
        return {"emitted": True}
    except Exception as e:
        log.debug("langfuse emit failed (fail-open): %s", e)
        return {"emitted": False, "reason": str(e)[:120]}
