"""
api_keys — the single place every engine reads third-party credentials from.

Each install of AUGUR brings its own keys: users paste them into the
Settings → API Keys panel (stored in the local SQLite settings table) or
export them as environment variables. Precedence is env > DB, so a
developer/CI override always wins without touching the user's saved key.

Adding a provider = one PROVIDERS entry (+ optional test function). The
Settings panel, masking, and /api/keys routes all render from this registry.

Security invariants:
  • get_api_key() is the only sanctioned reader — engines must not reach
    into os.environ / db.get_settings() for credentials directly.
  • Full key material never leaves the server: /api/settings and /api/keys
    responses carry mask() output only (•••• + last 4).
  • A masked value submitted back (the UI echoing a placeholder) is ignored
    on save, so round-tripping the Settings form can't corrupt a real key.

Python 3.9 compatible.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Dict, List, Optional

log = logging.getLogger("augur.api_keys")

_MASK_PREFIX = "••••"


def _test_openai(key: str) -> Dict[str, Any]:
    """One cheap authenticated call — models.list is free and fast."""
    try:
        from openai import OpenAI
        client = OpenAI(api_key=key, timeout=10)
        client.models.list()
        return {"ok": True, "detail": "Authenticated with OpenAI."}
    except Exception as e:
        msg = str(e)
        if "401" in msg or "invalid_api_key" in msg.lower():
            return {"ok": False, "detail": "OpenAI rejected the key (401)."}
        # Scrub anything resembling a secret (e.g. sk-... tokens) before the
        # message reaches the API response / logs.
        msg = re.sub(r"sk-[A-Za-z0-9_\-]{6,}", "sk-***", msg)
        return {"ok": False, "detail": "Could not verify: {}".format(msg[:160])}


PROVIDERS: Dict[str, Dict[str, Any]] = {
    "openai": {
        "label": "OpenAI",
        "env": "OPENAI_API_KEY",
        "setting": "openai_api_key",
        "hint": "sk-...",
        "format": r"^sk-[A-Za-z0-9_\-]{10,}$",
        "unlocks": "Jarvis voice & grounded answers, AI theses, filing "
                   "summaries, earnings briefs, insider pattern analysis.",
        "test": _test_openai,
    },
    # Future providers slot in here, e.g.:
    # "fred":    {"label": "FRED",    "env": "FRED_API_KEY",    "setting": "fred_api_key", ...},
    # "finnhub": {"label": "Finnhub", "env": "FINNHUB_API_KEY", "setting": "finnhub_api_key", ...},
}

# Setting names whose values must be masked in any API response.
SENSITIVE_SETTINGS = {p["setting"] for p in PROVIDERS.values()}


def get_api_key(provider: str) -> str:
    """Resolve a provider's key: environment first, then the DB setting."""
    spec = PROVIDERS.get(provider)
    if not spec:
        return ""
    key = (os.environ.get(spec["env"]) or "").strip()
    if key:
        return key
    try:
        import database as db
        return (db.get_settings().get(spec["setting"]) or "").strip()
    except Exception:
        return ""


def mask(key: Optional[str]) -> str:
    if not key:
        return ""
    # Only reveal a tail when the key is long enough that 4 chars are a small
    # fraction of it — never expose most of a short token.
    return _MASK_PREFIX + key[-4:] if len(key) >= 12 else _MASK_PREFIX


def is_masked(value: Any) -> bool:
    """True for values produced by mask() — used to ignore UI echoes."""
    return isinstance(value, str) and value.startswith(_MASK_PREFIX)


def mask_settings(settings: Dict[str, Any]) -> Dict[str, Any]:
    """Copy of a settings dict with all sensitive values masked."""
    out = dict(settings or {})
    for name in SENSITIVE_SETTINGS:
        if out.get(name):
            out[name] = mask(out[name])
    return out


def _key_source(provider: str) -> Optional[str]:
    spec = PROVIDERS.get(provider) or {}
    if (os.environ.get(spec.get("env", "")) or "").strip():
        return "env"
    try:
        import database as db
        if (db.get_settings().get(spec.get("setting", "")) or "").strip():
            return "db"
    except Exception:
        pass
    return None


def list_providers() -> List[Dict[str, Any]]:
    """Registry view for the Settings panel — no key material included."""
    out = []
    for pid, spec in PROVIDERS.items():
        key = get_api_key(pid)
        out.append({
            "provider": pid,
            "label": spec["label"],
            "hint": spec.get("hint", ""),
            "unlocks": spec.get("unlocks", ""),
            "configured": bool(key),
            "source": _key_source(pid),
            "masked": mask(key),
        })
    return out


def save_key(provider: str, key: str) -> Dict[str, Any]:
    spec = PROVIDERS.get(provider)
    if not spec:
        return {"error": "unknown provider"}
    key = (key or "").strip()
    if not key:
        return {"error": "empty key"}
    if is_masked(key):
        return {"error": "that's the masked placeholder, not a key"}
    fmt = spec.get("format")
    if fmt and not re.match(fmt, key):
        return {"error": "key doesn't look like a {} key ({})".format(
            spec["label"], spec.get("hint", ""))}
    import database as db
    db.set_setting(spec["setting"], key)
    return {"status": "saved", "masked": mask(key)}


def delete_key(provider: str) -> Dict[str, Any]:
    spec = PROVIDERS.get(provider)
    if not spec:
        return {"error": "unknown provider"}
    import database as db
    db.set_setting(spec["setting"], "")
    note = None
    if (os.environ.get(spec["env"]) or "").strip():
        note = "Note: {} is still set in the environment and takes precedence.".format(spec["env"])
    return {"status": "removed", "note": note}


def test_provider(provider: str) -> Dict[str, Any]:
    spec = PROVIDERS.get(provider)
    if not spec:
        return {"ok": False, "detail": "unknown provider"}
    key = get_api_key(provider)
    if not key:
        return {"ok": False, "detail": "No key configured."}
    fn = spec.get("test")
    if not fn:
        return {"ok": True, "detail": "No live test available; key is present."}
    return fn(key)
