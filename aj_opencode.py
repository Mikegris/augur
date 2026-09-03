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
    # Block the whole wealth.db family as a prefix/substring, not just the exact
    # name — renamed copies (wealth.db.bak), WAL/SHM (wealth.db-wal) and any
    # backup variant must all be denied.
    "wealth.db", "wealth.db-*", "wealth.db*", "*wealth.db*",
    "*.lock", ".aj_*", "secrets/*", "config/*",
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
    old code only matched 'secrets/*' as a prefix).

    Matching is CASE-INSENSITIVE by design: macOS APFS (and Windows NTFS) are
    case-insensitive filesystems, so '/tmp/WEALTH.DB' opens the same file as
    '/tmp/wealth.db' — a case-sensitive guard would be trivially bypassed. We
    lower-case both sides and use fnmatchcase (plain fnmatch normalizes per the
    HOST filesystem, which is exactly the inconsistency we're avoiding)."""
    p = str(path or "").replace("\\", "/").lower()
    base = p.rsplit("/", 1)[-1]
    segments = [s for s in p.split("/") if s]
    for pat in FORBIDDEN_PATHS:
        pat = pat.lower()
        pat_dir = pat[:-2] if pat.endswith("/*") else pat  # 'secrets/*' -> 'secrets'
        if fnmatch.fnmatchcase(p, pat) or fnmatch.fnmatchcase(base, pat):
            return False
        for seg in segments:
            if fnmatch.fnmatchcase(seg, pat) or fnmatch.fnmatchcase(seg, pat_dir):
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
