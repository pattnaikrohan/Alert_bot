"""Pasted connection strings: every common SQL Server and Snowflake form becomes a connection's parts; the bot's own
safety settings still apply; nothing it can't honour is silently accepted; and the password is never echoed back."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from test_adf import adf_ws  # noqa: F401 - adf_ws is a fixture

from alertbot.dq.connstr import ConnStringError, parse

PW = "p@ss;w0rd}x"


@pytest.mark.parametrize("s", [
    "Server=tcp:203.0.113.10,1433;Initial Catalog=TeamTesseract;Persist Security Info=False;User ID=jnana;"
    "Password={p@ss;w0rd}}x};MultipleActiveResultSets=False;Encrypt=True;TrustServerCertificate=False;Connection Timeout=30;",
    'Data Source=203.0.113.10,1433;Database=TeamTesseract;UID=jnana;PWD="p@ss;w0rd}x";Encrypt=yes',
    "Driver={ODBC Driver 18 for SQL Server};Server=203.0.113.10;Port=1433;Database=TeamTesseract;Uid=jnana;Pwd={p@ss;w0rd}}x};",
    "jdbc:sqlserver://203.0.113.10:1433;databaseName=TeamTesseract;user=jnana;password={p@ss;w0rd}}x};encrypt=true;trustServerCertificate=false",
    "mssql+pyodbc://jnana:p%40ss%3Bw0rd%7Dx@203.0.113.10:1433/TeamTesseract?driver=ODBC+Driver+18+for+SQL+Server&encrypt=yes",
    "sqlserver://jnana:p%40ss%3Bw0rd%7Dx@203.0.113.10:1433/TeamTesseract",
])
def test_sql_server_forms(s):
    p = parse(s)
    assert (p.kind, p.host, p.database, p.username, p.secret) == ("sqlserver", "203.0.113.10", "TeamTesseract", "jnana", PW)
    assert p.port in (1433, None) and p.options["encrypt"] is True and p.options["trust_server_certificate"] is False
    assert PW not in repr(p) and "secret" not in p.preview() and p.preview()["has_password"]


def test_sql_server_details():
    p = parse(r"Server=SQLVM01\REPORTING;Database=Sales;User Id=bot;Password=x;TrustServerCertificate=True;Encrypt=Optional;Foo=1")
    assert p.host == "SQLVM01\\REPORTING" and p.port is None
    assert p.options == {"encrypt": False, "trust_server_certificate": True} and p.ignored == ["foo"]
    assert parse("Server=(local);Database=d;User ID=u;Password=x").host == "localhost"
    assert parse("jdbc:sqlserver://host;instanceName=INST;databaseName=d;user=u;password=x").host == "host\\INST"


@pytest.mark.parametrize("s", [
    "snowflake://bot:secret%21@myorg-acct/DB_PROD/RAW?warehouse=COMPUTE_WH&role=ANALYST",
    "jdbc:snowflake://myorg-acct.snowflakecomputing.com/?user=bot&password=secret!&db=DB_PROD&warehouse=COMPUTE_WH&role=ANALYST",
    "account=myorg-acct;user=bot;password=secret!;db=DB_PROD;warehouse=COMPUTE_WH;role=ANALYST;schema=RAW",
])
def test_snowflake_forms(s):
    p = parse(s)
    assert (p.kind, p.host, p.database, p.username, p.secret, p.auth) == ("snowflake", "myorg-acct", "DB_PROD", "bot", "secret!", "password")
    assert p.options == {"warehouse": "COMPUTE_WH", "role": "ANALYST"}


@pytest.mark.parametrize("s, says", [
    ("Server=h;Database=d;Trusted_Connection=True", "Windows"),
    ("Server=h;Database=d;Integrated Security=SSPI", "Windows"),
    ("Server=h.database.windows.net;Database=d;Authentication=Active Directory Password;User ID=u;Password=SECRETPW", "Microsoft Entra"),
    ("Server=np:\\\\.\\pipe\\sql;Database=d;User ID=u;Password=SECRETPW", "named pipes"),
    ("Database=d;User ID=u;Password=SECRETPW", "server"),
    ("Server=h;User ID=u;Password=SECRETPW", "database"),
    ("Server=h,99999;Database=d;User ID=u;Password=SECRETPW", "port"),
    ("Server=h;Database=d;Password={SECRETPW", "never closes"),
    ("account=a;user=u;password=SECRETPW;db=d;authenticator=externalbrowser", "Snowflake sign-in"),
    ("jdbc:postgresql://h/d?password=SECRETPW", "only SQL Server and Snowflake"),
])
def test_what_cant_be_honoured_is_refused_without_echoing_it(s, says):
    with pytest.raises(ConnStringError) as e:
        parse(s)
    assert says in str(e.value) and "SECRETPW" not in str(e.value)


def test_api_preview_and_connect_never_echo_the_password(adf_ws, monkeypatch):  # noqa: F811
    from alertbot.api import databases
    from alertbot.api.main import app

    api = TestClient(app, headers={"Authorization": "Bearer dev:chinmoy@contoso.com"})
    s = "Server=tcp:203.0.113.10,1433;Initial Catalog=TeamTesseract;User ID=jnana;Password=VERY-SECRET-PW;Encrypt=True;Bogus=1"
    r = api.post("/api/databases/parse", json={"connection_string": s})
    assert r.status_code == 200 and "VERY-SECRET-PW" not in r.text
    assert r.json()["host"] == "203.0.113.10" and r.json()["has_password"] and r.json()["ignored"] == ["bogus"]

    bad = api.post("/api/databases/parse", json={"connection_string": "Server=h;Database=d;Trusted_Connection=yes;Password=VERY-SECRET-PW"})
    assert bad.status_code == 422 and "Windows" in bad.json()["detail"] and "VERY-SECRET-PW" not in bad.text

    # a request that fails validation says why, without echoing the body (which can hold a password)
    invalid = api.post("/api/databases", json={"kind": "sqlserver", "host": "bad host!", "database": "d", "auth": "sql",
                                              "username": "u", "secret": "VERY-SECRET-PW"})
    assert invalid.status_code == 422 and "host" in invalid.json()["detail"] and "VERY-SECRET-PW" not in invalid.text

    # connecting from a string: parsed, tested (here: refused by the server), never echoed, nothing kept on failure
    monkeypatch.setattr(databases, "test_connection", lambda session, c: {"ok": False, "error": "The database refused the sign-in.", "can_write": None})
    r = api.post("/api/databases/from-string", json={"connection_string": s, "name": "TT"})
    assert r.status_code == 400 and "refused" in r.text and "VERY-SECRET-PW" not in r.text
    assert api.get("/api/databases").json()["connections"] == []
