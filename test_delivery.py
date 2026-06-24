"""Offline tests for jarvis_delivery (v2.4) — local digest delivery.

No notifications, files, or email actually fire against real targets: the
briefing is a fixture, the file channel writes to a temp dir, notify/email are
stubbed. Verifies render content, the file-drop channel, and that
deliver_digest fans out without raising when channels are unavailable.
"""
import os
import sys
import tempfile

import jarvis_delivery as jd


_FIXTURE = {
    "greeting": "Good morning. Markets are open.",
    "headline": "Book up 1.2%; NVDA leads, 2 earnings this week.",
    "insights": [
        {"priority": 1, "kind": "alert", "tone": "warn",
         "title": "AAPL alert tripped", "detail": "crossed 230"},
        {"priority": 2, "kind": "earnings", "tone": "info",
         "title": "MSFT reports Thu", "detail": "2 days out"},
    ],
    "portfolio": {"total_value": "$104,200"},
}


def _patch_briefing():
    import jarvis
    jarvis.get_briefing = lambda *a, **k: dict(_FIXTURE)
    # Stub the v2.5 health line so the digest render stays offline/fast.
    import portfolio_insights
    portfolio_insights.portfolio_health = lambda max_symbols=25: {
        "analyzed": 3, "tone": "constructive", "weighted_momentum": 64,
        "breadth_above_50dma_pct": 60}


def test_render_contains_key_fields():
    _patch_briefing()
    d = jd.render_digest()
    assert d["subject"].startswith("AUGUR briefing"), d["subject"]
    assert "NVDA leads" in d["text"], d["text"]
    assert "AAPL alert tripped" in d["text"]
    assert "AAPL alert tripped" in d["html"] and "<li>" in d["html"]
    assert d["summary"] and len(d["summary"]) <= 240
    print("  [OK] render_digest: subject, headline, cards in text + html")


def test_file_channel_writes(tmpdir=None):
    _patch_briefing()
    d = tempfile.mkdtemp()
    jd._setting = lambda k, default="": d if k == "digest_dir" else default
    out = jd._channel_file(jd.render_digest())
    assert out.startswith("wrote "), out
    files = os.listdir(d)
    assert len(files) == 1 and files[0].endswith(".md"), files
    body = open(os.path.join(d, files[0])).read()
    assert "AUGUR briefing" in body
    print("  [OK] file channel drops a dated markdown digest")


def test_email_skipped_without_config():
    jd._setting = lambda k, default="": default      # no SMTP settings
    # ensure no env creds leak in
    for k in ("AUGUR_SMTP_HOST", "AUGUR_SMTP_TO", "AUGUR_SMTP_FROM", "AUGUR_SMTP_USER"):
        os.environ.pop(k, None)
    status = jd._channel_email(jd.render_digest() if False else {"text": "x", "html": "x"})
    assert status.startswith("skipped"), status
    print("  [OK] email channel skips silently when unconfigured")


def test_deliver_fans_out_no_raise():
    _patch_briefing()
    d = tempfile.mkdtemp()

    def _set(k, default=""):
        if k == "digest_dir":
            return d
        return default
    jd._setting = _set
    # stub notify so it never touches the OS even on a mac
    jd._CHANNELS = dict(jd._CHANNELS)
    jd._CHANNELS["notify"] = lambda digest: "sent (stub)"
    out = jd.deliver_digest(channels=["file", "notify", "email"])
    assert set(out["channels"].keys()) == {"file", "notify", "email"}, out
    assert out["channels"]["file"].startswith("wrote "), out
    assert out["channels"]["email"].startswith("skipped"), out
    assert out["summary"]
    print("  [OK] deliver_digest fans out to all channels without raising")


def test_unknown_channel():
    _patch_briefing()
    jd._setting = lambda k, default="": default
    out = jd.deliver_digest(channels=["bogus"])
    assert out["channels"]["bogus"] == "unknown channel", out
    print("  [OK] unknown channel reported, no raise")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    print("jarvis_delivery — {} tests".format(len(fns)))
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
