"""Authentication and project access.

Every project route depends on `project_access`, which re-checks the
`user_projects` table on every request. A user who is not a member gets a 404
(not 403), so the API does not even confirm that a project exists.

Modes (ALERTBOT_AUTH_MODE):
  microsoft  people sign in on the portal with their Microsoft account (device code + Microsoft Authenticator, no app
             registration needed); the portal then holds an HttpOnly session cookie. The default.
  dev    automated tests only. `Authorization: Bearer dev:<email>` for a seeded user.
  entra  Microsoft Entra ID access tokens issued for this API, validated against the
         tenant's signing keys (signature, audience, issuer, expiry). With
         ALERTBOT_REQUIRE_MFA (default on) the token must also prove a second factor
         (Microsoft Authenticator push / number match, passwordless phone sign-in,
         Windows Hello): its `amr` claim has to contain one of MFA_METHODS. Enforce
         the prompt itself with a Conditional Access policy; this check makes sure no
         token that skipped it is ever accepted.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import timedelta
from functools import lru_cache
from typing import Annotated

import jwt
from fastapi import Depends, Header, HTTPException, Path, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import ProjectConfig, load_projects
from ..db import Project, User, UserProject, WebSession, get_session
from ..settings import get_settings
from ..timeutil import utcnow

SessionDep = Annotated[Session, Depends(get_session)]
COOKIE = "alertbot_session"
SAFE_METHODS = ("GET", "HEAD", "OPTIONS")

# amr values Entra emits when a second factor was performed (mfa) or a phishing-resistant /
# device-bound method was used (ngcmfa = Authenticator passwordless / Windows Hello, fido, wia+mfa).
MFA_METHODS = frozenset({"mfa", "ngcmfa", "fido", "hwk"})


@lru_cache(maxsize=4)
def _jwks(tenant: str) -> jwt.PyJWKClient:
    return jwt.PyJWKClient(f"https://login.microsoftonline.com/{tenant}/discovery/v2.0/keys", cache_keys=True)


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(status.HTTP_401_UNAUTHORIZED, detail=detail, headers={"WWW-Authenticate": "Bearer"})


def _entra_claims(token: str) -> dict:
    s = get_settings()
    if not (s.entra_tenant_id and s.entra_api_audience):
        raise HTTPException(500, "Entra auth is enabled but ALERTBOT_ENTRA_TENANT_ID / _API_AUDIENCE are not set")
    try:
        key = _jwks(s.entra_tenant_id).get_signing_key_from_jwt(token).key
        return jwt.decode(
            # v2 tokens carry the API's client id as `aud`; accept the api:// form of it too.
            token, key, algorithms=["RS256"],
            audience=sorted({s.entra_api_audience, s.entra_api_audience.removeprefix("api://")}),
            issuer=f"https://login.microsoftonline.com/{s.entra_tenant_id}/v2.0",
            options={"require": ["exp", "iss", "aud", "oid"]},
        )
    except jwt.PyJWTError as exc:
        raise _unauthorized(f"invalid token: {exc}") from exc


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def link_user(session: Session, identity: dict) -> User:
    """The portal user for a Microsoft identity: the same person again (by object id), a membership pre-added by email,
    or someone new (who sees "ask for access"). The very first person to sign in becomes admin and owner of every project,
    so a fresh install never needs a hard-coded user."""
    from ..engine.cycle import ensure_project

    oid, email = identity.get("oid"), (identity.get("email") or "").lower()
    user = session.scalars(select(User).where(User.entra_oid == oid)).first() if oid else None
    if user is None and email:
        user = session.scalars(select(User).where(User.email == email)).first()
    if user is None:
        user = User(email=email or f"{oid}@unknown", name=identity.get("name") or email)
        session.add(user)
    user.entra_oid = oid or user.entra_oid
    user.name = identity.get("name") or user.name
    session.flush()
    if not session.scalars(select(User).where(User.is_admin.is_(True))).first():
        user.is_admin = True
        for cfg in load_projects().values():
            project = ensure_project(session, cfg)
            link = session.get(UserProject, (user.id, project.id))
            if link is None:
                session.add(UserProject(user_id=user.id, project_id=project.id, role="owner"))
            else:
                link.role = "owner"
    session.flush()
    return user


def create_web_session(session: Session, user: User, mfa: bool, azure_account: str | None) -> str:
    token = secrets.token_urlsafe(32)
    now = utcnow()
    session.add(WebSession(token_hash=_hash(token), user_id=user.id, created_at=now, last_seen_at=now, mfa=mfa,
                           expires_at=now + timedelta(days=get_settings().session_days), azure_account=azure_account))
    session.flush()
    return token


def web_session(session: Session, token: str | None) -> WebSession | None:
    if not token:
        return None
    return session.scalars(select(WebSession).where(WebSession.token_hash == _hash(token), WebSession.expires_at > utcnow())).first()


def end_web_session(session: Session, token: str | None) -> None:
    if token:
        session.query(WebSession).filter(WebSession.token_hash == _hash(token)).delete()


def current_user(request: Request, session: SessionDep, authorization: Annotated[str | None, Header()] = None) -> User:
    mode = get_settings().auth_mode
    if mode == "microsoft":
        ws = web_session(session, request.cookies.get(COOKIE))
        if ws is None:
            raise _unauthorized("not signed in")
        # cookies ride along on cross-site requests; actions must also carry the portal's own header
        if request.method not in SAFE_METHODS and request.headers.get("x-alertbot") != "1":
            raise HTTPException(status.HTTP_403_FORBIDDEN, "missing X-AlertBot header")
        user = session.get(User, ws.user_id)
        if user is None:
            raise _unauthorized("not signed in")
        _seen(session, user, ws)
        return user

    if not authorization or not authorization.lower().startswith("bearer "):
        raise _unauthorized("missing bearer token")
    token = authorization.split(" ", 1)[1].strip()

    if mode == "dev":
        if not token.startswith("dev:"):
            raise _unauthorized("dev mode expects 'Bearer dev:<email>'")
        user = session.scalars(select(User).where(User.email == token[4:].lower())).first()
        if user is None:
            raise _unauthorized("unknown dev user")
    else:
        claims = _entra_claims(token)
        if get_settings().require_mfa and not MFA_METHODS & set(claims.get("amr") or []):
            raise _unauthorized("mfa_required: sign in again and approve the Microsoft Authenticator prompt")
        oid = claims["oid"]
        email = (claims.get("preferred_username") or claims.get("email") or claims.get("upn") or "").lower()
        user = session.scalars(select(User).where(User.entra_oid == oid)).first()
        if user is None and email:
            # Link a pre-provisioned membership (added by email) on first sign-in.
            user = session.scalars(select(User).where(User.email == email, User.entra_oid.is_(None))).first()
        if user is None:
            user = User(email=email or f"{oid}@unknown", name=claims.get("name") or email, entra_oid=oid)
            session.add(user)
        user.entra_oid = oid
        user.name = claims.get("name") or user.name

    if user.id is None:  # first sign-in: the new person is saved now
        user.last_seen_at = utcnow()
        session.commit()
    else:
        _seen(session, user, None)
    return user


SEEN_EVERY = timedelta(minutes=2)


def _seen(session: Session, user: User, ws: WebSession | None) -> None:
    """Note when someone was last here: at most every two minutes, and committed at once. A request must never carry a
    write into its own work (an Azure, database or AI call): SQLite would make every other request wait on it."""
    now = utcnow()
    if user.last_seen_at is not None and now - user.last_seen_at < SEEN_EVERY and (ws is None or now - ws.last_seen_at < SEEN_EVERY):
        if session.dirty or session.new:
            session.commit()  # e.g. a renamed user from the token: saved now, not held
        return
    user.last_seen_at = now
    if ws is not None:
        ws.last_seen_at = now
    session.commit()


UserDep = Annotated[User, Depends(current_user)]


@dataclass
class Access:
    user: User
    project: Project
    role: str
    cfg: ProjectConfig

    @property
    def is_owner(self) -> bool:
        return self.role == "owner" or self.user.is_admin


def project_access(user: UserDep, session: SessionDep, key: Annotated[str, Path()]) -> Access:
    project = session.scalars(select(Project).where(Project.key == key, Project.active.is_(True))).first()
    cfg = load_projects().get(key)
    if project is None or cfg is None:
        raise HTTPException(404, "project not found")
    link = session.get(UserProject, (user.id, project.id))
    if link is None:
        raise HTTPException(404, "project not found")
    return Access(user=user, project=project, role=link.role, cfg=cfg)


AccessDep = Annotated[Access, Depends(project_access)]


def require_owner(access: AccessDep) -> Access:
    if not access.is_owner:
        raise HTTPException(403, "only project owners can do this")
    return access


OwnerDep = Annotated[Access, Depends(require_owner)]
