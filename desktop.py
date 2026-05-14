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

import logging
import os
import socket
import sys
import threading
import time
import warnings
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


def _port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    s = socket.socket()
    try:
        return s.connect_ex((host, port)) == 0
    finally:
        s.close()


def _wait_for_port(port: int, timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _port_in_use(port):
            return True
        time.sleep(0.1)
    return False


def _start_flask(port: int) -> None:
    # Import lazily so the missing-pywebview error (below) fires before the
    # heavy app.py import cost.
    from app import app, _start_idea_warmer

    _start_idea_warmer()
    # debug=False + use_reloader=False is critical: Flask's reloader forks a
    # child, which fights pywebview for the main thread.
    app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False, threaded=True)


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

    port = int(os.environ.get("PORT", 5001))
    if _port_in_use(port):
        sys.stderr.write(
            f"✗ Port {port} is already in use. Set PORT=<free-port> and try again,\n"
            f"  or close whatever is using it (often a stray `./run.sh`).\n"
        )
        return 1

    flask_thread = threading.Thread(target=_start_flask, args=(port,), daemon=True)
    flask_thread.start()

    if not _wait_for_port(port):
        sys.stderr.write(f"✗ Flask didn't start on port {port} within 30s. Aborting.\n")
        return 1
    log.info("Flask is up on http://127.0.0.1:%d", port)

    log.info("[desktop] pywebview version: %s", getattr(webview, "__version__", "?"))
    log.info("[desktop] creating window ...")
    win = webview.create_window(
        title="AUGUR — Wealth Intelligence System",
        url=f"http://127.0.0.1:{port}",
        width=1400,
        height=900,
        min_size=(900, 600),
    )
    log.info("[desktop] window created: %r — calling webview.start() ...", win)
    # pywebview's start() blocks the main thread until the window closes.
    # The Flask thread is a daemon, so it terminates with us.
    webview.start(debug=bool(os.environ.get("AUGUR_WEBVIEW_DEBUG")))
    log.info("[desktop] webview.start() returned — exiting")
    return 0


if __name__ == "__main__":
    sys.exit(main())
