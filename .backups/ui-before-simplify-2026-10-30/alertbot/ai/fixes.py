"""Step-by-step fixes for pipeline errors, that learn from whether they worked.

    diagnose      the run's error → a signature (ErrorCode, or what the message says) → the known fix, with the details
                  of this failure filled in (the row, the step, the column counts, the expression)
    attempts      every set of steps shown is an attempt; the person answers "did it work?"
                     yes → confidence for that fix grows; what they did (if they say) becomes the team's fix
                     no  → they say why; the next approach is chosen from that (the documented alternatives first,
                           then the AI rethinks with what was tried and why it didn't work)
    unknown       errors the library doesn't know: the AI writes steps (marked as not yet proven) until one works

The library is written from Microsoft's own troubleshooting guides and Q&A answers (linked on each fix). The AI only sees
the error text with values scrubbed, the step's name and type, and what was already tried.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

# ── the library ───────────────────────────────────────────────────────────────

MS = "https://learn.microsoft.com"


@dataclass
class Variant:
    key: str
    name: str
    steps: list[dict[str, str]]
    verify: str
    when: str = ""  # words in a "didn't work, because…" that point to this approach


@dataclass
class Known:
    id: str
    title: str
    match: re.Pattern[str]
    meaning: str
    variants: list[Variant]
    docs: list[dict[str, str]] = field(default_factory=list)
    source: str = "Microsoft Learn"


def S(do: str, code: str | None = None) -> dict[str, str]:  # noqa: N802 - a step
    return {"do": do, **({"code": code} if code else {})}


LIBRARY: list[Known] = [
    Known("delimited_more_columns", "A file has more columns than the dataset expects", re.compile(r"DelimitedTextMoreColumnsThanDefined", re.I),
          "Row {row} of a source file has more columns than {expected_text}: a new column upstream, a stray delimiter inside a value, "
          "or files with different layouts in the same folder.",
          [Variant("data", "Fix the row that broke it", [
              S("Open the run in ADF Studio (Monitor → Pipeline runs → {pipeline}) and select the failed step {step_text}. Note the file named in the error."),
              S("Download that file from the storage account and open it in a text editor (not Excel, which hides quotes and delimiters)."),
              S("Go to row {row}. Count its delimiters: it has more columns than {expected_text}."),
              S("If a value contains the delimiter (e.g. a comma inside an address), it must be wrapped in quotes. Fix the file at the source, "
                "or ask the sender to quote text values."),
              S("Upload the corrected file to the same folder and rerun the pipeline from the failed step (Monitor → Rerun from failed activity)."),
           ], "The copy step succeeds and the target table gets today's rows.", "row data value line record"),
           Variant("format", "Check the delimiter, quote and escape settings", [
              S("In ADF Studio, open the source dataset of {step_text} (Author → Datasets)."),
              S("Compare Column delimiter, Row delimiter, Quote character, Escape character and Encoding with a sample of the real file. "
                "A wrong quote character (none instead of \") makes every quoted comma split a value."),
              S("If the error said the expected column count is 1, the compression or format setting doesn't match the files "
                "(e.g. a .gz file read as plain text): set Compression type to match."),
              S("Click Preview data on the dataset: every column should line up. Publish, then rerun."),
           ], "Preview data shows the columns lined up, and the rerun succeeds.", "delimiter quote escape setting format compression preview"),
           Variant("schema", "Files with different layouts in one folder", [
              S("If the source is a folder or a wildcard, list the files it picked up (Monitor → the copy step → Input/Details)."),
              S("Open one file per layout and compare the header rows: a new or extra column means the files don't share one schema."),
              S("Either move the differently shaped files to their own folder (and dataset), or tighten the wildcard file name so only one layout is read."),
              S("If the extra column is expected from now on, add it to the dataset schema (Import schema) and to the copy step's mapping, then publish and rerun."),
           ], "All files in the folder share one header; the rerun succeeds.", "folder files schema header layout wildcard new column")],
          docs=[{"label": "Troubleshoot the delimited text connector", "url": f"{MS}/azure/data-factory/connector-troubleshoot-delimited-text"}]),

    Known("delimited_null_header", "A column in the file has no header name", re.compile(r"DelimitedTextColumnNameNotAllowNull", re.I),
          "With 'First row as header' on, the file's first row has an empty column name.",
          [Variant("header", "Fill in the empty header", [
              S("Open the source file and look at its first row: one column name is empty (e.g. two delimiters in a row, or a trailing delimiter)."),
              S("Fix the header at the source, or remove the trailing delimiter."),
              S("Rerun the pipeline from the failed step."),
           ], "The first row has a name for every column, and the rerun succeeds."),
           Variant("noheader", "Read it without a header row", [
              S("In the source dataset, turn off 'First row as header'."),
              S("Map columns by position (Prop_0, Prop_1, …) in the copy step's Mapping tab, and skip the header row with a Filter or by setting 'Skip line count' to 1."),
              S("Publish and rerun."),
           ], "The copy reads the file and the mapping lands each column in the right place.", "header setting first row position")],
          docs=[{"label": "Troubleshoot the delimited text connector", "url": f"{MS}/azure/data-factory/connector-troubleshoot-delimited-text"}]),

    Known("empty_array_index", "An expression read the first item of an empty list", re.compile(r"cannot be selected from empty array|array index '0'", re.I),
          "{step_text} used an expression like {expression_text}, which takes item [0] from the output of {source_text}. That output was empty "
          "this time (for example, no file matched the filter), so there was no first item.",
          [Variant("guard", "Guard the expression so an empty result doesn't fail", [
              S("In ADF Studio, open {pipeline} and select the step {step_text}; find the expression that ends in .value[0]."),
              S("Wrap it in a check for an empty list, so it only reads [0] when there is something there:",
                "@if(empty(activity('{source}').output.value), '', activity('{source}').output.value[0].name)"),
              S("Better still, put the steps that use the file inside an If Condition with the expression below, so nothing runs when there's no file:",
                "@greater(length(activity('{source}').output.value), 0)"),
              S("Publish, then rerun. When the folder is empty the run now succeeds and simply loads nothing."),
           ], "The pipeline succeeds on a run where nothing matched, and still loads files when they're there.", "expression if empty guard"),
           Variant("why_empty", "Find out why nothing matched", [
              S("Open the failed run in Monitor and select {source_text}: check its Output. ItemsCount is how many it looked at; FilteredItemsCount how many matched."),
              S("If the folder had files but none matched, compare the filter condition (name pattern, date) with the actual file names."),
              S("If the folder was empty, the upstream delivery was late or missing: check with the sender, or move the trigger later."),
              S("Fix the condition or the schedule, publish, and rerun."),
           ], "The filter finds today's file and the run loads it.", "file folder late missing sender filter condition name pattern trigger")],
          docs=[{"label": "Filter activity", "url": f"{MS}/azure/data-factory/control-flow-filter-activity"},
                {"label": "Microsoft Q&A: guard an empty array", "url": f"{MS}/en-us/answers/questions/1521850/filter-activity-failing-when-input-is-empty-in-adf"}],
          source="Microsoft Learn and Microsoft Q&A"),

    Known("json_complex_value", "A nested JSON value can't go into a flat column", re.compile(r"JsonUnsupportedHierarchicalComplexValue", re.I),
          "The copy step found a nested object or array in the JSON and the target column expects a single value.",
          [Variant("to_string", "Map complex values to a string", [
              S("In ADF Studio, open {pipeline} → the copy step {step_text} → Mapping tab."),
              S("Turn on 'Map complex values to string' (advanced mapping settings)."),
              S("Map the nested property to a text column in the target (e.g. NVARCHAR(MAX) in SQL Server, VARIANT or VARCHAR in Snowflake)."),
              S("Publish and rerun. The nested value lands as JSON text you can parse later (OPENJSON / PARSE_JSON)."),
           ], "The copy succeeds and the column holds the JSON text.", "string text column map"),
           Variant("flatten", "Flatten the array instead", [
              S("In the Mapping tab, click Import schemas, then set 'Collection reference' to the array you want one row per item for."),
              S("Map each property of the array items to its own target column."),
              S("For deeper nesting, use a Mapping Data Flow with a Flatten transformation instead of the copy step."),
              S("Publish and rerun."),
           ], "Each array item lands as its own row with its own columns.", "flatten array rows collection reference nested"),
           Variant("skip", "Skip the column (if you don't need it)", [
              S("Remove the nested property from the copy step's mapping, or drop that column from the target."),
              S("Alternatively, turn on fault tolerance ('Skip incompatible rows') with logging, so bad rows are logged rather than failing the run."),
              S("Publish and rerun."),
           ], "The run succeeds; the skipped rows (if any) are listed in the log.", "skip drop don't need")],
          docs=[{"label": "Copy activity schema and type mapping", "url": f"{MS}/azure/data-factory/copy-activity-schema-and-type-mapping"},
                {"label": "Microsoft Q&A: JSON object into one column", "url": f"{MS}/en-gb/answers/questions/49678/how-to-insert-json-object-in-single-column-in-azur"}],
          source="Microsoft Learn and Microsoft Q&A"),

    Known("source_missing", "The source file or blob wasn't there", re.compile(r"UserErrorSourceBlobNotExist|UserErrorFileNotFound|BlobNotFound|PathNotFound|"
                                                                             r"specified blob does not exist|FileNotFound", re.I),
          "The copy step looked for {path_text} and it didn't exist when the run started.",
          [Variant("arrived", "Check the file arrived, then rerun", [
              S("In the storage account (Azure portal → Storage browser), open the container and folder the error names{path_hint} and check the file is there now."),
              S("If it is there now, it arrived after the run started: rerun the pipeline from the failed step."),
              S("If it isn't, ask whoever delivers it (or check the upstream pipeline that writes it) and rerun once it lands."),
           ], "The file exists and the rerun succeeds.", "late arrived delivery upstream now there"),
           Variant("path", "Fix the path the dataset points at", [
              S("Open the source dataset of {step_text} and look at its container, directory and file name, including any parameters or dynamic content."),
              S("Compare them, character by character, with the real path (case matters, and so do dates in the name such as yyyyMMdd)."),
              S("If the name contains a date, check the expression produces today's date in the right timezone and format."),
              S("Fix the dataset or the parameter, Preview data to confirm, publish and rerun."),
           ], "Preview data shows the file, and the rerun succeeds.", "path name folder parameter date wrong case"),
           Variant("wait", "Wait for the file instead of failing", [
              S("Add a Validation activity before the copy step, pointing at the same dataset, with a timeout (e.g. 1 hour) and a sleep interval (e.g. 5 minutes)."),
              S("Or switch the trigger to a Storage event trigger, so the pipeline runs when the file lands."),
              S("Publish. The next late file makes the run wait rather than fail."),
           ], "The next run waits for the file and then loads it.", "timing schedule trigger earlier wait event")],
          docs=[{"label": "Validation activity", "url": f"{MS}/azure/data-factory/control-flow-validation-activity"},
                {"label": "Storage event triggers", "url": f"{MS}/azure/data-factory/how-to-create-event-trigger"}]),

    Known("missing_file_name", "The dataset has no file name", re.compile(r"FormatBasedDatasetMissingFileName", re.I),
          "{step_text} uses a dataset that points at a folder without a file name, where this kind of step needs a single file.",
          [Variant("filename", "Give the dataset a file name", [
              S("Open the dataset used by {step_text} (Author → Datasets) → Connection tab."),
              S("Fill in File name, or use a dataset parameter for it and pass the name from the pipeline (e.g. from a Get Metadata / ForEach item)."),
              S("If the step is a copy with a wildcard, set the wildcard on the copy step's Source tab ('Wildcard paths') instead of on the dataset."),
              S("Publish and rerun."),
           ], "Preview data shows the file, and the run succeeds.", "file name parameter wildcard")],
          docs=[{"label": "Copy activity: source wildcards", "url": f"{MS}/azure/data-factory/connector-azure-blob-storage#copy-activity-properties"}]),

    Known("sql_connect", "The database couldn't be reached or refused the login", re.compile(r"SqlFailedToConnect|Cannot connect to SQL|Login failed|"
                                                                                             r"network-related or instance-specific|SqlFailedToConnect", re.I),
          "The step couldn't open a connection to the database: a firewall, the server name, or the login.",
          [Variant("firewall", "Open the firewall to Data Factory", [
              S("In the Azure portal, open the SQL server → Networking. For Azure SQL, allow Azure services, or add the Data Factory integration runtime's IP range."),
              S("For an on-premises or VM SQL Server, make sure the self-hosted integration runtime machine can reach it (port 1433)."),
              S("In ADF Studio, open the linked service and click Test connection."),
              S("Rerun once the test passes."),
           ], "Test connection succeeds, and so does the rerun.", "firewall network ip port reach"),
           Variant("login", "Fix the login", [
              S("Open the linked service and check the user, the password (or its Key Vault secret) and the database name."),
              S("If the password changed, update the Key Vault secret the linked service uses."),
              S("Click Test connection, then rerun."),
           ], "Test connection succeeds, and so does the rerun.", "login password user credential secret key vault expired")],
          docs=[{"label": "Troubleshoot the SQL Server connector", "url": f"{MS}/azure/data-factory/connector-troubleshoot-sql-server"}]),

    Known("access_denied", "Data Factory isn't allowed to read or write there", re.compile(r"AuthorizationPermissionMismatch|AuthorizationFailure|"
                                                                                           r"AuthenticationFailed|\b403\b|Forbidden|not authorized|AccessDenied", re.I),
          "The identity Data Factory uses doesn't have permission on the storage, database or service the step talks to.",
          [Variant("role", "Grant the role", [
              S("Find which identity the linked service uses: Managed identity (the factory's name), a service principal, or a key."),
              S("For Blob or Data Lake storage, give that identity 'Storage Blob Data Reader' (to read) or 'Storage Blob Data Contributor' "
                "(to write) on the storage account (Access control (IAM) → Add role assignment)."),
              S("Role assignments take a few minutes to apply. Then Test connection on the linked service and rerun."),
           ], "Test connection succeeds, and so does the rerun.", "role permission iam identity"),
           Variant("network", "Check the storage firewall", [
              S("In the storage account → Networking, check whether public access is restricted."),
              S("Allow trusted Microsoft services, or use a managed private endpoint from Data Factory."),
              S("Test connection, then rerun."),
           ], "Test connection succeeds, and so does the rerun.", "firewall network private endpoint")],
          docs=[{"label": "Security and access control troubleshooting", "url": f"{MS}/azure/data-factory/security-and-access-control-troubleshoot-guide"}]),

    Known("column_not_found", "A mapped column no longer exists", re.compile(r"UserErrorInvalidColumnMappingColumnNotFound|ColumnNotFound|Invalid column name", re.I),
          "The copy step's mapping names a column that isn't in the source or the target any more (renamed or dropped).",
          [Variant("remap", "Refresh the mapping", [
              S("Open the copy step {step_text} → Mapping tab and click Import schemas."),
              S("Re-map the renamed column, or remove the mapping for the dropped one."),
              S("Publish and rerun."),
           ], "Every mapped column exists on both sides, and the rerun succeeds.")],
          docs=[{"label": "Copy activity schema and type mapping", "url": f"{MS}/azure/data-factory/copy-activity-schema-and-type-mapping"}]),

    Known("type_conversion", "A value couldn't be converted to the column's type", re.compile(r"TypeConversionFailure|cannot be converted|Conversion failed|"
                                                                                              r"Error converting data type", re.I),
          "A value in the source doesn't fit the target column's type (text in a number column, a date in an unexpected format).",
          [Variant("format", "Tell the copy how to read the value", [
              S("Open the copy step {step_text} → Mapping tab → Type conversion settings."),
              S("Set the date/time format, culture and 'treat boolean as number' to match the source."),
              S("Publish and rerun."),
           ], "The values convert and the rerun succeeds.", "date format culture"),
           Variant("skip", "Log the bad rows instead of failing", [
              S("In the copy step's Settings tab, enable Fault tolerance → 'Skip incompatible rows', with logging to a storage folder."),
              S("Publish and rerun, then review the logged rows and fix them at the source."),
           ], "The run succeeds and the bad rows are in the log.", "skip log rows source data")],
          docs=[{"label": "Copy activity fault tolerance", "url": f"{MS}/azure/data-factory/copy-activity-fault-tolerance"}]),

    Known("transient", "A temporary failure (timeout, throttling or a service hiccup)", re.compile(r"throttl|\b429\b|timed? ?out|timeout|ServiceUnavailable|"
                                                                                                   r"InternalServerError|temporar|transient|try again", re.I),
          "The service on the other side was slow, busy or briefly unavailable.",
          [Variant("retry", "Rerun, and add retries", [
              S("Rerun the pipeline from the failed step: transient errors usually pass the second time."),
              S("On the step that failed, set Retry to 2 or 3 with a Retry interval of 60–120 seconds (General tab)."),
              S("If it's a timeout, raise the step's Timeout, or run it at a quieter time."),
           ], "The rerun succeeds; later blips are absorbed by the retries.", "retry rerun again"),
           Variant("capacity", "Reduce the load", [
              S("Lower the copy step's 'Degree of copy parallelism' and 'Data integration units' so the source or sink isn't overwhelmed."),
              S("Check whether other jobs hit the same database at that time, and stagger the schedules."),
              S("Rerun."),
           ], "The run succeeds without throttling.", "load busy parallel capacity")],
          docs=[{"label": "Activity policy: retries and timeout", "url": f"{MS}/azure/data-factory/concepts-pipelines-activities#activity-policy"}]),

    Known("ir_offline", "The self-hosted integration runtime is offline", re.compile(r"(self-hosted|integration runtime).{0,60}(offline|not available|unavailable|"
                                                                                     r"isn't running|is not running|limited)", re.I),
          "The machine running the self-hosted integration runtime is off, disconnected, or out of capacity.",
          [Variant("restart", "Bring the runtime back", [
              S("In ADF Studio → Manage → Integration runtimes, check the status of the self-hosted runtime and its nodes."),
              S("On the machine, open the Microsoft Integration Runtime Configuration Manager: restart the service if it's stopped."),
              S("Check the machine is on, has network access to *.servicebus.windows.net, and has free CPU and memory."),
              S("Once the status is Running, rerun the pipeline."),
           ], "The runtime shows Running and the rerun succeeds.")],
          docs=[{"label": "Troubleshoot the self-hosted integration runtime", "url": f"{MS}/azure/data-factory/self-hosted-integration-runtime-troubleshoot-guide"}]),

    Known("aml_pipeline", "The Azure Machine Learning pipeline failed", re.compile(r"Azure ?ML pipeline run failed|AzureMLExecutePipeline|Machine Learning", re.I),
          "The step started an Azure Machine Learning pipeline, and that pipeline failed inside Azure ML.",
          [Variant("aml", "Read the failure in Azure ML", [
              S("Open the run in Monitor, select {step_text} and copy the Azure ML run ID from its Output."),
              S("In Azure ML studio → Jobs, open that run and find the failed step: its Outputs + logs (std_log.txt) say why."),
              S("Fix it there (data, compute, or code), then rerun the Data Factory pipeline from the failed step."),
           ], "The Azure ML run completes and the Data Factory step succeeds.")],
          docs=[{"label": "Execute Azure ML pipeline activity", "url": f"{MS}/azure/data-factory/transform-data-machine-learning-service"}]),

    Known("expression", "An expression couldn't be evaluated", re.compile(r"InvalidTemplate|cannot be evaluated|is not valid|Unable to parse expression", re.I),
          "An expression in {step_text} refers to something that isn't there (a missing property, a null, or a wrong type).",
          [Variant("check", "Find the part of the expression that's missing", [
              S("Open the failed run in Monitor and select the step that produced the value the expression reads; check its Output for the property name."),
              S("Fix the property path in the expression (names are case-sensitive), or guard it with coalesce(…, '') for values that can be missing."),
              S("Publish and rerun."),
           ], "The expression evaluates and the rerun succeeds.")],
          docs=[{"label": "Expressions and functions", "url": f"{MS}/azure/data-factory/control-flow-expression-language-functions"}]),
]

ESCALATE = Variant("escalate", "Collect the evidence and escalate", [
    S("Copy the run ID and the failed step's Output and Error (Monitor → the run → the failed step → the output icon)."),
    S("Search Microsoft Q&A for the error code: {qa_link}"),
    S("If it's still unclear, open an Azure support request from the factory (Help + support) and attach the run ID and the error."),
], "Someone with the evidence can take it from here.")

# ── reading an error ──────────────────────────────────────────────────────────

_CODE = re.compile(r"ErrorCode=(\w+)")
_STEP = re.compile(r"Operation on target (.+?) failed", re.S)
_ROW = re.compile(r"row number (\d+)", re.I)
_EXPECTED = re.compile(r"expected column count:?\s*(\d+)", re.I)
_EXPR = re.compile(r"The expression '(.+?)' cannot be evaluated", re.S)
_SRC = re.compile(r"activity\('([^']+)'\)")
_PATH = re.compile(r"(?:path|Path|folderPath|fileName)['\"]?\s*[:=]\s*['\"]?([^,'\"\n]{3,200})")


def context_of(error: str, pipeline: str | None) -> dict[str, Any]:
    code = _CODE.search(error)
    step = _STEP.search(error)
    row = _ROW.search(error)
    exp = _EXPECTED.search(error)
    expr = _EXPR.search(error)
    src = _SRC.findall(expr.group(1) if expr else error)
    path = _PATH.search(error)
    return {"code": code.group(1) if code else None, "pipeline": pipeline or "the pipeline", "step": step.group(1).strip() if step else None,
            "row": row.group(1) if row else "the row named in the error", "expected": exp.group(1) if exp else None,
            "expression": expr.group(1).strip() if expr else None, "source": src[0] if src else "the previous step",
            "path": path.group(1).strip().rstrip(".;:") if path else None}


def signature_of(error: str) -> tuple[str, Known | None]:
    for k in LIBRARY:
        if k.match.search(error or ""):
            return k.id, k
    code = _CODE.search(error or "")
    if code:
        return f"code:{code.group(1)}", None
    first = re.sub(r"'[^']*'|\d+", "…", (error or "").strip().splitlines()[0] if error else "")[:120]
    return f"text:{first}", None


class _Fill(dict):
    def __missing__(self, k: str) -> str:
        return "{" + k + "}"


def render(text: str, ctx: dict[str, Any]) -> str:
    v = _Fill({
        "pipeline": ctx["pipeline"], "step": ctx.get("step") or "the failed step", "source": ctx.get("source") or "the previous step",
        "row": ctx.get("row"), "step_text": f"‘{ctx['step']}’" if ctx.get("step") else "the failed step",
        "source_text": f"‘{ctx['source']}’" if ctx.get("source") and ctx["source"] != "the previous step" else "the step that feeds it",
        "expected_text": f"the {ctx['expected']} the dataset defines" if ctx.get("expected") else "the dataset defines",
        "expression_text": f"‘{ctx['expression'][:120]}’" if ctx.get("expression") else "‘…output.value[0]…’",
        "path_text": f"‘{ctx['path']}’" if ctx.get("path") else "the file",
        "path_hint": f" (‘{ctx['path']}’)" if ctx.get("path") else "",
        "qa_link": f"https://learn.microsoft.com/en-us/answers/search?query={(ctx.get('code') or 'Azure Data Factory error')}",
    })
    try:
        return text.format_map(v)
    except (ValueError, IndexError):
        return text


def steps_of(v: Variant, ctx: dict[str, Any]) -> list[dict[str, str]]:
    return [{"do": render(s["do"], ctx), **({"code": render(s["code"], ctx)} if s.get("code") else {})} for s in v.steps]


# ── the AI, for errors the library doesn't know (and when every known approach failed) ──

AI_PROMPT = """You are a senior Azure Data Factory / Synapse / Logic Apps support engineer. A pipeline run failed. Write a precise,
practical fix a data engineer can follow in ADF Studio and the Azure portal.
- 3 to 7 numbered steps, each one concrete action (where to click, what to set, what to check). Put any expression or setting
  value to type in "code".
- Use only what the error says; don't invent names. If something must be checked first, make that step 1.
- If earlier attempts are listed, they did NOT work: take a genuinely different approach that fits the person's reason.
Return JSON only: {"title": "short name of the problem", "meaning": "one sentence: what went wrong",
 "steps": [{"do": "...", "code": "optional"}], "verify": "how they'll know it worked"}"""


def ai_steps(error: str, ctx: dict[str, Any], tried: list[dict[str, Any]]) -> dict[str, Any] | None:
    from ..config import load_ai_config
    from ..dq.privacy import scrub_for_llm
    from .llm import AIUnavailable, client
    from .usage import tagged

    if not load_ai_config().ready:
        return None
    facts = {"error": scrub_for_llm(error[:3000]), "pipeline": ctx.get("pipeline"), "failed_step": ctx.get("step"),
             "error_code": ctx.get("code"),
             "already_tried": [{"approach": t.get("title"), "steps": [s.get("do") for s in t.get("steps", [])][:8],
                                "why_it_did_not_work": t.get("reason")} for t in tried][-4:]}
    try:
        with tagged(feature="fixes"):
            res = client().chat_json(AI_PROMPT, json.dumps(facts, default=str), max_tokens=1100)
        out = res[0]
    except (AIUnavailable, ValueError):
        return None
    except Exception:  # noqa: BLE001 - the library or the escalation path still answers
        return None
    steps = [{"do": str(s.get("do") or "").strip()[:500], **({"code": str(s["code"])[:400]} if s.get("code") else {})}
             for s in (out.get("steps") or []) if isinstance(s, dict) and str(s.get("do") or "").strip()][:8]
    if len(steps) < 2:
        return None
    return {"title": str(out.get("title") or "Suggested fix")[:120], "meaning": str(out.get("meaning") or "")[:400], "steps": steps,
            "verify": str(out.get("verify") or "The rerun succeeds.")[:300], "model": res[1].model}


# ── choosing what to show, and learning from the answers ──────────────────────


def score(worked: int, failed: int) -> float:
    return (worked + 1) / (worked + failed + 2)


def pick_variant(known: Known, tried: set[str], stats: dict[str, tuple[int, int]], reason: str = "") -> Variant | None:
    """The next approach: one the person's reason points to, else the one that's worked best for this team, else the next."""
    left = [v for v in known.variants if v.key not in tried]
    if not left:
        return None
    words = set(re.findall(r"[a-z]{3,}", reason.lower()))
    if words:
        hits = [(len(words & set(v.when.split())), v) for v in left]
        best = max(hits, key=lambda h: h[0])
        if best[0] > 0:
            return best[1]
    return max(left, key=lambda v: (score(*stats.get(v.key, (0, 0))), -known.variants.index(v)))


def stats_for(rows: list[Any]) -> dict[str, tuple[int, int]]:
    out: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for r in rows:
        if r.status == "worked":
            out[r.variant][0] += 1
        elif r.status == "failed":
            out[r.variant][1] += 1
    return {k: (v[0], v[1]) for k, v in out.items()}


def quick_fix(session: Any, incident: Any) -> dict[str, Any] | None:
    """The fix the bot would show first for an incident's failure, without recording anything (for the assistant)."""
    from sqlalchemy import select

    from ..db import CheckResult, FixAttempt, Run

    refs = [incident.run_ref] + [r.details.get("run_id") or r.run_ref for r in session.scalars(
        select(CheckResult).where(CheckResult.incident_id == incident.id, CheckResult.status == "fail"))]
    run = next((r for ref in refs if ref for r in [session.scalars(select(Run).where(
        Run.project_id == incident.project_id, (Run.run_id == ref) | (Run.trigger_id == ref))).first()] if r is not None and r.error), None)
    if run is None:
        return None
    sig, known = signature_of(run.error or "")
    ctx = context_of(run.error or "", run.site)
    stats = stats_for(list(session.scalars(select(FixAttempt).where(FixAttempt.signature == sig))))
    if known is None:
        return {"title": "An error the library doesn't know yet", "steps": [], "known": False, "pipeline": run.site}
    v = pick_variant(known, set(), stats)
    return {"title": known.title, "meaning": render(known.meaning, ctx), "approach": v.name if v else "", "known": True,
            "steps": steps_of(v, ctx) if v else [], "verify": render(v.verify, ctx) if v else "", "pipeline": run.site,
            "worked": stats.get(v.key, (0, 0))[0] if v else 0}
