"""
ai/schemas.py
=============

JSON schemas for every AI operation.

The schemas follow the strict subset accepted by BOTH Anthropic structured
outputs (output_config.format) and OpenAI Responses structured outputs
(text.format, strict=true):
  * every object has "additionalProperties": false
  * every property is listed in "required" (optional values use a null type)

The same dicts are used locally by ``ai.validators.validate_schema`` so the
browser providers (which cannot enforce a schema) get exactly the same check.
"""

from __future__ import annotations

from typing import Any

PIPELINE_ROLES = ["aiengineer", "awsdataengineer", "azuredataengineer", "pythondeveloper"]
GROUP_CATEGORIES = PIPELINE_ROLES + ["other"]


def _obj(properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties.keys()),
        "additionalProperties": False,
    }


_STR = {"type": "string"}
_NULLABLE_STR = {"type": ["string", "null"]}
_BOOL = {"type": "boolean"}
_SCORE = {"type": "integer", "minimum": 0, "maximum": 100}
_STR_LIST = {"type": "array", "items": {"type": "string"}}


# ---------------------------------------------------------------------------
# 1. Job grouping
# ---------------------------------------------------------------------------
# The model returns ONLY assignments by job id. Python keeps the original job
# blocks verbatim, applies the invalid-email / <5-years overrides, builds the
# contact headers, writes the five role files and creates the ZIP.

GROUP_JOBS_SCHEMA = _obj(
    {
        "run_date": _STR,
        "assignments": {
            "type": "array",
            "items": _obj(
                {
                    "job_id": _STR,
                    "category": {"type": "string", "enum": GROUP_CATEGORIES},
                    "invalid_email": _BOOL,
                    "experience_below_5_years": _BOOL,
                    "reason": _STR,
                }
            ),
        },
        "excluded_non_hiring": {
            "type": "array",
            "items": _obj({"job_id": _STR, "reason": _STR}),
        },
    }
)


# ---------------------------------------------------------------------------
# 2. CV / JD matching
# ---------------------------------------------------------------------------
# The model returns scores + the best candidate CV id per role. It does NOT set
# MATCHED/NOT_MATCHED: Python applies the 80/80 rule.

CV_MATCH_SCHEMA = _obj(
    {
        "run_date": _STR,
        "roles": {
            "type": "array",
            "items": _obj(
                {
                    "role": {"type": "string", "enum": PIPELINE_ROLES},
                    "best_candidate_cv_id": _NULLABLE_STR,
                    "jd_match_score": _SCORE,
                    "ats_compatibility_score": _SCORE,
                    "critical_requirements_missing": _STR_LIST,
                    "explanation": _STR,
                }
            ),
        },
    }
)


# ---------------------------------------------------------------------------
# 3. CV package generation (one role per request)
# ---------------------------------------------------------------------------
# The model returns TEXT content only. Python writes MD/DOCX/PDF/POC/ZIP.

CV_GENERATE_SCHEMA = _obj(
    {
        "role": {"type": "string", "enum": PIPELINE_ROLES},
        "cv_markdown": _STR,
        "email_reply_text": _STR,
        "interview_prep_markdown": _STR,
        "rapid_fire_markdown": _STR,
        "poc_index_markdown": _STR,
        "poc_guides": {
            "type": "array",
            "items": _obj({"slug": _STR, "title": _STR, "markdown": _STR}),
        },
    }
)


# ---------------------------------------------------------------------------
# 4. Recruiter e-mail classification
# ---------------------------------------------------------------------------

RECRUITER_ROLES = [
    "aiengineer",
    "awsdataengineer",
    "azuredataengineer",
    "dataengineer",
    "pythondeveloper",
    "other",
    "unknown",
]

RECRUITER_INTENTS = [
    "REQUEST_CANDIDATE_DETAILS",
    "REQUEST_RESUME",
    "AVAILABILITY_CHECK",
    "INTERVIEW_SCHEDULING",
    "JOB_OPPORTUNITY",
    "SALARY_NEGOTIATION",
    "OFFER_OR_CONTRACT",
    "REJECTION",
    "APPLICATION_ACKNOWLEDGEMENT",
    "OTHER",
]


def recruiter_classify_schema(field_keys: list[str]) -> dict[str, Any]:
    """
    Chat 1. Besides intent/role/fields the model reports the technical skills
    asked about, facts it found in experience_details.md WITH a verbatim
    evidence quote (Python re-checks every quote against the latest local file),
    what is missing/ambiguous, and the Project document version it saw.
    """
    return _obj(
        {
            "is_recruiter_email": _BOOL,
            "role": {"type": "string", "enum": RECRUITER_ROLES},
            "intent": {"type": "string", "enum": RECRUITER_INTENTS},
            "requested_fields": {
                "type": "array",
                "items": {"type": "string", "enum": field_keys},
            },
            "requested_skills": _STR_LIST,
            "extracted_facts": {
                "type": "array",
                "items": _obj(
                    {
                        "field_key": {"type": "string", "enum": field_keys},
                        "value": _STR,
                        "evidence": _STR,
                    }
                ),
            },
            "missing_or_ambiguous": _STR_LIST,
            "knowledge_version_seen": _NULLABLE_STR,
            "unmapped_requests": _STR_LIST,
            "sensitive_topics": _STR_LIST,
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "requires_manual_review": _BOOL,
            "reason": _STR,
        }
    )


# ---------------------------------------------------------------------------
# 5. Recruiter e-mail reply wording
# ---------------------------------------------------------------------------
# The model writes wording only. Candidate facts are inserted by Python from
# experience_details.md; the model may reference them only as {{FIELD_KEY}}.
# "greeting" is kept for compatibility but ignored: Python always writes
# "Dear Recruiter," and the configured signature.

RECRUITER_REPLY_SCHEMA = _obj(
    {
        "greeting": _STR,
        "opening_paragraph": _STR,
        "closing_paragraph": _STR,
        "used_fields": _STR_LIST,
        "invented_values": _BOOL,
    }
)
