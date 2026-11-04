"""Process-level settings, read from the environment (and `.env`).

Everything project-specific lives in `config/projects/*.yaml`; this file only
holds what the process itself needs to start.

ALERTBOT_ENV_FILE picks an extra env file layered over `.env`, so the fake-data
prototype and the live setup can sit side by side:  ALERTBOT_ENV_FILE=.env.live
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def env_files() -> tuple[str, ...]:
    """`.env`, then ALERTBOT_ENV_FILE (later files win)."""
    extra = os.environ.get("ALERTBOT_ENV_FILE", "")
    return (".env", extra) if extra and extra != ".env" else (".env",)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ALERTBOT_", env_file=env_files(), extra="ignore")

    db_url: str = "sqlite:///./.data/alertbot.db"
    config_dir: Path = Path("./config")
    timezone: str = "UTC"
    portal_url: str = "http://localhost:5173"

    # microsoft: people sign in on the portal with their Microsoft account (device code + Authenticator); no app
    #            registration needed. entra: MSAL in the browser against an app registration. dev: tests only.
    auth_mode: Literal["dev", "entra", "microsoft"] = "microsoft"
    session_days: int = Field(default=7, ge=1, le=90)
    entra_tenant_id: str = ""
    entra_api_audience: str = ""
    # Entra mode only: reject tokens whose amr claim shows no second factor (Microsoft Authenticator).
    require_mfa: bool = True

    keyvault_url: str = ""
    # Microsoft sign-in token cache (OS-encrypted) and which signed-in account the bot uses for Data Factory reads.
    msal_cache: Path = Path("./.data/msal-cache.bin")
    bot_azure_account: Path = Path("./.data/azure-account.json")

    # Where alerts go for projects that don't name their own flow (e.g. projects found automatically in Azure):
    # a secret reference such as env://PA_CSDW_URL or kv://name. Empty: such projects stay in dry-run.
    default_flow_url: str = ""
    # Encrypts the database passwords / keys people connect (Fernet key, or kv://name in production). Empty: a key file
    # is created once in .data/ (git-ignored).
    secret_key: str = ""
    secret_key_file: Path = Path("./.data/alertbot-secret.key")
    # Data-quality queries: each one is time-limited, and only this many run at once (they still cost the database).
    dq_query_timeout_s: int = Field(default=60, ge=5, le=900)
    dq_max_concurrent: int = Field(default=4, ge=1, le=32)

    # How far back each cycle looks at trigger rows and runs.
    lookback_hours: int = Field(default=48, ge=1)
    # Rows that land up to this long after a run ends still count towards it.
    load_grace_minutes: int = Field(default=10, ge=0)

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


@lru_cache
def get_settings() -> Settings:
    return Settings()
