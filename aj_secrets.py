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
            kb = f.read().strip()
        # Validate the file-loaded key the same way the env key is validated, so
        # a zero-byte/truncated/corrupted key file fails loudly HERE rather than
        # surfacing later inside _fernet()/encrypt (and being swallowed by a
        # broad except in store()).
        Fernet(kb)
        return kb
    # Generate + persist atomically with restrictive perms:
    #  * umask 077 so the file is never group/world-readable even briefly;
    #  * write the FULL key to a unique temp file (fchmod 0600, fsync) and only
    #    then publish it into place with os.link() — link is atomic AND fails if
    #    the destination already exists, so (a) any concurrent reader sees either
    #    NO file or the COMPLETE key, never a half-written (empty/truncated) one,
    #    and (b) exactly ONE process can publish a key (no two DIFFERENT keys);
    #  * the loser of the publish race re-reads the winner's key.
    import uuid as _uuid
    key = Fernet.generate_key()
    old_umask = os.umask(0o077)
    tmp = "{}.{}.{}.tmp".format(path, os.getpid(), _uuid.uuid4().hex)
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.fchmod(fd, 0o600)
            os.write(fd, key)
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            os.link(tmp, path)
        except FileExistsError:
            # Another process already published a key — adopt theirs.
            with open(path, "rb") as f:
                kb = f.read().strip()
            Fernet(kb)
            return kb
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
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
        cur = db.get_conn().execute(
            "SELECT 1 FROM settings WHERE key=?", (_PREFIX + scope,))
        try:
            return bool(cur.fetchone())
        finally:
            cur.close()
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
        # >= so a zero/negative TTL is deterministically expired (the two
        # time.time() reads can be equal under clock granularity; `>` made a
        # ttl_s=0 lease intermittently read as still-valid).
        if time.time() >= self.expires_at:
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
        cur = db.get_conn().execute(
            "SELECT value FROM settings WHERE key=?", (_PREFIX + scope,))
        try:
            row = cur.fetchone()
        finally:
            cur.close()
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


def _persist_key_file(key: bytes) -> str:
    """Atomically (re)write the master-key file with 0600 perms. Unlike the
    first-generation path in _load_master_key (os.link — must FAIL if a key
    already exists), rotation must REPLACE the existing file, so publish with
    os.replace: atomic, and any concurrent reader sees either the complete old
    key or the complete new one, never a truncated file."""
    import uuid as _uuid
    path = _key_file()
    old_umask = os.umask(0o077)
    tmp = "{}.{}.{}.tmp".format(path, os.getpid(), _uuid.uuid4().hex)
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.fchmod(fd, 0o600)
            os.write(fd, key)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        os.umask(old_umask)
    return path


def rotate_master_key(new_key: Optional[Any] = None) -> Dict[str, Any]:
    """Rotate the Fernet master key: decrypt every stored `__aj_sec_*` blob
    with the CURRENT key, re-encrypt all under the new one (generated when not
    supplied), persist the blobs + the new key. Returns counts ONLY — never key
    material or secret values (callers print the dict).

    Atomic-ish: nothing is written until EVERY blob decrypts under the current
    key (any failure aborts with the store untouched); the re-encrypted blobs
    then land in ONE transaction, and if persisting the new key subsequently
    fails the old ciphertexts are restored from memory (also one transaction).

    Key persistence follows the module's load convention (env > file): the
    `.aj_secret_key` file is always rewritten, and when AUGUR_SECRETS_KEY is
    the active source this process's env is updated too — but an EXTERNAL env
    source (launchd/shell profile) cannot be reached from here, so the result
    flags env_key_active=True and the operator must update it (else a restart
    resolves the OLD key and every rotated blob becomes undecryptable)."""
    from cryptography.fernet import Fernet
    try:
        old_f = _fernet()
    except Exception as e:
        return {"ok": False, "error": "current master key unavailable: {}".format(e)}
    # Resolve + validate the new key BEFORE touching anything.
    try:
        if new_key is None:
            new_kb = Fernet.generate_key()
        else:
            new_kb = new_key.encode() if isinstance(new_key, str) else bytes(new_key)
        new_f = Fernet(new_kb)   # malformed supplied key fails loudly here
    except Exception as e:
        return {"ok": False, "error": "invalid new key: {}".format(e)}

    # Snapshot every secret blob. ESCAPE the LIKE pattern: '_' is a LIKE
    # wildcard, so a bare '__aj_sec_%' could also match unrelated keys.
    try:
        cur = db.get_conn().execute(
            "SELECT key, value FROM settings WHERE key LIKE '@_@_aj@_sec@_%' ESCAPE '@'")
        try:
            rows = [(r["key"], r["value"]) for r in cur.fetchall()]
        finally:
            cur.close()
    except Exception as e:
        return {"ok": False, "error": "could not read secret store: {}".format(e)}

    # Phase 1 — decrypt ALL under the current key; any failure aborts with the
    # store untouched (a blob the old key can't open would be silently
    # destroyed by re-encryption, so rotation must not proceed past it).
    old_blobs: Dict[str, str] = {}
    new_blobs: Dict[str, str] = {}
    for key, val in rows:
        scope = key[len(_PREFIX):]
        try:
            plain = old_f.decrypt(base64.b64decode(val))
        except Exception as e:                    # InvalidToken, bad base64, …
            return {"ok": False, "rotated": 0,
                    "error": "decrypt failed for scope '{}' — rotation aborted, "
                             "nothing changed ({})".format(scope, type(e).__name__)}
        old_blobs[key] = val
        new_blobs[key] = base64.b64encode(new_f.encrypt(plain)).decode("ascii")
        del plain

    def _write_blobs(blobs: Dict[str, str]) -> None:
        with db._write_lock:
            conn = db.get_conn()
            for k, v in blobs.items():
                conn.execute(
                    "INSERT OR REPLACE INTO settings (key, value) VALUES (?,?)", (k, v))
            conn.commit()
        try:
            db._invalidate_settings_cache()
        except Exception:
            pass

    # Phase 2 — publish the re-encrypted blobs (one transaction), then the key.
    env_active = bool(os.environ.get("AUGUR_SECRETS_KEY"))
    try:
        _write_blobs(new_blobs)
        key_file = _persist_key_file(new_kb)
        if env_active:
            # env beats file at load time — keep THIS process consistent; the
            # external source is the operator's to update (flagged in result).
            os.environ["AUGUR_SECRETS_KEY"] = new_kb.decode("ascii")
    except Exception as e:
        # Key persistence failed after the blobs landed: restore the old
        # ciphertexts so the store matches the key that IS loadable.
        try:
            _write_blobs(old_blobs)
        except Exception:
            log.critical("rotate_master_key: could not restore old blobs after "
                         "key-persist failure — secret store may be inconsistent")
        return {"ok": False, "rotated": 0,
                "error": "could not persist new key — rotation rolled back ({})".format(e)}

    _log_access("rotate_key", "*", actor="human")
    try:
        import aj_db
        aj_db.audit("secret_rotate_key", {"rotated": len(new_blobs),
                                          "env_key_active": env_active},
                    actor="human")
    except Exception:
        log.debug("rotation audit failed", exc_info=True)
    return {"ok": True, "rotated": len(new_blobs), "key_file": key_file,
            "env_key_active": env_active}


def _log_access(action: str, scope: str, caller: str = "", actor: str = "system") -> None:
    # Audit the ACCESS, never the secret value.
    try:
        import aj_db
        aj_db.audit("secret_access", {"action": action, "scope": scope,
                                      "caller": caller or "non-llm"}, actor=actor)
    except Exception:
        log.debug("secret access audit failed", exc_info=True)
