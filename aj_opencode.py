"""AJTA — opencode sandbox (AJTA-SPEC-1.0 §16, VERIFY-OPENCODE).

Optional. Generated code produces CANDIDATE signals/backtests ONLY and NEVER
touches the execution path (§12). Fail-closed: the runner is disabled until the
VERIFY-OPENCODE gate is recorded (`aj_verify_opencode`=pass) — which requires
the spec's contract tests (incl. the non-interactive-write test) to pass on the
pinned opencode version first.

This module is a fail-closed stub: it documents the contract and the
forbidden-path guard, and refuses to run until verified. Wire the pinned
opencode binary into `_run_sandboxed()` once VERIFY-OPENCODE is closed.
"""
from __future__ import annotations

import fnmatch
import logging
from typing import Any, Dict, List

log = logging.getLogger("augur.aj_opencode")

# Paths the sandbox may NEVER read/write (Appendix C). Candidate generation has
# no business touching the book, locks, or secrets.
FORBIDDEN_PATHS: List[str] = [
    "wealth.db", "wealth.db-*", "*.lock", ".aj_*", "secrets/*", "config/*",
]


def available() -> bool:
    """True only when VERIFY-OPENCODE has been recorded as passed."""
    try:
        import database as db
        return str(db.get_settings().get("aj_verify_opencode")) == "pass"
    except Exception:
        return False


def path_allowed(path: str) -> bool:
    """False if the path touches anything forbidden — checked against the full
    path, the basename, AND every individual segment, so a forbidden directory
    (e.g. 'secrets') ANYWHERE in the path is caught, not just at the start (the
    old code only matched 'secrets/*' as a prefix)."""
    p = str(path or "").replace("\\", "/")
    base = p.rsplit("/", 1)[-1]
    segments = [s for s in p.split("/") if s]
    for pat in FORBIDDEN_PATHS:
        pat_dir = pat[:-2] if pat.endswith("/*") else pat  # 'secrets/*' -> 'secrets'
        if fnmatch.fnmatch(p, pat) or fnmatch.fnmatch(base, pat):
            return False
        for seg in segments:
            if fnmatch.fnmatch(seg, pat) or fnmatch.fnmatch(seg, pat_dir):
                return False
    return True


def run_candidate(spec: Dict[str, Any]) -> Dict[str, Any]:
    """Generate a candidate signal/backtest in the sandbox. Returns a result
    envelope that is RESEARCH ONLY — callers must never route it to execution.
    Fail-closed when not verified."""
    if not available():
        return {"enabled": False,
                "error": "opencode disabled — VERIFY-OPENCODE not passed",
                "result": None, "executable": False}
    # Verified path: the real runner would invoke the pinned opencode binary in
    # a non-interactive sandbox with the forbidden-path guard. Until wired, it
    # returns a clearly-marked empty candidate (never an execution).
    return _run_sandboxed(spec)


def _run_sandboxed(spec: Dict[str, Any]) -> Dict[str, Any]:  # pragma: no cover
    log.info("opencode run_candidate (stub) — wire pinned binary here")
    return {"enabled": True, "result": None, "executable": False,
            "note": "candidate-only; never touches the execution path (§16)"}
