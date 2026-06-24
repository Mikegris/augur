"""Local digest delivery for Jarvis (v2.4).

Lets Jarvis reach the user OUTSIDE the open browser tab. The briefing and
triggered watches are already built server-side (jarvis.get_briefing /
away_digest); this renders them to text/HTML and pushes them through local-
first channels:

  * macOS notification  — a one-line nudge (osascript; darwin only)
  * markdown file drop  — the full digest written to ./digests/<date>.md so it
                          can be read anytime, anywhere on the machine
  * SMTP email          — OPTIONAL, only when creds are configured; skipped
                          silently otherwise (keeps the local-only default)

Design values honored: everything stays on the machine unless the user
deliberately configures SMTP, and every channel is independently best-effort —
a missing binary, absent creds, or a send failure never raises. The cloud
/schedule and the claude.ai Gmail MCP are NOT reachable from this Flask
process, so we don't depend on them; the cache warmer drives the daily cadence.
"""
import datetime as _dt
import logging
import os
import platform
import subprocess
from typing import Any, Dict, List, Optional

log = logging.getLogger("augur.jarvis_delivery")

_REPO = os.path.dirname(os.path.abspath(__file__))


def _setting(key: str, default: str = "") -> str:
    try:
        import database as db
        return (db.get_settings() or {}).get(key) or default
    except Exception:
        return default


def _esc_html(s: Any) -> str:
    return (str(s or "").replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


def render_digest() -> Dict[str, str]:
    """Render the current briefing to {subject, summary, text, html}.
    Fail-open: renders whatever the briefing produced, or a minimal stub."""
    brief: Dict[str, Any] = {}
    try:
        import jarvis
        brief = jarvis.get_briefing() or {}
    except Exception as e:
        log.debug("render_digest: briefing failed: %s", e)

    greeting = str(brief.get("greeting") or "Your AUGUR briefing")
    headline = str(brief.get("headline") or "")
    cards: List[Dict[str, Any]] = brief.get("insights") or []
    portfolio = brief.get("portfolio") or {}

    today = _dt.date.today().isoformat()
    subject = "AUGUR briefing — {}".format(today)

    # Short summary for the notification: headline, else the top card title.
    summary = headline or (cards[0].get("title") if cards else "No new signals.")

    # Plaintext
    tlines = [subject, "=" * len(subject), "", greeting]
    if headline:
        tlines += ["", headline]
    if portfolio:
        pv = portfolio.get("total_value")
        if pv is not None:
            tlines += ["", "Book: {}".format(pv)]
    if cards:
        tlines += ["", "Insights:"]
        for c in cards[:10]:
            title = c.get("title") or ""
            detail = c.get("detail") or ""
            tlines.append("  • {}{}".format(title, " — " + detail if detail else ""))
    else:
        tlines += ["", "No prioritized insights right now."]
    text = "\n".join(tlines)

    # HTML
    hparts = ["<h2>{}</h2>".format(_esc_html(subject)),
              "<p>{}</p>".format(_esc_html(greeting))]
    if headline:
        hparts.append("<p><strong>{}</strong></p>".format(_esc_html(headline)))
    if cards:
        hparts.append("<ul>")
        for c in cards[:10]:
            title = _esc_html(c.get("title") or "")
            detail = _esc_html(c.get("detail") or "")
            hparts.append("<li><strong>{}</strong>{}</li>".format(
                title, " — " + detail if detail else ""))
        hparts.append("</ul>")
    else:
        hparts.append("<p>No prioritized insights right now.</p>")
    html = "\n".join(hparts)

    return {"subject": subject, "summary": str(summary)[:240],
            "text": text, "html": html}


def _channel_notify(digest: Dict[str, str]) -> str:
    """macOS notification banner (darwin only)."""
    if platform.system() != "Darwin":
        return "skipped (not macOS)"
    try:
        title = "AUGUR — Jarvis"
        msg = digest.get("summary") or "Briefing ready."
        # Pass via env to avoid AppleScript-string injection from dynamic text.
        script = ('display notification (system attribute "AUGUR_MSG") '
                  'with title (system attribute "AUGUR_TITLE")')
        env = dict(os.environ, AUGUR_MSG=msg, AUGUR_TITLE=title)
        subprocess.run(["osascript", "-e", script], env=env,
                       timeout=10, check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return "sent"
    except Exception as e:
        log.debug("notify failed: %s", e)
        return "failed: {}".format(e)


def _channel_file(digest: Dict[str, str]) -> str:
    """Write the full digest as a dated markdown file."""
    try:
        d = (_setting("digest_dir") or os.path.join(_REPO, "digests")).strip()
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, "{}.md".format(_dt.date.today().isoformat()))
        with open(path, "w") as f:
            f.write(digest.get("text") or "")
        return "wrote {}".format(path)
    except Exception as e:
        log.debug("file drop failed: %s", e)
        return "failed: {}".format(e)


def _smtp_config() -> Optional[Dict[str, str]]:
    """SMTP creds from env > DB settings; None when not fully configured."""
    def g(env_key, set_key):
        return (os.environ.get(env_key) or _setting(set_key)).strip()
    host = g("AUGUR_SMTP_HOST", "smtp_host")
    to = g("AUGUR_SMTP_TO", "smtp_to")
    sender = g("AUGUR_SMTP_FROM", "smtp_from") or g("AUGUR_SMTP_USER", "smtp_user")
    if not host or not to or not sender:
        return None
    return {
        "host": host, "to": to, "from": sender,
        "port": g("AUGUR_SMTP_PORT", "smtp_port") or "587",
        "user": g("AUGUR_SMTP_USER", "smtp_user"),
        "password": g("AUGUR_SMTP_PASS", "smtp_pass"),
    }


def _channel_email(digest: Dict[str, str]) -> str:
    """SMTP email — only when fully configured; skipped silently otherwise."""
    cfg = _smtp_config()
    if not cfg:
        return "skipped (no SMTP configured)"
    try:
        import smtplib
        from email.mime.multipart import MIMEMultipart
        from email.mime.text import MIMEText
        msg = MIMEMultipart("alternative")
        msg["Subject"] = digest.get("subject") or "AUGUR briefing"
        msg["From"] = cfg["from"]
        msg["To"] = cfg["to"]
        msg.attach(MIMEText(digest.get("text") or "", "plain"))
        msg.attach(MIMEText(digest.get("html") or "", "html"))
        with smtplib.SMTP(cfg["host"], int(cfg["port"]), timeout=20) as s:
            try:
                s.starttls()
            except Exception:
                pass  # server may not support STARTTLS
            if cfg["user"] and cfg["password"]:
                s.login(cfg["user"], cfg["password"])
            s.sendmail(cfg["from"], [t.strip() for t in cfg["to"].split(",")],
                       msg.as_string())
        return "sent to {}".format(cfg["to"])
    except Exception as e:
        log.debug("email failed: %s", e)
        return "failed: {}".format(e)


_CHANNELS = {"notify": _channel_notify, "file": _channel_file, "email": _channel_email}


def deliver_digest(channels: Optional[List[str]] = None) -> Dict[str, Any]:
    """Render once and fan out to the requested channels. `channels` defaults
    to the `digest_channels` setting (comma-separated), else ['file']. Returns
    {summary, channels: {name: status}} — never raises."""
    if channels is None:
        raw = _setting("digest_channels", "file")
        channels = [c.strip() for c in raw.split(",") if c.strip()] or ["file"]
    digest = render_digest()
    statuses: Dict[str, str] = {}
    for name in channels:
        fn = _CHANNELS.get(name)
        if fn is None:
            statuses[name] = "unknown channel"
            continue
        try:
            statuses[name] = fn(digest)
        except Exception as e:  # belt-and-suspenders; channels already guard
            statuses[name] = "failed: {}".format(e)
    return {"summary": digest.get("summary"), "subject": digest.get("subject"),
            "channels": statuses}
