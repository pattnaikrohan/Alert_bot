"""The assistant drives the app in plain English: it proposes, the person confirms, the API re-checks and does it with
that person's own rights. No AI here (none configured): plain commands are understood by the rules. And the chart
assistant only makes charts."""

from __future__ import annotations

from datetime import timedelta

from fastapi.testclient import TestClient
from test_adf import NOW, adf_ws  # noqa: F401 - adf_ws is a fixture

from alertbot.db import Incident, IncidentEvent, Mute, Project, User, UserProject, session_scope


def _api(who="chinmoy@contoso.com"):
    from alertbot.api.main import app

    return TestClient(app, headers={"Authorization": f"Bearer dev:{who}"})


def _seed():
    from alertbot.db import BotState

    with session_scope() as s:
        p = s.query(Project).filter_by(key="adfp").one()
        s.add(BotState(key="inventory:adfp", value={"items": [{"name": n, "type": "pipeline", "lineage": []} for n in ("pl_sql", "pl_uat")]}))
        ids = []
        for scope, sev, title in (("site:pl_sql", "critical", "pl_sql pipeline failed"), ("site:pl_uat", "critical", "pl_uat missed its schedule"),
                                  ("table:DB.S.ORDERS", "warning", "ORDERS loaded unusually many rows")):
            inc = Incident(project_id=p.id, fingerprint=f"adfp:{scope}", scope=scope, title=title, severity=sev, status="open",
                           checks=["row_count"] if "ORDERS" in title else ["run_failed"], opened_at=NOW - timedelta(hours=2))
            s.add(inc)
            s.flush()
            ids.append(inc.id)
    return ids


def chat(api, message, **kw):
    r = api.post("/api/bot/chat", json={"message": message, **kw})
    assert r.status_code == 200, r.text
    return r.json()


def test_plain_commands_become_proposals_and_confirming_does_them(adf_ws):  # noqa: F811
    sql, uat, orders = _seed()
    api = _api()
    r = chat(api, "ack the pl_sql one")
    assert [a["tool"] for a in r["actions"]] == ["acknowledge"] and r["actions"][0]["args"] == {"incidents": [sql]}
    assert r["focus"] == sql and r["actions"][0]["allowed"]
    with session_scope() as s:
        assert s.get(Incident, sql).status == "open"  # nothing happens until it's confirmed
    done = api.post("/api/bot/act", json={"tool": "acknowledge", "args": r["actions"][0]["args"]}).json()
    assert done["ok"] and f"#{sql}" in done["message"]
    with session_scope() as s:
        assert s.get(Incident, sql).status == "acknowledged" and s.get(Incident, sql).acknowledged_by == "Chinmoy"

    r = chat(api, "resolve it, re-ran the pipeline after the file landed", focus=sql)
    a = r["actions"][0]
    assert a["tool"] == "resolve" and a["args"]["incidents"] == [sql] and "re-ran the pipeline" in a["args"]["note"]
    api.post("/api/bot/act", json={"tool": a["tool"], "args": a["args"]})
    with session_scope() as s:
        i = s.get(Incident, sql)
        assert i.status == "resolved" and i.resolved_by == "Chinmoy" and "re-ran" in i.resolution

    r = chat(api, "ack everything critical")
    assert r["actions"][0]["args"]["incidents"] == [uat]  # the resolved one is gone; the warning isn't critical
    r = chat(api, f"note on #{orders}: \"vendor resent yesterday's file\"")
    assert r["actions"][0]["tool"] == "comment" and r["actions"][0]["args"]["text"] == "vendor resent yesterday's file"
    api.post("/api/bot/act", json={"tool": "comment", "args": r["actions"][0]["args"]})
    with session_scope() as s:
        assert s.query(IncidentEvent).filter_by(incident_id=orders, kind="comment").one().message == "vendor resent yesterday's file"

    r = chat(api, f"accept #{orders} as the new normal")
    assert r["actions"][0]["tool"] == "accept_normal"
    r = chat(api, f"accept #{uat} as the new normal")
    assert not r["actions"] and "can't become 'normal'" in r["text"]  # a missed schedule isn't a learned value


def test_mute_send_and_owner_rights_are_checked_again_on_confirm(adf_ws):  # noqa: F811
    sql, uat, _ = _seed()
    owner = _api()
    r = chat(owner, "mute pl_uat for 2 hours")
    a = r["actions"][0]
    assert a["tool"] == "mute" and a["args"]["scope"] == "site:pl_uat" and a["args"]["hours"] == 2
    owner.post("/api/bot/act", json={"tool": "mute", "args": a["args"]})
    with session_scope() as s:
        assert s.query(Mute).filter_by(scope="site:pl_uat").count() == 1
    r = chat(owner, f"send #{uat} to teams")
    assert not r["actions"] and "muted" in r["text"]  # it says why, and how to undo it
    r = chat(owner, "unmute")
    owner.post("/api/bot/act", json={"tool": "unmute", "args": r["actions"][0]["args"]})
    r = chat(owner, f"send #{uat} to teams")
    assert r["actions"][0]["tool"] == "send_alert"
    sent = owner.post("/api/bot/act", json={"tool": "send_alert", "args": r["actions"][0]["args"]}).json()
    assert sent["ok"] and f"#{uat}" in sent["message"]

    # a viewer gets the proposal marked as not allowed, and a forged confirm is refused by the API
    with session_scope() as s:
        other = s.query(User).filter_by(email="other@contoso.com").one()
        s.add(UserProject(user_id=other.id, project_id=s.query(Project).filter_by(key="adfp").one().id, role="viewer"))
    viewer = _api("other@contoso.com")
    r = chat(viewer, "mute pl_sql for 1 hour")
    assert r["actions"][0]["allowed"] is False and "owner" in r["text"]
    assert viewer.post("/api/bot/act", json={"tool": "mute", "args": {"project": "adfp", "target": "pl_sql", "hours": 1}}).status_code == 403
    assert viewer.post("/api/bot/act", json={"tool": "acknowledge", "args": {"incidents": [99999]}}).status_code == 409


def test_questions_navigation_and_help(adf_ws):  # noqa: F811
    sql, *_ = _seed()
    api = _api()
    assert chat(api, "take me to lineage")["navigate"] == "/lineage"
    assert chat(api, f"open incident {sql}")["navigate"] == f"/p/adfp/i/{sql}"
    assert chat(api, "show me ai usage")["navigate"] == "/ai-usage"
    hi = chat(api, "hi")
    assert "need" in hi["text"] and hi["suggestions"]
    assert "Nothing changes until you confirm" in chat(api, "what can you do?")["text"]
    q = chat(api, "what's wrong right now?")
    assert q["text"] and not q["actions"]  # a question is answered, not acted on


def test_the_chart_assistant_only_makes_charts():
    from alertbot.ai.charts import OFF_TOPIC, understand

    names = {"pipelines": ["pl_sql"], "tables": []}
    for q in ("hi", "acknowledge incident 12", "tell me a joke", "why did pl_sql fail?", "send the morning report"):
        reply, spec, _ = understand([{"role": "user", "content": q}], names)
        assert reply == OFF_TOPIC and spec is None, q
    reply, spec, _ = understand([{"role": "user", "content": "yes"}], names)
    assert spec is None and "what to show" in reply
    reply, spec, _ = understand([{"role": "user", "content": "failed runs per day this month as bars"}], names)
    assert spec is not None and spec.metric == "failed_runs" and spec.viz == "bar"


def test_open_ended_requests_are_planned_by_the_ai_and_held_to_the_same_rules(adf_ws, monkeypatch):  # noqa: F811
    import json
    from types import SimpleNamespace

    from alertbot.ai import agent, llm

    sql, uat, _ = _seed()
    seen: list[list[dict]] = []

    class Model:
        def chat(self, messages, **_kw):
            seen.append(messages)
            plan = {"intent": "act", "reply": "I'll take pl_uat and tell the team.",
                    "actions": [{"tool": "acknowledge", "args": {"incidents": [uat]}}, {"tool": "send_alert", "args": {"incident": uat}},
                                {"tool": "resolve", "args": {"incidents": [987654]}}, {"tool": "drop_database", "args": {}}]}
            return llm.ChatResult(text=json.dumps(plan), model="stand-in/model", tried=["stand-in/model"])

    monkeypatch.setattr(agent, "load_ai_config", lambda: SimpleNamespace(ready=True))
    monkeypatch.setattr(llm, "client", lambda: Model())
    r = chat(_api(), "please deal with the uat thing and give the team a heads up")
    assert [a["tool"] for a in r["actions"]] == ["acknowledge", "send_alert"]  # a made-up incident and tool are dropped, said why
    assert "987654" in r["text"] and r["mode"] == "ai" and r["model"] == "stand-in/model"
    system = seen[0][0]["content"]
    assert "acknowledge(" in system and "/lineage" in system and "{tools}" not in system
    assert any(f'"id": {uat}' in m["content"] for m in seen[0])  # it planned from the live state
