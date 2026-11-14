"""
recruiter_email/prefilter.py
============================

Deterministic, cheap checks that run BEFORE any AI call (and again inside the
safety gates): automated/no-reply detection, recruiter/role pre-filter,
request-line extraction, sensitive-topic and unsupported-request detection.
"""

from __future__ import annotations

import os
import re
from typing import Any

from recruiter_email.field_registry import detect_fields

_NOREPLY_LOCAL = re.compile(
    r"^(?:no-?reply|do-?not-?reply|donotreply|mailer-daemon|postmaster|notifications?|"
    r"alerts?|newsletter|news|digest|bounce|auto-?confirm|jobalerts?|info-?alerts?)\b",
    re.IGNORECASE,
)
_AUTOMATED_SUBJECT = re.compile(
    r"(automatic reply|auto(?:matic)?[- ]?reply|out of office|undeliverable|delivery status notification|"
    r"mail delivery (?:failed|subsystem)|job alert|jobs? (?:for|matching) you|recommended jobs|newsletter|"
    r"your application (?:was|has been) (?:sent|submitted|received)|application received|"
    r"thank you for applying|calendar|invitation:|accepted:|declined:|receipt|verify your|otp)",
    re.IGNORECASE,
)
_ROLE_PATTERNS: dict[str, re.Pattern[str]] = {
    "aiengineer": re.compile(
        r"\b(?:gen\s*-?ai|generative\s+ai|agentic\s+ai|ai(?:/ml)?|llm|ml|machine\s+learning|"
        r"prompt|nlp)\s+(?:engineer|developer|lead|architect|specialist)\b", re.IGNORECASE),
    "azuredataengineer": re.compile(r"\bazure\s+data\s+(?:engineer|developer)\b", re.IGNORECASE),
    "awsdataengineer": re.compile(r"\baws\s+data\s+(?:engineer|developer)\b", re.IGNORECASE),
    "dataengineer": re.compile(
        r"\b(?:big\s+)?data\s+(?:engineer|engineering|developer)\b|\b(?:pyspark|databricks|etl)\s+(?:engineer|developer)\b",
        re.IGNORECASE),
    "pythondeveloper": re.compile(
        r"\bpython\s+(?:developer|engineer|backend|programmer)\b|\bbackend\s+(?:python\s+)?(?:engineer|developer)\b",
        re.IGNORECASE),
}
_RECRUITER_PHRASES = re.compile(
    r"(share (?:your|the following|below)|please (?:share|provide|send|confirm)|kindly (?:share|provide|send)|"
    r"required details|candidate (?:details|information)|profile details|your (?:profile|resume|cv)|"
    r"updated (?:resume|cv)|opportunity|opening|position|requirement|job description|\bjd\b|"
    r"interested candidates?|shortlist|interview|application for|we(?:\s+are|['’]re)\s+hiring|"
    r"currently hiring|hiring for|urgent hiring|open positions?|job vacancies?|vacancy|"
    r"contract opening|role opening|recruiting for|talent acquisition|"
    r"looking for (?:a |the )?candidate|job requirements?|career opportunit(?:y|ies)|"
    r"interview invitation|staffing team|position is open)",
    re.IGNORECASE,
)
_SENSITIVE = {
    "salary_negotiation": re.compile(
        r"(negotia|best (?:ctc|offer|package)|revis\w* (?:your )?expect|reduce (?:your )?expect|"
        r"lower (?:your )?expect|final (?:ctc|offer)|counter[- ]?offer|budget is|can you (?:come down|adjust))",
        re.IGNORECASE),
    "offer_or_acceptance": re.compile(
        r"(offer letter|accept(?:ance)? (?:of )?(?:the |our )?offer|release (?:the|an) offer|letter of intent|"
        r"joining bonus|sign(?:ing)? bonus)", re.IGNORECASE),
    "resignation": re.compile(r"(resign|relieving letter|last working day|buy[- ]?out)", re.IGNORECASE),
    "contract_or_legal": re.compile(
        r"(\bbond\b|service agreement|\bnda\b|non[- ]disclosure|declaration|background (?:check|verification)|"
        r"sign (?:the )?(?:contract|agreement)|contract terms|legal|affidavit|undertaking)", re.IGNORECASE),
}
_UNSUPPORTED = {
    "date_of_birth": re.compile(r"(date of birth|\bdob\b|birth ?date)", re.IGNORECASE),
    "government_id": re.compile(r"(\bpan\b(?: card| number)?|aadha?r|passport (?:no|number)|\bssn\b|national id)", re.IGNORECASE),
    "bank_details": re.compile(r"(bank (?:account|details)|ifsc|account number)", re.IGNORECASE),
    "documents": re.compile(r"(pay ?slips?|salary slips?|offer letter copy|experience letter)", re.IGNORECASE),
}
_QUOTE_START = re.compile(r"^(?:On .{5,200} wrote:|-----\s*Original Message\s*-----|From:\s.+)$", re.IGNORECASE)
_REQUEST_LINE = re.compile(
    r"([:?]\s*$|^\s*(?:[-*•]|\d+[.)])\s+|share|provide|send|let (?:me|us) know|mention|confirm|what is|what's)",
    re.IGNORECASE,
)


def strip_quoted_history(body: str) -> str:
    """Only analyse the newest message, not the quoted thread below it."""
    kept: list[str] = []
    for line in (body or "").splitlines():
        if line.lstrip().startswith(">") or _QUOTE_START.match(line.strip()):
            break
        kept.append(line)
    return "\n".join(kept)


def request_lines(body: str) -> str:
    """
    Lines that ask for something, plus the bare list that follows a request
    header such as "Please share:" (recruiters often list one field per line
    without bullets: "Notice period", "Current CTC", ...).
    """
    kept: list[str] = []
    in_list = False
    for line in strip_quoted_history(body).splitlines():
        stripped = line.strip()
        if _REQUEST_LINE.search(line):
            kept.append(line)
            in_list = stripped.endswith(":") or in_list
            continue
        if in_list and stripped and len(stripped) <= 80:
            kept.append(line)
            continue
        if in_list and (not stripped and kept and kept[-1].strip() and not kept[-1].strip().endswith(":")):
            in_list = False  # blank line after the list items ends the list
        elif stripped:
            in_list = False
    return "\n".join(kept)


def sender_address(from_header: str) -> str:
    match = re.search(r"<([^>]+)>", from_header or "")
    return (match.group(1) if match else (from_header or "")).strip().lower()


def _self_addresses(candidate_email: str | None) -> set[str]:
    addresses = {a.strip().lower() for a in os.getenv("RECRUITER_SELF_ADDRESSES", "").split(",") if a.strip()}
    if candidate_email:
        addresses.add(candidate_email.strip().lower())
    return addresses


def is_automated(from_header: str, subject: str, headers: dict[str, Any] | None,
                 candidate_email: str | None = None) -> tuple[bool, str | None]:
    address = sender_address(from_header)
    local = address.split("@")[0] if "@" in address else address
    domain = address.split("@")[-1] if "@" in address else ""
    lowered = {str(k).lower(): str(v) for k, v in (headers or {}).items()}

    if address in _self_addresses(candidate_email):
        return True, "SELF_SENT"
    if _NOREPLY_LOCAL.match(local):
        return True, "NO_REPLY_SENDER"
    blocked = {d.strip().lower() for d in os.getenv("RECRUITER_BLOCKED_SENDER_DOMAINS", "").split(",") if d.strip()}
    if domain and domain in blocked:
        return True, "BLOCKED_DOMAIN"
    if lowered.get("auto-submitted", "no").lower() not in {"no", ""}:
        return True, "AUTO_SUBMITTED_HEADER"
    if lowered.get("precedence", "").lower() in {"bulk", "list", "junk"}:
        return True, "BULK_PRECEDENCE"
    if "list-unsubscribe" in lowered or "list-id" in lowered:
        return True, "MAILING_LIST"
    if "x-autoreply" in lowered or "x-autorespond" in lowered:
        return True, "AUTOREPLY_HEADER"
    if _AUTOMATED_SUBJECT.search(subject or ""):
        return True, "AUTOMATED_SUBJECT"
    return False, None


def run_prefilter(from_header: str, subject: str, body: str, headers: dict[str, Any] | None,
                  candidate_email: str | None = None) -> dict[str, Any]:
    """Pure: deterministic checks on one e-mail. Returns a plain dict."""
    newest = strip_quoted_history(body)
    text = f"{subject}\n{newest}"
    role_hints = [role for role, pattern in _ROLE_PATTERNS.items() if pattern.search(text)]
    role_hint = role_hints[0] if role_hints else None
    asked = request_lines(body)
    fields = detect_fields(f"{subject}\n{asked}")
    recruiter_phrase = bool(_RECRUITER_PHRASES.search(text))
    recruiter_signal = bool(role_hint or fields or recruiter_phrase)
    automated, automated_reason = is_automated(from_header, subject, headers, candidate_email)

    # Recruiters sometimes send bulk-campaign outreach with List-Unsubscribe or
    # Precedence:list. Allow those messages through to AI classification only
    # when hiring content is clear; force manual review rather than auto-send.
    bulk_recruiter = False
    if (automated and automated_reason in {'MAILING_LIST', 'BULK_PRECEDENCE'}
            and recruiter_signal and (recruiter_phrase or fields)
            and '@' in sender_address(from_header)):
        automated, automated_reason, bulk_recruiter = False, None, True
    return {
        "automated": automated,
        "automated_reason": automated_reason,
        "recruiter_signal": recruiter_signal,
        "bulk_recruiter": bulk_recruiter,
        "role_hint": role_hint,
        "role_hints": role_hints,
        "deterministic_fields": fields,
        "sensitive_topics": [name for name, pattern in _SENSITIVE.items() if pattern.search(newest)],
        "unsupported_requests": [name for name, pattern in _UNSUPPORTED.items() if pattern.search(asked)],
    }
