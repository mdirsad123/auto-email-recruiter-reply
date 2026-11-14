"""
ai/providers/openai_api.py
==========================

ChatGPT/OpenAI API provider (enabled by USE_CHATGPT_API) using the Responses API (``client.responses.create``) with
strict JSON-schema structured output (``text.format``).

Same rules as the Anthropic provider: SDK retries disabled, availability
errors -> ProviderUnavailableError, request/output defects -> no fallback.

Entry point: ``call_openai_api(request, settings, client=None) -> result dict``.
"""

from __future__ import annotations

import os
from typing import Any

from functools import lru_cache

from ai.errors import (
    FailureType,
    InvalidRequestError,
    OutputValidationError,
    ProviderUnavailableError,
    provider_not_configured,
)
from ai.json_utils import parse_json_object
from ai.provider_result import AIRequest, make_result
from ai.validators import provider_schema

PROVIDER = "chatgpt_api"


def _retry_after(exc: Any) -> float | None:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None) or {}
    try:
        value = headers.get("retry-after")
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def classify_openai_error(exc: Exception, provider: str = PROVIDER) -> Exception:
    import openai

    message = str(exc)
    lowered = message.lower()

    if isinstance(exc, openai.APITimeoutError):
        return ProviderUnavailableError(message, failure_type=FailureType.TIMEOUT, provider=provider)
    if isinstance(exc, openai.APIConnectionError):
        return ProviderUnavailableError(message, failure_type=FailureType.NETWORK, provider=provider)

    if isinstance(exc, openai.APIStatusError):
        status = int(getattr(exc, "status_code", 0) or 0)
        code = str(getattr(exc, "code", "") or "")
        if status == 429:
            quota = code == "insufficient_quota" or "quota" in lowered or "billing" in lowered
            return ProviderUnavailableError(
                message,
                failure_type=FailureType.QUOTA_EXHAUSTED if quota else FailureType.RATE_LIMIT,
                provider=provider,
                retryable=not quota,
                retry_after_seconds=_retry_after(exc),
                status_code=status,
            )
        if status >= 500:
            return ProviderUnavailableError(
                message, failure_type=FailureType.SERVER_ERROR, provider=provider,
                retry_after_seconds=_retry_after(exc), status_code=status,
            )
        if status in (401, 403):
            return ProviderUnavailableError(
                message, failure_type=FailureType.AUTH, provider=provider, retryable=False, status_code=status
            )
        if status == 408:
            return ProviderUnavailableError(message, failure_type=FailureType.TIMEOUT, provider=provider)
        return InvalidRequestError(
            f"OpenAI rejected the request (HTTP {status}): {message}",
            provider=provider,
            details={"status_code": status, "code": code},
        )
    return exc


def openai_model(settings: dict[str, Any]) -> str:
    return os.getenv(settings.get("model_env", "OPENAI_MODEL"), "").strip() or settings.get("default_model", "gpt-5")


@lru_cache(maxsize=4)
def _sdk_client(timeout_seconds: float) -> Any:
    from openai import OpenAI

    # Talks to api.openai.com directly (not the OpenRouter client used elsewhere).
    # max_retries=0: execute_ai_task() owns retry/backoff and logging.
    return OpenAI(max_retries=0, timeout=timeout_seconds)


def get_openai_client(settings: dict[str, Any]) -> Any:
    if not os.getenv("OPENAI_API_KEY", "").strip():
        raise provider_not_configured("OPENAI_API_KEY is not set.", PROVIDER)
    try:
        return _sdk_client(float(settings.get("timeout_seconds", 120.0)))
    except ImportError as exc:
        raise provider_not_configured("The 'openai' package is not installed.", PROVIDER) from exc


def build_openai_request(request: AIRequest, settings: dict[str, Any]) -> dict[str, Any]:
    """Pure: Responses API keyword arguments for one request."""
    kwargs: dict[str, Any] = {
        "model": openai_model(settings),
        "instructions": request.system_prompt,
        "input": request.user_prompt,
        "max_output_tokens": request.max_output_tokens,
        "store": False,
    }
    effort_map = settings.get("reasoning_effort_map", {})
    if request.effort and effort_map:
        kwargs["reasoning"] = {"effort": effort_map.get(request.effort, request.effort)}
    if request.output_schema is not None:
        kwargs["text"] = {"format": {"type": "json_schema", "name": request.schema_name,
                                     "schema": provider_schema(request.output_schema), "strict": True}}
    return kwargs


def check_response_status(response: Any) -> None:
    status = getattr(response, "status", "completed")
    if status == "incomplete":
        reason = getattr(getattr(response, "incomplete_details", None), "reason", "unknown")
        raise OutputValidationError(
            f"OpenAI response incomplete ({reason}).",
            failure_type=FailureType.OUTPUT_TRUNCATED if reason == "max_output_tokens" else FailureType.OUTPUT_VALIDATION,
            provider=PROVIDER,
        )
    if status == "failed":
        error = getattr(response, "error", None)
        raise ProviderUnavailableError(f"OpenAI response failed: {getattr(error, 'message', error)}",
                                       failure_type=FailureType.SERVER_ERROR, provider=PROVIDER)


def call_openai_api(request: AIRequest, settings: dict[str, Any], client: Any = None) -> dict[str, Any]:
    client = client or get_openai_client(settings)
    kwargs = build_openai_request(request, settings)
    try:
        response = client.responses.create(**kwargs)
    except Exception as exc:  # noqa: BLE001
        mapped = classify_openai_error(exc, PROVIDER)
        if mapped is exc:
            raise
        raise mapped from exc

    check_response_status(response)
    text = getattr(response, "output_text", "") or ""
    if not text:
        # Refusals arrive as a refusal content part instead of text.
        raise OutputValidationError("OpenAI returned no text output (possible refusal).",
                                    failure_type=FailureType.MODEL_REFUSAL, provider=PROVIDER)
    usage = getattr(response, "usage", None)
    return make_result(
        PROVIDER, request.operation,
        data=parse_json_object(text, provider=PROVIDER) if request.output_schema else None,
        raw_text=text,
        model=getattr(response, "model", None) or kwargs["model"],
        usage={"input_tokens": getattr(usage, "input_tokens", None),
               "output_tokens": getattr(usage, "output_tokens", None)},
    )
