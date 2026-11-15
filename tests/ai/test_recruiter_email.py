"""
Recruiter e-mail engine: guide cases 1-8 plus missing value, hallucination,
sensitive topics and the auto-send gates. All candidate values are fake.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ai.settings import OP_RECRUITER_CLASSIFY, OP_RECRUITER_REPLY
from recruiter_email.candidate_facts import CandidateFactsError, has_fact, parse_facts
from recruiter_email.ledger import get_message, mark_reply
from recruiter_email.prefilter import run_prefilter
from recruiter_email.service import (
    CREATE_DRAFT,
    CREATE_DRAFT_MANUAL_REVIEW,
    IGNORE,
    LABEL_MANUAL_REVIEW,
    SEND_REPLY,
    IncomingEmail,
    load_recruiter_config,
    process_recruiter_email,
    validate_wording,
)
from tests.ai.conftest import execute_with, fake_provider, make_execute, unavailable

FAKE_FACTS = """
FULL_NAME=Test Candidate
TOTAL_EXPERIENCE_YEARS=9
LLM_EXPERIENCE_YEARS=3
LANGGRAPH_EXPERIENCE=Yes - production agent workflows
AGENTCORE_EXPERIENCE=
NOTICE_PERIOD_DAYS=30
CURRENT_CTC=
EXPECTED_CTC=XX LPA
CURRENT_LOCATION=Test City
LINKEDIN_PROFILE_URL=https://www.linkedin.com/in/test-candidate
MOBILE_NUMBER=+00 0000000000
EMAIL_ID=candidate@example.test
"""


def facts_loader():
    return parse_facts(FAKE_FACTS, Path("experience_details.txt"))


def classification(**over):
    data = {"is_recruiter_email": True, "role": "aiengineer", "intent": "REQUEST_CANDIDATE_DETAILS",
            "requested_fields": ["NOTICE_PERIOD_DAYS", "CURRENT_LOCATION"], "unmapped_requests": [],
            "sensitive_topics": [], "confidence": 0.96, "requires_manual_review": False, "reason": "asks details",
            "requested_skills": [], "extracted_facts": [], "missing_or_ambiguous": [], "knowledge_version_seen": None}
    data.update(over)
    return data


GOOD_WORDING = {"greeting": "Hi,", "opening_paragraph": "Thank you for reaching out about the role. "
                "Please find my details below.", "closing_paragraph": "Happy to discuss further.",
                "used_fields": [], "invented_values": False}


def json_engine():
    import tempfile
    from recruiter_email.ledger import JSONLedger
    return JSONLedger(Path(tempfile.mkdtemp(prefix="jobbot-test-")) / "ledger.json")


def make_service(classify=None, wording=None, auto_send=False, engine=None, execute=None,
                 loader=facts_loader):
    """Returns process(email) bound to fakes; .provider/.engine are exposed for assertions."""
    def respond(request):
        if request.operation == OP_RECRUITER_CLASSIFY:
            return classify if classify is not None else classification()
        assert request.operation == OP_RECRUITER_REPLY
        return wording if wording is not None else GOOD_WORDING

    provider = fake_provider("claude_api", [respond])
    engine = engine or json_engine()
    config = load_recruiter_config({"AUTO_SEND_RECRUITER_REPLY": "true" if auto_send else "false"})

    def process(email):
        return process_recruiter_email(email, engine=engine, config=config,
                                       execute=execute or execute_with(provider), facts_loader=loader)

    process.provider, process.engine = provider, engine
    return process


def email(subject, body, sender="Priya <priya@talentco.example.in>", message_id="m-1", headers=None):
    return IncomingEmail(message_id=message_id, thread_id="t-" + message_id, sender=sender, subject=subject,
                         body=body, headers=headers or {})


# --------------------------------------------------------------- guide cases
def test_case1_exact_ai_engineer_subject():
    fields = ["LLM_EXPERIENCE_YEARS", "NOTICE_PERIOD_DAYS", "CURRENT_CTC", "EXPECTED_CTC"]
    svc = make_service(classify=classification(requested_fields=fields))
    result = svc(email("Application for AI Engineer",
                               "Please share:\nLLM experience\nNotice period\nCurrent CTC\nExpected CTC"))
    assert result["role"] == "aiengineer" and result["requested_fields"] == fields
    # CURRENT_CTC is blank in the facts file -> manual review, never sent
    assert result["status"] == "MANUAL_REVIEW" and result["action"] == CREATE_DRAFT_MANUAL_REVIEW
    assert result["missing_fields"] == ["CURRENT_CTC"]
    assert "[please fill in before sending]" in result["reply_body"]
    assert "Notice Period: 30 days" in result["reply_body"] or "30 days" in result["reply_body"]


def test_case2_reply_thread_subject():
    result = make_service()(
        email("Re: AI Engineer Opportunity", "Please share your current location and notice period."))
    assert result["status"] == "READY_TO_SEND" and result["action"] == CREATE_DRAFT  # draft-only default
    assert "Test City" in result["reply_body"] and result["reply_subject"] == "Re: AI Engineer Opportunity"
    assert "DRAFT_ONLY_MODE" in result["reasons"] and result["auto_send"] is False


def test_case3_aws_data_engineer_role_hint():
    pre = run_prefilter("hr@x.example.in", "AWS Data Engineer Requirement", "Please share total experience", {})
    assert pre["role_hint"] == "awsdataengineer" and pre["recruiter_signal"]


def test_case4_python_developer_role_hint():
    pre = run_prefilter("hr@x.example.in", "Python Developer - Required Details", "Please share details", {})
    assert pre["role_hint"] == "pythondeveloper" and pre["recruiter_signal"]


def test_case5_missing_profile_value_never_auto_sends():
    svc = make_service(classify=classification(requested_fields=["CURRENT_CTC"]), auto_send=True)
    result = svc(email("Re: AI Engineer", "Please share your current CTC."))
    assert result["action"] == SEND_REPLY and result["auto_send"] is True
    assert "MISSING_PROFILE_VALUE" in result["reasons"]
    assert "[please fill in before sending]" not in result["reply_body"]


def test_case6_rejection_email_gets_no_reply():
    svc = make_service(classify=classification(intent="REJECTION", requested_fields=[]))
    result = svc(email("Your application for AI Engineer",
                               "Thank you for applying. We have decided to move ahead with another candidate."))
    assert result["action"] == IGNORE and result["reply_body"] is None


def test_case7_automated_job_board_mail_skipped():
    svc = make_service()
    result = svc(email("New AI Engineer jobs for you", "Apply now", sender="Jobs <noreply@jobboard.example>",
                               headers={"List-Unsubscribe": "<mailto:u@jobboard.example>"}))
    assert result["status"] == "SKIPPED_AUTOMATED" and result["action"] == IGNORE
    assert svc.provider.calls == []  # no AI call for automated mail


def test_case8_duplicate_trigger_skipped():
    svc = make_service()
    msg = email("Re: AI Engineer Opportunity", "Please share your notice period.")
    first = svc(msg)
    calls = len(svc.provider.calls)
    second = svc(msg)
    assert first["action"] == CREATE_DRAFT
    assert second["status"] == "DUPLICATE_SKIPPED" and second["action"] == IGNORE
    assert len(svc.provider.calls) == calls


# --------------------------------------------------------------- safety gates
def test_auto_send_only_when_every_gate_passes():
    svc = make_service(auto_send=True)
    result = svc(email("Re: AI Engineer Opportunity", "Please share your current location and notice period."))
    assert result["action"] == SEND_REPLY and result["auto_send"] is True


def test_low_confidence_blocks_auto_send():
    svc = make_service(classify=classification(confidence=0.7), auto_send=True)
    result = svc(email("Re: AI Engineer", "Please share your notice period."))
    assert result["action"] == SEND_REPLY
    assert any(r.startswith("LOW_CONFIDENCE") for r in result["reasons"])


@pytest.mark.parametrize("subject,body,intent", [
    ("Offer discussion", "Can you accept 20 LPA? Please confirm the offer.", "SALARY_NEGOTIATION"),
    ("Offer letter", "Please sign the offer letter and confirm your joining date.", "OFFER_OR_CONTRACT"),
])
def test_salary_offer_contract_always_manual_review_without_draft(subject, body, intent):
    svc = make_service(classify=classification(intent=intent, requested_fields=[]), auto_send=True)
    result = svc(email(subject, body))
    assert result["action"] == SEND_REPLY
    assert "I will review the details" in result["reply_body"]
    assert "I accept" not in result["reply_body"]


def test_hallucinated_number_in_wording_is_replaced_and_blocks_send():
    wording = dict(GOOD_WORDING, opening_paragraph="I have 12 years of experience and can join immediately.")
    svc = make_service(wording=wording, auto_send=True)
    result = svc(email("Re: AI Engineer Opportunity", "Please share your notice period."))
    assert "HALLUCINATION_SUSPECTED" in result["reasons"]
    assert result["action"] == SEND_REPLY
    assert "12 years" not in result["reply_body"]


def test_placeholders_are_filled_from_file_only():
    wording = dict(GOOD_WORDING, opening_paragraph="My notice period is {{NOTICE_PERIOD_DAYS}}.")
    result = make_service(wording=wording)(
        email("Re: AI Engineer Opportunity", "Please share your notice period."))
    assert "My notice period is 30 days." in result["reply_body"]


def test_validate_wording_flags_facts_written_by_model():
    v = validate_wording(["Reach me at a@b.example or https://x.example, CTC 25 LPA", "{{SECRET}}"], {"FULL_NAME"})
    assert {"model_written_digit", "model_written_email", "model_written_url",
            "placeholder_not_allowed:SECRET"} <= set(v)


def test_all_providers_down_labels_for_manual_review_and_allows_retry():
    from recruiter_email.ledger import claim_message

    svc = make_service(execute=execute_with(fake_provider("claude_api", [unavailable()])))
    msg = email("Re: AI Engineer Opportunity", "Please share your notice period.")
    result = svc(msg)
    assert result["status"] == "PROVIDERS_UNAVAILABLE" and result["action"] == LABEL_MANUAL_REVIEW
    # transient failure -> the next trigger may claim it again
    assert claim_message(svc.engine, msg.message_id, msg.thread_id, "x@y.example")[0] is True


def test_missing_facts_file_is_manual_review():
    def broken_loader():
        raise CandidateFactsError("not found")

    result = make_service(loader=broken_loader)(email("Re: AI Engineer Opportunity", "Please share your notice period."))
    assert result["action"] == LABEL_MANUAL_REVIEW
    assert "CANDIDATE_FACTS_FILE_MISSING_OR_INVALID" in result["reasons"]


def test_recruiter_ops_use_the_same_flags_only_enabled_providers_called():
    """USE_CLAUDE_API=false, USE_CHATGPT_API=true, web off: only the OpenAI API is used for both AI steps."""
    def respond(request):
        return classification() if request.operation == OP_RECRUITER_CLASSIFY else GOOD_WORDING

    openai = fake_provider("chatgpt_api", [respond])
    execute = make_execute({"chatgpt_api": openai}, enabled=["chatgpt_api"])  # others must_not_call
    result = make_service(execute=execute)(email("Re: AI Engineer Opportunity", "Please share your notice period."))
    assert result["provider"] == "chatgpt_api" and result["wording_provider"] == "chatgpt_api"
    assert [r.operation for r in openai.calls] == [OP_RECRUITER_CLASSIFY, OP_RECRUITER_REPLY]


def test_recruiter_all_flags_false_goes_to_manual_review_without_sending():
    result = make_service(execute=make_execute({}, enabled=[]))(
        email("Re: AI Engineer Opportunity", "Please share your notice period."))
    assert result["action"] == LABEL_MANUAL_REVIEW and "NO_PROVIDER_ENABLED" in result["reasons"]


def test_classifier_never_sees_candidate_values():
    svc = make_service()
    svc(email("Re: AI Engineer Opportunity", "Please share your notice period."))
    classify_request = next(r for r in svc.provider.calls if r.operation == OP_RECRUITER_CLASSIFY)
    prompt = classify_request.system_prompt + classify_request.user_prompt
    for secret in ("Test City", "candidate@example.test", "XX LPA", "0000000000"):
        assert secret not in prompt


def test_thread_cooldown_after_sent_reply():
    svc = make_service(auto_send=True)
    first = svc(email("Re: AI Engineer Opportunity", "Please share your notice period.", message_id="a"))
    assert first["action"] == SEND_REPLY
    mark_reply(svc.engine, "a", "SENT", "gmail-sent-id")
    second = svc(IncomingEmail(message_id="b", thread_id="t-a", sender="Priya <priya@talentco.example.in>",
                                       subject="Re: AI Engineer Opportunity", body="Also share your location."))
    assert "THREAD_RECENTLY_AUTO_REPLIED" in second["reasons"] and second["action"] == SEND_REPLY


def test_ledger_stores_no_raw_sender_or_values():
    svc = make_service()
    svc(email("Re: AI Engineer Opportunity", "Please share your notice period."))
    row = get_message(svc.engine, "m-1")
    assert row["sender_domain"] == "talentco.example.in" and len(row["sender_hash"]) == 64
    assert "priya@" not in str(row) and "Test City" not in str(row)


def test_legacy_txt_parser_still_rejects_bad_lines():
    with pytest.raises(CandidateFactsError):
        parse_facts("not a key value line", Path("x.txt"))
    with pytest.raises(CandidateFactsError):
        parse_facts("A=1\nA=2", Path("x.txt"))


def test_markdown_parser_accepts_prose_lines():
    facts = parse_facts("# Profile\n\nnot a key value line\nA_KEY=1\n", Path("experience_details.md"))
    assert facts == {"A_KEY": "1"}


def test_example_template_contains_no_values():
    example = Path(__file__).resolve().parents[2] / "config" / "experience_details.example.md"
    facts = parse_facts(example.read_text(encoding="utf-8"), example)
    assert not [k for k in facts if has_fact(facts, k)]
