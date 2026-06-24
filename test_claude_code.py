"""Offline tests for jarvis_claude (v2.4) — the Claude-Code-powered backend.

No network, no real `claude` binary: subprocess.Popen is faked with canned
stream-json events. Covers parse → answer, tool→progress mapping, citation
extraction, the safe default permission model, cancellation/termination, and
graceful fallback (None) so the caller degrades to local routing.
"""
import io
import json
import sys

import jarvis_claude as jc


class _FakeStdin:
    def write(self, _):
        pass

    def close(self):
        pass


class _FakePopen:
    """Minimal stand-in for subprocess.Popen yielding canned JSONL lines."""
    def __init__(self, lines, *, captured_cmd=None, hang=False):
        self.pid = 4242
        self.stdin = _FakeStdin()
        self.stdout = iter([l if l.endswith("\n") else l + "\n" for l in lines])
        self.stderr = io.StringIO("")
        self._terminated = False
        self._hang = hang
        if captured_cmd is not None:
            captured_cmd.append(self._cmd)

    def wait(self, timeout=None):
        return 0

    def terminate(self):
        self._terminated = True


def _install_fake(monkey_lines, captured_cmd=None):
    """Patch jc so ask_claude runs against a fake CLI. Returns nothing; the
    caller restores via the returned closure isn't needed — each test sets its
    own attrs fresh."""
    jc._probe_at = 0.0
    jc._claude_bin = lambda: "/usr/bin/claude"          # pretend installed
    jc._setting = lambda k, d="": d                       # all defaults
    jc._portfolio_brief = lambda: "Portfolio: AAPL, MSFT."

    def _popen(cmd, **kw):
        if captured_cmd is not None:
            captured_cmd.append(cmd)
        return _FakePopen(monkey_lines)
    jc.subprocess.Popen = _popen
    jc.os.killpg = lambda *a, **k: None
    jc.os.getpgid = lambda *a, **k: 4242


def _events_success():
    return [
        json.dumps({"type": "system", "subtype": "init", "model": "claude"}),
        json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "WebSearch", "input": {"query": "SpaceX valuation 2026"}},
        ]}}),
        json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "WebFetch", "input": {"url": "https://example.com/spacex"}},
        ]}}),
        json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Read", "input": {"file_path": "/x/forecast_ensemble.py"}},
        ]}}),
        json.dumps({"type": "result", "subtype": "success", "is_error": False,
                    "result": "SpaceX was last valued around $350B in a 2025 tender offer.",
                    "total_cost_usd": 0.12, "num_turns": 4}),
    ]


def test_success_parse_and_progress():
    statuses = []
    _install_fake(_events_success())
    out = jc.ask_claude("what's SpaceX worth?", progress=statuses.append)
    assert out is not None, "expected a payload"
    assert "350B" in out["answer"], out["answer"]
    assert "WebSearch" in out["used"] and "Read" in out["used"], out["used"]
    assert out["engine"] == "claude-code"
    # citation harvested from the WebFetch tool_use
    cites = out.get("citations") or []
    assert any("example.com/spacex" in c["url"] for c in cites), cites
    # progress streamed the tool activity in human terms
    joined = " | ".join(statuses)
    assert "searching the web" in joined.lower(), joined
    assert "reading" in joined.lower(), joined
    print("  [OK] success: parsed answer, tools, citations, progress")


def test_safe_default_command():
    cmd = []
    _install_fake(_events_success(), captured_cmd=cmd)
    jc.ask_claude("deep dive on NVDA")
    sent = cmd[0]
    assert "bypassPermissions" not in sent, "default must NOT bypass permissions"
    assert "default" in sent, sent
    assert "--allowedTools" in sent, "default must scope tools to an allowlist"
    # the read-only allowlist must not include shell / write tools by default
    ai = sent.index("--allowedTools")
    allow = sent[ai + 1: ai + 7]
    assert "Bash" not in allow and "Write" not in allow and "Edit" not in allow, allow
    assert "Read" in allow and "WebSearch" in allow, allow
    print("  [OK] safe default: no bypass, read-only allowlist, no Bash/Write")


def test_empty_answer_falls_back():
    events = [json.dumps({"type": "result", "subtype": "error_max_turns",
                          "is_error": True, "result": ""})]
    _install_fake(events)
    out = jc.ask_claude("anything")
    assert out is None, "empty/errored run must return None so caller falls back"
    print("  [OK] empty/errored run returns None (graceful fallback)")


def test_cancellation_returns_none():
    killed = {"n": 0}
    _install_fake(_events_success())
    jc.os.killpg = lambda *a, **k: killed.__setitem__("n", killed["n"] + 1)
    out = jc.ask_claude("anything", cancelled=lambda: True)
    assert out is None, "a cancelled run must return None"
    assert killed["n"] >= 1, "cancelled run must terminate the process group"
    print("  [OK] cancellation: returns None and SIGTERMs the process group")


def test_unavailable_returns_none():
    jc._probe_at = 0.0
    jc._claude_bin = lambda: None  # not installed
    out = jc.ask_claude("anything")
    assert out is None
    print("  [OK] unavailable (no binary) returns None")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    print("jarvis_claude — {} tests".format(len(fns)))
    failed = 0
    for fn in fns:
        try:
            fn()
        except AssertionError as e:
            failed += 1
            print("  [XX] {}: {}".format(fn.__name__, e))
        except Exception as e:
            failed += 1
            print("  [XX] {}: unexpected {}: {}".format(fn.__name__, type(e).__name__, e))
    print("PASS" if failed == 0 else "{} FAILED".format(failed))
    sys.exit(1 if failed else 0)
