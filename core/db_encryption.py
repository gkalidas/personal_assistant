"""
Encrypted backup for sensitive SQLite databases.

Strategy: keep the live DB as plain SQLite (for fast reads/writes during session),
but write encrypted snapshots on demand or on shutdown.

Encryption: AES-256-GCM via the 'cryptography' package.
Key: 32-byte key stored at ~/.config/gk/master.key (600 perms, never committed).
     Auto-generated on first use.

Usage:
  encrypt_db("personal_assistant.db")   → writes personal_assistant.db.enc
  decrypt_db("personal_assistant.db.enc", "personal_assistant.db")
  rotate_key()                           → re-encrypts all .enc files with new key
"""

import logging
import os
import sqlite3
from pathlib import Path

log = logging.getLogger(__name__)

_KEY_DIR  = Path.home() / ".config" / "gk"
_KEY_FILE = _KEY_DIR / "master.key"
_KEY_SIZE = 32  # AES-256

# DBs considered sensitive — encrypt these
SENSITIVE_DBS = [
    "personal_assistant.db",   # diary drafts, events, health data
]


def _ensure_key() -> bytes:
    """Return the 32-byte master key, generating it if it doesn't exist."""
    _KEY_DIR.mkdir(parents=True, exist_ok=True)
    if _KEY_FILE.exists():
        key = _KEY_FILE.read_bytes()
        if len(key) == _KEY_SIZE:
            return key
    # Generate a new key
    key = os.urandom(_KEY_SIZE)
    _KEY_FILE.write_bytes(key)
    _KEY_FILE.chmod(0o600)
    log.info("Generated new master key at %s", _KEY_FILE)
    return key


def _aes_encrypt(data: bytes, key: bytes) -> bytes:
    """AES-256-GCM encrypt. Returns nonce(12) + tag(16) + ciphertext."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    nonce = os.urandom(12)
    aesgcm = AESGCM(key)
    ct = aesgcm.encrypt(nonce, data, None)  # ct includes tag appended
    return nonce + ct


def _aes_decrypt(data: bytes, key: bytes) -> bytes:
    """AES-256-GCM decrypt. Input: nonce(12) + tag+ciphertext."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    nonce = data[:12]
    ct    = data[12:]
    aesgcm = AESGCM(key)
    return aesgcm.decrypt(nonce, ct, None)


def encrypt_db(db_path: str | Path, out_path: str | Path | None = None) -> Path:
    """
    Encrypt a SQLite DB file with AES-256-GCM.
    Writes to <db_path>.enc by default.
    Returns the encrypted file path.
    """
    db_path = Path(db_path)
    if not db_path.exists():
        raise FileNotFoundError(f"DB not found: {db_path}")
    out = Path(out_path) if out_path else db_path.with_suffix(db_path.suffix + ".enc")

    key  = _ensure_key()
    data = db_path.read_bytes()
    enc  = _aes_encrypt(data, key)
    out.write_bytes(enc)
    out.chmod(0o600)
    log.info("Encrypted %s → %s (%d bytes)", db_path.name, out.name, len(enc))
    return out


def decrypt_db(enc_path: str | Path, out_path: str | Path | None = None) -> Path:
    """
    Decrypt an encrypted DB file.
    Writes to <enc_path> without the .enc suffix by default.
    Returns the decrypted file path.
    """
    enc_path = Path(enc_path)
    if not enc_path.exists():
        raise FileNotFoundError(f"Encrypted DB not found: {enc_path}")
    # Remove last suffix (.enc) for output
    stem = enc_path.stem  # e.g. personal_assistant.db
    out  = Path(out_path) if out_path else enc_path.parent / stem

    key  = _ensure_key()
    enc  = enc_path.read_bytes()
    data = _aes_decrypt(enc, key)
    out.write_bytes(data)
    log.info("Decrypted %s → %s", enc_path.name, out.name)
    return out


def backup_sensitive_dbs(project_root: str | Path | None = None) -> list[str]:
    """
    Encrypt all sensitive DBs in the project root.
    Returns list of created .enc files.
    """
    root   = Path(project_root) if project_root else Path(__file__).parent.parent
    backed = []
    for db_name in SENSITIVE_DBS:
        db_path = root / db_name
        if db_path.exists():
            try:
                enc = encrypt_db(db_path)
                backed.append(str(enc))
            except Exception as e:
                log.error("Failed to encrypt %s: %s", db_name, e)
    return backed


def verify_key_exists() -> bool:
    """Return True if the master key exists and is the right size."""
    return _KEY_FILE.exists() and len(_KEY_FILE.read_bytes()) == _KEY_SIZE


def key_info() -> dict:
    """Return key metadata (never the key itself)."""
    if not _KEY_FILE.exists():
        return {"exists": False, "path": str(_KEY_FILE)}
    stat = _KEY_FILE.stat()
    perm = oct(stat.st_mode)[-3:]
    return {
        "exists": True,
        "path":   str(_KEY_FILE),
        "perms":  perm,
        "safe":   perm == "600",
        "size_bytes": stat.st_size,
    }


def rotate_key() -> dict:
    """
    Generate a new master key and re-encrypt all .enc files in the project.
    Returns summary of files re-encrypted.
    """
    root = Path(__file__).parent.parent
    old_key = _ensure_key()

    # Collect existing .enc files
    enc_files = list(root.glob("*.enc"))
    if not enc_files:
        return {"rotated": 0, "message": "No .enc files found — nothing to rotate"}

    # Decrypt with old key, generate new key, re-encrypt
    decrypted = {}
    for f in enc_files:
        try:
            decrypted[f] = _aes_decrypt(f.read_bytes(), old_key)
        except Exception as e:
            log.error("Could not decrypt %s during rotation: %s", f, e)

    # Write new key
    new_key = os.urandom(_KEY_SIZE)
    _KEY_FILE.write_bytes(new_key)
    _KEY_FILE.chmod(0o600)

    rotated = []
    for f, data in decrypted.items():
        try:
            f.write_bytes(_aes_encrypt(data, new_key))
            rotated.append(str(f))
        except Exception as e:
            log.error("Could not re-encrypt %s: %s", f, e)

    log.info("Key rotated. Re-encrypted %d file(s).", len(rotated))
    return {"rotated": len(rotated), "files": rotated}
