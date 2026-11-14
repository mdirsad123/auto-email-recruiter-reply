"""
ai/json_utils.py
================

Strict JSON extraction for model text (mostly the browser providers, which
cannot enforce a JSON schema). A parse failure is an OutputValidationError,
never a provider-availability problem.
"""

from __future__ import annotations

import json
import re
from typing import Any

from ai.errors import OutputValidationError

_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL | re.IGNORECASE)


def parse_json_object(text: str, *, provider: str | None = None) -> dict[str, Any]:
    if text is None or not str(text).strip():
        raise OutputValidationError("Model returned empty output.", provider=provider)

    candidate = str(text).strip()
    candidates = [candidate]
    candidates.extend(match.group(1) for match in _FENCE.finditer(candidate))

    first = candidate.find("{")
    last = candidate.rfind("}")
    if first != -1 and last > first:
        candidates.append(candidate[first : last + 1])

    errors = []
    for item in candidates:
        try:
            value = json.loads(item)
        except json.JSONDecodeError as exc:
            errors.append(str(exc))
            continue
        if isinstance(value, dict):
            return value
        errors.append("top-level JSON value is not an object")

    raise OutputValidationError(
        "Model output is not a valid JSON object.",
        provider=provider,
        details={"parse_errors": errors[:3], "output_chars": len(candidate)},
    )
