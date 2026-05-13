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

log = logging.getLogger("augur.desktop")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


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

    webview.create_window(
        title="AUGUR — Wealth Intelligence System",
        url=f"http://127.0.0.1:{port}",
        width=1400,
        height=900,
        min_size=(900, 600),
    )
    # pywebview's start() blocks the main thread until the window closes.
    # The Flask thread is a daemon, so it terminates with us.
    webview.start()
    return 0


if __name__ == "__main__":
    sys.exit(main())
