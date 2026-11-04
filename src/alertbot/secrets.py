"""Secret references.

Config never holds secrets, only references to them:

    kv://<name>    Azure Key Vault secret (production; needs ALERTBOT_KEYVAULT_URL)
    env://<VAR>    environment variable / .env entry (local development)
    <anything>     used literally
    ""             not set → the caller decides (e.g. notifier falls back to dry-run)
"""

from __future__ import annotations

import logging
import os
from functools import lru_cache

from dotenv import load_dotenv  # ships with pydantic-settings

from .settings import env_files, get_settings

log = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def load_dotenv_once() -> None:
    for path in reversed(env_files()):  # the layered file first, so its values win
        load_dotenv(path, override=False)


class SecretError(RuntimeError):
    pass


def resolve_secret(ref: str | None) -> str | None:
    if not ref:
        return None
    if ref.startswith("env://"):
        load_dotenv_once()
        return os.environ.get(ref.removeprefix("env://")) or None
    if ref.startswith("kv://"):
        return _from_key_vault(ref.removeprefix("kv://"))
    return ref


def describe_secret(ref: str | None) -> str:
    """Human-readable status of a reference that never reveals the value."""
    if not ref:
        return "not configured"
    try:
        value = resolve_secret(ref)
    except SecretError as exc:
        return f"{ref} (unavailable: {exc})"
    return f"{ref} ({'set' if value else 'empty'})"


@lru_cache(maxsize=64)
def _from_key_vault(name: str) -> str | None:
    vault_url = get_settings().keyvault_url
    if not vault_url:
        log.debug("kv://%s requested but ALERTBOT_KEYVAULT_URL is not set; treating as empty", name)
        return None
    try:
        from azure.identity import DefaultAzureCredential
        from azure.keyvault.secrets import SecretClient
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise SecretError("install the 'azure' extra to read Key Vault secrets") from exc
    try:
        client = SecretClient(vault_url=vault_url, credential=DefaultAzureCredential())
        return client.get_secret(name).value or None
    except Exception as exc:  # pragma: no cover - network
        raise SecretError(str(exc)) from exc
