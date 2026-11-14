"""
Knowledge base v2: config/experience_details.md (natural-English Markdown),
role-aware retrieval, Project-aware Claude/ChatGPT Web prompts, version sync,
fixed greeting + signature. No real browser, API, or Postgres is used.

Numbering follows the "Testing Requirements" list of the v2 spec.
Live browser checks that must be done by hand are listed in
README.md (section "Manual live tests").
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ai.errors import FailureType, ProviderUnavailableError
from ai.knowledge_prompts import chat_message, project_config, project_id_from_url, verify_project_page
from ai.provider_result import COMPLETED, AIRequest
from ai.providers.browser_chat import ensure_inside_project
from ai.providers.chatgpt_web import call_chatgpt_web
from ai.providers.claude_web import call_claude_web
from ai.settings import OP_RECRUITER_CLASSIFY, OP_RECRUITER_REPLY
from recruiter_email import knowledge_sync
from recruiter_email.email_template import DEFAULT_TEMPLATE, compose_email, load_template
from recruiter_email.field_registry import detect_fields
from recruiter_email.knowledge_base import (
    CandidateFactsError,
    load_knowledge,
    parse_knowledge,
)
from recruiter_email.knowledge_retrieval import (
    build_knowledge_context,
    resolve_requested_fields,
    resolve_target_role,
    unsupported_numbers,
    verify_ai_facts,
)
from recruiter_email.service import (
    CREATE_DRAFT,
    CREATE_DRAFT_MANUAL_REVIEW,
    LABEL_MANUAL_REVIEW,
    SEND_REPLY,
    IncomingEmail,
    load_recruiter_config,
    process_recruiter_email,
)
from tests.ai.conftest import execute_with, fake_provider, make_execute, unavailable
from tests.ai.test_recruiter_email import GOOD_WORDING, classification, json_engine

ROOT = Path(__file__).resolve().parents[2]

# Fake candidate (values are NOT the real ones, except the structure of the spec example).
DOC = """# Professional Profile

Document version: 2026-11-09.1

My full name is Test Candidate Kumar.

I have overall 11+ years of professional experience.

My relevant experience in AWS Data Engineering,
Azure Data Engineering, and AI Engineering is 8.5 years.

## Technical Experience

I have experience in Python, PySpark, AWS,
Azure, Databricks, REST APIs, FastAPI,
backend development, Generative AI, LLMs,
Claude, AI agents, and data engineering.

- I have 3 years of experience in LLMs, GenAI and AI agents.
- PySpark experience: 6 years
- My Python development experience is 10 years.

| Skill | Years |
|---|---|
| Databricks experience | 5 |

## Compensation

CURRENT_CTC=XX LPA
CURRENT_LOCATION=Test City
MOBILE_NUMBER=+00 0000000000
"""

SIGNATURE = (
    "Best regards,\n"
    "Md Irsad\n"
    "+91-8409718735 | mdirsadtech7305@gmail.com\n"
    "LinkedIn: https://www.linkedin.com/in/md-irsad-17ab4b218\n"
    "GitHub: https://github.com/mdirsad123\n"
)
SPEC_EMAIL = "Hello,\n\nPlease share:\n\nFull Name:\nTotal Experience:\nRelevant Experience:\n"


def kb_of(text: str = DOC) -> dict:
    return parse_knowledge(text, "experience_details.md")


def mail(subject: str, body: str, message_id: str = "k-1") -> IncomingEmail:
    return IncomingEmail(message_id=message_id, thread_id="t-" + message_id,
                         sender="Priya <priya@talentco.example.in>", subject=subject, body=body)


def run(kb_text: str = DOC, classify: dict | None = None, wording: dict | None = None, *, auto_send: bool = False,
        execute=None, sync_state: dict | None = None, subject: str = "AWS Data Engineer requirement",
        body: str = SPEC_EMAIL, loader=None, message_id: str = "k-1"):
    def respond(request):
        if request.operation == OP_RECRUITER_CLASSIFY:
            return classify or classification(
                role="awsdataengineer",
                requested_fields=["FULL_NAME", "TOTAL_EXPERIENCE_YEARS", "RELEVANT_EXPERIENCE_YEARS"])
        return wording or GOOD_WORDING

    provider = fake_provider("claude_api", [respond])
    result = process_recruiter_email(
        mail(subject, body, message_id), engine=json_engine(),
        config=load_recruiter_config({"AUTO_SEND_RECRUITER_REPLY": "true" if auto_send else "false"}),
        execute=execute or execute_with(provider), knowledge_loader=loader or (lambda: kb_of(kb_text)),
        sync_state=sync_state if sync_state is not None else {})
    result["_provider"] = provider
    return result


# ------------------------------------------------------------- 1-6 parsing / retrieval
def test_01_natural_language_markdown_parsing():
    kb = kb_of()
    facts = kb["facts"]
    assert facts["FULL_NAME"] == "Test Candidate Kumar"
    assert facts["AWS_DATA_ENGINEERING_EXPERIENCE_YEARS"] == "8.5"  # sentence wraps over two lines
    assert facts["AZURE_DATA_ENGINEERING_EXPERIENCE_YEARS"] == "8.5"
    assert facts["AI_ENGINEERING_EXPERIENCE_YEARS"] == "8.5"
    assert facts["PYTHON_DEVELOPMENT_EXPERIENCE_YEARS"] == "10"
    assert facts["DATABRICKS_EXPERIENCE_YEARS"] == "5"  # table row
    assert kb["version"] == "2026-11-09.1" and kb["revision"].startswith("2026-11-09.1#")
    assert "PYSPARK_EXPERIENCE_YEARS" in facts and "AWS_EXPERIENCE_YEARS" not in facts  # mention != duration


def test_02_mixed_markdown_and_key_value_facts():
    facts = kb_of(DOC + "\nNOTICE_PERIOD_DAYS=30\nPROFILE_SUMMARY=I have overall 11+ years of experience.\n")["facts"]
    assert facts["NOTICE_PERIOD_DAYS"] == "30" and facts["CURRENT_CTC"] == "XX LPA"
    assert facts["TOTAL_EXPERIENCE_YEARS"] == "11+"  # prose and KV agree -> no conflict


@pytest.mark.parametrize("sentence,expected", [
    ("I have overall 11+ years of professional experience.", "11+"),
    ("Total experience: 12 years", "12"),
    ("I have 9 years of total experience.", "9"),
    ("My overall experience is 10.5 years.", "10.5"),
])
def test_03_total_experience_extraction(sentence, expected):
    assert kb_of(f"# P\n\n{sentence}\n")["facts"]["TOTAL_EXPERIENCE_YEARS"] == expected


@pytest.mark.parametrize("role,expected_key,value", [
    ("awsdataengineer", "AWS_DATA_ENGINEERING_EXPERIENCE_YEARS", "8.5"),
    ("azuredataengineer", "AZURE_DATA_ENGINEERING_EXPERIENCE_YEARS", "8.5"),
    ("aiengineer", "AI_ENGINEERING_EXPERIENCE_YEARS", "8.5"),
    ("pythondeveloper", "PYTHON_DEVELOPMENT_EXPERIENCE_YEARS", "10"),
])
def test_04_role_aware_relevant_experience(role, expected_key, value):
    resolution = resolve_requested_fields(kb_of(), ["RELEVANT_EXPERIENCE_YEARS"], role)
    assert resolution["values"] == {"RELEVANT_EXPERIENCE_YEARS": value}
    assert resolution["sources"]["RELEVANT_EXPERIENCE_YEARS"] == expected_key


def test_04b_relevant_experience_never_reused_for_unrelated_role():
    # Generic data engineer: the document has no "Data Engineering" duration -> missing, NOT 8.5
    resolution = resolve_requested_fields(kb_of(), ["RELEVANT_EXPERIENCE_YEARS"], "dataengineer")
    assert resolution["missing"] == ["RELEVANT_EXPERIENCE_YEARS"]
    assert resolve_requested_fields(kb_of(), ["RELEVANT_EXPERIENCE_YEARS"], None)["missing"]


@pytest.mark.parametrize("text,key", [
    ("Gen AI exp:", "GENAI_EXPERIENCE_YEARS"),
    ("Generative AI experience", "GENAI_EXPERIENCE_YEARS"),
    ("Years in Spark", "PYSPARK_EXPERIENCE_YEARS"),
    ("Large Language Models experience", "LLM_EXPERIENCE_YEARS"),
    ("Agentic AI exp", "AI_AGENT_EXPERIENCE_YEARS"),
    ("Experience in Azure Databricks", "DATABRICKS_EXPERIENCE_YEARS"),
])
def test_05_technical_skill_synonyms(text, key):
    assert key in detect_fields(text)
    if key in kb_of()["facts"]:
        assert resolve_requested_fields(kb_of(), [key], None)["values"][key]


def test_05b_role_area_phrase_is_not_read_as_single_skill():
    assert detect_fields("AWS Data Engineering experience:") == ["AWS_DATA_ENGINEERING_EXPERIENCE_YEARS"]
    assert detect_fields("Relevant Experience:") == ["RELEVANT_EXPERIENCE_YEARS"]


def test_06_missing_and_ambiguous_experience_go_to_manual_review():
    conflicting = DOC + "\nMy relevant experience in AWS Data Engineering is 7 years.\n"
    kb = kb_of(conflicting)
    assert "AWS_DATA_ENGINEERING_EXPERIENCE_YEARS" in kb["conflicts"]
    result = run(conflicting, auto_send=True)
    assert result["action"] == CREATE_DRAFT_MANUAL_REVIEW and result["auto_send"] is False
    assert "CONFLICTING_PROFILE_VALUE:AWS_DATA_ENGINEERING_EXPERIENCE_YEARS" in result["reasons"]
    assert "Relevant Experience: [please fill in before sending]" in result["reply_body"]

    no_relevant = DOC.replace("My relevant experience in AWS Data Engineering,\nAzure Data Engineering, "
                              "and AI Engineering is 8.5 years.", "")
    result = run(no_relevant, auto_send=True, message_id="k-2")
    assert "MISSING_PROFILE_VALUE" in result["reasons"] and result["action"] != SEND_REPLY

    two_roles = classification(role="awsdataengineer", requested_fields=["RELEVANT_EXPERIENCE_YEARS"])
    result = run(classify=two_roles, subject="AWS Data Engineer / AI Engineer openings",
                 body="Please share your relevant experience.", auto_send=True, message_id="k-3")
    assert "ROLE_AMBIGUOUS" in result["reasons"] and result["action"] == CREATE_DRAFT_MANUAL_REVIEW


def test_06b_role_resolution_rules():
    assert resolve_target_role("awsdataengineer", ["awsdataengineer", "dataengineer"]) == ("awsdataengineer", [])
    assert resolve_target_role("unknown", ["azuredataengineer"]) == ("azuredataengineer", [])
    assert resolve_target_role("other", []) == (None, ["RELEVANT_EXPERIENCE_ROLE_UNKNOWN"])
    assert resolve_target_role("aiengineer", ["awsdataengineer"])[1] == ["ROLE_AMBIGUOUS"]


# ------------------------------------------------------------- 7 reload
def test_07_updated_local_markdown_is_used_on_next_request(tmp_path, monkeypatch):
    path = tmp_path / "experience_details.md"
    path.write_text(DOC, encoding="utf-8")
    monkeypatch.setenv("CANDIDATE_FACTS_PATH", str(path))
    first = run(loader=load_knowledge, message_id="r-1")
    assert "Total Experience: 11+ years" in first["reply_body"]
    path.write_text(DOC.replace("11+ years", "12+ years").replace("2026-11-09.1", "2026-11-10.1"), encoding="utf-8")
    second = run(loader=load_knowledge, message_id="r-2")
    assert "Total Experience: 12+ years" in second["reply_body"]
    assert first["knowledge"]["revision"] != second["knowledge"]["revision"]
    assert second["knowledge"]["version"] == "2026-11-10.1"


# ------------------------------------------------------------- 8-12 web providers + versions
def browser_ok(provider):
    return None


def test_08_claude_web_opens_project_and_references_knowledge(monkeypatch):
    monkeypatch.setenv("CLAUDE_PROJECT_URL", "https://claude.ai/project/0199abcd-1234-7000-8000-abcdefabcdef")
    seen = {}

    def runner(**kwargs):
        seen.update(kwargs)
        return '{"ok": true}', "https://claude.ai/chat/xyz"

    request = AIRequest(operation=OP_RECRUITER_CLASSIFY, system_prompt="s", user_prompt="u",
                        output_schema={"type": "object"},
                        knowledge={"document": "experience_details.md", "version": "v1", "revision": "v1#abc",
                                   "purpose": "classify"})
    result = call_claude_web(request, {}, chat_runner=runner, browser_check=browser_ok)
    assert result["status"] == COMPLETED
    assert seen["chat_url"] == "https://claude.ai/project/0199abcd-1234-7000-8000-abcdefabcdef"
    assert seen["project"]["id"] == "0199abcd-1234-7000-8000-abcdefabcdef"
    assert "Refer to the latest `experience_details.md` uploaded to this Claude Project" in seen["message"]
    assert "Extract only information explicitly supported by that document" in seen["message"]
    request_text = seen["request_file"].read_text(encoding="utf-8")
    assert "## PROJECT KNOWLEDGE" in request_text and "Claude Project" in request_text
    assert result["handoff"]["project_knowledge"]["project_verified"] is True


def test_09_chatgpt_web_opens_project_and_references_knowledge(monkeypatch):
    monkeypatch.setenv("CHATGPT_PROJECT_URL", "https://chatgpt.com/g/g-p-6789abcdef0123-jobbot/project")
    seen = {}

    def runner(**kwargs):
        seen.update(kwargs)
        return '```json\n{"ok": true}\n```', "https://chatgpt.com/g/g-p-6789abcdef0123-jobbot/c/1"

    request = AIRequest(operation=OP_RECRUITER_REPLY, system_prompt="s", user_prompt="u",
                        output_schema={"type": "object"},
                        knowledge={"document": "experience_details.md", "revision": "v1#abc", "purpose": "reply"})
    result = call_chatgpt_web(request, {}, chat_runner=runner)
    assert result["data"] == {"ok": True}
    assert seen["chat_url"].endswith("/project") and seen["project"]["id"] == "g-p-6789abcdef0123"
    assert "Use the latest `experience_details.md` uploaded to this ChatGPT Project" in seen["message"]
    assert "report it as missing" in seen["message"]


def test_10_wrong_or_inaccessible_project_is_unavailable_and_falls_back():
    project = {"configured": True, "id": "g-p-6789abcdef0123", "name": "JobBot", "verify_file_visible": True}
    assert verify_project_page("https://chatgpt.com/", "", project, "experience_details.md")
    assert verify_project_page("https://chatgpt.com/g/g-p-6789abcdef0123-x/project", "Other", project,
                               "experience_details.md")
    assert verify_project_page("https://chatgpt.com/g/g-p-6789abcdef0123-x/project",
                               "JobBot files: experience_details.md", project, "experience_details.md") is None

    class Driver:
        current_url = "https://claude.ai/new"

        def execute_script(self, script):
            return "Claude"

    chat = {"driver": Driver(), "provider": "claude_web"}
    with pytest.raises(ProviderUnavailableError) as info:
        ensure_inside_project(chat, {"configured": True, "id": "0199abcd-1234"}, "experience_details.md")
    assert info.value.failure_type == FailureType.PROJECT_KNOWLEDGE_UNAVAILABLE

    # Router: Claude Web cannot reach its Project -> ChatGPT Web answers.
    def respond(request):
        return classification(role="awsdataengineer", requested_fields=["TOTAL_EXPERIENCE_YEARS"]) \
            if request.operation == OP_RECRUITER_CLASSIFY else GOOD_WORDING

    claude = fake_provider("claude_web", [ProviderUnavailableError(
        "not inside project", failure_type=FailureType.PROJECT_KNOWLEDGE_UNAVAILABLE, retryable=False)])
    chatgpt = fake_provider("chatgpt_web", [respond])
    result = run(execute=make_execute({"claude_web": claude, "chatgpt_web": chatgpt}), message_id="f-1")
    assert result["provider"] == "chatgpt_web" and "Total Experience: 11+ years" in result["reply_body"]


def test_10b_required_project_missing_is_unavailable(monkeypatch):
    monkeypatch.setenv("RECRUITER_REQUIRE_PROJECT_KNOWLEDGE", "true")
    monkeypatch.delenv("CLAUDE_PROJECT_URL", raising=False)
    request = AIRequest(operation=OP_RECRUITER_CLASSIFY, system_prompt="s", user_prompt="u",
                        knowledge={"document": "experience_details.md", "purpose": "classify"})
    with pytest.raises(ProviderUnavailableError) as info:
        call_claude_web(request, {}, chat_runner=lambda **k: ("{}", ""), browser_check=browser_ok)
    assert info.value.failure_type == FailureType.PROJECT_KNOWLEDGE_UNAVAILABLE


def test_11_project_local_version_mismatch_blocks_auto_send():
    outdated = {"claude_api": {}}  # API providers are never "outdated"
    result = run(auto_send=True, sync_state=outdated, message_id="v-0")
    assert result["action"] == SEND_REPLY and result["knowledge"]["project_knowledge"] == "not_applicable"

    seen_old = classification(role="awsdataengineer", knowledge_version_seen="2026-10-01.1",
                              requested_fields=["FULL_NAME", "TOTAL_EXPERIENCE_YEARS", "RELEVANT_EXPERIENCE_YEARS"])
    result = run(classify=seen_old, auto_send=True, message_id="v-1")
    assert "PROJECT_KNOWLEDGE_VERSION_MISMATCH" in result["reasons"] and result["action"] != SEND_REPLY
    assert "Relevant Experience: 8.5 years" in result["reply_body"]  # facts still from the local file

    def respond(request):
        return classification(role="awsdataengineer", requested_fields=["TOTAL_EXPERIENCE_YEARS"]) \
            if request.operation == OP_RECRUITER_CLASSIFY else GOOD_WORDING

    web = fake_provider("claude_web", [respond])
    state = {"claude_web": {"sha256": "0" * 64}}
    result = run(execute=execute_with(web), sync_state=state, auto_send=True, message_id="v-2")
    assert "PROJECT_KNOWLEDGE_OUTDATED:claude_web" in result["reasons"]
    assert result["knowledge"]["project_knowledge"] == knowledge_sync.OUTDATED


def test_11b_sync_state_round_trip(tmp_path):
    kb = kb_of()
    path = tmp_path / "sync.json"
    knowledge_sync.mark_uploaded("both", kb, path)
    state = knowledge_sync.read_sync_state(path)
    assert knowledge_sync.project_status("claude_web", kb, state) == knowledge_sync.IN_SYNC
    assert knowledge_sync.project_status("chatgpt_web", kb_of(DOC + "\nmore\n"), state) == knowledge_sync.OUTDATED
    status = knowledge_sync.knowledge_status(kb, state)
    assert status["local"]["version"] == "2026-11-09.1" and not status["action_required"]


def test_12_no_project_configured_uses_local_attachment(monkeypatch):
    monkeypatch.delenv("CHATGPT_PROJECT_URL", raising=False)
    seen = {}

    def runner(**kwargs):
        seen.update(kwargs)
        return '{"ok": true}', "https://chatgpt.com/c/1"

    request = AIRequest(operation=OP_RECRUITER_CLASSIFY, system_prompt="s",
                        user_prompt="LOCAL KNOWLEDGE EXCERPTS:\n[Profile] I have overall 11+ years",
                        output_schema={"type": "object"},
                        knowledge={"document": "experience_details.md", "purpose": "classify"})
    result = call_chatgpt_web(request, {}, chat_runner=runner)
    assert result["status"] == COMPLETED and seen["chat_url"] == "https://chatgpt.com/"
    assert "LOCAL KNOWLEDGE EXCERPTS" in seen["message"]
    assert "11+ years" in seen["request_file"].read_text(encoding="utf-8")
    assert result["handoff"]["project_knowledge"]["project_configured"] is False


def test_12b_missing_local_file_is_manual_review_even_if_project_exists(tmp_path):
    def missing():
        raise CandidateFactsError("not found")

    result = run(loader=missing)
    assert result["action"] == LABEL_MANUAL_REVIEW
    assert "CANDIDATE_FACTS_FILE_MISSING_OR_INVALID" in result["reasons"]


# ------------------------------------------------------------- 13 provider flags
@pytest.mark.parametrize("enabled,expected", [
    (["claude_api", "chatgpt_api", "claude_web", "chatgpt_web"], "claude_api"),
    (["chatgpt_api", "claude_web"], "chatgpt_api"),
    (["claude_web", "chatgpt_web"], "claude_web"),
    (["chatgpt_web"], "chatgpt_web"),
])
def test_13_provider_configuration_combinations(enabled, expected):
    def respond(request):
        return classification(role="awsdataengineer", requested_fields=["TOTAL_EXPERIENCE_YEARS"]) \
            if request.operation == OP_RECRUITER_CLASSIFY else GOOD_WORDING

    fakes = {name: fake_provider(name, [respond]) for name in enabled}
    result = run(execute=make_execute(fakes, enabled=enabled))
    assert result["provider"] == expected and result["wording_provider"] == expected
    assert result["reply_body"].startswith("Dear Recruiter,\n") and result["reply_body"].endswith(SIGNATURE)
    assert [r.knowledge["purpose"] for r in fakes[expected].calls] == ["classify", "reply"]


def test_13b_first_provider_down_second_answers_same_reply():
    def respond(request):
        return classification(role="awsdataengineer", requested_fields=["TOTAL_EXPERIENCE_YEARS"]) \
            if request.operation == OP_RECRUITER_CLASSIFY else GOOD_WORDING

    down = fake_provider("claude_api", [unavailable("QUOTA_EXHAUSTED", retryable=False)])
    up = fake_provider("chatgpt_api", [respond])
    result = run(execute=execute_with(down, up))
    assert result["provider"] == "chatgpt_api" and "Total Experience: 11+ years" in result["reply_body"]


# ------------------------------------------------------------- 14 greeting + signature
def test_14_exact_greeting_and_signature_without_duplicates():
    noisy = dict(GOOD_WORDING, greeting="Hi Priya,",
                 opening_paragraph="Dear Recruiter, thank you for reaching out. Please find my details below.",
                 closing_paragraph="Happy to discuss further.\n\nBest regards,\nTest Candidate")
    result = run(wording=noisy)
    body = result["reply_body"]
    assert body.startswith("Dear Recruiter,\n\nthank you for reaching out.") or \
        body.startswith("Dear Recruiter,\n\nThank you")
    assert body.count("Dear Recruiter") == 1 and body.count("Best regards") == 1
    assert body.endswith(SIGNATURE) and "Hi Priya" not in body and "Test Candidate\n" not in body
    assert "Full Name: Test Candidate Kumar\nTotal Experience: 11+ years\nRelevant Experience: 8.5 years" in body


def test_14b_template_is_central_and_exact():
    template = load_template()
    assert template["greeting"] == "Dear Recruiter,"
    assert "\n".join([template["closing"], *template["signature_lines"]]) + "\n" == SIGNATURE
    assert compose_email("Thanks.", ["A: 1"], "Bye.", DEFAULT_TEMPLATE).endswith(SIGNATURE)


# ------------------------------------------------------------- 15 FastAPI / n8n contract
N8N_KEYS = {"status", "action", "auto_send", "message_id", "thread_id", "label", "reply_subject", "reply_body",
            "provider", "role", "intent", "confidence", "requested_fields", "missing_fields",
            "unmapped_requests", "sensitive_topics", "reasons", "steps"}


def test_15_n8n_response_fields_and_subject_preserved():
    result = run(subject="Re: AWS Data Engineer requirement")
    assert N8N_KEYS <= set(result)
    assert result["reply_subject"] == "Re: AWS Data Engineer requirement" and result["action"] == CREATE_DRAFT
    assert result["label"] == "JobBot/Recruiter/DraftReady"


def test_15b_knowledge_endpoints(monkeypatch, tmp_path):
    pytest.importorskip("fastapi")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app import main as ai_endpoints

    path = tmp_path / "experience_details.md"
    path.write_text(DOC, encoding="utf-8")
    monkeypatch.setenv("CANDIDATE_FACTS_PATH", str(path))
    monkeypatch.setenv("RECRUITER_KNOWLEDGE_SYNC_PATH", str(tmp_path / "sync.json"))
    app = FastAPI()
    app.include_router(ai_endpoints.router)
    client = TestClient(app)
    status = client.get("/ai/recruiter-email/knowledge-status").json()
    assert status["projects"]["claude_web"]["status"] == "unknown" and len(status["action_required"]) == 2
    marked = client.post("/ai/recruiter-email/knowledge-uploaded", json={"provider": "claude"}).json()
    assert marked["projects"]["claude_web"]["status"] == "in_sync"
    assert "XX LPA" not in str(marked) and "0000000000" not in str(marked)


# ------------------------------------------------------------- 16 hallucination guards
def test_16_no_unsupported_experience_values_in_reply():
    invented = dict(GOOD_WORDING, opening_paragraph="I bring 12 years of AWS expertise.")
    result = run(wording=invented, auto_send=True)
    assert "HALLUCINATION_SUSPECTED" in result["reasons"] and "12 years" not in result["reply_body"]
    assert result["action"] == CREATE_DRAFT_MANUAL_REVIEW
    assert unsupported_numbers("Total: 11+ years, Relevant: 9 years", ["11+", "8.5"]) == ["9"]


def test_16b_ai_extracted_facts_need_local_evidence():
    kb = kb_of()
    resolution = resolve_requested_fields(kb, ["LLM_EXPERIENCE_YEARS", "KAFKA_EXPERIENCE_YEARS"], None)
    fake_quote = {"field_key": "KAFKA_EXPERIENCE_YEARS", "value": "4 years",
                  "evidence": "I have 4 years of experience in Kafka."}
    merged = verify_ai_facts(kb, [fake_quote], resolution, ["LLM_EXPERIENCE_YEARS", "KAFKA_EXPERIENCE_YEARS"], None)
    assert merged["rejected"][0]["reason"] == "EVIDENCE_NOT_IN_LOCAL_DOCUMENT"
    assert "KAFKA_EXPERIENCE_YEARS" in merged["missing"]

    conflict = {"field_key": "LLM_EXPERIENCE_YEARS", "value": "5 years", "evidence": "x" * 20}
    merged = verify_ai_facts(kb, [conflict], resolution, ["LLM_EXPERIENCE_YEARS"], None)
    assert "AI_FACT_CONFLICT:LLM_EXPERIENCE_YEARS" in merged["reasons"]

    # Accepted only when the verbatim sentence exists locally and names the skill.
    doc = DOC + "\nOn streaming, I have used Kafka for 4 years in production pipelines.\n"
    kb2 = kb_of(doc)
    res2 = resolve_requested_fields(kb2, ["KAFKA_EXPERIENCE_YEARS"], None)
    good = {"field_key": "KAFKA_EXPERIENCE_YEARS", "value": "4 years",
            "evidence": "On streaming, I have used Kafka for 4 years in production pipelines."}
    merged = verify_ai_facts(kb2, [good], res2, ["KAFKA_EXPERIENCE_YEARS"], None)
    assert merged["values"]["KAFKA_EXPERIENCE_YEARS"] == "4 years" and merged["accepted"] == ["KAFKA_EXPERIENCE_YEARS"]
    assert "AI_ASSISTED_FACT_USED" in merged["reasons"]  # blocks auto-send, still drafted


def test_16c_confidential_details_never_reach_ai_prompts():
    result = run()
    prompts = " ".join(r.system_prompt + r.user_prompt for r in result["_provider"].calls)
    for secret in ("XX LPA", "Test City", "0000000000"):
        assert secret not in prompts
    assert "11+ years" in prompts  # relevant experience excerpts ARE provided
    context = build_knowledge_context(kb_of(), "Please share CTC and phone", role_hint="awsdataengineer")
    assert "LPA" not in context["text"] and "0000" not in context["text"]


# ------------------------------------------------------------- shipped files
def test_example_markdown_template_contains_no_values():
    kb = parse_knowledge((ROOT / "config" / "experience_details.example.md").read_text(encoding="utf-8"), "x.md")
    assert kb["facts"] == {}


def test_migration_from_legacy_txt():
    legacy = ("# comment\nPROFILE_SUMMARY=My full name is A B, I have overall 9 years of experience\n"
              "PYTHON_EXPERIENCE_YEARS=7\nBest regards,\nA B\n")
    md = knowledge_sync.migrate_txt_to_md(legacy, "2026-11-09.1")
    facts = kb_of(md)["facts"]
    assert facts["TOTAL_EXPERIENCE_YEARS"] == "9" and facts["PYTHON_EXPERIENCE_YEARS"] == "7"
    assert "Best regards" not in md and kb_of(md)["version"] == "2026-11-09.1"


def test_project_url_parsing_and_messages():
    assert project_id_from_url("https://claude.ai/project/0199abcd-1234-7000") == "0199abcd-1234-7000"
    assert project_id_from_url("https://claude.ai/code/project/chan_01WCTQe3b4") == "chan_01WCTQe3b4"
    assert project_id_from_url("https://chatgpt.com/g/g-p-6789abcdef0123-jobbot/project") == "g-p-6789abcdef0123"
    project = project_config("claude_web", {"project_url_env": "X_URL", "project_name_env": "X_NAME"},
                             {"X_URL": "https://claude.ai/project/0199abcd-1234-7000", "X_NAME": "JobBot"})
    assert project["configured"] and project["name"] == "JobBot" and not project["required"]
    plain = chat_message("claude_web", {}, "req.md", None)
    assert plain.startswith("Read the attached file req.md") and "Project" not in plain
