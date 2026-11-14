"""Standalone FastAPI app: the endpoints n8n calls (no real AI, in-memory SQLite)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from ai.settings import OP_RECRUITER_CLASSIFY
from app import main
from tests.ai.conftest import execute_with, fake_provider
from tests.ai.test_recruiter_email import GOOD_WORDING, classification, facts_loader, json_engine


@pytest.fixture
def client():
    def respond(request):
        return classification() if request.operation == OP_RECRUITER_CLASSIFY else GOOD_WORDING

    deps = {"engine": json_engine(), "execute": execute_with(fake_provider("claude_api", [respond])),
            "facts_loader": facts_loader}
    main.app.dependency_overrides[main.recruiter_dependencies] = lambda: deps
    yield TestClient(main.app)
    main.app.dependency_overrides.clear()


PAYLOAD = {"message_id": "gm-1", "thread_id": "th-1", "from": "Priya <priya@talentco.example.in>",
           "subject": "Re: AI Engineer Opportunity", "body": "Please share your notice period and current location."}


def test_recruiter_reply_then_mark_result(client):
    response = client.post("/ai/recruiter-email-reply", json=PAYLOAD)
    assert response.status_code == 200
    body = response.json()
    assert body["action"] == "CREATE_DRAFT" and body["label"] == "JobBot/Recruiter/DraftReady"
    assert body["reply_body"].startswith("Dear Recruiter,")
    assert client.post("/ai/recruiter-email-reply", json=PAYLOAD).json()["status"] == "DUPLICATE_SKIPPED"
    marked = client.post("/ai/recruiter-email/mark-result",
                         json={"message_id": "gm-1", "outcome": "DRAFTED", "reply_ref": "r-1"})
    assert marked.status_code == 200
    assert client.post("/ai/recruiter-email/mark-result",
                       json={"message_id": "nope", "outcome": "SENT"}).status_code == 404
    assert client.post("/ai/recruiter-email/mark-result",
                       json={"message_id": "gm-1", "outcome": "WHATEVER"}).status_code == 422


def test_recruiter_reply_rejects_empty_mail(client):
    assert client.post("/ai/recruiter-email-reply", json={**PAYLOAD, "subject": "", "body": " "}).status_code == 400


def test_providers_status_never_shows_keys(client, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-SECRET")
    monkeypatch.setenv("USE_CLAUDE_API", "false")
    monkeypatch.setenv("USE_CHATGPT_API", "false")
    monkeypatch.setenv("USE_CLAUDE_WEB", "true")
    monkeypatch.setenv("USE_CHATGPT_WEB", "false")
    body = client.get("/ai/providers/status").json()
    assert body["enabled_providers"] == ["claude_web"] and body["anthropic_api_key_present"] is True
    assert "SECRET" not in str(body)


def test_health(client):
    body = client.get("/health").json()
    assert body["service"] == "jobbot-recruiter-email" and body["knowledge_file_available"] is True
