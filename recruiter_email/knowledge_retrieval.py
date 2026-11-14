"""
recruiter_email/knowledge_retrieval.py
======================================

Lightweight hybrid retrieval over experience_details.md (no vector database):

1. ``build_knowledge_context``  - pick the few Markdown paragraphs relevant to
   one recruiter e-mail (role terms, requested fields, skill aliases, the
   sentences that back structured facts) and drop confidential sentences
   (CTC, phone, e-mail, IDs ...). This is the only candidate text an AI sees.
2. ``resolve_target_role``      - which role "Relevant Experience" refers to.
3. ``resolve_requested_fields`` - verified value per requested field, with the
   role-aware rule for RELEVANT_EXPERIENCE_YEARS.
4. ``verify_ai_facts``          - accept AI-extracted facts ONLY when the quoted
   evidence exists in the latest local Markdown and contains the value.
5. ``unsupported_numbers``      - final check that every number in the reply
   comes from a verified value.

All functions are pure.
"""

from __future__ import annotations

import re
from typing import Any

from recruiter_email.field_registry import FIELDS, is_sensitive, label_for
from recruiter_email.knowledge_aliases import (
    COMPATIBLE_ROLES,
    ROLE_AREAS,
    ROLE_LABELS,
    ROLE_RELEVANT_KEYS,
    SKILLS,
    aliases_for,
    skills_mentioned,
)
from recruiter_email.knowledge_base import comparable, is_missing, normalise_text

RELEVANT_KEY = "RELEVANT_EXPERIENCE_YEARS"

# Sentences with these never leave the machine inside an AI prompt.
CONFIDENTIAL = re.compile(
    r"(\bctc\b|salary|\blpa\b|\blakhs?\b|\bcrores?\b|compensation|\bpackage\b|\bpay\b|"
    r"phone|mobile|whats\s*app|\bcontact\s+(?:no|number)|@|https?://|www\.|\+?\d[\d\s\-()]{8,}\d|"
    r"aadha?r|\bpan\b|passport|date of birth|\bdob\b|\baddress\b|bank|ifsc|\bssn\b)",
    re.IGNORECASE,
)
# KEY=VALUE facts that may appear in prompts (experience only, never contact / money / location).
_PROMPT_SAFE_KEY = re.compile(r"(EXPERIENCE|_YEARS$|^FULL_NAME$|^PROFILE_SUMMARY$|^SKILLS?$|^CURRENT_ROLE$)")
# Facts an AI may help extract (never sensitive or contact values).
AI_EXTRACTABLE = re.compile(r"(EXPERIENCE|_YEARS$|^FULL_NAME$|^CURRENT_ROLE$)")

_ROLE_TERMS = {
    "aiengineer": "ai engineer engineering genai generative llm agent agents agentic machine learning",
    "awsdataengineer": "aws data engineer engineering glue lambda redshift s3 emr pyspark spark",
    "azuredataengineer": "azure data engineer engineering databricks adf synapse pyspark spark",
    "dataengineer": "data engineer engineering pipelines etl pyspark spark",
    "pythondeveloper": "python developer development backend fastapi rest api",
}
_STOP = set("i a an the and or of in on with to for is are was my me have has had as at by be this that it from "
            "years year experience please share details".split())


def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z][a-z0-9+#.-]{1,}", (text or "").lower()) if t not in _STOP}


def safe_text(text: str) -> str:
    """Drop confidential sentences from one block of text."""
    parts = re.split(r"(?<=[.!?;])\s+", text)
    return " ".join(p for p in parts if p.strip() and not CONFIDENTIAL.search(p)).strip()


def safe_block(block: dict[str, str]) -> str:
    if block["kind"] == "kv":
        key, _, value = block["text"].partition("=")
        key = key.strip(" -*`")
        if not _PROMPT_SAFE_KEY.search(key) or is_sensitive(key):
            return ""
        return f"{key}={safe_text(value)}" if safe_text(value) else ""
    return safe_text(block["text"])


def build_knowledge_context(kb: dict[str, Any], email_text: str, *, role_hint: str | None = None,
                            requested_keys: list[str] | None = None, max_chars: int = 3500,
                            max_blocks: int = 14) -> dict[str, Any]:
    """
    Retrieve the relevant, non-confidential parts of experience_details.md for one e-mail.
    Returns {"text", "blocks_used", "skills_in_email", "revision", "version", "sha256"}.
    """
    skills = skills_mentioned(email_text)
    query = _tokens(email_text) | _tokens(_ROLE_TERMS.get(role_hint or "", ""))
    for key in (requested_keys or []) + skills:
        query |= _tokens(label_for(key)) | _tokens(" ".join(aliases_for(key)))
    evidence_texts = {normalise_text(e) for values in kb.get("evidence", {}).values() for e in values}

    scored: list[tuple[float, int, str, str]] = []
    for index, block in enumerate(kb.get("blocks", [])):
        text = safe_block(block)
        if not text:
            continue
        words = _tokens(text) | _tokens(block["heading"])
        score = float(len(words & query))
        normalised = normalise_text(text)
        if any(e and (e in normalised or normalised in e) for e in evidence_texts):
            score += 3.0  # backs a structured fact (name, total / relevant / skill experience)
        if re.search(r"\b(?:overall|total|relevant)\b", text, re.IGNORECASE):
            score += 2.0
        if score > 0:
            scored.append((score, index, block["heading"], text))

    chosen = sorted(sorted(scored, key=lambda s: (-s[0], s[1]))[:max_blocks], key=lambda s: s[1])
    lines: list[str] = []
    used = 0
    for _score, _index, heading, text in chosen:
        line = f"[{heading or 'General'}] {text}"
        if used + len(line) > max_chars:
            break
        lines.append(line)
        used += len(line) + 1
    return {
        "text": "\n".join(lines) or "(no relevant non-confidential excerpt found)",
        "blocks_used": len(lines),
        "skills_in_email": skills,
        "revision": kb.get("revision"),
        "version": kb.get("version"),
        "sha256": kb.get("sha256"),
        "document": kb.get("document"),
    }


# ---------------------------------------------------------------------------
# Role-aware resolution
# ---------------------------------------------------------------------------

def resolve_target_role(classified_role: str | None, role_hints: list[str] | None) -> tuple[str | None, list[str]]:
    """
    The role a generic "Relevant Experience" question refers to.
    Classifier role first; the pre-filter hints only break ties or flag ambiguity.
    """
    hints = [h for h in dict.fromkeys(role_hints or []) if h in ROLE_RELEVANT_KEYS]
    if classified_role in ROLE_RELEVANT_KEYS:
        clashes = [h for h in hints if h != classified_role and frozenset({h, classified_role}) not in COMPATIBLE_ROLES]
        return classified_role, (["ROLE_AMBIGUOUS"] if clashes else [])
    specific = [h for h in hints if h != "dataengineer"] or hints
    if len(specific) == 1:
        return specific[0], []
    return None, (["ROLE_AMBIGUOUS"] if specific else ["RELEVANT_EXPERIENCE_ROLE_UNKNOWN"])


def resolve_requested_fields(kb: dict[str, Any], requested: list[str], role: str | None) -> dict[str, Any]:
    """
    -> {"values": {KEY: raw value}, "missing": [KEY], "reasons": [...], "sources": {KEY: source key}}
    Conflicting keys (document contradicts itself) are missing + CONFLICTING_PROFILE_VALUE.
    """
    facts, conflicts = kb.get("facts", {}), kb.get("conflicts", {})
    values: dict[str, str] = {}
    sources: dict[str, str] = {}
    missing: list[str] = []
    reasons: list[str] = []
    for key in requested:
        lookup = (key,)
        if key == RELEVANT_KEY:
            lookup = ROLE_RELEVANT_KEYS.get(role or "", ())
            if not lookup:
                missing.append(key)
                continue
        hit = None
        for candidate in lookup:
            if candidate in conflicts:
                reasons.append(f"CONFLICTING_PROFILE_VALUE:{candidate}")
                break
            if not is_missing(facts.get(candidate)):
                hit = candidate
                break
        if hit:
            values[key] = facts[hit].strip()
            sources[key] = hit
        else:
            missing.append(key)
    return {"values": values, "missing": missing, "reasons": sorted(set(reasons)), "sources": sources}


# ---------------------------------------------------------------------------
# AI-extracted facts: accepted only with evidence in the local Markdown
# ---------------------------------------------------------------------------

def _numbers(text: str) -> list[str]:
    return re.findall(r"\d+(?:\.\d+)?", text or "")


def evidence_supports(kb: dict[str, Any], key: str, value: str, evidence: str, role: str | None) -> str | None:
    """None when the evidence is valid, else the rejection reason."""
    quote = normalise_text(evidence)
    if len(quote) < 12:
        return "EVIDENCE_TOO_SHORT"
    if quote not in normalise_text(kb.get("text", "")):
        return "EVIDENCE_NOT_IN_LOCAL_DOCUMENT"
    value_numbers = _numbers(value)
    if value_numbers:
        if not set(value_numbers) <= set(_numbers(quote)):
            return "VALUE_NOT_IN_EVIDENCE"
    elif normalise_text(value) not in quote:
        return "VALUE_NOT_IN_EVIDENCE"
    area_key = (ROLE_RELEVANT_KEYS.get(role or "", (None,))[0]) if key == RELEVANT_KEY else key
    if key == RELEVANT_KEY and area_key is None:
        return "RELEVANT_EXPERIENCE_ROLE_UNKNOWN"
    aliases = aliases_for(area_key) if area_key else ()
    if aliases and not any(re.search(r"(?<![\w-])" + re.escape(a) + r"(?![\w-])", quote) for a in aliases):
        return "EVIDENCE_DOES_NOT_NAME_THE_SKILL_OR_ROLE"
    if key == "TOTAL_EXPERIENCE_YEARS" and not re.search(r"\b(?:overall|total|professional)\b", quote):
        return "EVIDENCE_NOT_TOTAL_EXPERIENCE"
    return None


def verify_ai_facts(kb: dict[str, Any], extracted: list[dict[str, Any]], resolution: dict[str, Any],
                    requested: list[str], role: str | None) -> dict[str, Any]:
    """
    Merge AI-extracted facts into a resolution, safely.
      * deterministic value present and equal  -> confirmed
      * deterministic value present, different -> AI_FACT_CONFLICT (manual review)
      * deterministic value missing            -> accepted only with valid local evidence
    Returns {"values", "missing", "accepted", "rejected", "reasons"}.
    """
    values = dict(resolution["values"])
    missing = list(resolution["missing"])
    accepted: list[str] = []
    rejected: list[dict[str, str]] = []
    reasons: list[str] = []
    for item in extracted or []:
        key, value, evidence = item.get("field_key"), str(item.get("value") or ""), str(item.get("evidence") or "")
        if key not in requested or is_missing(value):
            continue
        if not AI_EXTRACTABLE.search(key) or is_sensitive(key):
            rejected.append({"field_key": key, "reason": "FIELD_NOT_AI_EXTRACTABLE"})
            continue
        if key in values:
            if comparable(values[key]) != comparable(value):
                reasons.append(f"AI_FACT_CONFLICT:{key}")
            continue
        problem = evidence_supports(kb, key, value, evidence, role)
        if problem:
            rejected.append({"field_key": key, "reason": problem})
            continue
        values[key] = value.strip()
        missing = [k for k in missing if k != key]
        accepted.append(key)
    if accepted:
        reasons.append("AI_ASSISTED_FACT_USED")
    if rejected:
        reasons.append("AI_FACT_REJECTED")
    return {"values": values, "missing": missing, "accepted": accepted, "rejected": rejected,
            "reasons": sorted(set(reasons))}


def unsupported_numbers(text: str, allowed_values: list[str]) -> list[str]:
    """Numbers in ``text`` that do not occur in any verified value (hallucination guard)."""
    allowed = {n for value in allowed_values for n in _numbers(value)}
    return sorted({n for n in _numbers(text) if n not in allowed})


def role_label(role: str | None) -> str | None:
    return ROLE_LABELS.get(role or "")


def known_field_keys(kb: dict[str, Any]) -> list[str]:
    """Every key the classifier may request: registry + document facts + skill / role-area keys."""
    return sorted(set(FIELDS) | set(kb.get("facts", {})) | set(kb.get("conflicts", {})) | set(SKILLS) | set(ROLE_AREAS))
