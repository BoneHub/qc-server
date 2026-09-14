"""Server private key, API key generation and hashing.

The server owns a single private key. It is used to derive reviewer key digests and to
compare the admin key, so that a leaked ``users.json`` does not hand out access to the
dataset: only ``HMAC-SHA256(private_key, api_key)`` is ever written to disk.
"""

from __future__ import annotations

import hmac
import os
import secrets
from hashlib import sha256
from pathlib import Path

API_KEY_PREFIX = "bhqc_"
API_KEY_BYTES = 24
KEY_PREFIX_DISPLAY_LEN = len(API_KEY_PREFIX) + 6

PRIVATE_KEY_FILE_NAME = "server_private_key"
ADMIN_KEY_FILE_NAME = "admin_key"

ENV_PRIVATE_KEY = "BONEHUB_QC_PRIVATE_KEY"
ENV_ADMIN_KEY = "BONEHUB_QC_ADMIN_KEY"


def load_or_create_private_key(state_dir: Path) -> str:
    """Return the server private key, generating and storing one on first start.

    ``BONEHUB_QC_PRIVATE_KEY`` wins when set, which is how a container keeps a stable key
    across recreated state folders. Changing the private key invalidates every issued
    reviewer API key, because the stored digests no longer match.
    """
    from_env = os.environ.get(ENV_PRIVATE_KEY)
    if from_env:
        return from_env

    key_path = state_dir / PRIVATE_KEY_FILE_NAME
    if key_path.exists():
        key = key_path.read_text(encoding="utf-8").strip()
        if key:
            return key

    key = secrets.token_urlsafe(48)
    state_dir.mkdir(parents=True, exist_ok=True)
    key_path.write_text(key, encoding="utf-8")
    _restrict_permissions(key_path)
    return key


def load_or_create_admin_key(state_dir: Path) -> tuple[str, bool]:
    """Return ``(admin_key, was_generated)`` for the admin panel."""
    from_env = os.environ.get(ENV_ADMIN_KEY)
    if from_env:
        return from_env, False

    key_path = state_dir / ADMIN_KEY_FILE_NAME
    if key_path.exists():
        key = key_path.read_text(encoding="utf-8").strip()
        if key:
            return key, False

    key = generate_api_key()
    state_dir.mkdir(parents=True, exist_ok=True)
    key_path.write_text(key, encoding="utf-8")
    _restrict_permissions(key_path)
    return key, True


def generate_api_key() -> str:
    """A fresh reviewer API key. Shown once, then only its digest is kept."""
    return API_KEY_PREFIX + secrets.token_urlsafe(API_KEY_BYTES)


def hash_api_key(api_key: str, private_key: str) -> str:
    """HMAC digest of an API key under the server private key."""
    return hmac.new(private_key.encode("utf-8"), api_key.encode("utf-8"), sha256).hexdigest()


def key_prefix(api_key: str) -> str:
    """Short, non-secret fragment of a key so administrators can tell keys apart."""
    return api_key[:KEY_PREFIX_DISPLAY_LEN]


def keys_match(candidate_hash: str, stored_hash: str) -> bool:
    return hmac.compare_digest(candidate_hash, stored_hash)


def _restrict_permissions(path: Path) -> None:
    """Best-effort 0600. Silently ignored on filesystems that do not support it."""
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
