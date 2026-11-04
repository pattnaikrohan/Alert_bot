"""The rules bot: decides what a column's values should look like, from names only.

It has one job and nothing else: given a table's name and its columns' names and types, propose rules from a fixed set
(no digits, a UN/LOCODE, a latitude between -90 and 90…). It is never given a value, a row, a count or a tool; it can't
run anything, and what it says is only ever parsed into dq.suspicious.ValueRule (anything outside that set is dropped).
The bot's code, not the model, turns a rule into SQL.
"""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import ValidationError

from ..dq.suspicious import FORMATS, ColType, ValueRule
from . import llm, usage

SYSTEM = """You are AlertBot's data-rules bot. You have exactly one job: from a table's name and its columns' names and types, \
propose rules about what a REAL value in each column must look like, so that obviously wrong values (like a port name with a \
digit in it) can be found. You never see data, counts or examples, and you never ask for them. You don't write SQL, \
don't answer questions, don't do anything else.

Answer with JSON only: {"rules": [{"column": "<exact column name>", "kind": "<kind>", <parameters>, "reason": "<one short plain sentence>", \
"confidence": "high|medium|low"}]}

Kinds (only these):
- no_digits (text): the value never contains a digit. Names of ports, terminals, countries, cities, regions, carriers.
- no_symbols (text): never contains @ # $ % ^ * = + < > ? ! ~ | { } _ . Names of places and companies.
- trimmed (text): no spaces at the start or end.
- uppercase (text): always in capitals. Codes, only when the name says code.
- placeholder (text): isn't a placeholder like N/A, NULL, TBD, -, TEST.
- length (text): "min" and/or "max" characters.
- format (text): "format" is one of: FORMATS.
- range (number): "min" and/or "max". Latitude -90..90, longitude -180..180, percentages 0..100, counts and amounts that can't be negative (min 0).
- date_sane (date): "min_year" (earliest believable year) and/or "future_days" (how far ahead a date may be: 1 for when a row was \
created or loaded, a few years for planned dates like an ETA).
- pattern (text): "shapes": a list of 1 to 6 shapes the WHOLE value must match one of. In a shape, {L} is a letter, {D} a digit, \
{A} a letter or digit, each with an optional count ({L2}, {D7}, {A3}); any other letter, digit, space or - / . : stands for itself. \
Examples: UN/LOCODE ["{L2}{A3}", "{L2} {A3}"], IMO number ["{D7}", "IMO{D7}"], container number ["{L4}{D7}"], ISO date as text \
["{D4}-{D2}-{D2}"], a 3-letter code ["{L3}"]. Use it for any code, identifier or reference whose name tells you its shape.
- forbid (text): "forbid": characters that must never appear, any of "digits", "letters", "symbols", "spaces". E.g. a code \
never has spaces; a numeric reference stored as text never has letters.
- allowed_values (text): "values": the complete list of values a correct row can have (compared in capitals, trimmed). Only \
when the name makes the full list obvious (a yes/no flag; a direction like IMPORT/EXPORT; a well-known short code set). Use \
"low" if the list might be incomplete.
- lowercase (text): always in lower case (language codes like en, de; when the name says so).

Be careful, a wrong rule floods people with false alarms:
- Some names DO contain digits: vessel names (MSC ANNA 2), addresses, berth or gate names (Berth 12), voyage and flight numbers, \
product codes. Don't add no_digits there.
- Free text (titles, descriptions, bodies, comments, notes), URLs, links, file paths and JSON naturally contain symbols and digits: \
never no_symbols or no_digits there. Dates, times, ids, scores and numbers stored as text contain digits and - or : : never \
no_digits or no_symbols there.
- Only propose a rule you're sure every correct value obeys. Fewer, sure rules beat many guesses. Use "low" when unsure.
- Never propose rules for personal data (people's names, emails, phones, ids of people) or secrets.
- Look at EVERY column, whatever its name, in the context of the table's name: what is this table, what does this column hold, \
what would a correct value look like? Propose a rule wherever you are sure; skip a column when nothing is certain.
- If "propose_for" is given, propose rules only for those columns (they are new in the table); the others are context.
- Use only the listed columns, with exactly their names. At most 3 rules per column and 40 in total.

Also say what you understood each column to hold: add "meanings": {"<column>": "<a few plain words, e.g. the name of the port where the vessel calls>"} for every column you judge (all of them, or the propose_for ones). If the input has "meanings", a \
person who knows this table wrote them: trust them over the names, and base the rules on them."""


class Unavailable(RuntimeError):
    """No usable answer (no AI, or nothing it said fitted)."""


def ask(table: str, columns: list[tuple[str, ColType]], only: list[str] | None = None,
        meanings: dict[str, str] | None = None) -> tuple[list[ValueRule], str, dict[str, str]]:
    """Proposed rules for one table (not yet checked against it: dq.suspicious.clean does that), the model, and what
    the bot understood each column to mean. `meanings` are people's own words for columns: the bot is told to trust them."""
    if not columns:
        raise Unavailable("no columns to judge")
    system = SYSTEM.replace("FORMATS", ", ".join(f"{k} ({v[0]})" for k, v in FORMATS.items()))
    payload: dict[str, Any] = {"table": table, "columns": [{"name": c, "type": t} for c, t in columns[:200]]}
    if only:
        payload["propose_for"] = only[:100]
    known = {c for c, _ in columns}
    if meanings:
        payload["meanings"] = {c: m for c, m in meanings.items() if c in known}
    try:
        with usage.tagged(feature="rules"):
            data, res = llm.client().chat_json(system, json.dumps(payload), max_tokens=3500)
    except llm.AIUnavailable as exc:
        raise Unavailable(str(exc)[:120]) from exc
    except Exception as exc:  # noqa: BLE001 - an unreadable answer is the same as none
        raise Unavailable(type(exc).__name__) from exc
    rules = parse(data)
    found = parse_meanings(data, known)
    if not rules and not found:
        raise Unavailable("its answer had no usable rule")
    return rules, res.model, found


def parse_meanings(data: Any, known: set[str]) -> dict[str, str]:
    """{column: what the bot thinks it holds}, for real columns only, as short plain text."""
    raw = data.get("meanings") if isinstance(data, dict) else None
    out: dict[str, str] = {}
    for c, m in (raw.items() if isinstance(raw, dict) else []):
        if c in known and isinstance(m, str) and (text := clean_meaning(m)):
            out[c] = text
    return out


def clean_meaning(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[\x00-\x1f\x7f]", " ", text)).strip()[:200]


def parse(data: Any) -> list[ValueRule]:
    """Whatever the model said, as ValueRules; anything that isn't one is dropped."""
    items = data.get("rules") if isinstance(data, dict) else None
    out = []
    for x in items if isinstance(items, list) else []:
        if not isinstance(x, dict):
            continue
        fields = {k: x.get(k) for k in ("column", "kind", "min", "max", "format", "min_year", "future_days", "shapes", "reason", "confidence")
                  if x.get(k) is not None}
        if x.get("forbid") is not None:
            fields["chars"] = x["forbid"]
        if x.get("values") is not None:
            fields["allowed"] = x["values"]
        if isinstance(fields.get("reason"), str):
            fields["reason"] = fields["reason"][:240]
        try:
            out.append(ValueRule(**fields, source="ai"))
        except (ValidationError, TypeError):
            continue
    return out[:60]
