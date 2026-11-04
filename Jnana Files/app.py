import threading
from datetime import datetime, timedelta, timezone

from flask import Flask, jsonify, request
from flask_cors import CORS
from azure.identity import DeviceCodeCredential
from azure.mgmt.datafactory import DataFactoryManagementClient

# --- Fill these in (same values as check_pipeline_runs.py) ---
SUBSCRIPTION_ID = "b10dba21-0331-4510-a7cb-924077c9cb52"
RESOURCE_GROUP = "dev-csdw-rg"
FACTORY_NAME = "dev-csdw-data-factory"
PIPELINE_NAME = "DEV-SG-TERMINAL-CSV"
LOOKBACK_DAYS = 7

app = Flask(__name__)
CORS(app)  # allow the Vite dev server (different port) to call this API

# --- Auth state, driven entirely through the API so the browser never needs a terminal ---
_auth_lock = threading.Lock()
auth_state = {
    "status": "idle",  # idle | waiting | authenticated | error
    "verification_uri": None,
    "user_code": None,
    "message": None,
}
_credential = None
_client = None


def _device_code_prompt(verification_uri, user_code, expires_on):
    # Called by azure-identity as soon as a device code is issued —
    # this is what lets the frontend show the code before login completes.
    with _auth_lock:
        auth_state.update(
            {
                "status": "waiting",
                "verification_uri": verification_uri,
                "user_code": user_code,
                "message": f"Go to {verification_uri} and enter code {user_code}",
            }
        )


def _run_device_login():
    global _credential, _client
    try:
        cred = DeviceCodeCredential(prompt_callback=_device_code_prompt)
        # Forces the login to happen now, synchronously in this background thread.
        cred.get_token("https://management.azure.com/.default")
        with _auth_lock:
            _credential = cred
            _client = DataFactoryManagementClient(_credential, SUBSCRIPTION_ID)
            auth_state.update(
                {
                    "status": "authenticated",
                    "message": "Signed in successfully.",
                }
            )
    except Exception as exc:
        with _auth_lock:
            auth_state.update({"status": "error", "message": str(exc)})


@app.route("/api/auth/start", methods=["POST"])
def auth_start():
    with _auth_lock:
        if auth_state["status"] in ("waiting", "authenticated"):
            return jsonify(auth_state)
        auth_state.update(
            {"status": "waiting", "verification_uri": None, "user_code": None, "message": "Requesting device code..."}
        )
    threading.Thread(target=_run_device_login, daemon=True).start()
    return jsonify(auth_state)


@app.route("/api/auth/status")
def auth_status():
    with _auth_lock:
        return jsonify(auth_state)


@app.route("/api/auth/logout", methods=["POST"])
def auth_logout():
    global _credential, _client
    with _auth_lock:
        _credential = None
        _client = None
        auth_state.update(
            {"status": "idle", "verification_uri": None, "user_code": None, "message": None}
        )
    return jsonify(auth_state)


def _require_client():
    with _auth_lock:
        if auth_state["status"] != "authenticated" or _client is None:
            return None
        return _client


@app.route("/api/pipelines")
def list_pipelines():
    """All pipeline names defined in the factory, not just the default one."""
    client = _require_client()
    if client is None:
        return jsonify({"error": "not_authenticated"}), 401
    try:
        pipelines = client.pipelines.list_by_factory(
            resource_group_name=RESOURCE_GROUP, factory_name=FACTORY_NAME
        )
        names = sorted(p.name for p in pipelines)
        return jsonify({"factory_name": FACTORY_NAME, "pipelines": names, "default": PIPELINE_NAME})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/pipeline-runs")
def pipeline_runs():
    client = _require_client()
    if client is None:
        return jsonify({"error": "not_authenticated"}), 401

    pipeline_name = request.args.get("pipeline", PIPELINE_NAME)
    days = int(request.args.get("days", LOOKBACK_DAYS))

    try:
        filter_params = {
            "lastUpdatedAfter": datetime.now(timezone.utc) - timedelta(days=days),
            "lastUpdatedBefore": datetime.now(timezone.utc),
            "filters": [
                {
                    "operand": "PipelineName",
                    "operator": "Equals",
                    "values": [pipeline_name],
                }
            ],
        }

        runs = client.pipeline_runs.query_by_factory(
            resource_group_name=RESOURCE_GROUP,
            factory_name=FACTORY_NAME,
            filter_parameters=filter_params,
        )

        runs_data = []
        for run in runs.value:
            duration = round(run.duration_in_ms / 1000, 1) if run.duration_in_ms else None
            invoked_by = run.invoked_by.as_dict() if run.invoked_by else None
            runs_data.append(
                {
                    "status": run.status,
                    "run_id": run.run_id,
                    "pipeline_name": run.pipeline_name,
                    "run_group_id": getattr(run, "run_group_id", None),
                    "is_latest": getattr(run, "is_latest", None),
                    "run_start": run.run_start.isoformat() if run.run_start else None,
                    "run_end": run.run_end.isoformat() if run.run_end else None,
                    "duration_seconds": duration,
                    "message": run.message or "",
                    "parameters": run.parameters or {},
                    "invoked_by": invoked_by,  # name + type of trigger/manual run
                    "annotations": getattr(run, "annotations", None) or [],
                }
            )
        runs_data.sort(key=lambda r: r["run_start"] or "", reverse=True)

        return jsonify(
            {
                "pipeline_name": pipeline_name,
                "factory_name": FACTORY_NAME,
                "lookback_days": days,
                "fetched_at": datetime.now(timezone.utc).isoformat(),
                "runs": runs_data,
            }
        )

    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


def _fetch_activity_runs(client, run_id, days):
    """Flat list of every activity run (including ForEach/If/Switch inner
    iterations) that executed within one pipeline run, fully parsed."""
    filter_params = {
        "lastUpdatedAfter": datetime.now(timezone.utc) - timedelta(days=days + 1),
        "lastUpdatedBefore": datetime.now(timezone.utc),
    }
    activities = client.activity_runs.query_by_pipeline_run(
        resource_group_name=RESOURCE_GROUP,
        factory_name=FACTORY_NAME,
        run_id=run_id,
        filter_parameters=filter_params,
    )

    result = []
    for act in activities.value:
        output = act.output or {}
        input_ = act.input or {}
        duration = round(act.duration_in_ms / 1000, 1) if act.duration_in_ms else None

        copy_stats = None
        if isinstance(output, dict) and ("rowsCopied" in output or "dataRead" in output):
            copy_stats = {
                "rows_read": output.get("rowsRead"),
                "rows_copied": output.get("rowsCopied"),
                "rows_skipped": output.get("rowsSkipped"),
                "data_read_bytes": output.get("dataRead"),
                "data_written_bytes": output.get("dataWritten"),
                "copy_duration_seconds": output.get("copyDuration"),
                "throughput_kb_per_sec": output.get("throughput"),
                "used_parallel_copies": output.get("usedParallelCopies"),
                "sink": output.get("sink") or {},
                "source": output.get("source") or {},
                "execution_details": output.get("executionDetails") or [],
            }

        result.append(
            {
                "activity_name": act.activity_name,
                "activity_type": act.activity_type,
                "status": act.status,
                "activity_run_id": act.activity_run_id,
                "activity_run_start": act.activity_run_start.isoformat() if act.activity_run_start else None,
                "activity_run_end": act.activity_run_end.isoformat() if act.activity_run_end else None,
                "duration_seconds": duration,
                "error": act.error or {},
                "copy_stats": copy_stats,
                "input": input_,
                "output": output,
            }
        )
    result.sort(key=lambda a: a["activity_run_start"] or "")
    return result


@app.route("/api/pipeline-runs/<run_id>/activities")
def pipeline_run_activities(run_id):
    """Flat activity-run list (kept for backward compatibility / debugging)."""
    client = _require_client()
    if client is None:
        return jsonify({"error": "not_authenticated"}), 401
    days = int(request.args.get("days", LOOKBACK_DAYS))
    try:
        return jsonify({"run_id": run_id, "activities": _fetch_activity_runs(client, run_id, days)})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# --- Static pipeline-definition parsing: builds the authored activity tree
# (ForEach / If / Switch / Until containers and their nested activities,
# plus ExecutePipeline references to other pipelines) so we can merge it
# with the runtime activity runs and show exactly where a run failed. ---

def _static_children(activity_dict):
    tp = activity_dict.get("typeProperties") or {}
    children = []
    if isinstance(tp.get("activities"), list):
        children += [(a, None) for a in tp["activities"]]
    if isinstance(tp.get("ifTrueActivities"), list):
        children += [(a, "If True") for a in tp["ifTrueActivities"]]
    if isinstance(tp.get("ifFalseActivities"), list):
        children += [(a, "If False") for a in tp["ifFalseActivities"]]
    if isinstance(tp.get("defaultActivities"), list):
        children += [(a, "Default") for a in tp["defaultActivities"]]
    for case in tp.get("cases") or []:
        label = f"Case: {case.get('value')}"
        for a in case.get("activities") or []:
            children.append((a, label))
    return children


def _build_static_node(activity_dict, branch_label=None):
    node = {
        "name": activity_dict.get("name"),
        "type": activity_dict.get("type"),
        "branch": branch_label,
        "children": [],
        "execute_pipeline_ref": None,
    }
    if activity_dict.get("type") == "ExecutePipeline":
        tp = activity_dict.get("typeProperties") or {}
        node["execute_pipeline_ref"] = (tp.get("pipeline") or {}).get("referenceName")
    for child_dict, label in _static_children(activity_dict):
        node["children"].append(_build_static_node(child_dict, label))
    return node


_pipeline_def_cache = {}  # pipeline_name -> list of static root nodes (resets on restart)


def _get_static_pipeline_tree(client, pipeline_name):
    if pipeline_name in _pipeline_def_cache:
        return _pipeline_def_cache[pipeline_name]
    pipeline = client.pipelines.get(
        resource_group_name=RESOURCE_GROUP, factory_name=FACTORY_NAME, pipeline_name=pipeline_name
    )
    pdict = pipeline.as_dict()
    activities = pdict.get("activities") or []
    tree = [_build_static_node(a) for a in activities]
    _pipeline_def_cache[pipeline_name] = tree
    return tree


def _attach_runtime(static_node, runtime_by_name, client, days, depth):
    """Merge one static-definition node with its matching runtime activity
    run(s) (there can be several if it ran inside a loop), and recurse."""
    runs = runtime_by_name.get(static_node["name"], [])

    if not runs:
        status = "NotRun"
    elif any(r["status"] == "Failed" for r in runs):
        status = "Failed"
    elif any(r["status"] == "InProgress" for r in runs):
        status = "InProgress"
    elif all(r["status"] == "Succeeded" for r in runs):
        status = "Succeeded"
    else:
        status = runs[-1]["status"]

    node = {
        "name": static_node["name"],
        "type": static_node["type"],
        "branch": static_node["branch"],
        "status": status,
        "iteration_count": len(runs),
        "runs": runs,  # each: status/timing/error/copy_stats/input/output
        "children": [],
    }

    for child in static_node["children"]:
        node["children"].append(_attach_runtime(child, runtime_by_name, client, days, depth))

    # Recurse into a called child pipeline for ExecutePipeline activities.
    if static_node["execute_pipeline_ref"] and depth < 5:
        child_pipeline_name = static_node["execute_pipeline_ref"]
        child_trees = []
        for r in runs:
            child_run_id = (r.get("output") or {}).get("pipelineRunId")
            if not child_run_id:
                continue
            try:
                child_static = _get_static_pipeline_tree(client, child_pipeline_name)
                child_runtime_flat = _fetch_activity_runs(client, child_run_id, days)
                child_runtime_by_name = {}
                for a in child_runtime_flat:
                    child_runtime_by_name.setdefault(a["activity_name"], []).append(a)
                child_trees.append(
                    {
                        "pipeline_name": child_pipeline_name,
                        "run_id": child_run_id,
                        "activities": [
                            _attach_runtime(s, child_runtime_by_name, client, days, depth + 1) for s in child_static
                        ],
                    }
                )
            except Exception as exc:
                child_trees.append({"pipeline_name": child_pipeline_name, "run_id": child_run_id, "error": str(exc)})
        node["called_pipelines"] = child_trees

    return node


@app.route("/api/pipeline-runs/<run_id>/tree")
def pipeline_run_tree(run_id):
    """Full collapsible failure hierarchy: pipeline -> activities (incl.
    ForEach/If/Switch/Until nested children, and nested called pipelines via
    ExecutePipeline), each node carrying real run status/timing/errors/
    copy stats so the UI can expand straight down to the exact failure."""
    client = _require_client()
    if client is None:
        return jsonify({"error": "not_authenticated"}), 401

    pipeline_name = request.args.get("pipeline")
    days = int(request.args.get("days", LOOKBACK_DAYS))
    if not pipeline_name:
        return jsonify({"error": "missing 'pipeline' query param"}), 400

    try:
        static_tree = _get_static_pipeline_tree(client, pipeline_name)
        runtime_flat = _fetch_activity_runs(client, run_id, days)
        runtime_by_name = {}
        for a in runtime_flat:
            runtime_by_name.setdefault(a["activity_name"], []).append(a)

        tree = [_attach_runtime(node, runtime_by_name, client, days, depth=0) for node in static_tree]

        return jsonify({"run_id": run_id, "pipeline_name": pipeline_name, "activities": tree})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


if __name__ == "__main__":
    # use_reloader=False: avoids a second process that would reset auth_state mid-login
    app.run(debug=True, port=5050, use_reloader=False)
