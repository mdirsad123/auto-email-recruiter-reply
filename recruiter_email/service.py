"""
recruiter_email/service.py
==========================

The recruiter e-mail pipeline behind POST /ai/recruiter-email-reply.
Entry point: process_recruiter_email(email) -> dict for n8n.

    normalize -> load the LATEST config/experience_details.md (fresh, versioned)
    -> automated/no-reply guard -> duplicate claim (ledger)
    -> recruiter/role pre-filter -> retrieve relevant, non-confidential excerpts
    -> Chat 1: AI classifier (router; web providers run inside the Claude /
       ChatGPT Project and reference the uploaded experience_details.md)
    -> resolve requested fields from the local Markdown (role-aware relevant
       experience) + AI-extracted facts accepted only with local evidence
    -> missing / conflict / version-mismatch guards
    -> Chat 2: AI wording (router, placeholders only)
    -> hallucination validator -> "Dear Recruiter," + verified fact lines
       + fixed signature (config/recruiter_email_template.json)
    -> unsupported-number check -> safety gates -> status/action for n8n
    -> ledger + knowledge-revision usage log

Facts in the reply ALWAYS come from the newest local Markdown. The AI can only
reference them as {{FIELD_KEY}} placeholders; any digit, e-mail, URL, currency
or availability claim written by the model itself is treated as a hallucination.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from ai.errors import AllProvidersUnavailableError, JobBotAIError
from ai.observability import logger, redact
from ai.provider_result import AIRequest
from ai.provider_router import execute_ai_task
from ai.schemas import RECRUITER_REPLY_SCHEMA, recruiter_classify_schema
from ai.settings import OP_RECRUITER_CLASSIFY, OP_RECRUITER_REPLY, PROJECT_ROOT, parse_flag
from ai.validators import validate_schema
from recruiter_email import knowledge_sync
from recruiter_email import ledger as ledger_db
from recruiter_email.pending_questions import capture_pending_questions
from recruiter_email.candidate_facts import CandidateFactsError, get_fact
from recruiter_email.email_template import compose_email, load_template, template_email
from recruiter_email.field_registry import detect_fields, format_value, is_sensitive, label_for
from recruiter_email.knowledge_aliases import ROLE_LABELS
from recruiter_email.knowledge_base import knowledge_from_facts, load_knowledge
from recruiter_email.knowledge_retrieval import (
    AI_EXTRACTABLE,
    RELEVANT_KEY,
    build_knowledge_context,
    known_field_keys,
    resolve_requested_fields,
    resolve_target_role,
    unsupported_numbers,
    verify_ai_facts,
)
from recruiter_email.prefilter import request_lines, run_prefilter, sender_address, strip_quoted_history

PROMPTS = PROJECT_ROOT / "ai" / "prompts"

# Statuses
SKIPPED_AUTOMATED = "SKIPPED_AUTOMATED"
DUPLICATE_SKIPPED = "DUPLICATE_SKIPPED"
NOT_RECRUITER = "NOT_RECRUITER"
NO_REPLY_NEEDED = "NO_REPLY_NEEDED"
READY_TO_SEND = "READY_TO_SEND"
MANUAL_REVIEW = "MANUAL_REVIEW"
PROVIDERS_UNAVAILABLE = "PROVIDERS_UNAVAILABLE"
ERROR = "ERROR"

# Actions for the n8n Switch node
IGNORE = "IGNORE"
SEND_REPLY = "SEND_REPLY"
CREATE_DRAFT = "CREATE_DRAFT"
CREATE_DRAFT_MANUAL_REVIEW = "CREATE_DRAFT_MANUAL_REVIEW"
LABEL_MANUAL_REVIEW = "LABEL_MANUAL_REVIEW"

LABELS = {
    SEND_REPLY: "JobBot/Recruiter/AutoReplied",
    CREATE_DRAFT: "JobBot/Recruiter/DraftReady",
    CREATE_DRAFT_MANUAL_REVIEW: "JobBot/Recruiter/ManualReview",
    LABEL_MANUAL_REVIEW: "JobBot/Recruiter/ManualReview",
    IGNORE: "JobBot/Recruiter/Ignored",
}

_PLACEHOLDER = re.compile(r"\{\{\s*([A-Z][A-Z0-9_]*)\s*\}\}")
_FACT_LIKE = (
    ("digit", re.compile(r"\d")),
    ("email", re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+")),
    ("url", re.compile(r"(https?://|www\.|linkedin\.com)", re.IGNORECASE)),
    ("currency", re.compile(r"(₹|\$|€|£|\binr\b|\busd\b|\blpa\b|\blakhs?\b|\bcrores?\b|\bper annum\b)", re.IGNORECASE)),
    ("availability_claim", re.compile(
        r"(immediate(?:ly)? (?:joiner|available|join)|serving (?:my )?notice|willing to relocate|"
        r"open to relocat|currently (?:working|based|located)|i (?:am|'m) based in|i have \w+ years)",
        re.IGNORECASE)),
)


# ---------------------------------------------------------------------------
# Configuration (plain dict built from env by a pure function)
# ---------------------------------------------------------------------------

DEFAULT_AUTO_SEND_INTENTS = ("REQUEST_CANDIDATE_DETAILS",)
DEFAULT_DRAFT_INTENTS = ("REQUEST_CANDIDATE_DETAILS", "REQUEST_RESUME", "AVAILABILITY_CHECK",
                         "INTERVIEW_SCHEDULING", "JOB_OPPORTUNITY")
DEFAULT_TARGET_ROLES = ("aiengineer", "awsdataengineer", "azuredataengineer", "dataengineer", "pythondeveloper")
# Deprecated: the signature now comes from config/recruiter_email_template.json.
DEFAULT_SIGNATURE_FIELDS = ("FULL_NAME", "MOBILE_NUMBER", "EMAIL_ID", "LINKEDIN_PROFILE_URL")
# Reasons that do NOT block auto-send.
NON_BLOCKING_REASONS = {"REPLY_WORDING_DETERMINISTIC_FALLBACK"}


def _csv(value: str | None, default: tuple[str, ...]) -> tuple[str, ...]:
    parts = tuple(p.strip() for p in (value or "").split(",") if p.strip())
    return parts or default


def _float(value: str | None, default: float) -> float:
    try:
        return float(value) if value and value.strip() else default
    except ValueError:
        return default


def load_recruiter_config(env: dict[str, str] | None = None) -> dict[str, Any]:
    """Pure (given env): recruiter safety settings. Draft-only unless AUTO_SEND_RECRUITER_REPLY=true."""
    source = os.environ if env is None else env
    return {
        "auto_send": parse_flag(source.get("AUTO_SEND_RECRUITER_REPLY"), default=False),
        "threshold": _float(source.get("RECRUITER_CONFIDENCE_THRESHOLD"), 0.90),
        "auto_send_intents": _csv(source.get("RECRUITER_AUTO_SEND_INTENTS"), DEFAULT_AUTO_SEND_INTENTS),
        "draft_intents": _csv(source.get("RECRUITER_DRAFT_INTENTS"), DEFAULT_DRAFT_INTENTS),
        "target_roles": _csv(source.get("RECRUITER_TARGET_ROLES"), DEFAULT_TARGET_ROLES),
        "thread_cooldown_hours": _float(source.get("RECRUITER_THREAD_COOLDOWN_HOURS"), 24.0),
        "signature_fields": _csv(source.get("RECRUITER_SIGNATURE_FIELDS"), DEFAULT_SIGNATURE_FIELDS),
        "require_prefilter": parse_flag(source.get("RECRUITER_PREFILTER_REQUIRED"), default=True),
    }


@dataclass
class IncomingEmail:
    message_id: str
    thread_id: str
    sender: str
    subject: str
    body: str
    to: str = ""
    received_at: str | None = None
    headers: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Hallucination validation + reply assembly
# ---------------------------------------------------------------------------

def validate_wording(texts: list[str], allowed_fields: set[str]) -> list[str]:
    """Return violations in the model-written wording (before substitution)."""
    violations: list[str] = []
    for text in texts:
        for key in _PLACEHOLDER.findall(text):
            if key not in allowed_fields:
                violations.append(f"placeholder_not_allowed:{key}")
        stripped = _PLACEHOLDER.sub("", text)
        if "{{" in stripped or "}}" in stripped:
            violations.append("malformed_placeholder")
        for name, pattern in _FACT_LIKE:
            if pattern.search(stripped):
                violations.append(f"model_written_{name}")
    return sorted(set(violations))


def substitute(text: str, values: dict[str, str]) -> str:
    return _PLACEHOLDER.sub(lambda m: values.get(m.group(1), m.group(0)), text)


def fallback_wording(role_label: str | None) -> dict[str, str]:
    about = f" regarding the {role_label} opportunity" if role_label else ""
    return {
        "greeting": "Dear Recruiter,",
        "opening_paragraph": f"Thank you for reaching out{about}. Please find the requested details below.",
        "closing_paragraph": "Please let me know if you need any further information. "
                             "I would be happy to discuss the role further.",
    }


def detail_lines(requested: list[str], values: dict[str, str]) -> list[str]:
    """'Label: value' per requested field; a gap is marked for the human reviewer."""
    return [f"{label_for(key)}: {format_value(key, values[key]) if values.get(key) else '[please fill in before sending]'}"
            for key in requested]


def build_reply_body(wording: dict[str, str], requested: list[str], values: dict[str, str],
                     template: dict[str, Any] | None = None) -> str:
    """'Dear Recruiter,' + AI paragraphs (placeholders filled) + verified fact lines + fixed signature."""
    formatted = {key: format_value(key, value) for key, value in values.items()}
    return compose_email(substitute(wording["opening_paragraph"].strip(), formatted),
                         detail_lines(requested, values),
                         substitute(wording["closing_paragraph"].strip(), formatted), template)


def reply_subject(subject: str) -> str:
    subject = (subject or "").strip()
    return subject if re.match(r"^re\s*:", subject, re.IGNORECASE) else f"Re: {subject}".strip()


# ---------------------------------------------------------------------------
# Result helpers
# ---------------------------------------------------------------------------

def make_reply_result(email: IncomingEmail, status: str, action: str, **extra: Any) -> dict[str, Any]:
    """Pure: the response body for n8n (same keys for every outcome)."""
    return {
        "status": status,
        "action": action,
        "auto_send": action == SEND_REPLY,
        "message_id": email.message_id,
        "thread_id": email.thread_id,
        "label": LABELS.get(action),
        "reply_subject": None,
        "reply_body": None,
        "provider": None,
        "role": None,
        "intent": None,
        "confidence": None,
        "requested_fields": [],
        "missing_fields": [],
        "unmapped_requests": [],
        "sensitive_topics": [],
        "reasons": [],
        "steps": [],
        **extra,
    }


def record_result(engine: Any, result: dict[str, Any]) -> dict[str, Any]:
    """Write the audit row + one log line (field NAMES only, never values)."""
    try:
        ledger_db.finish_message(
            engine, result["message_id"], status=result["status"], action=result["action"],
            provider=result.get("provider"), role=result.get("role"), intent=result.get("intent"),
            confidence=result.get("confidence"), requested_fields=result.get("requested_fields"),
            missing_fields=result.get("missing_fields"), reasons=result.get("reasons"),
        )
    except Exception as exc:  # noqa: BLE001 - audit failure must not crash the reply path
        logger.error("recruiter ledger update failed: %s", redact(exc))
        raise ledger_db.LedgerError("Could not record recruiter message result; refusing to create duplicate email") from exc
    logger.info(
        "recruiter_email message=%s status=%s action=%s provider=%s intent=%s fields=%s missing=%s reasons=%s "
        "knowledge=%s",
        redact(result["message_id"], 80), result["status"], result["action"], result.get("provider"),
        result.get("intent"), result.get("requested_fields"), result.get("missing_fields"), result.get("reasons"),
        (result.get("knowledge") or {}).get("revision"),
    )
    return result


# ---------------------------------------------------------------------------
# AI steps (both go through execute_ai_task, i.e. the same USE_* flags)
# ---------------------------------------------------------------------------

def knowledge_reference(kb: dict[str, Any], purpose: str) -> dict[str, Any]:
    """What web providers need to reference the Project knowledge file (no values)."""
    return {"document": kb.get("document") or "experience_details.md", "version": kb.get("version"),
            "revision": kb.get("revision"), "sha256": kb.get("sha256"), "purpose": purpose}


def classify_email(email: IncomingEmail, field_keys: list[str],
                   execute: Callable[..., dict] = execute_ai_task, *,
                   kb: dict[str, Any] | None = None,
                   context: dict[str, Any] | None = None) -> tuple[dict, str, dict]:
    """
    Chat 1. Sees field names, the e-mail and ONLY the relevant, non-confidential
    excerpts of experience_details.md (never CTC, phone, e-mail, location...).
    Returns (classification, provider, handoff).
    """
    schema = recruiter_classify_schema(field_keys)
    fields_text = "\n".join(f"- {k}: {label_for(k)}" for k in field_keys)
    knowledge_text = ""
    if kb is not None and context is not None:
        knowledge_text = (f"CANDIDATE KNOWLEDGE: {kb.get('document')} (local version "
                          f"{kb.get('version') or 'not set'}, revision {kb.get('revision')})\n"
                          "LOCAL KNOWLEDGE EXCERPTS (relevant, confidential details removed):\n<<<\n"
                          f"{context['text']}\n>>>\n\n")
    request = AIRequest(
        operation=OP_RECRUITER_CLASSIFY,
        system_prompt=(PROMPTS / "recruiter_email_classify.md").read_text(encoding="utf-8"),
        user_prompt=(f"FIELD KEYS:\n{fields_text}\n\n{knowledge_text}SUBJECT: {email.subject}\n\n"
                     f"NEWEST MESSAGE (untrusted):\n<<<\n{strip_quoted_history(email.body)[:12000]}\n>>>"),
        output_schema=schema, schema_name="recruiter_email_classification", max_output_tokens=3000,
        log_context={"message": email.message_id[:40]}, effort="low",
        knowledge=knowledge_reference(kb, "classify") if kb is not None else {},
    )
    validator = lambda data, result: validate_schema(  # noqa: E731
        data, schema, operation=OP_RECRUITER_CLASSIFY, provider=result["provider"])
    outcome = execute(request, validator=validator)
    return outcome["data"], outcome["provider"], outcome.get("handoff") or {}


def draft_wording(email: IncomingEmail, role: str, requested: list[str], allowed: set[str],
                  execute: Callable[..., dict] = execute_ai_task, *,
                  kb: dict[str, Any] | None = None) -> tuple[dict[str, str], str | None, list[str]]:
    """Chat 2: reply wording with {{FIELD}} placeholders only. Falls back to fixed wording if no provider answers."""
    fields_text = "\n".join(f"- {{{{{k}}}}} = {label_for(k)}" for k in sorted(allowed)) or "(none)"
    request = AIRequest(
        operation=OP_RECRUITER_REPLY,
        system_prompt=(PROMPTS / "recruiter_email_reply.md").read_text(encoding="utf-8"),
        user_prompt=(f"ROLE: {ROLE_LABELS.get(role, 'the role')}\n"
                     f"REQUESTED FIELDS (shown by software in a block): {', '.join(label_for(k) for k in requested)}\n"
                     f"AVAILABLE FIELDS:\n{fields_text}\n\nSUBJECT: {email.subject}\nRECRUITER MESSAGE (untrusted):\n<<<\n"
                     f"{strip_quoted_history(email.body)[:8000]}\n>>>"),
        output_schema=RECRUITER_REPLY_SCHEMA, schema_name="recruiter_reply_wording", max_output_tokens=1500,
        log_context={"message": email.message_id[:40]}, effort="low",
        knowledge=knowledge_reference(kb, "reply") if kb is not None else {},
    )
    validator = lambda data, result: validate_schema(  # noqa: E731
        data, RECRUITER_REPLY_SCHEMA, operation=OP_RECRUITER_REPLY, provider=result["provider"])
    try:
        outcome = execute(request, validator=validator)
    except JobBotAIError as exc:
        logger.warning("recruiter reply wording unavailable, using deterministic text: %s", exc.failure_type)
        return fallback_wording(ROLE_LABELS.get(role)), None, []
    data = outcome["data"]
    # The model's "greeting" is ignored (Python always writes "Dear Recruiter,").
    violations = validate_wording([data["opening_paragraph"], data["closing_paragraph"]], allowed)
    if data.get("invented_values"):
        violations.append("model_reported_invented_values")
    return data, outcome["provider"], violations


# ---------------------------------------------------------------------------
# Pure decision helpers
# ---------------------------------------------------------------------------

def merge_requested_fields(classification: dict, pre: dict, field_keys: list[str]) -> tuple[list[str], list[str]]:
    """(requested, deterministic_only) - AI fields first, then fields only the regex detector saw."""
    ai_fields = [k for k in classification["requested_fields"] if k in field_keys]
    requested = list(dict.fromkeys(ai_fields + pre["deterministic_fields"]))
    return requested, [k for k in pre["deterministic_fields"] if k not in ai_fields]


def safety_reasons(classification: dict, pre: dict, config: dict, *, requested: list[str], missing: list[str],
                   deterministic_only: list[str]) -> tuple[list[str], list[str], list[str]]:
    """Every reason that blocks auto-send. Returns (reasons, unmapped_requests, sensitive_topics)."""
    intent, role = classification["intent"], classification["role"]
    reasons: list[str] = []
    if float(classification["confidence"]) < config["threshold"]:
        reasons.append(f"LOW_CONFIDENCE<{config['threshold']}")
    if classification["requires_manual_review"]:
        reasons.append("CLASSIFIER_REQUESTED_REVIEW")
    if pre.get("bulk_recruiter"):
        reasons.append("BULK_RECRUITER_OUTREACH")
    if intent not in config["auto_send_intents"]:
        reasons.append(f"INTENT_NOT_AUTO_SENDABLE:{intent}")
    if role not in config["target_roles"]:
        reasons.append(f"ROLE_NOT_TARGET:{role}")
    if missing:
        reasons.append("MISSING_PROFILE_VALUE")
    if "AGE" in requested:
        reasons.append("PERSONAL_AGE_REVIEW_REQUIRED")
    # Immigration and international relocation are legally consequential.
    # Never auto-send these answers regardless of the destination country.
    if set(requested) & {
        "VISA_STATUS", "PASSPORT_AVAILABLE", "INTERNATIONAL_RELOCATION_STATUS",
        "WILLING_TO_RELOCATE_INTERNATIONALLY", "VISA_SPONSORSHIP_REQUIRED",
        "WORK_AUTHORIZATION_DESTINATION",
    }:
        reasons.append("INTERNATIONAL_IMMIGRATION_REVIEW_REQUIRED")
    if not requested:
        reasons.append("NO_FIELDS_REQUESTED")
    # The AI may still call a known field "unmapped" even after the rule-based
    # detector mapped it. Do not create repeated false pending questions.
    requested_keys = set(requested)
    unmapped = [label for label in dict.fromkeys(
        classification["unmapped_requests"] + pre["unsupported_requests"]
    ) if not (set(detect_fields(str(label))) & requested_keys)]
    if unmapped:
        reasons.append("UNSUPPORTED_REQUEST")
    sensitive = list(dict.fromkeys(classification["sensitive_topics"] + pre["sensitive_topics"]))
    if sensitive or intent in {"SALARY_NEGOTIATION", "OFFER_OR_CONTRACT"}:
        reasons.append("SENSITIVE_TOPIC")
    if any(is_sensitive(k) for k in deterministic_only):
        reasons.append("FIELD_DETECTION_DISAGREEMENT")
    return reasons, unmapped, sensitive


def knowledge_reasons(kb: dict[str, Any], provider: str | None, classification: dict[str, Any],
                      sync_state: dict[str, Any] | None) -> tuple[list[str], dict[str, Any]]:
    """Version / Project-sync checks. Returns (blocking reasons, knowledge info for the n8n result)."""
    state = knowledge_sync.project_status(provider or "", kb, sync_state)
    seen = classification.get("knowledge_version_seen")
    reasons = []
    if state == knowledge_sync.OUTDATED:
        reasons.append(f"PROJECT_KNOWLEDGE_OUTDATED:{provider}")
    if knowledge_sync.version_seen_mismatch(kb, seen):
        reasons.append("PROJECT_KNOWLEDGE_VERSION_MISMATCH")
    info = {"document": kb.get("document"), "version": kb.get("version"), "revision": kb.get("revision"),
            "sha256": kb.get("sha256"), "project_knowledge": state, "project_version_seen": seen}
    return reasons, info


def choose_action(reasons: list[str], auto_send: bool) -> tuple[str, str]:
    """(status, action) once a reply body exists."""
    blocking = [r for r in reasons if r not in NON_BLOCKING_REASONS]
    if blocking:
        return MANUAL_REVIEW, CREATE_DRAFT_MANUAL_REVIEW
    return READY_TO_SEND, (SEND_REPLY if auto_send else CREATE_DRAFT)


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def _load_kb(knowledge_loader: Callable[[], dict] | None,
             facts_loader: Callable[[], dict[str, str]] | None) -> dict[str, Any] | None:
    try:
        if knowledge_loader is not None:
            return knowledge_loader()
        if facts_loader is not None:  # legacy injection: plain {KEY: value}
            return knowledge_from_facts(facts_loader())
        return load_knowledge()
    except CandidateFactsError as exc:
        logger.error("recruiter_email: %s", exc)  # input defect: never guess, never send
        return None


def process_recruiter_email(
    email: IncomingEmail,
    *,
    engine: Any = None,
    config: dict[str, Any] | None = None,
    execute: Callable[..., dict] = execute_ai_task,
    facts_loader: Callable[[], dict[str, str]] | None = None,
    knowledge_loader: Callable[[], dict[str, Any]] | None = None,
    template: dict[str, Any] | None = None,
    sync_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    normalize -> latest experience_details.md -> automated guard -> duplicate claim
    -> pre-filter -> retrieval -> Chat 1 (classifier) -> role-aware field resolution
    -> evidence-checked AI facts -> missing / conflict / version guards -> Chat 2 (wording)
    -> hallucination checks -> greeting + facts + signature -> safety gates -> audit row.
    """
    engine = engine if engine is not None else ledger_db.get_ledger_engine()
    config = config or load_recruiter_config()
    template = template or load_template()
    steps: list[str] = ["normalized"]
    kb = _load_kb(knowledge_loader, facts_loader)  # fresh read for EVERY e-mail
    knowledge_info: dict[str, Any] = {"document": "experience_details.md", "version": None, "revision": None,
                                      "sha256": None, "project_knowledge": None, "project_version_seen": None}
    if kb:
        knowledge_info.update({"document": kb.get("document"), "version": kb.get("version"),
                               "revision": kb.get("revision"), "sha256": kb.get("sha256")})

    def finish(result: dict[str, Any]) -> dict[str, Any]:
        result.setdefault("knowledge", knowledge_info)
        knowledge_sync.log_usage(email.message_id, kb, provider=result.get("provider"), status=result["status"],
                                 project_state=knowledge_info.get("project_knowledge"),
                                 version_seen=knowledge_info.get("project_version_seen"))
        saved = record_result(engine, result)
        try:
            capture_pending_questions(saved)
        except (OSError, ValueError) as exc:
            # Logging must never leak an email body; review-tracking failure must
            # not trigger another outgoing draft or send.
            logger.error("recruiter question review queue unavailable: %s", type(exc).__name__)
        return saved

    facts = kb["facts"] if kb else None
    own_email = (get_fact(facts, "EMAIL_ID") if facts else None) or template_email(template)
    pre = run_prefilter(email.sender, email.subject, email.body, email.headers, own_email)
    if pre["automated"]:
        claimed, _ = ledger_db.claim_message(engine, email.message_id, email.thread_id, sender_address(email.sender),
                                              subject=email.subject, body=email.body)
        result = make_reply_result(email, SKIPPED_AUTOMATED, IGNORE, reasons=[pre["automated_reason"]],
                                   steps=steps + ["automated_guard"], knowledge=knowledge_info)
        return record_result(engine, result) if claimed else result

    claimed, existing = ledger_db.claim_message(engine, email.message_id, email.thread_id,
                                                sender_address(email.sender), subject=email.subject,
                                                body=email.body)
    steps.append("duplicate_check")
    if not claimed:
        return make_reply_result(email, DUPLICATE_SKIPPED, IGNORE, steps=steps, knowledge=knowledge_info, reasons=[
            f"already processed: {existing.get('status') if existing else 'unknown'}"])

    # Same sender + normalized subject/body under different Gmail message IDs:
    # skip duplicate outreach, but NEVER block different questions from that recruiter.
    sender_window = _float(os.getenv("RECRUITER_SENDER_COOLDOWN_HOURS"), 72.0)
    if ledger_db.sender_recently_handled(engine, email.message_id, sender_window):
        return finish(make_reply_result(email, DUPLICATE_SKIPPED, IGNORE,
                                        steps=steps + ["sender_content_dedupe"],
                                        reasons=["SAME_SENDER_SAME_EMAIL_CONTENT_RECENTLY_HANDLED"]))

    if kb is None:
        # Local source unavailable: never fall back to Project-only knowledge.
        return finish(make_reply_result(email, ERROR, LABEL_MANUAL_REVIEW, steps=steps,
                                        reasons=["CANDIDATE_FACTS_FILE_MISSING_OR_INVALID"]))

    steps.append("prefilter")
    if config["require_prefilter"] and not pre["recruiter_signal"]:
        return finish(make_reply_result(email, NOT_RECRUITER, IGNORE, reasons=["NO_RECRUITER_SIGNAL"], steps=steps))

    field_keys = known_field_keys(kb)
    context = build_knowledge_context(  # retrieved ONCE, reused by nothing else (Chat 2 needs no facts)
        kb, f"{email.subject}\n{request_lines(email.body)}\n{strip_quoted_history(email.body)[:4000]}",
        role_hint=pre["role_hint"], requested_keys=pre["deterministic_fields"])
    steps.append("knowledge_retrieved")
    try:
        classification, provider, handoff = classify_email(email, field_keys, execute, kb=kb, context=context)
    except AllProvidersUnavailableError:
        return finish(make_reply_result(email, PROVIDERS_UNAVAILABLE, LABEL_MANUAL_REVIEW,
                                        reasons=["ALL_AI_PROVIDERS_UNAVAILABLE"], steps=steps + ["classifier"]))
    except JobBotAIError as exc:
        return finish(make_reply_result(email, ERROR, LABEL_MANUAL_REVIEW, reasons=[exc.failure_type],
                                        steps=steps + ["classifier"]))
    steps.append("classifier")
    version_reasons, info = knowledge_reasons(kb, provider, classification, sync_state)
    knowledge_info.update(info)
    if handoff.get("project_knowledge"):
        knowledge_info["project_session"] = handoff["project_knowledge"]

    role, intent = classification["role"], classification["intent"]
    common = {"provider": provider, "role": role, "intent": intent,
              "confidence": float(classification["confidence"])}
    if not classification["is_recruiter_email"]:
        return finish(make_reply_result(email, NOT_RECRUITER, IGNORE, reasons=["CLASSIFIER_NOT_RECRUITER"],
                                        steps=steps, **common))
    if intent in {"REJECTION", "APPLICATION_ACKNOWLEDGEMENT"} and not classification["requested_fields"]:
        return finish(make_reply_result(email, NO_REPLY_NEEDED, IGNORE, reasons=[f"INTENT_{intent}"],
                                        steps=steps, **common))

    requested, deterministic_only = merge_requested_fields(classification, pre, field_keys)
    # Role-aware "Relevant Experience" + values from the latest local Markdown only.
    target_role, role_reasons = resolve_target_role(role, pre.get("role_hints") or [pre.get("role_hint")])
    resolution = resolve_requested_fields(kb, requested, target_role)
    merged = verify_ai_facts(kb, classification.get("extracted_facts", []), resolution, requested, target_role)
    values, missing = merged["values"], merged["missing"]
    steps += ["facts_loaded", "fields_mapped", "facts_verified", "missing_value_guard"]

    reasons, unmapped, sensitive = safety_reasons(classification, pre, config, requested=requested,
                                                  missing=missing, deterministic_only=deterministic_only)
    if RELEVANT_KEY in requested:
        reasons += role_reasons
    reasons += resolution["reasons"] + merged["reasons"] + version_reasons
    reasons += [f"AI_FLAGGED_AMBIGUOUS:{item}" for item in classification.get("missing_or_ambiguous", [])
                if item in values and AI_EXTRACTABLE.search(item)]
    try:
        if ledger_db.thread_recently_auto_replied(engine, email.thread_id, config["thread_cooldown_hours"]):
            reasons.append("THREAD_RECENTLY_AUTO_REPLIED")
    except Exception as exc:  # noqa: BLE001
        reasons.append("LEDGER_UNAVAILABLE")
        logger.error("ledger thread check failed: %s", redact(exc))
    reasons = list(dict.fromkeys(reasons))

    details = {**common, "requested_fields": requested, "missing_fields": missing,
               "unmapped_requests": unmapped, "sensitive_topics": sensitive,
               "relevant_experience_role": target_role if RELEVANT_KEY in requested else None,
               "ai_facts_accepted": merged["accepted"], "ai_facts_rejected": merged["rejected"]}
    # No draft at all for binding / negotiation topics: a human answers those.
    if intent in {"SALARY_NEGOTIATION", "OFFER_OR_CONTRACT"} or intent not in config["draft_intents"]:
        return finish(make_reply_result(email, MANUAL_REVIEW, LABEL_MANUAL_REVIEW, reasons=reasons,
                                        steps=steps, **details))

    wording, wording_provider, violations = draft_wording(email, role, requested, set(values), execute, kb=kb)
    steps += ["reply_generated", "hallucination_check"]
    if violations:
        reasons.append("HALLUCINATION_SUSPECTED")
        wording = fallback_wording(ROLE_LABELS.get(role))  # never ship suspicious text
    if wording_provider is None:
        reasons.append("REPLY_WORDING_DETERMINISTIC_FALLBACK")

    body = build_reply_body(wording, requested, values, template)
    # Final guard: every number above the signature must come from a verified value.
    stray = unsupported_numbers(body.split(template["closing"])[0], list(values.values()))
    if stray:
        violations = sorted(set(violations) | {"unsupported_number_in_reply"})
        reasons.append("UNSUPPORTED_VALUE_IN_REPLY")
        body = build_reply_body(fallback_wording(ROLE_LABELS.get(role)), requested, values, template)
    steps.append("greeting_signature")

    status, action = choose_action(reasons, config["auto_send"])
    if status == READY_TO_SEND and not config["auto_send"]:
        reasons.append("DRAFT_ONLY_MODE")
    return finish(make_reply_result(
        email, status, action, reasons=reasons, steps=steps + ["safety_gates"],
        reply_subject=reply_subject(email.subject),
        reply_body=body,
        hallucination_violations=violations, wording_provider=wording_provider, **details,
    ))
