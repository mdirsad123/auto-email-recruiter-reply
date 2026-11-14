"""
ai/provider_result.py
=====================

Provider-neutral data shapes.

* ``AIRequest``  - one canonical request, identical for every provider
  (a frozen-style dataclass used purely as a typed record, no behaviour).
* provider result - a plain dict built by ``make_result()``.
* attempt record  - a plain dict built by ``make_attempt()``.
* router outcome  - a plain dict returned by ``execute_ai_task()``.

Plain dicts keep logging, JSON manifests and tests trivial.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Result status values
COMPLETED = "COMPLETED"   # structured data is available now
SUBMITTED = "SUBMITTED"   # async Claude web artifact flow; collected later by /cloude-download-*


@dataclass
class AIRequest:
    operation: str
    system_prompt: str
    user_prompt: str
    # JSON schema (strict subset understood by both Anthropic and OpenAI).
    output_schema: dict[str, Any] | None = None
    schema_name: str = "jobbot_output"
    max_output_tokens: int = 16000
    # Small, non-sensitive context used for logs (role, date, counts...).
    log_context: dict[str, Any] = field(default_factory=dict)
    # Files that browser providers should attach (API providers inline text).
    attachments: list[Path] = field(default_factory=list)
    # Operation-specific data the browser providers need to call the EXISTING
    # browser automation (e.g. input_path, folder_path, role).
    web_payload: dict[str, Any] = field(default_factory=dict)
    # "low" | "medium" | "high" (mapped to provider-specific effort)
    effort: str = "medium"
    # Candidate knowledge reference for recruiter e-mail operations, e.g.
    # {"document": "experience_details.md", "version": ..., "revision": ..., "purpose": "classify"}.
    # Web providers use it to open the configured Claude / ChatGPT Project and to
    # reference the Project knowledge file explicitly; API providers ignore it
    # (they receive the local excerpts inside user_prompt).
    knowledge: dict[str, Any] = field(default_factory=dict)


def make_result(provider: str, operation: str, *, data: Any = None, status: str = COMPLETED,
                raw_text: str | None = None, model: str | None = None,
                usage: dict[str, Any] | None = None, handoff: dict[str, Any] | None = None) -> dict[str, Any]:
    """What every call_* provider function returns."""
    return {
        "provider": provider,
        "operation": operation,
        "status": status,
        "data": data,
        "raw_text": raw_text,
        "model": model,
        "usage": usage or {},
        "handoff": handoff or {},
    }


def make_attempt(*, run_id: str, operation: str, provider: str, attempt: int, result: str,
                 duration_ms: int, failure_type: str | None = None, fallback_reason: str | None = None,
                 error: str | None = None, context: dict[str, Any] | None = None) -> dict[str, Any]:
    """One log line per provider attempt. result: SUCCESS|SUBMITTED|RETRY|FALLBACK|SKIPPED|FAILED."""
    return {
        "run_id": run_id,
        "operation": operation,
        "provider": provider,
        "attempt": attempt,
        "result": result,
        "duration_ms": duration_ms,
        "failure_type": failure_type,
        "fallback_reason": fallback_reason,
        "error": error,
        "context": dict(context or {}),
    }


def is_completed(outcome: dict[str, Any]) -> bool:
    return outcome.get("status") == COMPLETED
