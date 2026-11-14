"""
ai/providers/anthropic_api.py
=============================

Claude API provider (official ``anthropic`` Python SDK), enabled by USE_CLAUDE_API.

Entry point: ``call_claude_api(request, settings, client=None) -> result dict``.

* Structured JSON via ``output_config.format`` (json_schema).
* Streaming + ``get_final_message()`` so large CV packages never hit HTTP
  timeouts.
* The large, stable system prompt (master prompt / grouping rules) is marked
  for prompt caching, so repeated daily calls are cheaper.
* SDK-internal retries are disabled (max_retries=0): the router owns retries so
  every attempt is logged.
* Errors are mapped to ProviderUnavailableError ONLY for availability problems
  (429, 5xx/529, timeouts, network, quota/spend limit, auth). 400/404/413/422
  are request defects and stop the pipeline.
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

PROVIDER = "claude_api"
_QUOTA_MARKERS = ("credit balance", "billing", "spend limit", "quota")


def _retry_after(exc: Any) -> float | None:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None) or {}
    try:
        value = headers.get("retry-after")
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def classify_anthropic_error(exc: Exception, provider: str = PROVIDER) -> Exception:
    """Translate an anthropic SDK exception into the JobBot error hierarchy."""
    import anthropic

    message = str(exc)
    lowered = message.lower()

    if isinstance(exc, anthropic.APITimeoutError):
        return ProviderUnavailableError(message, failure_type=FailureType.TIMEOUT, provider=provider)
    if isinstance(exc, anthropic.APIConnectionError):
        return ProviderUnavailableError(message, failure_type=FailureType.NETWORK, provider=provider)

    if isinstance(exc, anthropic.APIStatusError):
        status = int(getattr(exc, "status_code", 0) or 0)
        retry_after = _retry_after(exc)

        if status == 429:
            quota = any(marker in lowered for marker in _QUOTA_MARKERS)
            return ProviderUnavailableError(
                message,
                failure_type=FailureType.QUOTA_EXHAUSTED if quota else FailureType.RATE_LIMIT,
                provider=provider,
                retryable=not quota,
                retry_after_seconds=retry_after,
                status_code=status,
            )
        if status == 529 or isinstance(exc, getattr(anthropic, "OverloadedError", ())):
            return ProviderUnavailableError(
                message, failure_type=FailureType.OVERLOADED, provider=provider,
                retry_after_seconds=retry_after, status_code=status,
            )
        if status >= 500:
            return ProviderUnavailableError(
                message, failure_type=FailureType.SERVER_ERROR, provider=provider,
                retry_after_seconds=retry_after, status_code=status,
            )
        if status in (401, 403):
            # Key revoked / org on hold / spend limit -> provider unusable now.
            failure = (
                FailureType.QUOTA_EXHAUSTED
                if any(marker in lowered for marker in _QUOTA_MARKERS)
                else FailureType.AUTH
            )
            return ProviderUnavailableError(
                message, failure_type=failure, provider=provider, retryable=False, status_code=status
            )
        if status == 408:
            return ProviderUnavailableError(
                message, failure_type=FailureType.TIMEOUT, provider=provider, status_code=status
            )
        if status == 400 and any(marker in lowered for marker in _QUOTA_MARKERS):
            # "Your credit balance is too low" is returned as a 400.
            return ProviderUnavailableError(
                message, failure_type=FailureType.QUOTA_EXHAUSTED, provider=provider,
                retryable=False, status_code=status,
            )
        return InvalidRequestError(
            f"Anthropic rejected the request (HTTP {status}): {message}",
            provider=provider,
            details={"status_code": status},
        )

    return exc


def claude_model(settings: dict[str, Any]) -> str:
    return os.getenv(settings.get("model_env", "ANTHROPIC_MODEL"), "").strip() or \
        settings.get("default_model", "claude-opus-5-5")


@lru_cache(maxsize=4)
def _sdk_client(timeout_seconds: float) -> Any:
    import anthropic

    # max_retries=0: execute_ai_task() owns retry/backoff and logging.
    return anthropic.Anthropic(max_retries=0, timeout=timeout_seconds)


def get_claude_client(settings: dict[str, Any]) -> Any:
    if not os.getenv("ANTHROPIC_API_KEY", "").strip():
        raise provider_not_configured("ANTHROPIC_API_KEY is not set.", PROVIDER)
    try:
        return _sdk_client(float(settings.get("timeout_seconds", 120.0)))
    except ImportError as exc:
        raise provider_not_configured("The 'anthropic' package is not installed.", PROVIDER) from exc


def build_claude_request(request: AIRequest, settings: dict[str, Any]) -> dict[str, Any]:
    """Pure: SDK keyword arguments for one request."""
    effort_map = settings.get("effort_map", {})
    kwargs: dict[str, Any] = {
        "model": claude_model(settings),
        "max_tokens": request.max_output_tokens,
        # Stable, large system prompt first and cached.
        "system": [{"type": "text", "text": request.system_prompt, "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user", "content": request.user_prompt}],
        "output_config": {"effort": effort_map.get(request.effort, request.effort)},
    }
    if request.output_schema is not None:
        kwargs["output_config"]["format"] = {"type": "json_schema", "schema": provider_schema(request.output_schema)}
    return kwargs


def stream_final_message(client: Any, kwargs: dict[str, Any], server_side_fallback: bool) -> Any:
    if server_side_fallback:
        # Server-side refusal fallback (routes a declined request to another Claude model).
        with client.beta.messages.stream(betas=["server-side-fallback-2026-07-01"], fallbacks="default",
                                         **kwargs) as stream:
            return stream.get_final_message()
    with client.messages.stream(**kwargs) as stream:
        return stream.get_final_message()


def message_text(message: Any) -> str:
    return "".join(getattr(block, "text", "") for block in getattr(message, "content", [])
                   if getattr(block, "type", "") == "text")


def check_stop_reason(message: Any, request: AIRequest) -> None:
    stop_reason = getattr(message, "stop_reason", None)
    if stop_reason == "refusal":
        raise OutputValidationError("Claude declined the request (stop_reason=refusal).",
                                    failure_type=FailureType.MODEL_REFUSAL, provider=PROVIDER)
    if stop_reason == "max_tokens":
        raise OutputValidationError(
            f"Output truncated at max_tokens={request.max_output_tokens}. "
            "Increase the limit for this operation; not retrying on another provider.",
            failure_type=FailureType.OUTPUT_TRUNCATED, provider=PROVIDER,
        )


def call_claude_api(request: AIRequest, settings: dict[str, Any], client: Any = None) -> dict[str, Any]:
    client = client or get_claude_client(settings)
    kwargs = build_claude_request(request, settings)
    try:
        message = stream_final_message(client, kwargs, settings.get("server_side_fallback", True))
    except Exception as exc:  # noqa: BLE001 - mapped below
        mapped = classify_anthropic_error(exc, PROVIDER)
        if mapped is exc:
            raise
        raise mapped from exc

    check_stop_reason(message, request)
    text = message_text(message)
    usage = getattr(message, "usage", None)
    return make_result(
        PROVIDER, request.operation,
        data=parse_json_object(text, provider=PROVIDER) if request.output_schema else None,
        raw_text=text,
        model=getattr(message, "model", None) or kwargs["model"],
        usage={
            "input_tokens": getattr(usage, "input_tokens", None),
            "output_tokens": getattr(usage, "output_tokens", None),
            "cache_read_input_tokens": getattr(usage, "cache_read_input_tokens", None),
        },
    )
