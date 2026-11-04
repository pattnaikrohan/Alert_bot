"""How the bot signs in to Azure for pipeline reads (`pipeline.auth`):

  default_credential  production: managed identity, a service principal (AZURE_CLIENT_ID/SECRET/TENANT_ID) or `az login`.
  device_code         pilot: a person's own Microsoft sign-in. An admin signing in to the portal connects the bot (or
                      `alertbot adf-login` in a terminal). Tokens stay in the OS-encrypted cache; see `alertbot.msauth`.
  signed_in_users     projects found in Azure: the sign-in of any portal user who can see the resource (most recent
                      first), so the bot reads exactly what those people are allowed to read.
"""

from __future__ import annotations

from typing import Any

from ..config import PipelineConfig
from ..msauth import (
    ARM_SCOPE,
    AccountsCredential,
    BotCredential,
    SignInRequired,
    bot_account,
    clear_bot_account,
)

__all__ = ["ARM_SCOPE", "SignInRequired", "credential_for", "sign_out", "signed_in_as"]


def credential_for(cfg: PipelineConfig) -> Any:
    if cfg.auth == "default_credential":
        from azure.identity import DefaultAzureCredential

        return DefaultAzureCredential(exclude_interactive_browser_credential=True)
    if cfg.auth == "signed_in_users":
        return AccountsCredential(_accounts_for(cfg.resource_id), what=cfg.resource_id.rsplit("/", 1)[-1] or "this resource")
    if bot_account() is None:
        raise SignInRequired("No one has connected the bot to Azure yet: an admin needs to sign in to the portal.")
    return BotCredential()


def _accounts_for(resource_id: str) -> list[str]:
    """Sign-ins of the people Azure showed this resource to, most recently confirmed first; then the bot's own."""
    from sqlalchemy import select

    from ..db import AzureAccess, session_scope

    with session_scope() as s:
        ids = list(dict.fromkeys(s.scalars(select(AzureAccess.home_account_id).where(
            AzureAccess.resource_id == resource_id.lower()).order_by(AzureAccess.seen_at.desc()))))
    bot = (bot_account() or {}).get("home_account_id")
    return ids + ([bot] if bot and bot not in ids else [])


def signed_in_as() -> str | None:
    acct = bot_account()
    return acct.get("username") if acct else None


def sign_out() -> bool:
    return clear_bot_account()
