"""Credentials: the server's identity, its private key, the admin key and API key hashing.

Credentials never go on the dataset share. They live in the credentials folder inside the
container (``BONEHUB_QC_CREDENTIALS_DIR``, a Docker volume on the Docker host), together
with the user accounts. The server's private key derives the digests of users' API keys and
compares the admin key, so only ``HMAC-SHA256(private_key, api_key)`` is ever written down.

The credentials folder also holds the server's id. It names the folder on the share where
this server keeps everything that is not a credential, so several servers -- each with its
own admin -- can work on one dataset without overwriting each other's state. A new
credentials folder is a new server.
"""

from __future__ import annotations

import hmac
import os
import re
import secrets
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path

API_KEY_PREFIX = "bhqc_"
API_KEY_BYTES = 24
KEY_PREFIX_DISPLAY_LEN = len(API_KEY_PREFIX) + 6

SERVER_ID_FILE_NAME = "server_id"
PRIVATE_KEY_FILE_NAME = "server_private_key"
ADMIN_KEY_FILE_NAME = "admin_key"

ENV_PRIVATE_KEY = "BONEHUB_QC_PRIVATE_KEY"
ENV_ADMIN_KEY = "BONEHUB_QC_ADMIN_KEY"

#: A server id becomes a folder name on the share, so it is kept to safe characters.
_SERVER_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


def load_or_create_server_id(credentials_dir: Path) -> tuple[str, bool]:
    """Return ``(server_id, was_generated)``: this server's name for its folder on the share."""
    id_path = credentials_dir / SERVER_ID_FILE_NAME
    if id_path.exists():
        server_id = id_path.read_text(encoding="utf-8").strip()
        if server_id:
            if not _SERVER_ID_PATTERN.match(server_id):
                raise RuntimeError(f"'{id_path}' holds '{server_id}', which is not a valid server id.")
            return server_id, False

    server_id = f"qc_{datetime.now(timezone.utc):%Y%m%d}_{secrets.token_hex(3)}"
    _write_secret(id_path, server_id)
    return server_id, True


def load_or_create_private_key(credentials_dir: Path) -> str:
    """Return the server private key, generating and storing one on first start.

    ``BONEHUB_QC_PRIVATE_KEY`` wins when set. Changing the private key invalidates every
    issued API key, because the stored digests no longer match.
    """
    from_env = os.environ.get(ENV_PRIVATE_KEY)
    if from_env:
        return from_env

    key_path = credentials_dir / PRIVATE_KEY_FILE_NAME
    if key_path.exists():
        key = key_path.read_text(encoding="utf-8").strip()
        if key:
            return key

    key = secrets.token_urlsafe(48)
    _write_secret(key_path, key)
    return key


def load_or_create_admin_key(credentials_dir: Path) -> tuple[str, bool]:
    """Return ``(admin_key, was_generated)`` for the admin panel.

    ``BONEHUB_QC_ADMIN_KEY`` wins when set; otherwise the key is generated on first start,
    printed once, and kept in the credentials folder.
    """
    from_env = os.environ.get(ENV_ADMIN_KEY)
    if from_env:
        return from_env, False

    key_path = credentials_dir / ADMIN_KEY_FILE_NAME
    if key_path.exists():
        key = key_path.read_text(encoding="utf-8").strip()
        if key:
            return key, False

    key = generate_api_key()
    _write_secret(key_path, key)
    return key, True


def generate_api_key() -> str:
    """A fresh API key for a user. Shown once, then only its digest is kept."""
    return API_KEY_PREFIX + secrets.token_urlsafe(API_KEY_BYTES)


def hash_api_key(api_key: str, private_key: str) -> str:
    """HMAC digest of an API key under the server private key."""
    return hmac.new(private_key.encode("utf-8"), api_key.encode("utf-8"), sha256).hexdigest()


def key_prefix(api_key: str) -> str:
    """Short, non-secret fragment of a key so administrators can tell keys apart."""
    return api_key[:KEY_PREFIX_DISPLAY_LEN]


def keys_match(candidate_hash: str, stored_hash: str) -> bool:
    return hmac.compare_digest(candidate_hash, stored_hash)


def ensure_credentials_dir(credentials_dir: Path) -> None:
    """Create the credentials folder, readable by the server alone where the OS allows."""
    credentials_dir.mkdir(parents=True, exist_ok=True)
    _restrict_permissions(credentials_dir, 0o700)


def _write_secret(path: Path, text: str) -> None:
    ensure_credentials_dir(path.parent)
    path.write_text(text, encoding="utf-8")
    _restrict_permissions(path, 0o600)


def _restrict_permissions(path: Path, mode: int) -> None:
    """Best effort. Silently ignored on filesystems that do not support it."""
    try:
        os.chmod(path, mode)
    except OSError:
        pass
