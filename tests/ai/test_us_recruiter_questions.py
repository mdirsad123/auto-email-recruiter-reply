"""International relocation answers and missing-question review queue."""
from __future__ import annotations

import json

from ai.settings import OP_RECRUITER_CLASSIFY
from recruiter_email.field_registry import detect_fields
from recruiter_email.knowledge_base import load_knowledge
from recruiter_email.pending_questions import capture_pending_questions
from recruiter_email.service import (
    CREATE_DRAFT_MANUAL_REVIEW,
    SEND_REPLY,
    IncomingEmail,
    load_recruiter_config,
    process_recruiter_email,
)
from tests.ai.conftest import execute_with, fake_provider
from tests.ai.test_recruiter_email import GOOD_WORDING, classification, json_engine


def test_international_questions_have_distinct_fields():
    body = "Please provide below details:\nFull Name\nLocation\nVisa Status\nWhen did you Relocate to US"
    fields = detect_fields(body)
    assert "VISA_STATUS" in fields
    assert "INTERNATIONAL_RELOCATION_STATUS" in fields
    assert "WILLING_TO_RELOCATE_INTERNATIONALLY" not in fields
    assert detect_fields("Are you willing to relocate to the US?") == ["WILLING_TO_RELOCATE_INTERNATIONALLY"]
    assert detect_fields("Do you need visa sponsorship?") == ["VISA_SPONSORSHIP_REQUIRED"]
    assert detect_fields("Are you authorized to work in the US?") == ["WORK_AUTHORIZATION_DESTINATION"]
    assert detect_fields("When did you relocate to Japan?") == ["INTERNATIONAL_RELOCATION_STATUS"]
    assert detect_fields("When did you relocate to Dubai UAE?") == ["INTERNATIONAL_RELOCATION_STATUS"]
    assert detect_fields("Are you willing to relocate to Saudi Arabia?") == ["WILLING_TO_RELOCATE_INTERNATIONALLY"]
    assert detect_fields("Are you willing to relocate to the UAE?") == ["WILLING_TO_RELOCATE_INTERNATIONALLY"]
    assert detect_fields("Do you require visa sponsorship for a job in Japan?") == ["VISA_SPONSORSHIP_REQUIRED"]
    assert detect_fields("Are you eligible to work in Saudi Arabia?") == ["WORK_AUTHORIZATION_DESTINATION"]


def test_user_confirmed_visa_reply_is_manual_review_even_with_auto_send_enabled(tmp_path, monkeypatch):
    monkeypatch.setenv("RECRUITER_QUESTION_REVIEW_PATH", str(tmp_path / "review.json"))
    body = "Please provide below details:\nFull Name\nLocation\nVisa Status\nWhen did you Relocate to US"

    def respond(request):
        if request.operation == OP_RECRUITER_CLASSIFY:
            return classification(
                role="pythondeveloper",
                requested_fields=["FULL_NAME", "CURRENT_LOCATION", "VISA_STATUS", "INTERNATIONAL_RELOCATION_STATUS"],
            )
        return GOOD_WORDING

    provider = fake_provider("claude_api", [respond])
    email = IncomingEmail(
        message_id="m-visa-01", thread_id="t-visa-01", sender="Recruiter <hr@example.org>",
        subject="Python Developer Opportunity", body=body,
    )
    result = process_recruiter_email(
        email,
        engine=json_engine(),
        config=load_recruiter_config({"AUTO_SEND_RECRUITER_REPLY": "true"}),
        execute=execute_with(provider),
        knowledge_loader=load_knowledge,
        sync_state={},
    )
    assert result["action"] == SEND_REPLY
    assert "INTERNATIONAL_IMMIGRATION_REVIEW_REQUIRED" in result["reasons"]
    # Forced mode acknowledges but avoids automatically revealing immigration answers.
    assert "I do not currently hold a visa for the destination country." not in result["reply_body"]
    assert "I am currently based in India and have not relocated to the destination country." not in result["reply_body"]
    assert "Willing to Relocate Internationally:" not in result["reply_body"]


def test_unknown_question_is_reviewed_not_added_to_authoritative_facts(tmp_path, monkeypatch):
    path = tmp_path / "recruiter_questions_to_review.json"
    monkeypatch.setenv("RECRUITER_QUESTION_REVIEW_PATH", str(path))
    result = {
        "message_id": "m-unknown-01",
        "missing_fields": ["WORK_AUTHORIZATION_DESTINATION"],
        "unmapped_requests": ["Passport validity date"],
    }
    capture_pending_questions(result)
    capture_pending_questions(result)
    stored = json.loads(path.read_text(encoding="utf-8"))
    entries = list(stored["messages"].values())
    assert len(entries) == 2
    assert all(entry["count"] == 1 and entry["review_status"] == "pending" for entry in entries)
    assert {item["question_label"] for item in entries} == {"Destination Country Work Authorization", "Passport validity date"}
    assert "m-unknown-01" not in path.read_text(encoding="utf-8")
    assert "passport validity" not in (load_knowledge()["facts"].get("VISA_STATUS") or "").lower()
