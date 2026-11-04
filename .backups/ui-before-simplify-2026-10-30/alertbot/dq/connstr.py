"""Connection strings people paste, turned into a connection's parts (the string itself is never stored).

Understood:
    SQL Server   ADO.NET / ODBC     Server=tcp:host,1433;Initial Catalog=db;User ID=u;Password=p;Encrypt=True;…
                 JDBC               jdbc:sqlserver://host:1433;databaseName=db;user=u;password=p;encrypt=true
                 URL                mssql://u:p@host:1433/db?encrypt=true   (also sqlserver://, mssql+pyodbc://)
    Snowflake    URL                snowflake://u:p@account/db/schema?warehouse=WH&role=R
                 JDBC               jdbc:snowflake://account.snowflakecomputing.com/?user=u&password=p&db=DB&warehouse=WH
                 key=value          account=acc;user=u;password=p;db=DB;warehouse=WH;role=R

Only what the bot needs is kept: server, port, database, user, password, encryption and certificate trust, warehouse and
role. The bot's own safety settings (read-only intent, time limits) always apply, whatever the string says; other
options are ignored and listed back. Errors name keys only, never values (the string holds a password).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qsl, unquote, urlsplit


class ConnStringError(ValueError):
    """Wording safe to show: never contains any part of the string."""


@dataclass
class Parsed:
    kind: str
    host: str
    database: str
    port: int | None = None
    username: str = ""
    secret: str = field(default="", repr=False)
    auth: str = "sql"
    options: dict[str, Any] = field(default_factory=dict)
    ignored: list[str] = field(default_factory=list)

    def __repr__(self) -> str:
        return f"Parsed(kind={self.kind}, host={self.host}, database={self.database}, username={self.username})"

    def fields(self) -> dict[str, Any]:
        return {"kind": self.kind, "host": self.host, "port": self.port, "database": self.database, "auth": self.auth,
                "username": self.username, "secret": self.secret, "options": self.options}

    def preview(self) -> dict[str, Any]:
        """What the portal may show back: everything except the password."""
        return {k: v for k, v in self.fields().items() if k != "secret"} | {"has_password": bool(self.secret), "ignored": self.ignored}


# ── key=value strings (ADO.NET, ODBC, Snowflake's .NET / ODBC form) ──


def pairs(s: str) -> list[tuple[str, str]]:
    """Split `k=v;k2={v;2};k3="v3"` respecting {…} and quoted values (with doubled escapes)."""
    out: list[tuple[str, str]] = []
    i, n = 0, len(s)
    while i < n:
        while i < n and s[i] in "; \t\r\n":
            i += 1
        if i >= n:
            break
        eq = s.find("=", i)
        if eq < 0:
            raise ConnStringError("part of the connection string isn't in key=value form")
        key = s[i:eq].strip().lower()
        i = eq + 1
        while i < n and s[i] in " \t":
            i += 1
        if i < n and s[i] in "{\"'":
            close = {"{": "}", '"': '"', "'": "'"}[s[i]]
            j, buf = i + 1, []
            while j < n:
                if s[j] == close:
                    if j + 1 < n and s[j + 1] == close:  # doubled = escaped
                        buf.append(close)
                        j += 2
                        continue
                    break
                buf.append(s[j])
                j += 1
            else:
                raise ConnStringError(f"the value of '{key}' opens a quote or brace that never closes")
            value, i = "".join(buf), j + 1
            while i < n and s[i] not in ";":
                i += 1
        else:
            j = s.find(";", i)
            j = n if j < 0 else j
            value, i = s[i:j].strip(), j
        if key:
            out.append((key, value))
    return out


_SQL = {
    "server": "host", "data source": "host", "address": "host", "addr": "host", "network address": "host", "servername": "host",
    "database": "database", "initial catalog": "database", "databasename": "database",
    "user id": "username", "uid": "username", "user": "username", "username": "username", "userid": "username",
    "password": "secret", "pwd": "secret",
    "encrypt": "encrypt", "trustservercertificate": "trust", "trust server certificate": "trust",
    "port": "port", "portnumber": "port", "instancename": "instance",
    "authentication": "authentication", "trusted_connection": "trusted", "integrated security": "trusted", "integratedsecurity": "trusted",
}
_SQL_HARMLESS = {
    "connection timeout", "connect timeout", "timeout", "logintimeout", "application name", "app", "applicationname", "applicationintent",
    "multipleactiveresultsets", "persist security info", "persistsecurityinfo", "pooling", "max pool size", "min pool size",
    "multisubnetfailover", "packet size", "packetsize", "driver", "provider", "workstation id", "wsid", "language",
    "connectretrycount", "connectretryinterval", "hostnameincertificate", "column encryption setting", "command timeout",
    "transparentnetworkipresolution", "pool blocking period", "load balance timeout", "enlist",
}
_SNOW = {"account": "host", "user": "username", "username": "username", "password": "secret", "db": "database", "database": "database",
         "warehouse": "warehouse", "role": "role", "host": "account_host", "authenticator": "authenticator"}
_SNOW_HARMLESS = {"schema", "port", "protocol", "application", "insecuremode", "login_timeout", "network_timeout", "tracing",
                  "client_session_keep_alive", "ocsp_fail_open"}

_ON = {"true", "yes", "1", "mandatory", "strict"}
_OFF = {"false", "no", "0", "optional"}


def _flag(key: str, v: str) -> bool:
    x = v.strip().lower()
    if x in _ON:
        return True
    if x in _OFF:
        return False
    raise ConnStringError(f"'{key}' should be true or false")


def _port(v: str | int | None) -> int | None:
    if v in (None, ""):
        return None
    try:
        p = int(str(v).strip())
    except ValueError as exc:
        raise ConnStringError("the port isn't a number") from exc
    if not 1 <= p <= 65535:
        raise ConnStringError("the port must be between 1 and 65535")
    return p


def _sql_server(value: str) -> tuple[str, int | None]:
    """`tcp:host,1433` / `host\\instance` / `(local)` → host, port."""
    v = value.strip()
    if re.match(r"^(np|lpc|admin):", v, re.I):
        raise ConnStringError("named pipes / shared memory can't be used from the bot's server; use the server's network name")
    v = re.sub(r"^tcp:", "", v, flags=re.I)
    port = None
    if "," in v:
        v, p = v.rsplit(",", 1)
        port = _port(p)
    if v.lower() in ("(local)", ".", "(localdb)"):
        v = "localhost"
    return v.strip(), port


def _sqlserver_from_pairs(kv: list[tuple[str, str]]) -> Parsed:
    got: dict[str, str] = {}
    ignored: list[str] = []
    for k, v in kv:
        role = _SQL.get(k)
        if role:
            got[role] = v
        elif k not in _SQL_HARMLESS:
            ignored.append(k)
    if got.get("trusted", "").strip().lower() in _ON | {"sspi"}:
        raise ConnStringError("this string signs in with Windows (Trusted_Connection / Integrated Security), which the bot's server can't do "
                              "on your behalf. Use a SQL login (User ID and Password), or 'My Microsoft account' for Azure SQL.")
    authn = got.get("authentication", "").strip().lower().replace(" ", "")
    if authn and authn not in ("sqlpassword", "sqlauthentication"):
        raise ConnStringError("this string uses Microsoft Entra sign-in (Authentication=…). For Azure SQL choose 'My Microsoft account' "
                              "instead; otherwise use a SQL login.")
    if not got.get("host"):
        raise ConnStringError("the string doesn't name a server (Server=… or Data Source=…)")
    host, port = _sql_server(got["host"])
    if got.get("instance") and "\\" not in host:
        host = f"{host}\\{got['instance'].strip()}"
    port = port or _port(got.get("port"))
    if not got.get("database"):
        raise ConnStringError("the string doesn't name a database (Database=… or Initial Catalog=…)")
    options: dict[str, Any] = {"encrypt": True, "trust_server_certificate": False}
    if "encrypt" in got:
        options["encrypt"] = _flag("Encrypt", got["encrypt"])
    if "trust" in got:
        options["trust_server_certificate"] = _flag("TrustServerCertificate", got["trust"])
    return Parsed(kind="sqlserver", host=host, port=port, database=got["database"].strip(), username=got.get("username", "").strip(),
                  secret=got.get("secret", ""), auth="sql", options=options, ignored=ignored)


def _account(host: str) -> str:
    return re.sub(r"\.snowflakecomputing\.com$", "", host.strip().lower().rstrip("/"))


def _snowflake_from_pairs(kv: list[tuple[str, str]]) -> Parsed:
    got: dict[str, str] = {}
    ignored: list[str] = []
    for k, v in kv:
        role = _SNOW.get(k)
        if role:
            got[role] = v
        elif k not in _SNOW_HARMLESS:
            ignored.append(k)
    authn = got.get("authenticator", "").strip().lower()
    if authn and authn != "snowflake":
        raise ConnStringError("this string uses another Snowflake sign-in (authenticator=…). Use a user and password here, or the key-pair option.")
    account = got.get("host") or (got.get("account_host") or "").split(".snowflakecomputing.com")[0]
    if not account:
        raise ConnStringError("the string doesn't name a Snowflake account (account=…)")
    if not got.get("database"):
        raise ConnStringError("the string doesn't name a database (db=…)")
    opts = {k: got[k] for k in ("warehouse", "role") if got.get(k)}
    return Parsed(kind="snowflake", host=_account(account), database=got["database"].strip(), username=got.get("username", "").strip(),
                  secret=got.get("secret", ""), auth="password", options=opts, ignored=ignored)


# ── URL and JDBC forms ──


def _from_url(s: str) -> Parsed:
    u = urlsplit(s)
    scheme = u.scheme.lower()
    query = [(k.lower(), v) for k, v in parse_qsl(u.query, keep_blank_values=True)]
    if scheme.startswith("snowflake"):
        path = [p for p in u.path.split("/") if p]
        kv = [("account", u.hostname or ""), ("user", unquote(u.username or "")), ("password", unquote(u.password or ""))]
        if path:
            kv.append(("db", unquote(path[0])))
        return _snowflake_from_pairs([(k, v) for k, v in kv if v] + query)
    # mssql:// sqlserver:// mssql+pyodbc:// …
    inner = dict(query).get("odbc_connect")
    if inner:
        return _sqlserver_from_pairs(pairs(unquote(inner)))
    kv = [("server", u.hostname or ""), ("user id", unquote(u.username or "")), ("password", unquote(u.password or ""))]
    if u.port:
        kv.append(("port", str(u.port)))
    db = u.path.strip("/")
    if db:
        kv.append(("database", unquote(db)))
    return _sqlserver_from_pairs([(k, v) for k, v in kv if v] + [(k, v) for k, v in query if k != "driver"])


def _from_jdbc_sqlserver(rest: str) -> Parsed:
    """jdbc:sqlserver://host[\\instance][:port][;k=v…]"""
    head, _, tail = rest.partition(";")
    kv = pairs(tail) if tail else []
    host = head.strip().rstrip("/")
    port = None
    m = re.match(r"^(.*):(\d+)$", host)
    if m:
        host, port = m.group(1), m.group(2)
    base = [("server", host)] if host else []
    if port:
        base.append(("port", port))
    return _sqlserver_from_pairs(base + kv)


def _from_jdbc_snowflake(rest: str) -> Parsed:
    u = urlsplit("snowflake://" + rest)
    return _snowflake_from_pairs([("account", u.hostname or "")] + [(k.lower(), v) for k, v in parse_qsl(u.query, keep_blank_values=True)])


def parse(s: str) -> Parsed:
    s = (s or "").strip()
    if not s:
        raise ConnStringError("the connection string is empty")
    if len(s) > 4000:
        raise ConnStringError("the connection string is too long")
    low = s.lower()
    if low.startswith("jdbc:sqlserver://"):
        return _from_jdbc_sqlserver(s[len("jdbc:sqlserver://"):])
    if low.startswith("jdbc:snowflake://"):
        return _from_jdbc_snowflake(s[len("jdbc:snowflake://"):])
    if re.match(r"^(mssql|sqlserver|snowflake)(\+\w+)?://", low):
        return _from_url(s)
    if low.startswith("jdbc:"):
        raise ConnStringError("only SQL Server and Snowflake JDBC strings are supported")
    kv = pairs(s)
    keys = {k for k, _ in kv}
    if "account" in keys and not keys & {"server", "data source", "address", "addr"}:
        return _snowflake_from_pairs(kv)
    return _sqlserver_from_pairs(kv)
