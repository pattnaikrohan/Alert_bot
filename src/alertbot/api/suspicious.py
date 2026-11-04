"""Suspicious values in the tables a project checks: the rules (from the rules bot, the names, or a person), how many
rows break each one, and (for the person who connected the database only) the values themselves.

    GET    /api/projects/{key}/suspicious                              every checked table: rules, counts
    POST   /api/projects/{key}/suspicious/{mapping}/rules              ask the rules bot again (people's own rules and choices stay)
    POST   /api/projects/{key}/suspicious/{mapping}/rule               add a rule
    PATCH  /api/projects/{key}/suspicious/{mapping}/rule/{rule}        switch a rule on or off
    PUT    /api/projects/{key}/suspicious/{mapping}/rule/{rule}        change a rule (it becomes the team's own)
    DELETE /api/projects/{key}/suspicious/{mapping}/rule/{rule}        remove a rule (a bot's rule is never suggested again)
    PUT    /api/projects/{key}/suspicious/{mapping}/meaning            what a column means, in the team's words (remembered)
    POST   /api/projects/{key}/suspicious/{mapping}/rethink            the bot thinks again about one column's rules
    POST   /api/projects/{key}/suspicious/{mapping}/check              count now
    GET    /api/projects/{key}/suspicious/{mapping}/rule/{rule}/values the values breaking it (database owner only; never kept)
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Path, Query
from pydantic import BaseModel, Field
from sqlalchemy import select

from ..db import DqMapping
from ..dq import suspicious as V
from ..dq.client import DbError
from ..dq.privacy import RawDataRejected
from ..dq.service import PlanError, checked, client_of, settings_of
from ..dq.vault import VaultError
from ..timeutil import iso, utcnow
from .auth import AccessDep, SessionDep

router = APIRouter()
Mid = Annotated[int, Path()]


def _mapping(access: AccessDep, session: SessionDep, mapping_id: int) -> DqMapping:
    m = session.get(DqMapping, mapping_id)
    if m is None or m.project_id != access.project.id:
        raise HTTPException(404, "table not found")
    return m


def _fail(exc: Exception) -> HTTPException:
    if isinstance(exc, PlanError):
        return HTTPException(422, str(exc))
    if isinstance(exc, (DbError, VaultError, RawDataRejected)):
        return HTTPException(502, str(exc))
    return HTTPException(502, "the database couldn't be read just now")


def _view(session: SessionDep, m: DqMapping, me: int) -> dict:
    rs, cs = V.rules_of(session, m.id), V.counts_of(session, m.id)
    counts = cs.get("counts") or {}
    rules = []
    for x in rs.get("rules") or []:
        r = V.ValueRule.model_validate(x)
        c = counts.get(r.id) or {}
        rules.append({**r.model_dump(), "label": V.label(r), "count": c.get("all"), "count_today": c.get("today")})
    rules.sort(key=lambda r: (not r["enabled"], -(r["count"] or 0), r["column"].lower(), r["kind"]))
    live = [r for r in rules if r["enabled"]]
    return {
        "id": m.id, "schema": m.schema_name, "table": m.table_name, "pipeline": m.pipeline, "database": m.connection.database,
        "connection": m.connection.name, "kind": m.connection.kind, "checked": checked(m), "owner": m.connection.user_id == me,
        "sensitive": settings_of(m).sensitive, "rules": rules, "generated_at": rs.get("generated_at"), "by": rs.get("by"),
        "model": rs.get("model"), "note": rs.get("note"), "dropped": rs.get("dropped") or [],
        "change_note": rs.get("change_note"), "changes": rs.get("changes") or [], "columns_at": rs.get("columns_at"),
        "checked_at": cs.get("checked_at"), "rows": cs.get("rows"), "rows_today": cs.get("rows_today"), "date_column": cs.get("date_column"),
        "columns": {c: t for c, t in (cs.get("columns") or {}).items() if c not in settings_of(m).sensitive and not V.PERSONAL.search(c)},
        "error": cs.get("error"), "suspicious": sum(r["count"] or 0 for r in live), "suspicious_today": sum(r["count_today"] or 0 for r in live),
        "rules_hit": sum(1 for r in live if r["count"]),
        "meanings": rs.get("meanings") or {}, "removed": len(rs.get("removed") or []), "column_note": rs.get("column_note"),
    }


@router.get("/api/projects/{key}/suspicious")
def suspicious_list(access: AccessDep, session: SessionDep) -> dict:
    maps = [m for m in session.scalars(select(DqMapping).where(DqMapping.project_id == access.project.id).order_by(DqMapping.table_name))
            if m.retired_at is None]
    tables = [_view(session, m, access.user.id) for m in maps]
    tables.sort(key=lambda t: (-t["suspicious"], t["table"].lower()))
    return {"tables": tables, "kinds": {k: list(v) for k, v in V.FITS.items()},
            "formats": {k: v[0] for k, v in V.FORMATS.items()}, "max_values": V.MAX_VALUES}


class GenIn(BaseModel):
    ai: bool = True


@router.post("/api/projects/{key}/suspicious/{mapping_id}/rules")
def suspicious_generate(mapping_id: Mid, body: GenIn, access: AccessDep, session: SessionDep) -> dict:
    """Ask the rules bot (names only) for this table's rules, then count what breaks them."""
    m = _mapping(access, session, mapping_id)
    prev = V.rules_of(session, m.id)
    session.commit()  # nothing held while the bot and the database answer
    now = utcnow()
    try:
        client = client_of(m.connection)
        with _tagged(access):
            rules, by, model, note, dropped, found = V.generate(client, m, use_ai=body.ai, meanings=V.person_meanings(prev))
        state = V.set_meanings(V.merge_rules(prev, rules, by, model, note, now) | {"dropped": dropped[:20]}, found, by, now)
        counts = V.check(client, m, V.live_rules(state), now)
        state, _ = V.auto_off(state, counts)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc
    V.save_rules(session, m.id, state)
    V.save_counts(session, m.id, counts)
    return _view(session, m, access.user.id)


def _tagged(access: AccessDep) -> Any:  # the AI calls made for this project and person (AI usage)
    from ..ai import usage

    return usage.tagged(project=access.project.key, user_id=access.user.id, feature="rules")


class RuleIn(V.ValueRule):
    pass


@router.post("/api/projects/{key}/suspicious/{mapping_id}/rule")
def suspicious_add(mapping_id: Mid, body: RuleIn, access: AccessDep, session: SessionDep) -> dict:
    m = _mapping(access, session, mapping_id)
    try:
        cols, _ = V.table_shape(client_of(m.connection), m)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc
    rule = V.ValueRule.model_validate({**body.model_dump(), "source": "person", "enabled": True,
                                       "reason": body.reason or f"Added by {access.user.name}"})
    rules, dropped = V.clean([rule], cols, set(settings_of(m).sensitive))
    if not rules:
        raise HTTPException(422, dropped[0] if dropped else "that rule doesn't fit this table")
    state = V.rules_of(session, m.id)
    have = {x["id"] for x in state.get("rules") or []}
    if rules[0].id in have:
        raise HTTPException(409, "this table already has that rule")
    state["rules"] = [*(state.get("rules") or []), rules[0].model_dump()]
    state.setdefault("generated_at", iso(utcnow()))
    state.setdefault("by", "person")
    V.save_rules(session, m.id, state)
    session.commit()  # the new rule is kept; then counted straight away (nothing held meanwhile)
    try:
        V.save_counts(session, m.id, V.check(client_of(m.connection), m, V.live_rules(state), utcnow()))
    except Exception as exc:  # noqa: BLE001 - the rule stays; the counts come with the next check
        raise _fail(exc) from exc
    return _view(session, m, access.user.id)


class ToggleIn(BaseModel):
    enabled: bool


@router.patch("/api/projects/{key}/suspicious/{mapping_id}/rule/{rule_id}")
def suspicious_toggle(mapping_id: Mid, rule_id: Annotated[str, Path(max_length=20)], body: ToggleIn, access: AccessDep, session: SessionDep) -> dict:
    m = _mapping(access, session, mapping_id)
    state = V.rules_of(session, m.id)
    rules = state.get("rules") or []
    hit = next((x for x in rules if x["id"] == rule_id), None)
    if hit is None:
        raise HTTPException(404, "rule not found")
    hit["enabled"] = body.enabled
    hit["kept_on"] = body.enabled  # a person's choice: the safety net leaves it alone after this
    V.save_rules(session, m.id, {**state, "rules": rules})
    return _view(session, m, access.user.id)


@router.delete("/api/projects/{key}/suspicious/{mapping_id}/rule/{rule_id}")
def suspicious_delete(mapping_id: Mid, rule_id: Annotated[str, Path(max_length=20)], access: AccessDep, session: SessionDep) -> dict:
    m = _mapping(access, session, mapping_id)
    state = V.rules_of(session, m.id)
    hit = next((x for x in state.get("rules") or [] if x["id"] == rule_id), None)
    if hit is None:
        raise HTTPException(404, "rule not found")
    rules = [x for x in state["rules"] if x["id"] != rule_id]
    removed = list(state.get("removed") or [])
    if hit.get("source") != "person":  # the bot won't suggest it again
        removed = [*removed, rule_id][-500:]
    V.save_rules(session, m.id, {**state, "rules": rules, "removed": removed})
    return _view(session, m, access.user.id)


@router.put("/api/projects/{key}/suspicious/{mapping_id}/rule/{rule_id}")
def suspicious_edit(mapping_id: Mid, rule_id: Annotated[str, Path(max_length=20)], body: RuleIn, access: AccessDep, session: SessionDep) -> dict:
    """Change a rule (the bot's or anyone's): it becomes the team's own, the bot won't bring its old version back, and it
    is counted straight away."""
    m = _mapping(access, session, mapping_id)
    state = V.rules_of(session, m.id)
    old = next((x for x in state.get("rules") or [] if x["id"] == rule_id), None)
    if old is None:
        raise HTTPException(404, "rule not found")
    try:
        cols, _ = V.table_shape(client_of(m.connection), m)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc
    rule = V.ValueRule.model_validate({**body.model_dump(), "source": "person", "enabled": True, "kept_on": True,
                                       "reason": body.reason or f"Changed by {access.user.name}"})
    fixed, dropped = V.clean([rule], cols, set(settings_of(m).sensitive))
    if not fixed:
        raise HTTPException(422, dropped[0] if dropped else "that rule doesn't fit this table")
    new = fixed[0]
    if new.id != rule_id and any(x["id"] == new.id for x in state.get("rules") or []):
        raise HTTPException(409, "this table already has that rule")
    rules = [new.model_dump() if x["id"] == rule_id else x for x in state["rules"]]
    removed = list(state.get("removed") or [])
    if old.get("source") != "person" and new.id != rule_id:
        removed = [*removed, rule_id][-500:]
    state = {**state, "rules": rules, "removed": removed}
    V.save_rules(session, m.id, state)
    session.commit()
    try:
        V.save_counts(session, m.id, V.check(client_of(m.connection), m, V.live_rules(state), utcnow()))
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc
    return _view(session, m, access.user.id)


class MeaningIn(BaseModel):
    column: str = Field(min_length=1, max_length=128)
    text: str | None = Field(default=None, max_length=200)  # None or empty: back to the bot's own meaning
    rethink: bool = True  # ask the bot for this column's rules again, with this meaning


class RethinkIn(BaseModel):
    column: str = Field(min_length=1, max_length=128)


def _rethink(session: SessionDep, access: AccessDep, m: DqMapping, state: dict, column: str) -> dict:
    """Ask the bot again about one column (with the team's meanings as facts), replace its rules there, count them."""
    session.commit()
    now = utcnow()
    try:
        client = client_of(m.connection)
        cols, _ = V.table_shape(client, m)
        if column not in cols:
            raise HTTPException(404, f"{column} isn't a column of {m.table_name} any more")
        with _tagged(access):
            fresh, by, _model, note, _dropped, found = V.generate(client, m, only=[column], cols=cols, meanings=V.person_meanings(state))
        state = V.set_meanings(V.replace_column(state, column, fresh), found, by, now)
        state = {**state, "column_note": {"column": column, "by": by, "rules": len(fresh), "at": iso(now), "note": note}}
        counts = V.check(client, m, V.live_rules(state), now)
        state, _ = V.auto_off(state, counts)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc
    V.save_rules(session, m.id, state)
    V.save_counts(session, m.id, counts)
    return _view(session, m, access.user.id)


@router.put("/api/projects/{key}/suspicious/{mapping_id}/meaning")
def suspicious_meaning(mapping_id: Mid, body: MeaningIn, access: AccessDep, session: SessionDep) -> dict:
    """Say what a column means (remembered, and given to the bot as a fact), or go back to the bot's own reading."""
    from ..ai.value_rules import clean_meaning

    m = _mapping(access, session, mapping_id)
    if body.column in set(settings_of(m).sensitive) or V.PERSONAL.search(body.column):
        raise HTTPException(422, f"{body.column} is personal or sensitive: never checked")
    state = V.rules_of(session, m.id)
    text = clean_meaning(body.text or "")
    meanings = dict(state.get("meanings") or {})
    if text:
        state = V.set_meanings(state, {body.column: text}, "person", utcnow(), who=access.user.name)
    else:
        meanings.pop(body.column, None)
        state = {**state, "meanings": meanings}
    if body.rethink:
        return _rethink(session, access, m, state, body.column)
    V.save_rules(session, m.id, state)
    return _view(session, m, access.user.id)


@router.post("/api/projects/{key}/suspicious/{mapping_id}/rethink")
def suspicious_rethink(mapping_id: Mid, body: RethinkIn, access: AccessDep, session: SessionDep) -> dict:
    m = _mapping(access, session, mapping_id)
    return _rethink(session, access, m, V.rules_of(session, m.id), body.column)


@router.post("/api/projects/{key}/suspicious/{mapping_id}/check")
def suspicious_check(mapping_id: Mid, access: AccessDep, session: SessionDep) -> dict:
    m = _mapping(access, session, mapping_id)
    rules = V.live_rules(V.rules_of(session, m.id))
    session.commit()
    try:
        counts = V.check(client_of(m.connection), m, rules, utcnow())
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc
    state, off = V.auto_off(V.rules_of(session, m.id), counts)
    if off:
        V.save_rules(session, m.id, state)
    V.save_counts(session, m.id, counts)
    return _view(session, m, access.user.id)


@router.get("/api/projects/{key}/suspicious/{mapping_id}/rule/{rule_id}/values")
def suspicious_values(mapping_id: Mid, rule_id: Annotated[str, Path(max_length=20)], access: AccessDep, session: SessionDep,
                      scope: Annotated[Literal["all", "today"], Query()] = "all") -> dict:
    """The values themselves: only for the person who connected this database (their login reads them), fetched now,
    returned to them, and kept nowhere."""
    m = _mapping(access, session, mapping_id)
    if m.connection.user_id != access.user.id:
        raise HTTPException(403, f"only the person who connected {m.connection.name} can see its values; everyone sees the counts")
    hit = next((V.ValueRule.model_validate(x) for x in V.rules_of(session, m.id).get("rules") or [] if x["id"] == rule_id), None)
    if hit is None:
        raise HTTPException(404, "rule not found")
    session.commit()
    try:
        vals = V.values(client_of(m.connection), m, hit, scope, utcnow())
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc
    return {"rule": {**hit.model_dump(), "label": V.label(hit)}, "scope": scope, "values": vals, "limit": V.MAX_VALUES,
            "note": "Fetched just now for you only. AlertBot doesn't keep, log, alert on, or send these values to AI."}
