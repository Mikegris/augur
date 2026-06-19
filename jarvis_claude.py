"""Claude-Code-powered Jarvis (v2.4).

Gives Jarvis "the same power as you" — i.e. the local Claude Code CLI. Instead
of being limited to the rule-based intents and the OpenAI function-calling agent
(both closed over the app's own data), this backend shells out to the installed
`claude` binary in headless mode (`claude -p --output-format stream-json`),
pointed at the project directory. Claude Code can then read the codebase, run
the app's own engines / CLI / SQLite, search and fetch the web, and write
analysis files — exactly the toolset the in-terminal assistant has.

Design notes / why it's shaped this way:
  * No new dependency and no API key. The CLI is already installed and
    authenticated on this machine (that's how the project is developed), so we
    reuse that auth. If `claude` isn't on PATH, `available()` is False and
    callers fall back to the existing OpenAI/rule paths — nothing regresses.
  * Streaming. We parse the stream-json event log and surface tool calls as
    Jarvis status lines, so the existing SSE `ask_stream` HUD shows Claude Code
    working live (reading files, searching the web, …).
  * Abandonable. The streaming caller has a hard deadline and a `cancelled()`
    signal; we poll both and SIGTERM the whole process group so an abandoned
    run can't leave a zombie `claude` wedging the machine. This mirrors the
    project's hard-won rule (sweeps C/D): never block forever in a thread the
    caller may walk away from.
  * Safe by default, powerful by opt-in. Even though the app is local and
    single-user, this backend is reachable from a web request, so the SHIPPED
    default does NOT disable approval gates. It runs with permission-mode
    "default" and an explicit read-only allowlist (Read/Grep/Glob/WebSearch/
    WebFetch) — full codebase awareness + open-world web research with zero
    ability to mutate files, run shell commands, or touch the user's data. A
    user who genuinely wants the full Claude-Code toolset (Bash, Write/Edit,
    or bypassPermissions) opts in DELIBERATELY via DB settings
    (`jarvis_claude_allowed_tools`, `jarvis_claude_permission`). The default is
    never an unattended agent with the gates off.
"""
import json
import os
import shutil
import signal
import subprocess
import threading
import time
from typing import Any, Dict, List, Optional

try:
    import database as db
except Exception:  # pragma: no cover - db should always import
    db = None  # type: ignore

import logging

log = logging.getLogger("augur.jarvis_claude")

# Repo root = this file's directory. Claude Code runs with this as cwd so it can
# read the engines, run cli.py, and query wealth.db.
_REPO = os.path.dirname(os.path.abspath(__file__))

# Hard wall-clock ceiling for a single Claude Code run. Comfortably under the
# ask_stream deadline (~1050s) so the subprocess is reaped before the stream
# worker is abandoned. Env-overridable.
_RUN_DEADLINE = float(os.environ.get("AUGUR_CLAUDE_DEADLINE", "600"))

# Cap on agent turns to bound cost/latency. Env-overridable.
_MAX_TURNS = int(os.environ.get("AUGUR_CLAUDE_MAX_TURNS", "24"))

# Shipped default: read-only investigation + web research, no mutation, no
# shell. This is what makes the feature safe to expose from a web request. A
# user can broaden it via the `jarvis_claude_allowed_tools` DB setting (space-
# separated, Claude-Code tool names, e.g. "Read Grep Glob Bash WebSearch
# WebFetch") and/or escalate `jarvis_claude_permission` — but only deliberately.
_DEFAULT_ALLOWED_TOOLS = "Read Grep Glob WebSearch WebFetch"

# Cached binary-availability probe (a `which` per request is wasteful and the
# answer rarely changes within a process).
_PROBE_TTL = 60.0
_probe_at = 0.0
_probe_ok = False


def _setting(key: str, default: str = "") -> str:
    if db is None:
        return default
    try:
        return (db.get_settings() or {}).get(key) or default
    except Exception:
        return default


def _claude_bin() -> Optional[str]:
    """Path to the `claude` CLI: explicit setting/env override, else PATH."""
    override = (os.environ.get("AUGUR_CLAUDE_BIN") or _setting("jarvis_claude_bin")).strip()
    if override:
        return override if os.path.exists(override) else None
    return shutil.which("claude")


def available() -> bool:
    """True when Claude Code can be invoked: binary present AND not disabled by
    the `jarvis_claude_enabled` setting (defaults to enabled when present)."""
    global _probe_at, _probe_ok
    now = time.time()
    if now - _probe_at < _PROBE_TTL:
        return _probe_ok
    ok = _claude_bin() is not None
    if ok and _setting("jarvis_claude_enabled", "1").strip().lower() in ("0", "off", "false", "no"):
        ok = False
    _probe_ok = ok
    _probe_at = now
    return ok


def _portfolio_brief() -> str:
    """One-line portfolio snapshot to ground Claude Code, best-effort."""
    if db is None:
        return ""
    try:
        holdings = db.get_portfolio() or []
        syms = [h.get("symbol") for h in holdings if h.get("symbol")]
        if not syms:
            return "The user has no portfolio positions yet."
        head = ", ".join(syms[:12])
        more = "" if len(syms) <= 12 else " (+{} more)".format(len(syms) - 12)
        return "The user's portfolio holds: {}{}.".format(head, more)
    except Exception:
        return ""


_SYSTEM_TEMPLATE = (
    "You are Jarvis, the AI assistant embedded in AUGUR/TERMINUS — a local, "
    "single-user, Bloomberg-style personal stock & wealth tracker. You are "
    "running as the `claude` CLI inside the app's own repository ({repo}). Use "
    "the tools you actually have: read the codebase and its ~50 analytical "
    "engines to understand exactly how this app computes things, and search/"
    "fetch the web for anything outside the local data. (You may also have "
    "shell/file tools if the user enabled them; if a tool is denied, work with "
    "what you have rather than retrying it.) {portfolio}\n\n"
    "Answer by actually investigating — read the relevant engine, check the web "
    "— rather than guessing. Then reply with a concise, plain-English answer (a "
    "few sentences; this renders in a chat panel, not a terminal). Lead with the "
    "conclusion. Cite web sources with their URLs when you used them. This is "
    "research, not advice: never place trades, send anything externally, or "
    "modify the user's portfolio, transactions, alerts, or settings."
)


def _build_prompt(query: str, history: Optional[List[Dict[str, Any]]]) -> str:
    """Compose the user-turn prompt, prepending a little recent context."""
    parts: List[str] = []
    turns = history or []
    recent = [t for t in turns if isinstance(t, dict)][-4:]
    if recent:
        parts.append("Earlier in this conversation:")
        for t in recent:
            q = str(t.get("q") or t.get("query") or "").strip()
            a = str(t.get("a") or t.get("answer") or "").strip()
            if q:
                parts.append("  User: {}".format(q[:300]))
            if a:
                parts.append("  Jarvis: {}".format(a[:300]))
        parts.append("")
    parts.append(query.strip())
    return "\n".join(parts)


def _tool_label(name: str, tool_input: Dict[str, Any]) -> Optional[str]:
    """Map a Claude Code tool_use to a short Jarvis status line."""
    n = (name or "").lower()

    def _short(v: Any, limit: int = 48) -> str:
        s = str(v or "").strip().replace("\n", " ")
        return s if len(s) <= limit else s[:limit] + "…"

    if n in ("read", "notebookread"):
        return "reading {}".format(_short(os.path.basename(str(tool_input.get("file_path", "")))))
    if n in ("edit", "write", "notebookedit", "multiedit"):
        return "writing {}".format(_short(os.path.basename(str(tool_input.get("file_path", "")))))
    if n == "grep":
        return "searching code for “{}”".format(_short(tool_input.get("pattern", ""), 32))
    if n == "glob":
        return "scanning files"
    if n in ("bash", "bashoutput"):
        return "running {}".format(_short(tool_input.get("command", ""), 56))
    if n == "websearch":
        return "searching the web for “{}”".format(_short(tool_input.get("query", ""), 40))
    if n == "webfetch":
        return "reading {}".format(_short(tool_input.get("url", ""), 56))
    if n == "task":
        return "delegating to a sub-agent"
    if n == "todowrite":
        return None  # internal bookkeeping — don't surface
    if n:
        return "using {}".format(n)
    return None


def _extract_citations(events: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    """Collect web URLs Claude Code consulted, for the .jv-cite UI."""
    cites: List[Dict[str, str]] = []
    seen = set()
    for ev in events:
        try:
            msg = ev.get("message") or {}
            for block in (msg.get("content") or []):
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use" and (block.get("name") or "").lower() == "webfetch":
                    url = str((block.get("input") or {}).get("url") or "").strip()
                    if url and url not in seen and url.startswith(("http://", "https://")):
                        seen.add(url)
                        cites.append({"title": url, "url": url})
        except Exception:
            continue
    return cites[:6]


def ask_claude(query: str,
               history: Optional[List[Dict[str, Any]]] = None,
               progress: Any = None,
               cancelled: Any = None,
               symbol: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Answer `query` by driving the local Claude Code CLI.

    Returns a Jarvis payload dict {answer, used, reasoning, citations} on
    success, or None when Claude Code is unavailable or the run failed — the
    caller then falls back to the existing routing so nothing regresses.
    `progress(str)` streams status lines; `cancelled()->bool` lets a walked-away
    streaming caller abandon the run (we SIGTERM the process group)."""
    if not query or not available():
        return None

    binary = _claude_bin()
    if not binary:
        return None

    def notify(text: str) -> None:
        if progress is None or not text:
            return
        try:
            progress("◉ Claude Code — {}".format(text))
        except Exception:
            pass

    def is_cancelled() -> bool:
        if cancelled is None:
            return False
        try:
            return bool(cancelled())
        except Exception:
            return False

    sys_prompt = _SYSTEM_TEMPLATE.format(
        repo=_REPO, portfolio=_portfolio_brief())
    # Default permission mode is "default" (NOT bypass) — escalation is an
    # explicit, deliberate opt-in via the DB setting.
    permission = (_setting("jarvis_claude_permission", "default").strip()
                  or "default")
    cmd = [binary, "-p",
           "--output-format", "stream-json",
           "--verbose",
           "--permission-mode", permission,
           "--add-dir", _REPO,
           "--max-turns", str(_MAX_TURNS),
           "--append-system-prompt", sys_prompt]
    # Tool allowlist: with permission-mode "default", only these run headlessly;
    # anything else is denied (headless can't prompt) — so the read-only default
    # truly can't mutate. Unless the user escalated to bypassPermissions, in
    # which case the allowlist still scopes what's auto-run.
    allowed = (_setting("jarvis_claude_allowed_tools", _DEFAULT_ALLOWED_TOOLS).strip()
               or _DEFAULT_ALLOWED_TOOLS)
    if allowed and permission != "bypassPermissions":
        cmd += ["--allowedTools"] + allowed.split()
    model = _setting("jarvis_claude_model").strip()
    if model:
        cmd += ["--model", model]

    prompt = _build_prompt(query, history)
    notify("investigating…")

    try:
        proc = subprocess.Popen(
            cmd, cwd=_REPO,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True,
            start_new_session=True)  # own process group → killable as a unit
    except Exception as e:
        log.warning("claude spawn failed: %s", e)
        return None

    try:
        proc.stdin.write(prompt)
        proc.stdin.close()
    except Exception:
        pass

    import queue as _queue
    lines: "_queue.Queue" = _queue.Queue()

    def _reader() -> None:
        try:
            for line in proc.stdout:
                lines.put(line)
        except Exception:
            pass
        finally:
            lines.put(None)  # EOF sentinel

    threading.Thread(target=_reader, daemon=True, name="claude-reader").start()

    events: List[Dict[str, Any]] = []
    used: List[str] = []
    answer = ""
    deadline = time.time() + _RUN_DEADLINE

    def _terminate() -> None:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except Exception:
            try:
                proc.terminate()
            except Exception:
                pass

    while True:
        if is_cancelled() or time.time() > deadline:
            _terminate()
            break
        try:
            line = lines.get(timeout=0.5)
        except _queue.Empty:
            continue
        if line is None:  # reader hit EOF
            break
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except Exception:
            continue
        events.append(ev)
        etype = ev.get("type")
        if etype == "assistant":
            for block in ((ev.get("message") or {}).get("content") or []):
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use":
                    name = block.get("name") or ""
                    label = _tool_label(name, block.get("input") or {})
                    if label:
                        used.append(name)
                        notify(label)
                elif block.get("type") == "text":
                    txt = (block.get("text") or "").strip()
                    if txt:
                        answer = txt  # last assistant text; result event confirms
        elif etype == "result":
            res = ev.get("result")
            if isinstance(res, str) and res.strip():
                answer = res.strip()
            if ev.get("is_error"):
                log.warning("claude result error: %s", ev.get("subtype"))

    # Drain the process so it can't linger.
    try:
        proc.wait(timeout=5)
    except Exception:
        _terminate()

    if is_cancelled():
        return None
    if not answer.strip():
        # Surface stderr for diagnostics but degrade to a fallback.
        try:
            err = (proc.stderr.read() or "").strip()[:300] if proc.stderr else ""
            if err:
                log.warning("claude produced no answer; stderr: %s", err)
        except Exception:
            pass
        return None

    # Deduplicate tool names while preserving order, for the reasoning trace.
    seen = set()
    consulted = []
    for t in used:
        if t not in seen:
            seen.add(t)
            consulted.append({"tool": t, "note": ""})

    payload: Dict[str, Any] = {
        "answer": answer.strip(),
        "used": [c["tool"] for c in consulted],
        "reasoning": {"plan": [], "consulted": consulted,
                      "angle": "investigated live with Claude Code"},
        "engine": "claude-code",
    }
    cites = _extract_citations(events)
    if cites:
        payload["citations"] = cites
    if symbol:
        payload["symbol"] = symbol
    return payload
