"""AJTA — secrets broker (AJTA-SPEC-1.0 §22.3).

A minimal privileged local service for broker/API credential custody:
  * stores tokens ENCRYPTED at rest (Fernet) — never in plaintext settings;
  * exposes `lease(scope)` returning short-lived, scoped material to NON-LLM
    components only (aj_execution / aj_broker);
  * NEVER returns secrets to the model context or the read MCP path — this
    module is deliberately NOT registered in aj_mcp_read or jarvis_tools, and
    every lease is logged (scope only, never the value);
  * MUST NOT store secrets in plaintext `settings` (§22.3).

Master key precedence (env > file): `AUGUR_SECRETS_KEY` (a urlsafe-base64 Fernet
key) beats an auto-generated key file (`.aj_secret_key`, mode 0600) beside the
DB. The encrypted secret blobs live in settings under `__aj_sec_<scope>` (the
`__` prefix hides them from get_settings()/the UI).
"""
from __future__ import annotations

import base64
import logging
import os
import time
from typing import Any, Dict, Optional

import database as db

log = logging.getLogger("augur.aj_secrets")

_PREFIX = "__aj_sec_"


def _key_file() -> str:
    # Follow the RESOLVED db path (realpath) so the desktop app and the browser
    # version — which reach the same wealth.db through a symlink at different
    # paths — share ONE key file and can decrypt each other's stored secrets.
    try:
        resolved = os.path.realpath(db.DB_PATH)
    except Exception:
        resolved = db.DB_PATH
    base = os.path.dirname(resolved) or "."
    return os.path.join(base, ".aj_secret_key")


def _load_master_key() -> bytes:
    """Resolve the Fernet master key: env first, then a 0600 key file
    (auto-generated on first use). Raises if cryptography is unavailable —
    a secrets failure must fail closed (§20.8), never silently downgrade."""
    from cryptography.fernet import Fernet
    env = os.environ.get("AUGUR_SECRETS_KEY")
    if env:
        kb = env.encode() if isinstance(env, str) else env
        Fernet(kb)   # validate now — a malformed env key fails loudly, not silently
        return kb
    path = _key_file()
    if os.path.exists(path):
        with open(path, "rb") as f:
            return f.read().strip()
    # Generate + persist atomically with restrictive perms:
    #  * umask 077 so the file is never group/world-readable even briefly;
    #  * O_EXCL so a concurrent first-use can't have two processes write
    #    DIFFERENT keys (which would make one's secrets undecryptable) — the
    #    loser re-reads the winner's key;
    #  * fchmod 0600 on the fd BEFORE writing the secret bytes.
    key = Fernet.generate_key()
    old_umask = os.umask(0o077)
    try:
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            with open(path, "rb") as f:
                return f.read().strip()
        try:
            os.fchmod(fd, 0o600)
            os.write(fd, key)
        finally:
            os.close(fd)
    finally:
        os.umask(old_umask)
    log.info("aj_secrets: generated master key at %s (0600)", path)
    return key


def _fernet():
    from cryptography.fernet import Fernet
    return Fernet(_load_master_key())


def available() -> bool:
    try:
        _fernet()
        return True
    except Exception:
        log.warning("aj_secrets unavailable (no cryptography / key)")
        return False


def store(scope: str, value: str) -> bool:
    """Encrypt + persist a secret under `scope`. The ciphertext (never the
    plaintext) is what lands in settings."""
    if not scope or value is None:
        return False
    try:
        token = _fernet().encrypt(str(value).encode("utf-8"))
        db.set_setting(_PREFIX + scope, base64.b64encode(token).decode("ascii"))
        _log_access("store", scope, actor="human")
        return True
    except Exception:
        log.exception("aj_secrets.store failed for scope=%s", scope)
        return False


def has(scope: str) -> bool:
    try:
        return bool(db.get_conn().execute(
            "SELECT 1 FROM settings WHERE key=?", (_PREFIX + scope,)).fetchone())
    except Exception:
        return False


class Lease(object):
    """Short-lived holder for leased secret material. The value is available
    only until `expires_at`; callers MUST use it immediately and not persist or
    log it. `__str__`/`repr` are redacted so it can't leak into logs/prompts."""

    __slots__ = ("scope", "_value", "expires_at")

    def __init__(self, scope: str, value: str, ttl_s: int):
        self.scope = scope
        self._value = value
        self.expires_at = time.time() + ttl_s

    @property
    def value(self) -> Optional[str]:
        if time.time() > self.expires_at:
            return None
        return self._value

    def __str__(self):
        return "<Lease scope={} REDACTED>".format(self.scope)

    __repr__ = __str__


def lease(scope: str, ttl_s: int = 300, caller: str = "") -> Optional[Lease]:
    """Return short-lived scoped material for a NON-LLM component. Logs the
    access (scope + caller, NEVER the value). Returns None when absent or on
    error (callers fail closed — no credentialed action proceeds, §20.8)."""
    row = None
    try:
        row = db.get_conn().execute(
            "SELECT value FROM settings WHERE key=?", (_PREFIX + scope,)).fetchone()
    except Exception:
        log.exception("aj_secrets.lease read failed for %s", scope)
    if not row or not row["value"]:
        return None
    try:
        token = base64.b64decode(row["value"])
        value = _fernet().decrypt(token).decode("utf-8")
    except Exception:
        log.exception("aj_secrets.lease decrypt failed for %s", scope)
        return None
    _log_access("lease", scope, caller=caller)
    return Lease(scope, value, ttl_s)


def delete(scope: str) -> None:
    try:
        with db._write_lock:
            db.get_conn().execute("DELETE FROM settings WHERE key=?", (_PREFIX + scope,))
            db.get_conn().commit()
        try:
            db._invalidate_settings_cache()
        except Exception:
            pass
        _log_access("delete", scope, actor="human")
    except Exception:
        log.exception("aj_secrets.delete failed for %s", scope)


def _log_access(action: str, scope: str, caller: str = "", actor: str = "system") -> None:
    # Audit the ACCESS, never the secret value.
    try:
        import aj_db
        aj_db.audit("secret_access", {"action": action, "scope": scope,
                                      "caller": caller or "non-llm"}, actor=actor)
    except Exception:
        log.debug("secret access audit failed", exc_info=True)
