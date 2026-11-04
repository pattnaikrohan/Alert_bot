"""Suspicious values: the rules bot sees names only; its rules are a fixed vocabulary turned into SQL by the bot;
counts are numbers; the values themselves go only to the person who connected the database, and are kept nowhere.
A SQLite file stands in for the warehouse; the SQL Server and Snowflake text is checked too."""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from test_adf import adf_ws  # noqa: F401 - fixture

from alertbot.db import BotState, Project, User, UserProject, session_scope
from alertbot.dq import sql as T
from alertbot.dq import suspicious as V
from alertbot.timeutil import utcnow

BAD = "Port Klang1"


def _ports(path):
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE PORTS (ID INTEGER, PORT_NAME TEXT, UNLOCODE TEXT, CONTACT_EMAIL TEXT, LATITUDE REAL, LOAD_DATE TIMESTAMP)")
    at = utcnow().isoformat(sep=" ")
    rows = [(1, "Singapore", "SGSIN", "ops@sg.example", 1.26), (2, "Rotterdam", "NLRTM", "ops@nl.example", 51.9), (3, BAD, "MYPKG", "a@b.example", 3.0),
            (4, BAD, "MYPKG", "a@b.example", 3.0), (5, "Busan@", "KRPUS", "x@y.example", 35.1), (6, "N/A", "XX", "z@z.example", 95.0),
            (7, "Hamburg ", "DEHAM", "h@h.example", 53.5), (8, "São Paulo", "BRSSZ", "s@p.example", -23.9)]
    db.executemany("INSERT INTO PORTS VALUES (?, ?, ?, ?, ?, ?)", [(*r, at) for r in rows])
    db.commit()
    db.close()


class RulesBot:
    """Stands in for the model: records what it was sent, answers with rules (some of them bad on purpose)."""

    def __init__(self):
        self.sent: list[str] = []

    def chat_json(self, system, user, max_tokens=0):
        self.sent.append(user)
        sent = json.loads(user)
        told = (sent.get("meanings") or {}).get("PORT_NAME", "")
        if sent.get("propose_for") == ["PORT_NAME"] and "LOCODE" in told:  # a person said what the column really holds
            return {"rules": [{"column": "PORT_NAME", "kind": "pattern", "shapes": ["{L2}{A3}"], "reason": "It holds UN/LOCODEs.", "confidence": "high"}],
                    "meanings": {"PORT_NAME": "the bot would rather call it a port name"}}, SimpleNamespace(model="rules-spy")
        if "propose_for" in sent:  # a new column appeared: rules for it only
            return {"rules": [{"column": "BOOKING_REF", "kind": "pattern", "shapes": ["{L3}{D4}"], "reason": "3 letters, 4 digits.", "confidence": "high"},
                              {"column": "PORT_NAME", "kind": "trimmed", "reason": "not asked for: must be ignored"}]}, SimpleNamespace(model="rules-spy")
        return {"rules": [
            {"column": "PORT_NAME", "kind": "no_digits", "reason": "Port names are words.", "confidence": "high"},
            {"column": "PORT_NAME", "kind": "no_symbols", "reason": "No symbols in a port name."},
            {"column": "UNLOCODE", "kind": "format", "format": "unlocode", "reason": "UN/LOCODE."},
            {"column": "LATITUDE", "kind": "range", "min": -90, "max": 90, "reason": "Latitude."},
            {"column": "CONTACT_EMAIL", "kind": "trimmed", "reason": "personal: must be dropped"},
            {"column": "PORT_NAME", "kind": "sql", "reason": "DROP TABLE PORTS"},          # not a kind
            {"column": "NOPE", "kind": "no_digits", "reason": "not a column"},
            {"column": "LATITUDE", "kind": "no_digits", "reason": "wrong type for the column"},
            {"column": "UNLOCODE", "kind": "format", "format": "regex:.*", "reason": "not a known format"},
        ], "meanings": {"PORT_NAME": "the name of the port", "UNLOCODE": "the port's UN/LOCODE", "NOPE": "not a column"}}, SimpleNamespace(model="rules-spy")


@pytest.fixture
def sx(adf_ws, tmp_path, monkeypatch):  # noqa: F811
    from alertbot.dq import client as dqclient
    from alertbot.dq import vault

    monkeypatch.setenv("ALERTBOT_SECRET_KEY_FILE", str(tmp_path / "secret.key"))
    vault._fernet.cache_clear()
    _ports(tmp_path / "src.db")
    with session_scope() as s:
        s.add(BotState(key="inventory:adfp", value={"items": [{"name": "pl_sql", "lineage": []}]}))
    bot = RulesBot()
    monkeypatch.setattr("alertbot.ai.llm.client", lambda: bot)
    app = __import__("alertbot.api.main", fromlist=["app"]).app
    api = TestClient(app, headers={"Authorization": "Bearer dev:chinmoy@contoso.com", "X-AlertBot": "1"})
    r = api.post("/api/databases", json={"kind": "sqlite", "host": str(tmp_path / "src.db"), "database": "main", "auth": "sql", "name": "Ports DB"})
    assert r.status_code == 200, r.text
    cid = r.json()["connection"]["id"]
    r = api.post("/api/projects/adfp/mappings", json={"pipeline": "pl_sql", "connection_id": cid, "schema": "main", "table": "PORTS",
                                                      "settings": {"sensitive": ["CONTACT_EMAIL"]}})
    assert r.status_code == 200, r.text
    yield SimpleNamespace(api=api, app=app, bot=bot, mid=r.json()["mapping"]["id"], tmp=tmp_path)
    for c in list(dqclient._ENGINES):
        dqclient.forget(c)
    vault._fernet.cache_clear()


def test_the_rules_bot_sees_names_only_and_only_its_vocabulary_survives(sx, caplog):
    caplog.set_level(logging.DEBUG)
    t = sx.api.post(f"/api/projects/adfp/suspicious/{sx.mid}/rules", json={"ai": True}).json()
    sent = json.loads(sx.bot.sent[0])
    assert sent == {"table": "main.main.PORTS", "columns": [
        {"name": "ID", "type": "number"}, {"name": "PORT_NAME", "type": "text"}, {"name": "UNLOCODE", "type": "text"},
        {"name": "LATITUDE", "type": "number"}, {"name": "LOAD_DATE", "type": "date"}]}  # the sensitive CONTACT_EMAIL isn't even named
    assert "CONTACT_EMAIL" not in sx.bot.sent[0] and BAD not in sx.bot.sent[0] and "Singapore" not in sx.bot.sent[0]  # names, never values
    kinds = {(r["column"], r["kind"]) for r in t["rules"]}
    assert kinds == {("PORT_NAME", "no_digits"), ("PORT_NAME", "no_symbols"), ("UNLOCODE", "format"), ("LATITUDE", "range")}
    assert t["by"] == "ai" and t["model"] == "rules-spy"
    counts = {(r["column"], r["kind"]): r["count"] for r in t["rules"]}
    assert counts == {("PORT_NAME", "no_digits"): 2, ("PORT_NAME", "no_symbols"): 1, ("UNLOCODE", "format"): 1, ("LATITUDE", "range"): 1}
    assert t["suspicious"] == 5 and t["rows"] == 8 and t["rules_hit"] == 4
    assert BAD not in caplog.text  # counts only in the logs


def test_values_go_to_the_database_owner_only_and_are_kept_nowhere(sx, caplog):
    caplog.set_level(logging.DEBUG)
    t = sx.api.post(f"/api/projects/adfp/suspicious/{sx.mid}/rules", json={"ai": True}).json()
    digits = next(r for r in t["rules"] if r["kind"] == "no_digits")
    got = sx.api.get(f"/api/projects/adfp/suspicious/{sx.mid}/rule/{digits['id']}/values").json()
    assert got["values"] == [{"value": BAD, "count": 2, "length": len(BAD)}] and "doesn't keep" in got["note"]
    trimmed = sx.api.post(f"/api/projects/adfp/suspicious/{sx.mid}/rule", json={"column": "PORT_NAME", "kind": "trimmed"}).json()
    rid = next(r["id"] for r in trimmed["rules"] if r["kind"] == "trimmed")
    assert next(r for r in trimmed["rules"] if r["kind"] == "trimmed")["count"] == 1  # counted as soon as it's added
    assert sx.api.get(f"/api/projects/adfp/suspicious/{sx.mid}/rule/{rid}/values").json()["values"][0]["value"] == "Hamburg "
    # nothing of it in the bot's own database, or its logs
    with session_scope() as s:
        s.commit()
    raw = (sx.tmp / "alertbot.db").read_bytes()
    assert BAD.encode() not in raw and b"Hamburg " not in raw and BAD not in caplog.text
    # someone else in the project sees the counts, not the values
    with session_scope() as s:
        other = s.query(User).filter_by(email="other@contoso.com").one_or_none()
        if other is None:
            other = User(email="other@contoso.com", name="Other")
            s.add(other)
            s.flush()
        s.add(UserProject(user_id=other.id, project_id=s.query(Project).filter_by(key="adfp").one().id, role="member"))
    them = TestClient(sx.app, headers={"Authorization": "Bearer dev:other@contoso.com", "X-AlertBot": "1"})
    seen = them.get("/api/projects/adfp/suspicious").json()["tables"][0]
    assert seen["suspicious"] == 6 and seen["owner"] is False
    r = them.get(f"/api/projects/adfp/suspicious/{sx.mid}/rule/{digits['id']}/values")
    assert r.status_code == 403 and "only the person who connected" in r.json()["detail"]
    # a sensitive column can't be checked or shown, even by hand
    r = sx.api.post(f"/api/projects/adfp/suspicious/{sx.mid}/rule", json={"column": "CONTACT_EMAIL", "kind": "trimmed"})
    assert r.status_code == 422 and "never checked" in r.json()["detail"]


def test_people_decide_last_and_without_ai_the_names_decide(sx, monkeypatch):
    t = sx.api.post(f"/api/projects/adfp/suspicious/{sx.mid}/rules", json={"ai": True}).json()
    sym = next(r for r in t["rules"] if r["kind"] == "no_symbols")
    t = sx.api.patch(f"/api/projects/adfp/suspicious/{sx.mid}/rule/{sym['id']}", json={"enabled": False}).json()
    assert t["suspicious"] == 4  # a rule switched off doesn't count
    sx.api.post(f"/api/projects/adfp/suspicious/{sx.mid}/rule", json={"column": "PORT_NAME", "kind": "placeholder"})
    from alertbot.ai import llm

    def down():
        raise llm.AIUnavailable("no key")

    monkeypatch.setattr("alertbot.ai.llm.client", down)
    t = sx.api.post(f"/api/projects/adfp/suspicious/{sx.mid}/rules", json={"ai": True}).json()
    assert t["by"] == "names" and "couldn't answer" in t["note"]
    rules = {(r["column"], r["kind"]): r for r in t["rules"]}
    assert rules[("PORT_NAME", "no_symbols")]["enabled"] is False  # still off
    assert rules[("PORT_NAME", "placeholder")]["source"] == "person" and rules[("PORT_NAME", "placeholder")]["count"] == 1
    assert ("UNLOCODE", "format") in rules and ("LATITUDE", "range") in rules  # from the names alone
    assert sx.api.delete(f"/api/projects/adfp/suspicious/{sx.mid}/rule/{sym['id']}").status_code == 200


def test_the_worker_gives_each_checked_table_rules_once_then_counts_on_schedule(sx):
    with session_scope() as s:
        assert not V.rules_of(s, sx.mid)
    assert V.scan_due() == 1
    with session_scope() as s:
        rs, cs = V.rules_of(s, sx.mid), V.counts_of(s, sx.mid)
    assert rs["by"] == "ai" and len(rs["rules"]) == 4 and sum(c["all"] for c in cs["counts"].values()) == 5 and cs["error"] is None
    assert V.scan_due() == 0  # not due again until the table's own check interval has passed
    assert len(sx.bot.sent) == 1  # the bot was asked once


def test_the_guard_stops_rules_that_would_flood_people_with_false_alarms():
    cols = {"IMAGE_URL": "text", "BODY": "text", "ARTICLE_TIME": "text", "SIM": "text", "PORT_NAME": "text", "LANG": "text"}
    proposed = [V.ValueRule(column=c, kind=k, source="ai", confidence=conf) for c, k, conf in [
        ("IMAGE_URL", "no_symbols", "high"), ("BODY", "no_symbols", "medium"), ("ARTICLE_TIME", "no_symbols", "high"), ("SIM", "no_digits", "high"),
        ("PORT_NAME", "no_digits", "high"), ("LANG", "uppercase", "low")]]
    kept, dropped = V.clean(proposed, cols, set())
    assert [(r.column, r.kind, r.enabled) for r in kept] == [("PORT_NAME", "no_digits", True), ("LANG", "uppercase", False)]  # low: off at first
    assert len(dropped) == 4 and any("link" in d for d in dropped) and any("date or time" in d for d in dropped)
    mine = V.ValueRule(column="BODY", kind="no_digits", source="person").checked("text")
    assert V.sane(mine) is None  # a person's own rule is theirs to judge
    old = {"generated_at": "2026-10-22T00:00:00Z", "rules": [p.checked("text").model_dump() for p in proposed[:2]] + [mine.model_dump()]}
    again = V.recheck(old)
    assert [x["column"] for x in again["rules"]] == ["BODY"] and again["guard"] == V.GUARD and len(again["dropped"]) == 2


def test_a_rule_most_rows_break_is_the_rules_mistake_and_switches_itself_off():
    flag = V.ValueRule(column="IS_DUPLICATE", kind="placeholder", source="ai").checked("text")
    assert V.sane(flag) and "flag" in V.sane(flag)  # flags: never a placeholder rule
    bad = V.ValueRule(column="DATA_TYPE", kind="uppercase", source="ai").checked("text")
    mine = V.ValueRule(column="DATA_TYPE", kind="no_digits", source="person").checked("text")
    ok = V.ValueRule(column="PORT_NAME", kind="no_digits", source="ai").checked("text")
    state = {"rules": [bad.model_dump(), mine.model_dump(), ok.model_dump()]}
    counts = {"rows": 100, "counts": {bad.id: {"all": 97}, mine.id: {"all": 90}, ok.id: {"all": 3}}}
    new, off = V.auto_off(state, counts)
    assert off == [bad.id]
    by = {x["id"]: x for x in new["rules"]}
    assert by[bad.id]["enabled"] is False and "97% of rows" in by[bad.id]["reason"]
    assert by[mine.id]["enabled"] and by[ok.id]["enabled"]  # people's rules, and rules most rows obey, stay on
    by[bad.id].update(enabled=True, kept_on=True)  # a person switched it back on: left alone from then on
    assert V.auto_off(new, counts)[1] == []
    assert V.auto_off(state, {"rows": 10, "counts": counts["counts"]})[1] == []  # too few rows to judge
    assert "0" not in V.PLACEHOLDERS and "X" not in V.PLACEHOLDERS


def test_every_kind_is_safe_sql_on_every_database():
    now = datetime(2026, 10, 22)
    rules = [V.ValueRule(column="C", kind=k, **kw).checked(t) for k, t, kw in [
        ("no_digits", "text", {}), ("no_symbols", "text", {}), ("trimmed", "text", {}), ("uppercase", "text", {}), ("placeholder", "text", {}),
        ("length", "text", {"min": 2, "max": 5}), ("format", "text", {"format": "unlocode"}), ("format", "text", {"format": "digits"}),
        ("range", "number", {"min": -90, "max": 90}), ("date_sane", "date", {"min_year": 2000, "future_days": 1})]]
    for name in ("sqlserver", "snowflake", "sqlite"):
        d = T.DIALECTS[name]
        q = V.count_query(d, "dbo", "T", rules, "LOAD_DATE", now, now)
        T.assert_read_only(q.sql)
        assert len(q.spec.columns) == 2 + 2 * len(rules)
        for r in rules:
            sql, binds = V.values_query(d, "dbo", "T", r, None, None, now)
            assert "SELECT " in sql and ("TOP 50" in sql if name == "sqlserver" else "LIMIT 50" in sql)
            # only our own constants are bound: numbers, dates (as ISO text on SQLite), the placeholder list
            assert all(not isinstance(v, str) or v in V.PLACEHOLDERS or datetime.fromisoformat(v) for v in binds.values())
    ss = V.count_query(T.DIALECTS["sqlserver"], "dbo", "T", rules, None, None, now).sql
    assert "[C] LIKE '%[0-9]%'" in ss and "PATINDEX('%[@#$%^*=+<>?!~|{}_]%', [C]) > 0" in ss and "LIKE '[A-Z][A-Z][A-Z2-9][A-Z2-9][A-Z2-9]'" in ss
    sf = V.count_query(T.DIALECTS["snowflake"], "DB", "T", rules, None, None, now).sql
    assert "REGEXP_LIKE(\"C\", '.*[0-9].*', 's')" in sf and "NOT REGEXP_LIKE(\"C\", '^[A-Za-z]{2} ?[A-Za-z2-9]{3}$', 'i')" in sf
    with pytest.raises(ValueError):
        V.ValueRule(column="C", kind="range").checked("number")  # no limits
    with pytest.raises(ValueError):
        V.ValueRule(column="C", kind="no_digits").checked("number")  # wrong type
    with pytest.raises(Exception):  # noqa: B017 - pydantic refuses anything outside the vocabulary
        V.ValueRule(column="C", kind="raw_sql")


def test_names_alone_give_sensible_rules_and_skip_personal_data():
    rules = V.from_names("PORTS", [("PORT_NAME", "text"), ("VESSEL_NAME", "text"), ("CountryCode", "text"), ("POL_CODE", "text"),
                                   ("LATITUDE", "number"), ("LOAD_DATE", "date"), ("ETA", "date"), ("CONTACT_EMAIL", "text"), ("FIRST_NAME", "text")])
    got = {(r.column, r.kind) for r in rules}
    assert {("PORT_NAME", "no_digits"), ("PORT_NAME", "no_symbols"), ("CountryCode", "format"), ("POL_CODE", "format"),
            ("LATITUDE", "range"), ("LOAD_DATE", "date_sane"), ("ETA", "date_sane")} <= got
    assert ("VESSEL_NAME", "no_digits") not in got  # MSC ANNA 2 is a real vessel name
    assert not any(c in ("CONTACT_EMAIL", "FIRST_NAME") for c, _ in got)
    assert V.validate_values([("x", 2)], ["v", "n"]) == [{"value": "x", "count": 2, "length": 1}]
    from datetime import date
    assert [v["value"] for v in V.validate_values([(date(1899, 1, 1), 1), (datetime(2099, 1, 1, 5), 2)], ["v", "n"])] == ["1899-01-01", "2099-01-01 05:00:00"]
    with pytest.raises(Exception):  # noqa: B017
        V.validate_values([("x", 2, "extra")], ["v", "n", "other"])


def test_a_new_column_gets_its_own_rules_and_a_removed_one_takes_its_rules_with_it(sx):
    assert V.scan_due() == 1
    db = sqlite3.connect(sx.tmp / "src.db")
    db.execute("ALTER TABLE PORTS ADD COLUMN BOOKING_REF TEXT")
    db.execute("UPDATE PORTS SET BOOKING_REF = CASE WHEN ID = 3 THEN 'AB12' ELSE 'ABC' || (1000 + ID) END")
    db.commit()
    db.close()
    with session_scope() as s:  # an hour later, when the bot next looks at the table's columns
        rs = V.rules_of(s, sx.mid)
        V.save_rules(s, sx.mid, {**rs, "columns_at": "2026-01-01T00:00:00Z"})
    assert V.scan_due() == 1
    asked = json.loads(sx.bot.sent[-1])
    assert asked["propose_for"] == ["BOOKING_REF"] and {c["name"] for c in asked["columns"]} >= {"PORT_NAME", "BOOKING_REF"}
    t = sx.api.get("/api/projects/adfp/suspicious").json()["tables"][0]
    new = [r for r in t["rules"] if r["column"] == "BOOKING_REF"]
    assert [(r["kind"], r["label"], r["count"]) for r in new] == [("pattern", "looks like 3 letters, then 4 digits", 1)]  # 'AB12'
    assert not any(r["column"] == "PORT_NAME" and r["kind"] == "trimmed" and r["source"] == "ai" for r in t["rules"])  # not asked: ignored
    assert len([r for r in t["rules"] if r["column"] == "PORT_NAME"]) == 2 and "BOOKING_REF" in t["change_note"]  # the others stay
    db = sqlite3.connect(sx.tmp / "src.db")
    db.execute("ALTER TABLE PORTS DROP COLUMN LATITUDE")
    db.commit()
    db.close()
    with session_scope() as s:
        V.save_rules(s, sx.mid, {**V.rules_of(s, sx.mid), "columns_at": "2026-01-01T00:00:00Z"})
    V.scan_due()
    t = sx.api.get("/api/projects/adfp/suspicious").json()["tables"][0]
    assert not any(r["column"] == "LATITUDE" for r in t["rules"]) and "LATITUDE left the table" in t["change_note"]


def test_shapes_forbidden_characters_allowed_values_and_lower_case(sx):
    db = sqlite3.connect(sx.tmp / "src.db")
    db.execute("ALTER TABLE PORTS ADD COLUMN DIRECTION TEXT")
    db.execute("UPDATE PORTS SET DIRECTION = CASE ID WHEN 2 THEN 'export ' WHEN 4 THEN 'INBOUND' ELSE 'IMPORT' END")
    db.commit()
    db.close()
    add = lambda body: sx.api.post(f"/api/projects/adfp/suspicious/{sx.mid}/rule", json=body)  # noqa: E731
    assert add({"column": "UNLOCODE", "kind": "pattern", "shapes": ["{L2}{A3}"]}).status_code == 200
    assert add({"column": "PORT_NAME", "kind": "forbid", "chars": ["digits", "symbols"]}).status_code == 200
    assert add({"column": "DIRECTION", "kind": "allowed_values", "allowed": ["IMPORT", "EXPORT"]}).status_code == 200
    t = add({"column": "UNLOCODE", "kind": "lowercase"}).json()
    got = {(r["column"], r["kind"]): r["count"] for r in t["rules"] if r["source"] == "person"}
    assert got == {("UNLOCODE", "pattern"): 1, ("PORT_NAME", "forbid"): 3, ("DIRECTION", "allowed_values"): 1, ("UNLOCODE", "lowercase"): 8}
    rid = next(r["id"] for r in t["rules"] if r["kind"] == "allowed_values")
    assert [v["value"] for v in sx.api.get(f"/api/projects/adfp/suspicious/{sx.mid}/rule/{rid}/values").json()["values"]] == ["INBOUND"]
    # a shape is our tiny language, never SQL
    for bad in ["x'; DROP TABLE PORTS; --", "{L999}", "%", "[A-Z]", "{Q2}", "a" * 41]:
        r = add({"column": "UNLOCODE", "kind": "pattern", "shapes": [bad]})
        assert r.status_code == 422, bad
    now = datetime(2026, 10, 22)
    rules = [V.ValueRule(column="C", kind="pattern", shapes=["IMO{D7}", "{L2} {A3}", "{D4}-{D2}-{D2}"]).checked("text"),
             V.ValueRule(column="C", kind="forbid", chars=["letters", "spaces", "digits", "symbols"]).checked("text"),
             V.ValueRule(column="C", kind="allowed_values", allowed=["Y", "N"]).checked("text"),
             V.ValueRule(column="C", kind="lowercase").checked("text")]
    for name in ("sqlserver", "snowflake", "sqlite"):
        T.assert_read_only(V.count_query(T.DIALECTS[name], "dbo", "T", rules, None, None, now).sql)
    sf = V.count_query(T.DIALECTS["snowflake"], "DB", "T", rules[:1], None, None, now).sql
    assert "REGEXP_LIKE(\"C\", '^IMO[0-9]{7}$', 'i')" in sf and "'^[A-Za-z]{2}[ ][A-Za-z0-9]{3}$'" in sf and "'^[0-9]{4}[-][0-9]{2}[-][0-9]{2}$'" in sf
    ss = V.count_query(T.DIALECTS["sqlserver"], "dbo", "T", rules[:1], None, None, now).sql
    assert "[C] LIKE 'IMO[0-9][0-9][0-9][0-9][0-9][0-9][0-9]'" in ss
    # older rules keep their ids (people's on/off choices survive the upgrade)
    assert V.ValueRule(column="PORT_NAME", kind="no_digits").checked("text").id == V.ValueRule(column="PORT_NAME", kind="no_digits", shapes=None).checked("text").id


def test_names_give_rules_for_any_column_even_without_ai():
    cols = [("BOOKING_STATUS_CODE", "text"), ("IS_REEFER", "text"), ("CARGO_WEIGHT", "number"), ("TEU_COUNT", "number"), ("REMARKS", "text"),
            ("VOYAGE_REF", "text")]
    got = {(r.column, r.kind, r.enabled) for r in V.clean(V.from_names("BOOKINGS", cols), dict(cols), set())[0]}
    assert ("BOOKING_STATUS_CODE", "forbid", True) in got and ("CARGO_WEIGHT", "range", True) in got and ("TEU_COUNT", "range", True) in got
    assert ("IS_REEFER", "allowed_values", False) in got  # a guess: starts off
    assert ("VOYAGE_REF", "trimmed", True) in got
    assert not any(c == "REMARKS" for c, _, _ in got)  # free text: nothing certain


def test_meanings_are_shown_remembered_and_steer_the_bot(sx):
    url = f"/api/projects/adfp/suspicious/{sx.mid}"
    t = sx.api.post(f"{url}/rules", json={"ai": True}).json()
    assert t["meanings"]["PORT_NAME"]["text"] == "the name of the port" and t["meanings"]["PORT_NAME"]["by"] == "ai"
    assert "NOPE" not in t["meanings"]  # only real columns
    before = {r["id"] for r in t["rules"] if r["column"] != "PORT_NAME"}
    # a person says what PORT_NAME really holds: remembered, given to the bot as a fact, and its rules there are rethought
    t = sx.api.put(f"{url}/meaning", json={"column": "PORT_NAME", "text": "the port's UN/LOCODE, like SGSIN", "rethink": True}).json()
    asked = json.loads(sx.bot.sent[-1])
    assert asked["propose_for"] == ["PORT_NAME"] and asked["meanings"] == {"PORT_NAME": "the port's UN/LOCODE, like SGSIN"}
    m = t["meanings"]["PORT_NAME"]
    assert m["text"] == "the port's UN/LOCODE, like SGSIN" and m["by"] == "person" and m["who"] == "Chinmoy"  # the bot can't overwrite it
    port = [(r["kind"], r["count"]) for r in t["rules"] if r["column"] == "PORT_NAME"]
    assert port == [("pattern", 8)]  # none of the 8 names is a LOCODE; its earlier no_digits / no_symbols went
    assert before <= {r["id"] for r in t["rules"]}  # the other columns' rules are untouched
    # the whole table again: the person's meaning still stands, and goes to the bot
    sx.api.post(f"{url}/rules", json={"ai": True})
    assert json.loads(sx.bot.sent[-1])["meanings"] == {"PORT_NAME": "the port's UN/LOCODE, like SGSIN"}
    t = sx.api.get("/api/projects/adfp/suspicious").json()["tables"][0]
    assert t["meanings"]["PORT_NAME"]["by"] == "person"
    # and back to the bot's own reading
    t = sx.api.put(f"{url}/meaning", json={"column": "PORT_NAME", "text": "", "rethink": False}).json()
    assert "PORT_NAME" not in t["meanings"] or t["meanings"]["PORT_NAME"]["by"] != "person"


def test_any_rule_can_be_edited_or_deleted_and_the_bot_respects_it(sx):
    url = f"/api/projects/adfp/suspicious/{sx.mid}"
    t = sx.api.post(f"{url}/rules", json={"ai": True}).json()
    digits = next(r for r in t["rules"] if r["kind"] == "no_digits")
    sym = next(r for r in t["rules"] if r["kind"] == "no_symbols")
    # edit the bot's rule: it becomes the team's own, counted at once
    t = sx.api.put(f"{url}/rule/{digits['id']}", json={"column": "PORT_NAME", "kind": "forbid", "chars": ["digits", "spaces"],
                                                       "reason": "Our port names are one word"}).json()
    edited = next(r for r in t["rules"] if r["kind"] == "forbid")
    assert edited["source"] == "person" and edited["count"] == 4 and edited["reason"] == "Our port names are one word"
    assert not any(r["id"] == digits["id"] for r in t["rules"])
    # delete the bot's other rule
    t = sx.api.delete(f"{url}/rule/{sym['id']}").json()
    assert t["removed"] == 2 and not any(r["id"] == sym["id"] for r in t["rules"])
    # ask the bot again: neither old rule comes back, the edited one stays
    t = sx.api.post(f"{url}/rules", json={"ai": True}).json()
    ids = {r["id"] for r in t["rules"]}
    assert digits["id"] not in ids and sym["id"] not in ids and edited["id"] in ids
    assert sx.api.put(f"{url}/rule/{edited['id']}", json={"column": "CONTACT_EMAIL", "kind": "trimmed"}).status_code == 422  # sensitive
    assert sx.api.put(f"{url}/rule/nope", json={"column": "PORT_NAME", "kind": "trimmed"}).status_code == 404


def test_meanings_from_the_names_alone():
    assert V.meaning_from_name("POL_CD", "text") == "port of loading code"
    assert V.meaning_from_name("cargoWeightKg", "number") == "cargo weight kg (a number)"
    assert V.meaning_from_name("ETA", "date") == "estimated arrival (a date)"
