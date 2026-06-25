#!/usr/bin/env python3
"""AJTA overlay tests (§16 opencode, §18 voice, §21.1 Langfuse).

Confirms the optional overlays ship fail-closed / off and behave safely:
opencode disabled until VERIFY-OPENCODE + forbidden-path guard; voice never
auto-executes a high-risk command; Langfuse is a no-op unless configured.
"""
import os
import sys
import tempfile

os.environ["AUGUR_DB_PATH"] = tempfile.mktemp(suffix="_ajov.db")

import database as db          # noqa: E402
import aj_db                    # noqa: E402
import aj_opencode             # noqa: E402
import aj_voice                # noqa: E402
import aj_langfuse             # noqa: E402

aj_db.aj_init()


# ── opencode (§16) ────────────────────────────────────────────────────────────

def test_opencode_disabled_by_default():
    assert aj_opencode.available() is False
    r = aj_opencode.run_candidate({"strategy": "momentum"})
    assert r["enabled"] is False and r["executable"] is False
    assert "VERIFY-OPENCODE" in r["error"]


def test_opencode_forbidden_paths():
    assert aj_opencode.path_allowed("research/candidate.py") is True
    for bad in ("wealth.db", "wealth.db-wal", "x.lock", ".aj_secret_key",
                "secrets/key.txt", "config/risk.yaml"):
        assert aj_opencode.path_allowed(bad) is False, bad


def test_opencode_enables_only_with_verify():
    db.set_setting("aj_verify_opencode", "pass")
    try:
        assert aj_opencode.available() is True
        r = aj_opencode.run_candidate({"strategy": "x"})
        # even enabled, output is research-only and NEVER executable
        assert r["enabled"] is True and r["executable"] is False
    finally:
        db.set_setting("aj_verify_opencode", "")


# ── voice (§18) ───────────────────────────────────────────────────────────────

def test_voice_high_risk_never_auto_executes():
    for cmd in ("buy 100 shares of NVDA", "sell everything", "kill the agent",
                "enable live trading", "set max loss to 10000"):
        r = aj_voice.handle_command(cmd)
        assert r.get("requires_approval") is True and r.get("executed") is False, cmd


def test_voice_read_commands_execute():
    import fetcher
    fetcher.get_quotes_batch = lambda syms: {s: {"price": 100} for s in syms}
    r = aj_voice.handle_command("what's our status")
    assert r["ok"] and r.get("executed") is True and r.get("tool") == "aj_status"
    r2 = aj_voice.handle_command("how much P&L today")
    assert r2.get("tool") == "aj_day_pnl"


def test_voice_unknown_is_safe():
    r = aj_voice.handle_command("tell me a joke")
    assert r["ok"] and r.get("requires_approval") is not True and r.get("executed") is False


# ── langfuse (§21.1) ──────────────────────────────────────────────────────────

def test_langfuse_noop_without_config():
    # ensure env is clear
    for k in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"):
        os.environ.pop(k, None)
    assert aj_langfuse.enabled() is False
    out = aj_langfuse.emit_cycle_trace({"cycle_id": "c", "proposals": []})
    assert out["emitted"] is False   # fail-open no-op, never raises


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    print("aj_overlays — {} tests".format(len(fns)))
    failed = 0
    for fn in fns:
        try:
            fn()
            print("  [OK] {}".format(fn.__name__))
        except AssertionError as e:
            failed += 1
            print("  [XX] {}: {}".format(fn.__name__, e))
        except Exception as e:
            failed += 1
            print("  [XX] {}: unexpected {}: {}".format(fn.__name__, type(e).__name__, e))
    print("PASS" if failed == 0 else "{} FAILED".format(failed))
    sys.exit(1 if failed else 0)
