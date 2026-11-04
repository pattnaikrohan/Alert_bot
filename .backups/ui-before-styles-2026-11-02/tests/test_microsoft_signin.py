"""Portal sign-in with a Microsoft account (device code), with Microsoft's side faked at the MSAL boundary."""

from __future__ import annotations

import time

import jwt
import pytest


class FakeMsal:
    """Stands in for msal.PublicClientApplication: each started flow signs in as the next queued identity."""

    def __init__(self):
        self.queue: list[dict] = []
        self.accounts: list[dict] = []

    def initiate_device_flow(self, scopes):  # noqa: ANN001
        return {"user_code": "ABCD1234", "verification_uri": "https://microsoft.com/devicelogin", "expires_at": time.time() + 900}

    def acquire_token_by_device_flow(self, flow):  # noqa: ANN001
        who = self.queue.pop(0)
        if who.get("decline"):
            return {"error": "authorization_declined", "error_description": "The user declined."}
        self.accounts.append({"home_account_id": f"{who['oid']}.tid", "local_account_id": who["oid"], "username": who["email"]})
        at = jwt.encode({"oid": who["oid"], "amr": who.get("amr", ["pwd", "mfa"]), "tid": "tid"}, "kkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkk", algorithm="HS256")
        return {"access_token": at, "expires_in": 3600,
                "id_token_claims": {"oid": who["oid"], "preferred_username": who["email"], "name": who["name"], "tid": "tid"}}

    def get_accounts(self):
        return self.accounts

    def acquire_token_silent(self, scopes, account):  # noqa: ANN001
        return {"access_token": "arm-token", "expires_in": 3600} if account else None


@pytest.fixture
def ms(estate, monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from alertbot import msauth, settings
    from alertbot.api.main import app

    monkeypatch.setenv("ALERTBOT_AUTH_MODE", "microsoft")
    monkeypatch.setenv("ALERTBOT_BOT_AZURE_ACCOUNT", str(tmp_path / "azure-account.json"))
    settings.get_settings.cache_clear()
    fake = FakeMsal()
    monkeypatch.setattr(msauth, "app", lambda: fake)
    msauth._flows.clear()
    from alertbot.db import User, session_scope

    with session_scope() as s:  # a fresh install: access rules exist, but nobody has signed in or is admin yet
        for u in s.query(User):
            u.is_admin = False
    return fake, TestClient(app)


def sign_in(client, fake, who) -> dict:  # noqa: ANN001
    fake.queue.append(who)
    flow = client.post("/api/auth/device/start").json()
    assert flow["user_code"] == "ABCD1234" and flow["verification_uri"].startswith("https://")
    for _ in range(100):
        r = client.get(f"/api/auth/device/{flow['flow_id']}").json()
        if r["status"] != "waiting":
            return r
        time.sleep(0.02)
    raise AssertionError("sign-in never finished")


ROHAN = {"oid": "oid-rohan", "email": "rohan.pattnaik@example.com", "name": "Rohan Pattnaik"}


def test_config_has_no_static_users(ms):
    _, c = ms
    cfg = c.get("/api/auth/config").json()
    assert cfg == {"mode": "microsoft", "mfa": True}
    assert c.get("/api/me").status_code == 401


def test_first_person_becomes_admin_and_connects_the_bot(ms):
    from alertbot import msauth

    fake, c = ms
    r = sign_in(c, fake, ROHAN)
    assert r["status"] == "done" and r["user"]["admin"] is True and r["bot_connected_to_azure"] is True
    assert "alertbot_session" in c.cookies
    me = c.get("/api/me").json()
    assert {p["key"]: p["role"] for p in me["projects"]} == {"slipstream": "owner"}
    assert msauth.bot_account()["username"] == "rohan.pattnaik@example.com"
    assert msauth.bot_token()[0] == "arm-token"  # the worker can now read Azure silently
    assert c.get("/api/azure").json()["connected"] is True


def test_actions_need_the_portal_header(ms):
    fake, c = ms
    sign_in(c, fake, ROHAN)
    body = {"hours": 1, "reason": "x"}
    assert c.post("/api/projects/slipstream/mutes", json=body).status_code == 403  # a cross-site form can't add the header
    assert c.post("/api/projects/slipstream/mutes", json=body, headers={"X-AlertBot": "1"}).status_code == 201


def test_pre_added_people_get_their_projects_and_others_get_none(ms):
    from fastapi.testclient import TestClient

    from alertbot.api.main import app

    fake, c = ms
    sign_in(c, fake, ROHAN)
    priya = TestClient(app)  # a different browser
    r = sign_in(priya, fake, {"oid": "oid-priya", "email": "priya.sharma@contoso.com", "name": "Priya Sharma"})
    assert r["user"]["admin"] is False and r["bot_connected_to_azure"] is False
    assert [p["key"] for p in priya.get("/api/me").json()["projects"]] == ["slipstream"]
    stranger = TestClient(app)
    sign_in(stranger, fake, {"oid": "oid-x", "email": "someone@else.com", "name": "Some One"})
    assert stranger.get("/api/me").json()["projects"] == []
    assert stranger.get("/api/projects/slipstream/overview").status_code == 404


def test_sign_in_without_mfa_is_refused_and_declines_are_reported(ms):
    fake, c = ms
    r = sign_in(c, fake, {**ROHAN, "amr": ["pwd"]})
    assert r["status"] == "error" and r["message"].startswith("mfa_required")
    assert c.get("/api/me").status_code == 401
    r = sign_in(c, fake, {**ROHAN, "decline": True})
    assert r["status"] == "error" and "declined" in r["message"]


def test_sign_out_ends_the_session(ms):
    fake, c = ms
    sign_in(c, fake, ROHAN)
    assert c.get("/api/me").status_code == 200
    c.post("/api/auth/logout", headers={"X-AlertBot": "1"})
    assert c.get("/api/me").status_code == 401
