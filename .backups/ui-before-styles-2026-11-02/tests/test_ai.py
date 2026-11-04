"""The OpenRouter AI layer, driven by a fake OpenRouter: fallback between models, the evidence guard, triage,
similar incidents, and that AI never blocks or breaks a cycle."""

from __future__ import annotations

import json

import httpx
import pytest
from sqlalchemy import select

from alertbot.db import Incident, IncidentEmbedding, Notification, session_scope
from alertbot.engine.cycle import run_cycle
from alertbot.sim.scenarios import apply_scenario

AI_YAML = """
ai:
  enabled: true
  provider: openrouter
  models: [model/first:free, model/second:free, model/third:free]
  decision_model: model/decide:free
  embedding_model: model/embed:free
  speech_model: model/voice:free
  api_key: test-key
  timeout_seconds: 5
"""


class FakeOpenRouter:
    def __init__(self):
        self.calls: list[str] = []
        self.chat = {"model/first:free": 429, "model/second:free": "good", "model/third:free": "good"}
        self.briefing = "Site A's scrape on its latest run returned far fewer rows than usual and its price field was mostly empty, so today's data for 'Site A' is incomplete. Check the scraper's selectors first."
        self.vector = [0.1, 0.2, 0.3]

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer test-key"
        body = json.loads(request.content)
        path = request.url.path.removeprefix("/api/v1")
        self.calls.append(f"{path}:{body.get('model')}")
        if path == "/chat/completions":
            assert body["reasoning"] == {"enabled": False}
            mode = self.chat[body["model"]]
            if isinstance(mode, int):
                return httpx.Response(mode, json={"error": {"message": "rate limited", "code": mode}})
            return httpx.Response(200, json={"model": body["model"], "choices": [
                {"finish_reason": "stop", "message": {"content": json.dumps({"briefing": self.briefing, "confidence": "high"})}}]})
        if path == "/systemone":
            return httpx.Response(200, json={"answers": {"escalate": {"type": "noul", "noul": 0.97},
                                                         "urgency": {"type": "score", "score": 2.4, "probabilities": {"0": 0.01, "1": 0.09, "2": 0.6, "3": 0.3}}}})
        if path == "/embeddings":
            return httpx.Response(200, json={"data": [{"index": 0, "embedding": self.vector}]})
        return httpx.Response(404)


@pytest.fixture
def ai(estate, monkeypatch, workspace):
    from alertbot import config
    from alertbot.ai import llm

    (workspace / "config" / "ai.yaml").write_text(AI_YAML, encoding="utf-8")
    config.load_ai_config.cache_clear()
    llm._client_for.cache_clear()
    llm.HEALTH.resting_until.clear()
    fake = FakeOpenRouter()
    monkeypatch.setattr(llm, "_client_for", lambda _key: llm.OpenRouter(config.load_ai_config(), transport=httpx.MockTransport(fake.handler)))
    return fake


def incident(title: str) -> Incident:
    with session_scope() as s:
        return s.scalars(select(Incident).where(Incident.title == title)).one()


def test_ai_briefing_falls_back_between_models_and_keeps_the_exact_report(ai, estate):
    apply_scenario(estate, "incomplete")
    run_cycle(estate)
    inc = incident("Site A scrape incomplete")
    assert inc.analysis_source == "ai"
    assert inc.summary.startswith(ai.briefing)  # the AI briefing on top…
    assert "\n\n" in inc.summary and len(inc.summary) > len(ai.briefing) + 20  # …and the rules' exact report below
    assert "/chat/completions:model/first:free" in ai.calls and "/chat/completions:model/second:free" in ai.calls
    assert "/chat/completions:model/third:free" not in ai.calls  # stopped at the first model that answered
    labels = {e["label"]: e for e in inc.evidence}
    assert "model/second:free" in labels["AI briefing"]["detail"]
    assert labels["AI triage"]["data"]["urgency"] == "fix today" and labels["AI triage"]["data"]["escalate"] == 0.97
    # the alert that went out carries the AI briefing
    with session_scope() as s:
        note = s.scalars(select(Notification).where(Notification.incident_id == inc.id)).first()
        assert ai.briefing in json.dumps(note.payload)
        assert s.scalars(select(IncidentEmbedding).where(IncidentEmbedding.incident_id == inc.id)).first() is not None
    from alertbot.ai.llm import HEALTH

    assert not HEALTH.ok("model/first:free")  # the rate-limited model is rested for a while


def test_made_up_numbers_or_names_are_rejected(ai, estate):
    ai.briefing = "Site A lost 73,512 rows because the 'Payments API' timed out, affecting 'revenue_daily'."
    apply_scenario(estate, "incomplete")
    run_cycle(estate)
    inc = incident("Site A scrape incomplete")
    assert inc.analysis_source == "rules" and "73,512" not in inc.summary
    assert any(e["label"] == "AI triage" for e in inc.evidence)  # triage still works on the rules' report


def test_every_model_down_keeps_the_rules(ai, estate):
    ai.chat = {m: 503 for m in ai.chat}
    apply_scenario(estate, "failed")
    r = run_cycle(estate)
    assert r.status == "ok"
    inc = incident("Site A scrape failed")
    assert inc.analysis_source == "rules" and inc.likely_cause


def test_similar_past_incidents_are_found(ai, estate):
    apply_scenario(estate, "incomplete")
    run_cycle(estate)
    apply_scenario(estate, "recover")
    run_cycle(estate)
    apply_scenario(estate, "incomplete")
    run_cycle(estate)
    with session_scope() as s:
        latest = s.scalars(select(Incident).where(Incident.status == "open").order_by(Incident.id.desc())).first()
        similar = next(e for e in latest.evidence if e["label"] == "Similar past incidents")
        assert "Site A scrape incomplete" in similar["detail"] and "100% similar" in similar["detail"]


def test_guard_helpers():
    from alertbot.ai.llm import invented_numbers, parse_json

    assert invented_numbers("copied 41,108 rows (~41.1k), 4 tables", "rows_copied: 41108") == set()
    assert invented_numbers("copied 52,000 rows", "rows_copied: 41108") == {"52000"}
    assert parse_json('Sure!\n```json\n{"briefing": "x"}\n```') == {"briefing": "x"}


def test_quoted_names_ignore_apostrophes():
    from alertbot.ai.investigator import _QUOTED

    assert _QUOTED.findall("Site A's scrape for today's run failed at 'BA-ID PROD' and 'Filter_BA_ID'.") == ["BA-ID PROD", "Filter_BA_ID"]
