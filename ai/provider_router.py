"""
ai/provider_router.py
=====================

Function-based provider router: the ONE place where provider selection,
retries and fallback decisions live. Every AI task in JobBot (grouping, CV
matching, CV generation, recruiter e-mail classification and reply wording)
calls ``execute_ai_task(request, validator=...)``.

Selection
---------
Only the four .env flags decide which providers may run, always in this fixed
order (see ai/settings.py):

    USE_CLAUDE_API  -> claude_api   (Anthropic Messages API)
    USE_CHATGPT_API -> chatgpt_api  (OpenAI Responses API)
    USE_CLAUDE_WEB  -> claude_web   (existing Claude web Selenium flow)
    USE_CHATGPT_WEB -> chatgpt_web  (ChatGPT web chat)

A provider whose flag is false is never called. All flags false ->
AIConfigurationError.

Fallback
--------
ProviderUnavailableError (429, quota, 5xx, 529, timeout, network, missing
API key, browser down / logged out / usage limit)
    -> retry the same API provider with exponential backoff (if retryable),
       then try the NEXT ENABLED provider.
Anything else (invalid input, rejected request, output validation, model
refusal, bugs)
    -> stop immediately, log, re-raise. No fallback.
Every enabled provider unavailable
    -> AllProvidersUnavailableError (HTTP 503). The caller has written
       nothing yet, so existing outputs are untouched.
"""

from __future__ import annotations

import random
import time
import uuid
from typing import Any, Callable

from ai.errors import (
    AIConfigurationError,
    AllProvidersUnavailableError,
    ApplicationError,
    FailureType,
    JobBotAIError,
    ProviderUnavailableError,
)
from ai.observability import log_attempt, logger, redact
from ai.provider_result import COMPLETED, AIRequest, make_attempt
from ai.providers.anthropic_api import call_claude_api
from ai.providers.chatgpt_web import call_chatgpt_web
from ai.providers.claude_web import call_claude_web
from ai.providers.openai_api import call_openai_api
from ai.settings import PROVIDER_KINDS, PROVIDER_PRIORITY, load_ai_config, provider_settings

ProviderFunction = Callable[[AIRequest, dict], dict]
Validator = Callable[[Any, dict], Any]

# Fixed priority order (dicts keep insertion order).
PROVIDER_FUNCTIONS: dict[str, ProviderFunction] = {
    "claude_api": call_claude_api,
    "chatgpt_api": call_openai_api,
    "claude_web": call_claude_web,
    "chatgpt_web": call_chatgpt_web,
}


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def get_enabled_providers(config: dict[str, Any]) -> list[str]:
    """Providers whose USE_* flag is true, in the fixed priority order."""
    flags = config.get("flags", {})
    return [name for name in PROVIDER_PRIORITY if flags.get(name, False)]


def max_attempts_for(provider: str, retry: dict[str, Any]) -> int:
    retries = retry["api_max_retries"] if PROVIDER_KINDS.get(provider) == "api" else retry["web_max_retries"]
    return 1 + max(0, int(retries))


def handle_provider_fallback(error: ProviderUnavailableError, attempt: int, max_attempts: int) -> str:
    """Decide what to do after an availability failure: 'retry' the same provider or 'fallback'."""
    return "retry" if error.retryable and attempt < max_attempts else "fallback"


def backoff_seconds(attempt: int, error: ProviderUnavailableError, retry: dict[str, Any],
                    jitter: Callable[[], float] = random.random) -> float:
    """Exponential backoff (2s, 4s, ... capped), honouring retry-after, plus up to 25% jitter."""
    cap = float(retry["max_delay_seconds"])
    delay = min(cap, float(retry["initial_delay_seconds"]) * (2 ** (attempt - 1)))
    if error.retry_after_seconds:
        delay = min(cap, max(delay, float(error.retry_after_seconds)))
    return delay + 0.25 * delay * jitter()


def validate_ai_response(result: dict[str, Any], validator: Validator | None) -> dict[str, Any]:
    """Run the caller's validator on COMPLETED results; it raises OutputValidationError on bad data."""
    if validator is None or result.get("status") != COMPLETED:
        return result
    return {**result, "data": validator(result["data"], result)}


# ---------------------------------------------------------------------------
# Side-effecting steps (provider calls + logging), kept small
# ---------------------------------------------------------------------------

def _record(attempts: list[dict], log: Callable[[dict], None], **fields: Any) -> None:
    record = make_attempt(**fields)
    attempts.append(record)
    log(record)


def try_provider(name: str, request: AIRequest, *, config: dict[str, Any], function: ProviderFunction,
                 validator: Validator | None, run_id: str, attempts: list[dict],
                 sleep: Callable[[float], None], log: Callable[[dict], None]) -> dict[str, Any] | None:
    """
    Call one enabled provider (with retries). Returns the validated result, or
    None when the provider is unavailable and the router should fall back.
    Non-availability errors propagate (no fallback).
    """
    context = dict(request.log_context)
    settings = provider_settings(config, name)
    limit = max_attempts_for(name, config["retry"])
    base = {"run_id": run_id, "operation": request.operation, "provider": name}

    for attempt in range(1, limit + 1):
        started = time.monotonic()
        try:
            result = validate_ai_response(function(request, settings), validator)
        except ProviderUnavailableError as exc:
            exc.provider = exc.provider or name
            decision = handle_provider_fallback(exc, attempt, limit)
            label = "RETRY" if decision == "retry" else (
                "SKIPPED" if exc.failure_type == FailureType.NOT_CONFIGURED else "FALLBACK")
            _record(attempts, log, **base, attempt=attempt, result=label,
                    duration_ms=int((time.monotonic() - started) * 1000), failure_type=exc.failure_type,
                    fallback_reason=exc.failure_type, error=redact(exc), context=context)
            if decision == "retry":
                sleep(backoff_seconds(attempt, exc, config["retry"]))
                continue
            return None
        except JobBotAIError as exc:
            exc.provider = exc.provider or name
            _record(attempts, log, **base, attempt=attempt, result="FAILED",
                    duration_ms=int((time.monotonic() - started) * 1000), failure_type=exc.failure_type,
                    error=redact(exc), context=context)
            exc.details.setdefault("attempts", list(attempts))
            raise
        except Exception as exc:  # noqa: BLE001 - programming error, never fall back
            _record(attempts, log, **base, attempt=attempt, result="FAILED",
                    duration_ms=int((time.monotonic() - started) * 1000),
                    failure_type=FailureType.APPLICATION_ERROR,
                    error=redact(f"{type(exc).__name__}: {exc}"), context=context)
            raise ApplicationError(
                f"{request.operation}: unexpected {type(exc).__name__} in provider {name}: {exc}",
                provider=name, details={"attempts": list(attempts)},
            ) from exc

        _record(attempts, log, **base, attempt=attempt,
                result="SUCCESS" if result["status"] == COMPLETED else "SUBMITTED",
                duration_ms=int((time.monotonic() - started) * 1000),
                context={**context, "model": result.get("model") or ""})
        return result
    return None


def execute_ai_task(
    request: AIRequest,
    *,
    validator: Validator | None = None,
    config: dict[str, Any] | None = None,
    provider_functions: dict[str, ProviderFunction] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    log: Callable[[dict], None] = log_attempt,
    run_id: str | None = None,
) -> dict[str, Any]:
    """
    Run one AI task through the enabled providers.

    Returns the outcome dict:
        {"run_id", "operation", "provider", "status", "data", "handoff",
         "model", "attempts", "enabled_providers"}
    """
    config = config or load_ai_config()
    functions = provider_functions or PROVIDER_FUNCTIONS
    run_id = run_id or uuid.uuid4().hex[:12]
    enabled = get_enabled_providers(config)

    if not enabled:
        logger.error("ai_task operation=%s: every USE_* provider flag is false", request.operation)
        raise AIConfigurationError(
            "No AI provider is enabled. Set at least one of USE_CLAUDE_API, USE_CHATGPT_API, "
            "USE_CLAUDE_WEB, USE_CHATGPT_WEB to true in .env.",
            details={"operation": request.operation, "flags": config.get("flags", {})},
        )

    logger.info("ai_task run=%s operation=%s enabled_providers=%s", run_id, request.operation, enabled)
    attempts: list[dict] = []
    for name in enabled:
        logger.info("ai_task run=%s operation=%s trying provider=%s", run_id, request.operation, name)
        result = try_provider(name, request, config=config, function=functions[name], validator=validator,
                              run_id=run_id, attempts=attempts, sleep=sleep, log=log)
        if result is not None:
            logger.info("ai_task run=%s operation=%s selected provider=%s status=%s",
                        run_id, request.operation, name, result["status"])
            return {
                "run_id": run_id,
                "operation": request.operation,
                "provider": name,
                "status": result["status"],
                "data": result.get("data"),
                "handoff": result.get("handoff") or {},
                "model": result.get("model"),
                "attempts": attempts,
                "enabled_providers": enabled,
            }

    raise AllProvidersUnavailableError(request.operation, attempts)
