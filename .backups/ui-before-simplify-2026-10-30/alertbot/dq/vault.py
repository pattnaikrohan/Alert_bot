"""The password or private key a person enters for their database, encrypted at rest (Fernet: AES-128-CBC + HMAC).

The key comes from ALERTBOT_SECRET_KEY (a Fernet key, or kv://name for Key Vault). Without one, a key file is created
once in .data/ (git-ignored, readable by the bot's account only). Plain text never leaves this module except to open
a database connection.
"""

from __future__ import annotations

import os
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

from ..secrets import resolve_secret
from ..settings import get_settings


class VaultError(RuntimeError):
    pass


@lru_cache(maxsize=1)
def _fernet() -> Fernet:
    s = get_settings()
    key = resolve_secret(s.secret_key) if s.secret_key else None
    if not key:
        path = s.secret_key_file
        if path.exists():
            key = path.read_text(encoding="utf-8").strip()
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            key = Fernet.generate_key().decode()
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(key)
    try:
        return Fernet(key.encode() if isinstance(key, str) else key)
    except (ValueError, TypeError) as exc:
        raise VaultError("ALERTBOT_SECRET_KEY is not a valid key (use Fernet.generate_key())") from exc


def seal(plain: str) -> str:
    return _fernet().encrypt(plain.encode("utf-8")).decode("ascii") if plain else ""


def unseal(token: str) -> str:
    if not token:
        return ""
    try:
        return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise VaultError("the stored credential can't be decrypted (the secret key changed): reconnect the database") from exc
