"""
AUGUR — native desktop wrapper (macOS / Linux / Windows).

Runs the Flask app on a background thread and opens a native window
(WKWebView on macOS) pointing at it. The web UI is unchanged; this is
purely a packaging layer.

Run:
    python desktop.py

Requires `pywebview` and its platform-specific backend (see
requirements-desktop.txt).
"""

from __future__ import annotations

import json
import logging
import os
import plistlib
import socket
import sys
import threading
import time
import urllib.request
import warnings
import webbrowser
from pathlib import Path

# Silence known-benign warnings BEFORE third-party imports trigger them.
# Filters must use message patterns here — importing the warning class would
# already trigger the noisy module-level prints we're trying to suppress.
warnings.filterwarnings("ignore", message=r".*OpenSSL 1\.1\.1\+.*")
warnings.filterwarnings(
    "ignore",
    message=r"resource_tracker:.*process died unexpectedly.*",
)
warnings.filterwarnings(
    "ignore",
    category=RuntimeWarning,
    module=r"sklearn\..*",
)
logging.getLogger("yfinance").setLevel(logging.CRITICAL)

log = logging.getLogger("augur.desktop")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


def _user_data_dir() -> Path:
    """Platform-appropriate per-user writable dir for wealth.db.

    When run as `python desktop.py` from a checkout, CWD is writable and
    AUGUR_DB_PATH is left alone so it falls back to `./wealth.db` (matches
    the existing CLI / run.sh behaviour). When run from a `.app` bundle in
    /Applications, CWD is `/`, so we need an explicit writable location.
    """
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "AUGUR"
    if sys.platform.startswith("win"):
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "AUGUR"
    # Linux / other XDG
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "augur"


def _bootstrap_user_data_dir() -> None:
    """If running from a frozen bundle (py2app / pyinstaller), point the DB at
    the user data dir so we can actually write to it. No-op for dev runs from
    a checkout, where the working directory is already writable."""
    if not getattr(sys, "frozen", False):
        return
    if "AUGUR_DB_PATH" in os.environ:
        return  # user explicitly overrode it
    data_dir = _user_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    os.environ["AUGUR_DB_PATH"] = str(data_dir / "wealth.db")
    log.info("Using user data dir: %s", data_dir)


_bootstrap_user_data_dir()


# ── update check ──────────────────────────────────────────────────────────
# Polls GitHub's releases API once on startup. If a newer version is tagged,
# a native confirmation dialog asks the user whether to open the release
# page in their default browser. They drag the new .app to /Applications
# to install — same UX as the first-time install.
#
# Opt out: set AUGUR_NO_UPDATE_CHECK=1.
# Force a specific "current" version for testing: AUGUR_VERSION_OVERRIDE=0.1.0.

GITHUB_RELEASES_API = "https://api.github.com/repos/Mikegris/augur/releases/latest"


def _current_version() -> str:
    """Read CFBundleShortVersionString from the bundle's Info.plist; return
    'dev' for source runs (we skip the prompt in that case)."""
    override = os.environ.get("AUGUR_VERSION_OVERRIDE")
    if override:
        return override
    if not getattr(sys, "frozen", False):
        return "dev"
    plist_path = Path(sys.executable).parent.parent / "Info.plist"
    try:
        with open(plist_path, "rb") as f:
            return plistlib.load(f).get("CFBundleShortVersionString", "0.0.0")
    except Exception:
        return "0.0.0"


def _parse_semver(v: str) -> tuple:
    """Lenient parser: '1.2.3-rc.1' → (1, 2, 3). Stops at first non-numeric
    chunk so suffixes like '-beta' don't break comparison."""
    parts: list[int] = []
    for piece in v.lstrip("v").split("."):
        head = piece.split("-")[0].split("+")[0]
        try:
            parts.append(int(head))
        except ValueError:
            break
    return tuple(parts) if parts else (0,)


def _fetch_latest_release() -> tuple[str, str] | None:
    """Return (latest_tag_no_v_prefix, html_url) or None on any failure.

    Filters out pre-releases (`prerelease=true` in the API response) and
    drafts (`draft=true`). GitHub's /releases/latest endpoint *usually*
    excludes pre-releases automatically, but if a maintainer flips the
    "latest" toggle manually it can still return one — and our lenient
    semver parser would strip the `-rc.1` suffix and prompt the user to
    "upgrade" to a beta. _parse_semver("1.2.4-beta") returns (1,2,4),
    which compares strictly greater than a stable (1,2,3).

    Defensive: also guards against `data` being non-dict (GitHub returns
    `{"message": "Not Found"}` for an org with no releases yet — that's
    still a dict, fine — but a network proxy could inject something else).
    """
    try:
        req = urllib.request.Request(
            GITHUB_RELEASES_API,
            headers={
                "User-Agent": "AUGUR-updater",
                "Accept": "application/vnd.github+json",
            },
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            data = json.loads(r.read())
        if not isinstance(data, dict):
            return None
        if data.get("prerelease") or data.get("draft"):
            log.info("update check: latest release is pre-release/draft, skipping")
            return None
        tag = (data.get("tag_name") or "").lstrip("v")
        url = data.get("html_url") or ""
        return (tag, url) if tag and url else None
    except Exception as e:
        log.debug("update fetch failed: %s", e)
        return None


def _check_for_updates(window) -> None:
    """Background-thread worker — fetch latest release, prompt user if newer.
    Silent no-op on any error (offline, GitHub down, etc.); a wealth tracker
    that can't check for updates should still launch cleanly."""
    if os.environ.get("AUGUR_NO_UPDATE_CHECK") == "1":
        return
    current = _current_version()
    if current == "dev":
        log.info("update check skipped — dev run")
        return

    result = _fetch_latest_release()
    if not result:
        return
    latest, url = result

    if _parse_semver(latest) <= _parse_semver(current):
        log.info("up to date (have %s, latest %s)", current, latest)
        return

    log.info("update available: %s → %s", current, latest)
    try:
        confirmed = window.create_confirmation_dialog(
            "AUGUR — update available",
            f"Version {latest} is available (you have {current}).\n\n"
            f"Open the download page in your default browser?",
        )
        if confirmed:
            webbrowser.open(url)
    except Exception as e:
        log.warning("update prompt failed: %s", e)


def _port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    s = socket.socket()
    s.settimeout(0.5)
    try:
        return s.connect_ex((host, port)) == 0
    except OSError:
        # A transient socket/DNS error during the startup poll shouldn't crash
        # the launcher — treat it as "not yet up" and let the caller retry.
        return False
    finally:
        s.close()


def _wait_for_port(port: int, timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _port_in_use(port):
            return True
        time.sleep(0.1)
    return False


def _start_flask(port: int, ready: threading.Event, failed: threading.Event) -> None:
    # Import lazily so the missing-pywebview error (below) fires before the
    # heavy app.py import cost.
    from app import app, _start_idea_warmer
    # Bind the socket OURSELVES via make_server (it binds in the constructor)
    # instead of app.run(): a bind failure inside this daemon thread was
    # previously swallowed, and _wait_for_port would then see ANOTHER
    # instance's server on the port and happily attach our window to it —
    # which goes dead the moment that instance quits (TOCTOU with the
    # _port_in_use pre-check in main()). `ready`/`failed` let main() wait for
    # OUR server specifically, not just "something listening on the port".
    # No reloader here either — make_server never forks the reloader child
    # that fights pywebview for the main thread (why app.run used
    # debug=False/use_reloader=False).
    from werkzeug.serving import make_server
    try:
        server = make_server("127.0.0.1", port, app, threaded=True)
    except OSError as e:
        log.error("Flask could not bind 127.0.0.1:%d: %s", port, e)
        failed.set()
        return
    _start_idea_warmer()
    ready.set()  # socket is bound and listening; connections queue until serve_forever
    server.serve_forever()


def main() -> int:
    try:
        import webview
    except ImportError:
        sys.stderr.write(
            "✗ pywebview is not installed.\n\n"
            "  Install it with:\n"
            "    pip install -r requirements-desktop.txt\n\n"
            "  Then re-run:\n"
            "    python desktop.py\n"
        )
        return 1

    # A bad PORT ("auto", "", "5001 ") must not crash the launcher with a raw
    # ValueError — in the frozen .app stderr goes nowhere and it just looks
    # like the app doesn't open. Fall back to the default with a warning.
    port_raw = os.environ.get("PORT", "5001")
    try:
        port = int(port_raw)
    except ValueError:
        log.warning("PORT=%r is not a number — falling back to 5001", port_raw)
        port = 5001
    if _port_in_use(port):
        sys.stderr.write(
            f"✗ Port {port} is already in use. Set PORT=<free-port> and try again,\n"
            f"  or close whatever is using it (often a stray `./run.sh`).\n"
        )
        return 1

    flask_ready = threading.Event()
    flask_failed = threading.Event()
    flask_thread = threading.Thread(
        target=_start_flask, args=(port, flask_ready, flask_failed), daemon=True)
    flask_thread.start()

    # Wait for OUR Flask's bind, not merely for the port to answer: a second
    # instance racing past the _port_in_use pre-check would otherwise attach
    # its window to the first instance's server (see _start_flask).
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline and not flask_ready.is_set():
        if flask_failed.is_set() or not flask_thread.is_alive():
            sys.stderr.write(
                f"✗ Port {port} was grabbed before Flask could bind — is another\n"
                f"  AUGUR instance already running? Set PORT=<free-port> to run both.\n"
            )
            return 1
        time.sleep(0.1)
    if not flask_ready.is_set():
        sys.stderr.write(f"✗ Flask didn't start on port {port} within 30s. Aborting.\n")
        return 1
    log.info("Flask is up on http://127.0.0.1:%d", port)

    log.info("[desktop] pywebview version: %s", getattr(webview, "__version__", "?"))
    log.info("[desktop] creating window ...")
    # Per-launch cache-buster on the URL: WKWebView's persistent data store
    # (keyed by bundle id) survives app updates, so navigating to a bare "/"
    # could load a stale cached page from an older build. A changing query
    # param forces a fresh document fetch each launch; the /  route ignores it.
    win = webview.create_window(
        title="AUGUR — Wealth Intelligence System",
        url=f"http://127.0.0.1:{port}/?_launch={int(time.time())}",
        width=1400,
        height=900,
        min_size=(900, 600),
    )
    log.info("[desktop] window created: %r — calling webview.start() ...", win)

    # Fire the update check once the WKWebView has loaded the page — running
    # any earlier risks the dialog appearing on a blank window, and the user
    # has no idea what app is prompting them. Background thread so we don't
    # block the page interaction while polling GitHub.
    def _on_loaded():
        threading.Thread(
            target=_check_for_updates, args=(win,), daemon=True
        ).start()
    win.events.loaded += _on_loaded

    # pywebview's start() blocks the main thread until the window closes.
    # The Flask thread is a daemon, so it terminates with us.
    # Truthy-string check, not bool(): bool("0")/bool("false") are True, so an
    # explicit AUGUR_WEBVIEW_DEBUG=0 would ENABLE the inspector it disables.
    webview.start(debug=os.environ.get("AUGUR_WEBVIEW_DEBUG", "").strip().lower()
                  in ("1", "true", "yes"))
    log.info("[desktop] webview.start() returned — exiting")
    return 0


if __name__ == "__main__":
    sys.exit(main())
