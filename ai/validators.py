"""
ai/validators.py
================

1. ``validate_schema``  - small, dependency-free JSON-schema validator for the
   strict subset used in ``ai.schemas``. Used for EVERY provider, so browser
   output gets the same guarantees as API structured output.
2. ``provider_schema``  - copy of a schema without keywords some providers do
   not accept in structured-output mode (min/max etc.). Python still enforces
   them locally.
3. ``decide_cv_match``  - the deterministic 80/80 reuse rule. The LLM supplies
   scores; Python makes the MATCHED / NOT_MATCHED decision.
"""

from __future__ import annotations

import copy
import os
from typing import Any

from ai.errors import OutputValidationError

_UNSUPPORTED_PROVIDER_KEYWORDS = {
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "minLength",
    "maxLength",
    "minItems",
    "maxItems",
    "pattern",
    "format",
}

_TYPE_CHECKS = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "boolean": lambda v: isinstance(v, bool),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "null": lambda v: v is None,
}


def provider_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Return a deep copy without keywords that structured-output APIs reject."""

    def _strip(node: Any) -> Any:
        if isinstance(node, dict):
            return {
                key: _strip(value)
                for key, value in node.items()
                if key not in _UNSUPPORTED_PROVIDER_KEYWORDS
            }
        if isinstance(node, list):
            return [_strip(item) for item in node]
        return node

    return _strip(copy.deepcopy(schema))


def _schema_errors(value: Any, schema: dict[str, Any], path: str, errors: list[str]) -> None:
    if len(errors) >= 20:
        return

    expected = schema.get("type")
    if expected is not None:
        types = expected if isinstance(expected, list) else [expected]
        if not any(_TYPE_CHECKS[t](value) for t in types):
            errors.append(f"{path}: expected {types}, got {type(value).__name__}")
            return

    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: value {value!r} not in {schema['enum']}")

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path}: {value} < minimum {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path}: {value} > maximum {schema['maximum']}")

    if isinstance(value, dict):
        props = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"{path}.{key}: required property missing")
        if schema.get("additionalProperties") is False:
            for key in value:
                if key not in props:
                    errors.append(f"{path}.{key}: unexpected property")
        for key, sub in props.items():
            if key in value:
                _schema_errors(value[key], sub, f"{path}.{key}", errors)

    if isinstance(value, list) and "items" in schema:
        for index, item in enumerate(value):
            _schema_errors(item, schema["items"], f"{path}[{index}]", errors)


def validate_schema(
    value: Any,
    schema: dict[str, Any],
    *,
    operation: str,
    provider: str | None = None,
) -> Any:
    errors: list[str] = []
    _schema_errors(value, schema, "$", errors)
    if errors:
        raise OutputValidationError(
            f"{operation}: model output failed schema validation ({len(errors)} error(s)).",
            provider=provider,
            details={"schema_errors": errors},
        )
    return value


# ---------------------------------------------------------------------------
# CV match decision (Python is authoritative)
# ---------------------------------------------------------------------------

def cv_match_threshold() -> int:
    try:
        return int(os.getenv("CV_MATCH_MIN_SCORE", "80"))
    except ValueError:
        return 80


def cv_match_requires_no_critical_gaps() -> bool:
    # The existing new_cv_reuse_plan.md also requires "no critical mandatory
    # requirement missing". Kept ON by default to preserve current behaviour.
    return os.getenv("CV_MATCH_REQUIRE_NO_CRITICAL_GAPS", "true").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def meets_cv_match_threshold(score: Any) -> bool:
    """JD/ATS score rule: score >= CV_MATCH_MIN_SCORE (default 80)."""
    try:
        return float(score) >= cv_match_threshold()
    except (TypeError, ValueError):
        return False


def decide_cv_match(
    *,
    jd_match_score: Any,
    ats_compatibility_score: Any,
    candidate_cv_known: bool,
    critical_requirements_missing: list[str] | None,
) -> tuple[str, list[str]]:
    """
    Return (status, reasons). status is MATCHED only when BOTH scores pass the
    threshold, the CV exists in the local catalogue and (by default) no
    critical requirement is missing.
    """
    reasons: list[str] = []
    threshold = cv_match_threshold()
    if not meets_cv_match_threshold(jd_match_score):
        reasons.append(f"jd_match_score {jd_match_score} < {threshold}")
    if not meets_cv_match_threshold(ats_compatibility_score):
        reasons.append(f"ats_compatibility_score {ats_compatibility_score} < {threshold}")
    if not candidate_cv_known:
        reasons.append("no existing CV package from the local catalogue was selected")
    if cv_match_requires_no_critical_gaps() and critical_requirements_missing:
        reasons.append(f"{len(critical_requirements_missing)} critical requirement(s) missing")
    return ("MATCHED" if not reasons else "NOT_MATCHED"), reasons
