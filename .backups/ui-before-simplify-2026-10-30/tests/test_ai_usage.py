"""AI usage: every model call is metered with the provider's real token counts, put down to the part of the app that
asked and its project, and priced on other providers' models from the live price list (here a stand-in list: tests
never reach the network)."""

from __future__ import annotations

import json
from datetime import timedelta

import httpx
from sqlalchemy import select
from test_ai import AI_YAML, FakeOpenRouter  # noqa: F401
from test_ai import ai as ai  # noqa: F401 - fixture

from alertbot.ai import usage
from alertbot.db import AiCall, BotState, session_scope
from alertbot.engine.cycle import run_cycle
from alertbot.sim.scenarios import apply_scenario
from alertbot.timeutil import iso, utcnow

PRICES = {
    "anthropic/claude-sonnet-5.5": {"id": "anthropic/claude-sonnet-5.5", "name": "Claude Sonnet 5.5", "provider": "anthropic", "provider_name": "Anthropic",
                                    "kind": "chat", "input": 2.0, "output": 10.0, "cached_input": None, "created": 3, "context": 1000000},
    "openai/gpt-6-luna": {"id": "openai/gpt-6-luna", "name": "GPT-6 Luna", "provider": "openai", "provider_name": "OpenAI",
                          "kind": "chat", "input": 0.1, "output": 0.5, "cached_input": None, "created": 2, "context": 400000},
    "openai/gpt-6-luna:batch": {"id": "openai/gpt-6-luna:batch", "name": "GPT-6 Luna (batch)", "provider": "openai", "provider_name": "OpenAI",
                                "kind": "chat", "input": 0.05, "output": 0.25, "cached_input": None, "created": 2, "context": 400000},
    "voyageai/voyage-4": {"id": "voyageai/voyage-4", "name": "Voyage 4", "provider": "voyageai", "provider_name": "Voyage AI",
                          "kind": "embed", "input": 0.06, "output": 0.0, "cached_input": None, "created": 1, "context": 32000},
    "openai/text-embedding-4": {"id": "openai/text-embedding-4", "name": "Text embedding 4", "provider": "openai", "provider_name": "OpenAI",
                                "kind": "embed", "input": 0.02, "output": 0.0, "cached_input": None, "created": 1, "context": 8000},
}


def _prices():
    with session_scope() as s:
        row = s.get(BotState, usage.CATALOG_KEY) or BotState(key=usage.CATALOG_KEY)
        row.value, row.updated_at = {"fetched_at": iso(utcnow()), "source": "test", "models": PRICES}, utcnow()
        s.add(row)


class MeteredRouter(FakeOpenRouter):
    """The stand-in OpenRouter, answering with usage the way the real one does."""

    def handler(self, request: httpx.Request) -> httpx.Response:
        r = super().handler(request)
        path = request.url.path.removeprefix("/api/v1")
        if r.status_code != 200:
            return r
        body = r.json()
        if path == "/chat/completions":
            assert json.loads(request.content)["usage"] == {"include": True}  # asks for real counts and cost
            body["usage"] = {"prompt_tokens": 1800, "completion_tokens": 240, "total_tokens": 2040, "cost": 0,
                             "completion_tokens_details": {"reasoning_tokens": 0}}
        elif path == "/embeddings":
            body["usage"] = {"prompt_tokens": 90, "total_tokens": 90}
        return httpx.Response(200, json=body)


def test_every_call_is_metered_with_real_tokens_feature_and_project(ai, estate, monkeypatch):  # noqa: F811
    from alertbot import config
    from alertbot.ai import llm

    router = MeteredRouter()
    monkeypatch.setattr(llm, "_client_for", lambda _k: llm.OpenRouter(config.load_ai_config(), transport=httpx.MockTransport(router.handler)))
    apply_scenario(estate, "incomplete")
    run_cycle(estate)
    usage.flush()
    with session_scope() as s:
        calls = list(s.scalars(select(AiCall).order_by(AiCall.id)))
    by = {(c.kind, c.status): c for c in calls}
    assert by[("chat", "error")].http_status == 429 and by[("chat", "error")].prompt_tokens == 0  # the rate-limited model, counted
    ok = by[("chat", "ok")]
    assert (ok.prompt_tokens, ok.completion_tokens, ok.estimated, ok.cost_usd) == (1800, 240, False, 0.0)
    assert ok.feature == "incident_analysis" and ok.project_key == estate.project and ok.model == "model/second:free"
    assert by[("decide", "ok")].feature == "triage" and by[("decide", "ok")].estimated  # no usage reported: estimated, flagged
    assert by[("embed", "ok")].feature == "similar" and by[("embed", "ok")].prompt_tokens == 90
    assert all(c.latency_ms >= 0 for c in calls)


def test_the_same_work_is_priced_on_other_models(estate):
    _prices()
    now = utcnow()
    with session_scope() as s:
        s.add_all([
            AiCall(at=now - timedelta(hours=2), feature="incident_analysis", kind="chat", model="m:free", served_model="m:free", status="ok",
                   prompt_tokens=1_000_000, completion_tokens=200_000, project_key=estate.project, cost_usd=0),
            AiCall(at=now - timedelta(hours=1), feature="similar", kind="embed", model="e:free", status="ok", prompt_tokens=500_000),
            AiCall(at=now - timedelta(minutes=5), feature="voice", kind="speech", model="v:free", status="ok", characters=900, audio_seconds=12.5),
        ])
    with session_scope() as s:
        d = usage.summary(s, days=7, project_keys=None, user_id=None, scenarios=["anthropic/claude-sonnet-5.5", "openai/gpt-6-luna", "nope/unknown"])
    sc = {x["id"]: x for x in d["scenarios"]}
    assert set(sc) == {"anthropic/claude-sonnet-5.5", "openai/gpt-6-luna"}  # an unknown model is simply left out
    sonnet = sc["anthropic/claude-sonnet-5.5"]
    # 1M in × $2 + 0.2M out × $10, and Anthropic's embeddings priced on Voyage (0.5M × $0.06)
    assert sonnet["cost"] == {"input": 2.0, "output": 2.0, "embeddings": 0.03, "total": 4.03}
    assert sonnet["embedding_model"]["id"] == "voyageai/voyage-4"
    luna = sc["openai/gpt-6-luna"]
    assert luna["cost"]["total"] == round(0.1 + 0.1 + 0.01, 6) and luna["batch"] == {"input": 0.05, "output": 0.25}
    assert d["scenarios"][0]["id"] == "openai/gpt-6-luna"  # cheapest first
    t = d["totals"]
    assert t["generation"] == {"prompt_tokens": 1_000_000, "completion_tokens": 200_000} and t["embedding_tokens"] == 500_000
    assert t["actual_cost"] == 0 and t["voice"] == {"calls": 1, "characters": 900, "audio_seconds": 12.5}
    feats = {f["key"]: f for f in d["features"]}
    assert feats["incident_analysis"]["cost"]["anthropic/claude-sonnet-5.5"] == 4.0 and feats["similar"]["cost"]["openai/gpt-6-luna"] == 0.01


def test_defaults_come_from_the_live_list_and_the_page_is_per_person(estate):
    from fastapi.testclient import TestClient

    from alertbot.api.main import app

    _prices()
    assert usage.default_scenarios(PRICES)[:1] == ["anthropic/claude-sonnet-5.5"]
    with session_scope() as s:
        s.add(AiCall(feature="ask", kind="chat", model="m:free", status="ok", prompt_tokens=10, completion_tokens=5, project_key="someone-elses"))
    api = TestClient(app, headers={"Authorization": "Bearer dev:priya.sharma@contoso.com"})
    d = api.get("/api/ai/usage?days=7").json()
    assert d["totals"]["calls"] == 0  # another project's calls aren't shown to a viewer
    assert [m["id"] for m in api.get("/api/ai/models?q=luna").json()["models"]] == ["openai/gpt-6-luna"]
    api.put("/api/me/prefs", json={"ai_scenarios": ["openai/gpt-6-luna"]})
    assert [x["id"] for x in api.get("/api/ai/usage?days=7").json()["scenarios"]] == ["openai/gpt-6-luna"]


def test_speech_length_is_read_from_the_mp3():
    frame = bytes([0xFF, 0xFB, 0x90, 0x64]) + b"\x00" * 412  # MPEG-1 layer III, 128 kbps
    assert usage.mp3_seconds(frame * 100) == round(len(frame * 100) * 8 / 128000, 2)
    assert usage.mp3_seconds(b"not audio") is None
