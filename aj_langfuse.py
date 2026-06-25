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


def enabled() -> bool:
    return bool(os.environ.get("LANGFUSE_PUBLIC_KEY")
                and os.environ.get("LANGFUSE_SECRET_KEY"))


def _client():
    from langfuse import Langfuse
    return Langfuse(
        public_key=os.environ.get("LANGFUSE_PUBLIC_KEY"),
        secret_key=os.environ.get("LANGFUSE_SECRET_KEY"),
        host=os.environ.get("LANGFUSE_HOST", "http://localhost:3000"))


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
            trace.span(name="proposal", metadata=p)
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
