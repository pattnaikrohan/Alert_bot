"""The incident register keeps everything (who acknowledged, who resolved and how, comments), badges count only what
nobody has acknowledged, mappings follow renamed or changed pipelines in Azure, and lineage draws staging and clean-up
steps as what they are rather than as loops."""

from __future__ import annotations

from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text
from test_adf import NOW, adf_ws  # noqa: F401 - adf_ws is a fixture

from alertbot.db import DbConnection, DqMapping, Incident, IncidentEvent, Project, User, session_scope

HOST = "203.0.113.7"  # reserved for documentation: never a real server
DB = "TeamTesseract"


def _api(who="chinmoy@contoso.com"):
    from alertbot.api.main import app

    return TestClient(app, headers={"Authorization": f"Bearer dev:{who}"})


def _incident(s, p, scope, status="open", severity="warning", title="x"):
    inc = Incident(project_id=p.id, fingerprint=f"adfp:{scope}", scope=scope, title=title, severity=severity, opened_at=NOW - timedelta(hours=3),
                   status=status)
    s.add(inc)
    s.flush()
    return inc


def _write(table, kind="table", dynamic=False, platform="SQL Server"):
    return {"kind": kind, "platform": platform, "host": HOST, "database": DB, "table": f"{DB}.ALERT_BOT.{table}".upper(), "dynamic": dynamic}


def test_acknowledged_incidents_stay_but_stop_counting_as_needing_attention(adf_ws):  # noqa: F811
    with session_scope() as s:
        p = s.query(Project).filter_by(key="adfp").one()
        _incident(s, p, "site:pl_uat", severity="critical")
        acked = _incident(s, p, "site:pl_sql")
        _incident(s, p, "site:pl_old", status="resolved")
        ack_id = acked.id
    api = _api()
    assert api.post(f"/api/projects/adfp/incidents/{ack_id}/acknowledge").status_code == 200
    card = next(p for p in api.get("/api/me").json()["projects"] if p["key"] == "adfp")
    assert (card["open_incidents"], card["needs_attention"], card["acknowledged"], card["attention_severity"]) == (2, 1, 1, "critical")
    rows = api.get("/api/projects/adfp/incidents?status=all").json()
    assert len(rows) == 3  # nothing leaves the register
    row = next(r for r in rows if r["id"] == ack_id)
    assert row["acknowledged_by"] and row["acknowledged_at"] and row["status"] == "acknowledged"


def test_people_comment_and_resolve_with_their_name_and_how(adf_ws):  # noqa: F811
    with session_scope() as s:
        p = s.query(Project).filter_by(key="adfp").one()
        iid = _incident(s, p, "site:pl_uat").id
    api = _api()
    d = api.post(f"/api/projects/adfp/incidents/{iid}/comments", json={"text": "  Restarting trigger4 now  "}).json()
    assert [(c["by"], c["text"]) for c in d["comments"]] == [(d["comments"][0]["by"], "Restarting trigger4 now")]
    assert all(e["kind"] != "comment" for e in d["events"])  # comments have their own list
    assert api.post(f"/api/projects/adfp/incidents/{iid}/comments", json={"text": "   "}).status_code == 422
    d = api.post(f"/api/projects/adfp/incidents/{iid}/resolve", json={"note": "Trigger restarted"}).json()
    assert d["status"] == "resolved" and d["resolved_by"] == d["comments"][0]["by"] and d["resolution"] == "Trigger restarted" and d["resolved_at"]
    assert api.post(f"/api/projects/adfp/incidents/{iid}/resolve", json={}).status_code == 409
    row = next(r for r in api.get("/api/projects/adfp/incidents?status=all").json() if r["id"] == iid)
    assert row["comments"] == 1 and row["last_comment"]["text"] == "Restarting trigger4 now" and row["resolved_by"]


def test_the_bot_says_it_resolved_things_and_reopens_what_got_worse(adf_ws):  # noqa: F811
    from alertbot.config import load_projects
    from alertbot.engine.checks import Outcome
    from alertbot.engine.cycle import make_context
    from alertbot.engine.incidents import process

    with session_scope() as s:
        p = s.query(Project).filter_by(key="adfp").one()
        inc = _incident(s, p, "site:pl_sql", status="acknowledged")
        inc.checks, inc.acknowledged_by = ["row_count"], "Jnana"
        old = _incident(s, p, "site:pl_cp")
        old.checks = ["run_failed"]
        ctx = make_context(s, load_projects()["adfp"], NOW)
        process(ctx, [Outcome(check="run_failed", scope="site:pl_sql", status="fail", severity="critical", message="failed"),
                        Outcome(check="run_failed", scope="site:pl_cp", status="pass", severity="info", message="ok")], {}, alert=False)
    with session_scope() as s:
        worse = s.query(Incident).filter_by(scope="site:pl_sql").one()
        assert worse.status == "open" and any("needs attention again" in e.message for e in worse.events if e.kind == "worsened")
        fixed = s.query(Incident).filter_by(scope="site:pl_cp").one()
        assert fixed.status == "resolved" and fixed.resolved_by == "Alert Bot" and "pass again" in fixed.resolution


def test_a_table_no_pipeline_loads_any_more_closes_its_incident(adf_ws):  # noqa: F811
    from alertbot.config import load_projects
    from alertbot.engine.adf import close_gone
    from alertbot.engine.cycle import make_context

    items = [{"name": "pl_sql", "type": "pipeline", "lineage": [{"step": "Copy", "type": "Copy", "reads": [], "writes": [_write("WEATHER_OBSERVATION")]}]}]
    with session_scope() as s:
        p = s.query(Project).filter_by(key="adfp").one()
        _incident(s, p, "table:" + f"{DB}.ALERT_BOT.NEWS_ARTICLES2".upper())
        _incident(s, p, "table:" + f"{DB}.ALERT_BOT.WEATHER_OBSERVATION".upper())
        close_gone(make_context(s, load_projects()["adfp"], NOW), {"pl_sql"}, items)
    with session_scope() as s:
        gone = s.query(Incident).filter(Incident.scope.like("%NEWS_ARTICLES2")).one()
        kept = s.query(Incident).filter(Incident.scope.like("%WEATHER_OBSERVATION")).one()
        assert gone.status == "resolved" and gone.resolved_by == "Alert Bot" and "any more" in gone.resolution
        assert kept.status == "open"
    # a pipeline whose definition couldn't be read may load it: nothing is closed then
    with session_scope() as s:
        p = s.query(Project).filter_by(key="adfp").one()
        _incident(s, p, "table:" + f"{DB}.ALERT_BOT.OTHER".upper())
        close_gone(make_context(s, load_projects()["adfp"], NOW), {"pl_sql", "pl_x"}, items + [{"name": "pl_x", "type": "pipeline", "lineage": None}])
    with session_scope() as s:
        assert s.query(Incident).filter(Incident.scope.like("%.OTHER")).one().status == "open"


def test_mappings_follow_renamed_and_changed_pipelines(adf_ws):  # noqa: F811
    from alertbot.dq.service import reconcile_mappings

    with session_scope() as s:
        p = s.query(Project).filter_by(key="adfp").one()
        user = s.query(User).first() or User(email="x@contoso.com", name="X")
        s.add(user)
        s.flush()
        c = DbConnection(user_id=user.id, name="tt", kind="sqlserver", host=HOST, port=1433, database=DB, auth="sql", username="u", status="ok")
        s.add(c)
        s.flush()
        for pipe, table in [("pl_sql", "NEWS_ARTICLES2"), ("pl_sql_uat", "weather_observation"), ("pl_uat", "weather_observation"),
                            ("pipeline1", "NEWS_ARTICLES"), ("pl_nb", "NOTEBOOK_TABLE")]:
            s.add(DqMapping(project_id=p.id, connection_id=c.id, pipeline=pipe, schema_name="Alert_bot", table_name=table, settings={"key": ["ID"]}))
        s.flush()
        items = [
            {"name": "pl_sql", "lineage": [{"step": "c", "type": "Copy", "writes": [_write("WEATHER_OBSERVATION")]}]},
            {"name": "pl_uat", "lineage": [{"step": "c", "type": "Copy", "writes": [_write("WEATHER_OBSERVATION")]}]},
            {"name": "pl_cp_snoflks", "lineage": [{"step": "c", "type": "Copy", "writes": [_write("NEWS_ARTICLES")]}]},  # pipeline1, renamed
            {"name": "pl_nb", "lineage": [{"step": "nb", "type": "DatabricksNotebook", "writes": []}]},  # loads it invisibly
        ]
        notes = reconcile_mappings(s, p, items, NOW)
        got = {m.pipeline + ">" + m.table_name: (m.retired_at is not None, m.retired_reason, m.settings) for m in s.query(DqMapping)}
    assert got["pl_sql>NEWS_ARTICLES2"][0] and "now loads WEATHER_OBSERVATION" in got["pl_sql>NEWS_ARTICLES2"][1]
    assert got["pl_sql_uat>weather_observation"][0] and "no longer in Azure" in got["pl_sql_uat>weather_observation"][1]
    assert not got["pl_uat>weather_observation"][0]
    assert got["pl_cp_snoflks>NEWS_ARTICLES"] == (False, None, {"key": ["ID"]})  # the rename carried its settings
    assert not got["pl_nb>NOTEBOOK_TABLE"][0]  # no evidence it changed: left alone
    assert any("follows the renamed pipeline" in n for n in notes)

    with session_scope() as s:  # pl_sql loads NEWS_ARTICLES2 again: the mapping comes back by itself
        p = s.query(Project).filter_by(key="adfp").one()
        items[0]["lineage"][0]["writes"].append(_write("NEWS_ARTICLES2"))
        reconcile_mappings(s, p, items, NOW)
        assert s.query(DqMapping).filter_by(pipeline="pl_sql", table_name="NEWS_ARTICLES2").one().retired_at is None


def test_lineage_draws_staging_and_clean_up_as_one_hop_not_a_loop(adf_ws):  # noqa: F811
    from alertbot.db import BotState

    blob = {"kind": "files", "platform": "Blob Storage", "host": None, "table": "uat", "dynamic": True}
    stage = {"kind": "files", "platform": "Blob Storage", "host": None, "table": "dev", "dynamic": True}
    api_src = {"kind": "dataset", "platform": "REST API", "host": "eventregistry.org", "table": "RESTRESOURCE1"}
    with session_scope() as s:
        s.add(BotState(key="inventory:adfp", value={"items": [
            {"name": "pl_sql", "type": "pipeline", "lineage": [
                {"step": "Copy", "type": "Copy", "reads": [blob], "writes": [_write("WEATHER_OBSERVATION")]},
                {"step": "Delete1", "type": "Delete", "reads": [], "writes": [blob]}]},
            {"name": "pl_cp", "type": "pipeline", "lineage": [
                {"step": "api", "type": "Copy", "reads": [api_src], "writes": [stage]},
                {"step": "load", "type": "Copy", "reads": [stage], "writes": [_write("NEWS_ARTICLES")]}]},
        ], "at": NOW.isoformat()}))
    g = _api().get("/api/lineage").json()
    label = {n["id"]: n["label"] for n in g["nodes"]}
    flows = {(label[e["from"]], label[e["to"]], bool(e.get("staging"))) for e in g["edges"]}
    assert ("uat", "pl_sql", False) in flows and ("pl_sql", "WEATHER_OBSERVATION", False) in flows
    assert ("pl_sql", "uat", False) not in flows  # the clean-up isn't data flowing back
    assert ("pl_cp", "dev", True) in flows and ("dev", "pl_cp", False) not in flows
    nodes = {n["label"]: n for n in g["nodes"]}
    assert nodes["uat"]["cleaned_by"] == ["pl_sql"] and nodes["dev"]["staged_by"] == ["pl_cp"]


def test_new_columns_are_added_to_an_existing_database(tmp_path):
    from alertbot.db.session import add_missing_columns

    eng = create_engine(f"sqlite:///{(tmp_path / 'old.db').as_posix()}")
    with eng.begin() as c:
        c.execute(text("CREATE TABLE incidents (id INTEGER PRIMARY KEY, title VARCHAR(300))"))
        c.execute(text("INSERT INTO incidents (id, title) VALUES (1, 'kept')"))
        c.execute(text("CREATE TABLE dq_mappings (id INTEGER PRIMARY KEY, pipeline VARCHAR(200))"))
        c.execute(text("INSERT INTO dq_mappings (id, pipeline) VALUES (1, 'pl_sql')"))
    add_missing_columns(eng)
    cols = {c["name"] for c in inspect(eng).get_columns("incidents")}
    assert {"resolved_by", "resolution"} <= cols
    with eng.begin() as c:
        assert c.execute(text("SELECT title FROM incidents")).scalar() == "kept"
        assert c.execute(text("SELECT follow_azure FROM dq_mappings")).scalar() == 1
    assert IncidentEvent.__tablename__ == "incident_events"


def test_a_persons_resolve_holds_against_the_same_failure_but_not_a_new_one(adf_ws):  # noqa: F811
    from alertbot.config import load_projects
    from alertbot.engine.checks import Outcome
    from alertbot.engine.cycle import make_context
    from alertbot.engine.incidents import process

    def missed(last_run, severity="critical"):
        return Outcome(check="missed_run", scope="site:pl_uat", status="fail", severity=severity, message="missed",
                       details={"last_run_at": last_run})

    with session_scope() as s:
        ctx = make_context(s, load_projects()["adfp"], NOW)
        process(ctx, [missed("2026-10-14T10:00:00")], {}, alert=False)
        iid = s.query(Incident).filter_by(scope="site:pl_uat").one().id
        from alertbot.db import BotCycle, CheckResult
        cyc = BotCycle(project_id=ctx.project.id, started_at=NOW, status="ok")
        s.add(cyc)
        s.flush()
        s.add(CheckResult(cycle_id=cyc.id, project_id=ctx.project.id, check="missed_run", scope="site:pl_uat", status="fail",
                          severity="critical", incident_id=iid, details={"last_run_at": "2026-10-14T10:00:00"}))
    assert _api().post(f"/api/projects/adfp/incidents/{iid}/resolve", json={"note": "rescheduled"}).status_code == 200

    def count_after(outcome, at):
        with session_scope() as s:
            process(make_context(s, load_projects()["adfp"], at), [outcome], {}, alert=False)
        with session_scope() as s:
            return s.query(Incident).filter_by(scope="site:pl_uat").filter(Incident.status != "resolved").count()

    from alertbot.timeutil import utcnow

    now = utcnow()
    assert count_after(missed("2026-10-14T10:00:00"), now + timedelta(minutes=5)) == 0  # same failure: the person's call stands
    assert count_after(missed("2026-10-14T13:00:00"), now + timedelta(minutes=10)) == 1  # a new missed run: it's back


def test_a_table_the_pipeline_no_longer_loads_isnt_judged_on_old_loads(adf_ws):  # noqa: F811
    from alertbot.config import load_projects
    from alertbot.db import BotState, MetricObservation, Run
    from alertbot.engine.adf import AdfEvaluator
    from alertbot.engine.cycle import make_context

    with session_scope() as s:
        p = s.query(Project).filter_by(key="adfp").one()
        s.add(BotState(key="inventory:adfp", value={"items": [{"name": "pl_sql", "type": "pipeline", "lineage": [
            {"step": "c", "type": "Copy", "writes": [_write("WEATHER_OBSERVATION")]}]}]}))
        run = Run(project_id=p.id, trigger_id="r9", site="pl_sql", pipeline="t", trigger_status="succeeded", run_id="r9",
                  run_status="Succeeded", requested_at=NOW - timedelta(hours=1))
        s.add(run)
        for table in ("NEWS_ARTICLES2", "WEATHER_OBSERVATION"):
            s.add(MetricObservation(project_id=p.id, output_id=f"table:{DB.upper()}.ALERT_BOT.{table}", site="pl_sql", metric="row_count",
                                    field="", run_ref="r9", weekday=NOW.weekday(), value=50.0, observed_at=NOW - timedelta(hours=1)))
        s.flush()
        ev = AdfEvaluator(make_context(s, load_projects()["adfp"], NOW), [run])
        scopes = {o.scope for o in ev.table_checks("pl_sql", [run])}
    assert scopes == {f"table:{DB.upper()}.ALERT_BOT.WEATHER_OBSERVATION"}


def test_every_page_sees_a_handled_pipeline_the_same_way(adf_ws):  # noqa: F811
    """A failed run whose incident a person resolved (or acknowledged) is "being handled": not failing, not on the
    pipelines badge; Home's feed lists what was resolved today with who resolved it."""
    from alertbot.db import BotState, Run
    from alertbot.timeutil import utcnow

    now = utcnow()
    with session_scope() as s:
        p = s.query(Project).filter_by(key="adfp").one()
        s.add(BotState(key="inventory:adfp", value={"items": [{"name": n, "type": "pipeline", "lineage": []} for n in ("pl_sql", "pl_uat", "pl_ok")],
                                                    "at": now.isoformat()}))
        for n, st in (("pl_sql", "Failed"), ("pl_uat", "Failed"), ("pl_ok", "Succeeded")):
            s.add(Run(project_id=p.id, trigger_id=f"t-{n}", site=n, pipeline="t", trigger_status="x", run_id=f"r-{n}", run_status=st,
                      requested_at=now - timedelta(minutes=30)))
        inc = _incident(s, p, "site:pl_sql", severity="critical", title="pl_sql pipeline failed")
        inc.opened_at = now - timedelta(minutes=25)
        iid = inc.id
        other = _incident(s, p, "site:pl_uat", severity="critical", title="pl_uat pipeline failed")
        other.opened_at = now - timedelta(minutes=25)
    api = _api()
    counts = api.get("/api/pipelines?days=7").json()["counts"]
    assert counts["failing"] == 2 and counts.get("handled", 0) == 0
    api.post(f"/api/projects/adfp/incidents/{iid}/resolve", json={"note": "re ran again"})
    d = api.get("/api/pipelines?days=7").json()
    sql = next(i for i in d["items"] if i["name"] == "pl_sql")
    assert sql["state"] == "handled" and sql["handled"]["kind"] == "resolved" and sql["handled"]["note"] == "re ran again"
    assert d["counts"]["failing"] == 1 and d["counts"]["handled"] == 1
    feed = api.get("/api/feed").json()
    assert [i["id"] for i in feed["resolved_today"]] == [iid] and feed["resolved_today"][0]["resolved_by"]
    card = next(p for p in feed["projects"] if p["key"] == "adfp")
    assert card["resolved_today"] == 1 and card["needs_attention"] == 1
    uat = next(i for i in api.get("/api/projects/adfp/incidents?status=active").json() if i["scope"] == "site:pl_uat")
    api.post(f"/api/projects/adfp/incidents/{uat['id']}/acknowledge")
    d = api.get("/api/pipelines?days=7").json()
    assert d["counts"]["failing"] == 0 and d["counts"]["handled"] == 2
    assert next(i for i in d["items"] if i["name"] == "pl_uat")["handled"]["kind"] == "acknowledged"


def test_refresh_rereads_every_source_and_says_what_changed(adf_ws):  # noqa: F811
    """Refresh re-reads Azure as it is now: a pipeline deleted (and published) there is reported as removed and leaves
    the list; one added is reported as new."""
    from alertbot.db import BotState

    with session_scope() as s:
        s.add(BotState(key="inventory:adfp", value={"items": [{"name": "pl_deleted", "type": "pipeline", "lineage": []},
                                                              {"name": "DEV-MULTIFILE", "type": "pipeline", "lineage": []}],
                                                    "at": NOW.isoformat()}))
    r = _api().post("/api/refresh")
    assert r.status_code == 200, r.text
    src = next(x for x in r.json()["sources"] if x["key"] == "adfp")
    assert src["status"] == "ok" and src["removed"] == ["pl_deleted"] and set(src["added"]) == {"DEV-SG-TERMINAL-CSV", "DEV-EMPTY"}
    assert src["count"] == 3 and "checks" in src
    names = [i["name"] for i in _api().get("/api/pipelines?days=7").json()["items"] if i["project"] == "adfp"]
    assert "pl_deleted" not in names


def test_a_fresh_read_forgets_what_azure_said_before(adf_ws):  # noqa: F811
    from alertbot import connectors
    from alertbot.config import load_projects

    adf = connectors.AdfPipeline(load_projects()["adfp"].pipeline)
    adf._datasets["OLD"] = {"table": "STALE"}
    adf._definitions["gone"] = []
    adf.forget()
    assert adf._datasets == {} and adf._definitions == {} and adf._linked is None
