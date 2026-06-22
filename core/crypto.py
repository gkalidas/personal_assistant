"""
Application-layer field encryption for sensitive SQLite columns.

Key is auto-generated on first run and stored at ~/.config/gk/master.key
(outside the git repo — never committed).

Only TEXT fields (description, notes) are encrypted.  Numeric fields
(amount, value1, value2) stay plaintext so SQL aggregates (SUM/AVG) work.
Schema is NOT changed — encrypted values are stored with an 'enc:' prefix
so the code can detect and decrypt them transparently.
"""

import logging
from pathlib import Path

log = logging.getLogger(__name__)

_KEY_PATH = Path.home() / ".config" / "gk" / "master.key"
_PREFIX   = "enc:"

_fernet = None


def _generate_key():
    """Create a new Fernet key at _KEY_PATH (mode 0600) and return it."""
    from cryptography.fernet import Fernet
    key = Fernet.generate_key()
    _KEY_PATH.write_bytes(key)
    _KEY_PATH.chmod(0o600)
    log.info("generated new encryption key at %s", _KEY_PATH)
    return key


def _load_or_create_key():
    """Return the cached Fernet, loading the on-disk key or creating one (0600) if missing.

    If an existing key file is structurally invalid (e.g. raw bytes instead of a
    base64-encoded Fernet key), it is regenerated. This is safe: an unusable key
    can never have encrypted any data, so nothing is lost — but it does mean a
    bad key silently disabling encryption is now self-healing instead of
    permanent.
    """
    global _fernet
    if _fernet is not None:
        return _fernet

    from cryptography.fernet import Fernet

    _KEY_PATH.parent.mkdir(parents=True, exist_ok=True)

    if _KEY_PATH.exists():
        key = _KEY_PATH.read_bytes().strip()
        try:
            _fernet = Fernet(key)
            log.debug("loaded encryption key from %s", _KEY_PATH)
            return _fernet
        except Exception as e:
            log.error("encryption key at %s is invalid (%s) — regenerating. "
                      "Existing data is unaffected (an invalid key never encrypted anything).",
                      _KEY_PATH, e)
            key = _generate_key()
    else:
        key = _generate_key()

    _fernet = Fernet(key)
    return _fernet


def encrypt(value: str | None) -> str | None:
    """Encrypt a string field. Returns None unchanged."""
    if value is None:
        return None
    try:
        f = _load_or_create_key()
        token = f.encrypt(value.encode()).decode()
        return _PREFIX + token
    except Exception as e:
        log.error("encrypt failed: %s", e)
        return value  # fall back to plaintext rather than lose data


def decrypt(value: str | None) -> str | None:
    """Decrypt a string field. Plaintext values (no prefix) pass through."""
    if value is None:
        return None
    if not isinstance(value, str) or not value.startswith(_PREFIX):
        return value  # already plaintext
    try:
        f = _load_or_create_key()
        token = value[len(_PREFIX):].encode()
        return f.decrypt(token).decode()
    except Exception as e:
        log.error("decrypt failed: %s", e)
        return value  # return raw value rather than crash


def is_encrypted(value: str | None) -> bool:
    """True if the value carries the 'enc:' prefix (i.e. is an encrypted token)."""
    return isinstance(value, str) and value.startswith(_PREFIX)
