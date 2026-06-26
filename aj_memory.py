"""AJTA — Analyst Council memory log (merge plan §5.4).

An append-only markdown log of council decisions and post-hoc reflections,
co-located with the user DB. Chosen over a vector store (TradingAgents' paper-era
ChromaDB) deliberately: append-only matches AUGUR's audit ethos, adds ZERO heavy
dependency to the desktop bundle, and is human-auditable. Retrieval is metadata
filtering (same-symbol recents + cross-symbol lessons), not embeddings — a
future semantic-recall upgrade can slot behind a flag.

Entries:
    <!-- ts=<iso> symbol=<SYM> kind=<decision|lesson> -->
    <body...>
    <!-- ENTRY_END -->

Writes are atomic (temp file + os.replace). Never raises into the caller — a
memory failure must not break a trading cycle.
"""
from __future__ import annotations

import logging
import os
import re
import threading
from typing import Dict, List, Optional

log = logging.getLogger("augur.aj_memory")

_ENTRY_END = "<!-- ENTRY_END -->"
_HDR_RE = re.compile(r"<!--\s*ts=(\S+)\s+symbol=(\S+)\s+kind=(\S+)\s*-->")
_lock = threading.Lock()


def _path() -> str:
    try:
        import database as db
        base = os.path.dirname(db.DB_PATH) or "."
    except Exception:
        base = "."
    return os.path.join(base, "aj_council_memory.md")


def _now_iso() -> str:
    try:
        import aj_db
        return aj_db.utc_now_iso()
    except Exception:
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def append(symbol: str, kind: str, body: str) -> bool:
    """Append one entry atomically. kind in {'decision','lesson'}. Returns
    True on success; never raises."""
    symbol = str(symbol or "").upper() or "?"
    kind = str(kind or "decision")
    body = str(body or "").strip()
    if not body:
        return False
    entry = "<!-- ts={ts} symbol={sym} kind={kind} -->\n{body}\n{end}\n".format(
        ts=_now_iso(), sym=symbol, kind=kind, body=body, end=_ENTRY_END)
    path = _path()
    try:
        with _lock:
            prior = ""
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    prior = f.read()
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(prior + entry)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        return True
    except Exception:
        log.exception("aj_memory append failed")
        return False


def _parse() -> List[Dict[str, str]]:
    path = _path()
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read()
    except Exception:
        return []
    out: List[Dict[str, str]] = []
    for chunk in raw.split(_ENTRY_END):
        m = _HDR_RE.search(chunk)
        if not m:
            continue
        body = _HDR_RE.sub("", chunk).strip()
        if body:
            out.append({"ts": m.group(1), "symbol": m.group(2).upper(),
                        "kind": m.group(3), "body": body})
    return out


def recall(symbol: str, n_same: int = 5, n_cross: int = 3) -> List[str]:
    """Return up to n_same most-recent entries for `symbol` plus up to n_cross
    most-recent cross-symbol LESSONS, reverse-chronological, as rendered strings.
    Empty when no memory exists (the debate works fine without it)."""
    symbol = str(symbol or "").upper()
    entries = _parse()
    if not entries:
        return []
    same = [e for e in entries if e["symbol"] == symbol][-max(0, n_same):]
    cross = [e for e in entries
             if e["symbol"] != symbol and e["kind"] == "lesson"][-max(0, n_cross):]
    picked = list(reversed(same)) + list(reversed(cross))
    return ["[{} {} {}] {}".format(e["ts"], e["symbol"], e["kind"], e["body"])
            for e in picked]


def recall_text(symbol: str, n_same: int = 5, n_cross: int = 3) -> str:
    lines = recall(symbol, n_same=n_same, n_cross=n_cross)
    return "\n".join("- " + ln for ln in lines)


def clear() -> None:
    """Delete the memory log (tests / operator reset). Never raises."""
    try:
        p = _path()
        if os.path.exists(p):
            os.remove(p)
    except Exception:
        log.debug("aj_memory clear failed")
