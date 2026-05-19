"""
py2app build config for AUGUR.app.

Usage:
    pip install -r requirements-build.txt
    python setup_app.py py2app

Produces dist/AUGUR.app — a self-contained macOS bundle.
"""

import subprocess
import sys
from pathlib import Path
from setuptools import setup

HERE = Path(__file__).parent

# Project modules that app.py imports — list them explicitly so py2app's
# modulefinder doesn't miss any (it walks the import graph from desktop.py,
# but optional `try: import …` blocks can confuse static analysis).
LOCAL_MODULES = [
    "app",
    "ai_summarizer",
    "alt_data_engine",
    "alt_signals",
    "cache_store",
    "cache_warmer",
    "calendar_v2",
    "congress",
    "contagion_graph",
    "crypto_exchanges",
    "data_sources",
    "database",
    "earnings",
    "fetcher",
    "gex_engine",
    "historical_analog",
    "idea_generator",
    "idea_pool_warmer",
    "liquidity_monitor",
    "ml_forecast",
    "narrative_engine",
    "opportunity_scanner",
    "reflexivity_detector",
    "sec_edgar",
    "sec_filings_v2",
    "smart_money",
    "synthetic_insider",
]

# Third-party packages with dynamic imports or namespace-package quirks that
# py2app sometimes misses.
EXTRA_PACKAGES = [
    "webview",         # pywebview pulls platform backends at runtime
    "flask",
    "flask_compress",
    "werkzeug",
    "jinja2",
    "markupsafe",
    "yfinance",
    "pandas",
    "numpy",
    "sklearn",
    # sklearn lazy-imports joblib and threadpoolctl inside utils submodules,
    # which py2app's modulefinder doesn't follow. Missing them surfaces as
    # the opaque "cannot import name 'ClassifierMixin'" because the import
    # chain breaks one level up in sklearn/utils/__init__.py. List them
    # explicitly so the modulefinder pulls their full trees.
    "joblib",
    "threadpoolctl",
    "scipy",
    "bs4",
    "lxml",
    "pdfplumber",
    "finvizfinance",
    "openai",
    "httpx",
    "httpcore",
    "anyio",
    "sniffio",
    "pydantic",
    "pydantic_core",
    "requests",
    "urllib3",
    # chardet is the pure-Python char-detection fallback for `requests`.
    # charset_normalizer 3.x ships hash-named mypyc binaries that py2app
    # can't trace, so requests can't load it from the bundle even when the
    # files are physically present. chardet has no compiled extensions.
    "chardet",
    "charset_normalizer",
    "certifi",
    "idna",
    "curl_cffi",
    "cryptography",
]

def _walk_tree(root: Path, prefix: str):
    """Convert a source tree into py2app data_files tuples while preserving
    the subdirectory structure. data_files is [(dest_dir, [src_files])] —
    every file at the same dest_dir gets one entry.
    """
    buckets: dict[str, list[str]] = {}
    for f in root.rglob("*"):
        if not f.is_file():
            continue
        rel_dir = f.parent.relative_to(root)
        dest = f"{prefix}/{rel_dir}".rstrip("/") if str(rel_dir) != "." else prefix
        buckets.setdefault(dest, []).append(str(f))
    return [(dest, files) for dest, files in buckets.items()]


DATA_FILES = []
DATA_FILES += _walk_tree(HERE / "templates", "templates")
DATA_FILES += _walk_tree(HERE / "static",    "static")
if (HERE / ".env.example").exists():
    DATA_FILES.append(("", [str(HERE / ".env.example")]))

PLIST = {
    "CFBundleName":            "AUGUR",
    "CFBundleDisplayName":     "AUGUR",
    "CFBundleIdentifier":      "com.augur.wealth",
    "CFBundleShortVersionString": "0.1.12",
    "CFBundleVersion":         "0.1.12",
    "LSMinimumSystemVersion":  "10.13",
    "NSHighResolutionCapable": True,
    # We want a Dock icon + menubar; LSUIElement=0 keeps the app visible.
    "LSUIElement":             False,
    # No outgoing network restrictions; this is a per-user app.
    "NSAppTransportSecurity":  {"NSAllowsArbitraryLoads": True},
}

setup(
    name="AUGUR",
    app=["desktop.py"],
    data_files=DATA_FILES,
    # Disable setuptools' auto-discovery — it sees templates/ and static/ as
    # candidate packages otherwise and refuses to build.
    py_modules=[],
    packages=[],
    options={
        "py2app": {
            "argv_emulation": False,
            "plist": PLIST,
            "includes": LOCAL_MODULES,
            "packages": EXTRA_PACKAGES,
            "excludes": [
                "tkinter", "PyQt5", "PyQt6", "PySide2", "PySide6",
                # setuptools is a build-time dep only; its vendored dist-info
                # files collide with the top-level ones during py2app collect.
                "setuptools", "pkg_resources",
            ],
            "iconfile": str(HERE / "icon.icns"),
        }
    },
    setup_requires=["py2app"],
)


# ── post-build: fix multiprocessing helper python's dylib reference ─────────
# py2app ships a stripped helper at Contents/MacOS/python used by Python's
# multiprocessing module when it spawns worker subprocesses (joblib /
# sklearn n_jobs=-1 trigger this). The helper's LC_LOAD_DYLIB records
# `@executable_path/../../../../Python3` — four levels up resolves OUTSIDE
# the bundle, so every worker dies with `Library not loaded: Python3`.
# Rewrite that load command to point at the actual framework location.
# Without this, the bundled app spews ~4 dyld errors per second under load.
if len(sys.argv) > 1 and sys.argv[1] == "py2app":
    helper = HERE / "dist" / "AUGUR.app" / "Contents" / "MacOS" / "python"
    if helper.exists():
        try:
            subprocess.run(
                [
                    "install_name_tool", "-change",
                    "@executable_path/../../../../Python3",
                    "@executable_path/../Frameworks/Python3.framework/Python3",
                    str(helper),
                ],
                check=True,
                capture_output=True,
            )
            print(f"\n✓ rewrote Python3 dylib path in {helper.relative_to(HERE)}")
        except subprocess.CalledProcessError as e:
            print(f"\n⚠ install_name_tool failed: {e.stderr.decode()}", file=sys.stderr)
