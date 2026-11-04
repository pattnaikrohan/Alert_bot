"""Microsoft sign-in without an app registration: the device-code flow on Microsoft's public developer client
(the same one the Azure CLI and azure-identity use), exactly like "Jnana Files/app.py".

    portal  ──POST /api/auth/device/start──▶  a code + microsoft.com/devicelogin
    person  ──signs in there (Microsoft Authenticator)
    portal  ──polls──▶  signed in: an HttpOnly session cookie; the person's Microsoft identity decides their projects

The tokens go into one OS-encrypted MSAL cache (DPAPI on Windows) shared by the API, the worker and the CLI. When an
admin signs in, their account also becomes the bot's identity for reading Azure Data Factory; the worker then
gets fresh tokens silently until the sign-in expires, and the next admin sign-in renews it.
"""

from __future__ import annotations

import json
import logging
import secrets
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import lru_cache
from typing import Any

from .settings import get_settings

log = logging.getLogger(__name__)

CLIENT_ID = "04b07795-8ddb-461a-bbee-02f9e1bf7b46"  # Microsoft's public developer client (Azure CLI / azure-identity)
AUTHORITY = "https://login.microsoftonline.com/organizations"
ARM_SCOPE = "https://management.azure.com/.default"
MFA_METHODS = frozenset({"mfa", "ngcmfa", "fido", "hwk", "rsa"})


class SignInRequired(RuntimeError):
    """The bot has no usable Azure sign-in."""


@lru_cache(maxsize=1)
def app() -> Any:
    import msal
    from msal_extensions import PersistedTokenCache, build_encrypted_persistence

    path = get_settings().msal_cache
    path.parent.mkdir(parents=True, exist_ok=True)
    cache = PersistedTokenCache(build_encrypted_persistence(str(path)))
    return msal.PublicClientApplication(CLIENT_ID, authority=AUTHORITY, token_cache=cache)


def _claims(token: str) -> dict[str, Any]:
    import jwt

    try:  # received directly from Microsoft over TLS; we only read it
        return jwt.decode(token, options={"verify_signature": False})
    except jwt.PyJWTError:
        return {}


# ── device-code flows started from the portal ──────────────────────────────────


@dataclass
class Flow:
    id: str
    user_code: str
    verification_uri: str
    expires_at: float
    status: str = "waiting"  # waiting | done | error | expired
    message: str = ""
    identity: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


_flows: dict[str, Flow] = {}
_lock = threading.Lock()


def start_flow() -> Flow:
    """Ask Microsoft for a code; a background thread waits for the person to finish signing in."""
    _forget_old_flows()
    raw = app().initiate_device_flow(scopes=[ARM_SCOPE])
    if "user_code" not in raw:
        raise RuntimeError(raw.get("error_description") or raw.get("error") or "Microsoft didn't issue a sign-in code")
    flow = Flow(id=secrets.token_urlsafe(24), user_code=raw["user_code"], verification_uri=raw["verification_uri"],
                expires_at=float(raw.get("expires_at") or time.time() + 900), raw=raw)
    with _lock:
        _flows[flow.id] = flow
    threading.Thread(target=_wait, args=(flow,), daemon=True, name="device-flow").start()
    return flow


def _wait(flow: Flow) -> None:
    try:
        result = app().acquire_token_by_device_flow(flow.raw)  # blocks until signed in, declined or expired
    except Exception as exc:  # noqa: BLE001 - reported to the person who is waiting
        flow.status, flow.message = "error", str(exc)
        return
    if "access_token" not in result:
        code = result.get("error", "")
        flow.status = "expired" if code in ("expired_token", "code_expired") else "error"
        flow.message = result.get("error_description", code).split("\r\n")[0]
        return
    idc = result.get("id_token_claims") or {}
    at = _claims(result["access_token"])
    amr = set(at.get("amr") or idc.get("amr") or [])
    flow.identity = {
        "oid": idc.get("oid") or at.get("oid"),
        "email": (idc.get("preferred_username") or at.get("upn") or at.get("unique_name") or "").lower(),
        "name": idc.get("name") or at.get("name") or "",
        "tenant_id": idc.get("tid") or at.get("tid"),
        "home_account_id": _home_account_id(idc.get("oid") or at.get("oid")),
        "mfa": bool(amr & MFA_METHODS),
    }
    flow.status = "done"


def _home_account_id(oid: str | None) -> str | None:
    """The cache's id for the account that just signed in (oid.tenant)."""
    return next((a["home_account_id"] for a in app().get_accounts() if oid and a.get("local_account_id") == oid), None)


def terminal_sign_in(show: Any) -> dict[str, Any]:
    """The same device-code sign-in for a terminal (`alertbot adf-login`): connects the bot to Azure."""
    flow = start_flow()
    show(flow.verification_uri, flow.user_code, datetime.fromtimestamp(flow.expires_at, UTC))
    while (f := get_flow(flow.id)) and f.status == "waiting":
        time.sleep(2)
    finish_flow(flow.id)
    if not f or f.status != "done":
        raise RuntimeError(f.message if f else "sign-in was abandoned")
    set_bot_account(f.identity, set_by=f.identity["email"])
    return f.identity


def get_flow(flow_id: str) -> Flow | None:
    with _lock:
        flow = _flows.get(flow_id)
    if flow and flow.status == "waiting" and time.time() > flow.expires_at:
        flow.status, flow.message = "expired", "The code expired before sign-in finished."
    return flow


def finish_flow(flow_id: str) -> None:
    with _lock:
        _flows.pop(flow_id, None)


def _forget_old_flows() -> None:
    cutoff = time.time() - 1800
    with _lock:
        for k in [k for k, f in _flows.items() if f.expires_at < cutoff]:
            del _flows[k]


# ── the bot's Azure identity (for Data Factory reads) ──────────────────────────


def bot_account() -> dict[str, Any] | None:
    path = get_settings().bot_azure_account
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return None


def set_bot_account(identity: dict[str, Any], set_by: str) -> None:
    path = get_settings().bot_azure_account
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({  # ids only: the tokens stay in the encrypted cache
        "home_account_id": identity["home_account_id"], "username": identity["email"],
        "tenant_id": identity.get("tenant_id"), "set_by": set_by, "set_at": datetime.now(UTC).isoformat(),
    }, indent=2), encoding="utf-8")


def clear_bot_account() -> bool:
    path = get_settings().bot_azure_account
    if path.exists():
        path.unlink()
        return True
    return False


def bot_token(scope: str = ARM_SCOPE) -> tuple[str, int]:
    """A fresh access token for the bot's account, silently from the cache (refreshing as needed)."""
    acct = bot_account()
    if not acct or not acct.get("home_account_id"):
        raise SignInRequired("No one has connected the bot to Azure yet: an admin needs to sign in to the portal.")
    a = app()
    account = next((x for x in a.get_accounts() if x.get("home_account_id") == acct["home_account_id"]), None)
    result = a.acquire_token_silent([scope], account=account) if account else None
    if not result or "access_token" not in result:
        raise SignInRequired(f"The bot's Azure sign-in ({acct.get('username')}) has expired: an admin needs to sign in to the portal again.")
    return result["access_token"], int(time.time()) + int(result.get("expires_in", 3600))


def bot_connected() -> bool:
    try:
        bot_token()
        return True
    except Exception:  # noqa: BLE001
        return False


class BotCredential:
    """azure-core style credential over the bot's cached sign-in."""

    def get_token(self, *scopes: str, **_: Any) -> Any:
        from azure.core.credentials import AccessToken

        token, expires = bot_token(scopes[0] if scopes else ARM_SCOPE)
        return AccessToken(token, expires)


# ── people's own sign-ins (resources found in Azure are read with the access of someone who can see them) ──────


def user_token(home_account_id: str, scope: str = ARM_SCOPE) -> tuple[str, int]:
    """A fresh access token for a person who signed in to the portal, silently from the cache. Read-only use."""
    a = app()
    account = next((x for x in a.get_accounts() if x.get("home_account_id") == home_account_id), None)
    result = a.acquire_token_silent([scope], account=account) if account else None
    if not result or "access_token" not in result:
        raise SignInRequired("this person's Microsoft sign-in has expired; they need to sign in to the portal again")
    return result["access_token"], int(time.time()) + int(result.get("expires_in", 3600))


class AccountsCredential:
    """azure-core style credential that tries a list of signed-in people (most recent first) until one works."""

    def __init__(self, home_account_ids: list[str], what: str = "this resource"):
        self.ids = home_account_ids
        self.what = what

    def get_token(self, *scopes: str, **_: Any) -> Any:
        from azure.core.credentials import AccessToken

        for hid in self.ids:
            try:
                token, expires = user_token(hid, scopes[0] if scopes else ARM_SCOPE)
                return AccessToken(token, expires)
            except Exception:  # noqa: BLE001 - try the next person
                continue
        raise SignInRequired(f"No one who can see {self.what} has a current sign-in: one of them needs to sign in to the portal.")
