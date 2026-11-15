"""Cohere v2 Chat API provider with separate credentials, short timeouts and key failover.

The public HTTP v2 endpoint is used via the project's existing httpx dependency,
so the deprecated co.chat(message=...) v1 SDK signature is not required. Secrets
are read only from process environment and are never logged or copied to output.
"""
from __future__ import annotations

import json
import os
from typing import Any

import httpx

from ai.errors import (FailureType, OutputValidationError, ProviderUnavailableError,
                       provider_not_configured)
from ai.json_utils import parse_json_object
from ai.provider_result import AIRequest, make_result

URL = "https://api.cohere.com/v2/chat"
KEY_NAMES = ("COHERE_API_KEY", "COHERE_API_KEY2", "COHERE_API_KEY3")
PROVIDERS = ("cohere_api_1", "cohere_api_2", "cohere_api_3")


def cohere_key_env(provider: str) -> str:
    try:
        return KEY_NAMES[PROVIDERS.index(provider)]
    except ValueError as exc:
        raise ValueError("Unknown Cohere credential slot") from exc


def build_request(request: AIRequest, settings: dict[str, Any]) -> dict[str, Any]:
    instruction = request.system_prompt
    if request.output_schema is not None:
        # Cohere JSON mode needs explicit instruction that output be JSON. The
        # local validator is still the source of truth about the schema.
        instruction += ("\n\nReturn ONLY a JSON object matching this JSON schema exactly. "
                        "Do not add markdown, explanation or extra keys.\n"
                        + json.dumps(request.output_schema, ensure_ascii=False))
    body: dict[str, Any] = {
        "model": os.getenv("COHERE_MODEL", "").strip() or settings.get("default_model", "command-a-plus-05-2026"),
        "messages": [{"role": "system", "content": instruction},
                     {"role": "user", "content": request.user_prompt}],
        "temperature": 0.2,
        "max_tokens": min(max(256, request.max_output_tokens), 32000),
    }
    if request.output_schema is not None:
        body["response_format"] = {"type": "json_object"}
    return body


def _classify_http_error(response: httpx.Response, provider: str) -> ProviderUnavailableError:
    status = response.status_code
    # Do not log or return response bodies: provider diagnostics may echo prompt
    # excerpts, email addresses, bearer tokens, or application-level content.
    retry_after = None
    try:
        retry_after = float(response.headers.get("retry-after", ""))
    except ValueError:
        pass
    quota = status == 429 and any(marker in response.text.lower() for marker in
                                ("quota", "monthly", "billing", "trial call", "credits"))
    if status in (401, 403, 498):
        kind = FailureType.AUTH
    elif status == 429:
        kind = FailureType.QUOTA_EXHAUSTED if quota else FailureType.RATE_LIMIT
    elif status in (408, 504):
        kind = FailureType.TIMEOUT
    elif status >= 500:
        kind = FailureType.SERVER_ERROR
    else:
        kind = FailureType.PROVIDER_OUTAGE
    return ProviderUnavailableError(
        f"Cohere returned HTTP {status} (credential slot {provider[-1]}).",
        failure_type=kind, provider=provider,
        # Immediately try next key. On per-minute rate limit, the other key
        # may work, while retrying this one just delays incoming emails.
        retryable=False,
        retry_after_seconds=retry_after,
        status_code=status,
    )


def call_cohere_api(request: AIRequest, settings: dict[str, Any], *, provider: str,
                    client: Any = None) -> dict[str, Any]:
    key = os.getenv(cohere_key_env(provider), "").strip()
    if not key:
        raise provider_not_configured(f"{cohere_key_env(provider)} is not set.", provider)
    body = build_request(request, settings)
    timeout = float(os.getenv("COHERE_TIMEOUT_SECONDS", "") or settings.get("timeout_seconds", 20.0))
    timeout = min(max(timeout, 2.0), 90.0)
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json",
               "Accept": "application/json"}
    try:
        if client is None:
            with httpx.Client(timeout=timeout, follow_redirects=False) as owned:
                response = owned.post(URL, headers=headers, json=body)
        else:
            response = client.post(URL, headers=headers, json=body, timeout=timeout)
    except httpx.TimeoutException as exc:
        raise ProviderUnavailableError("Cohere request timed out", failure_type=FailureType.TIMEOUT,
                                       provider=provider, retryable=False) from exc
    except httpx.RequestError as exc:
        raise ProviderUnavailableError("Cohere network request failed", failure_type=FailureType.NETWORK,
                                       provider=provider, retryable=False) from exc

    if not 200 <= response.status_code < 300:
        raise _classify_http_error(response, provider)
    try:
        data = response.json()
        finish = data.get("finish_reason", "COMPLETE")
        if finish not in {"COMPLETE", "STOP_SEQUENCE"}:
            raise ValueError(f"incomplete finish_reason={finish}")
        parts = data.get("message", {}).get("content", [])
        text = "\n".join(str(part.get("text", "")) for part in parts
                         if isinstance(part, dict) and part.get("type") == "text").strip()
        if not text:
            raise ValueError("empty text")
        structured = parse_json_object(text, provider=provider) if request.output_schema else None
    except (ValueError, TypeError, KeyError, OutputValidationError) as exc:
        raise ProviderUnavailableError("Cohere returned invalid or incomplete JSON; trying next provider",
                                       failure_type=FailureType.OUTPUT_VALIDATION,
                                       provider=provider, retryable=False) from exc
    tokens = data.get("usage", {}).get("tokens", {}) or {}
    return make_result(provider, request.operation, data=structured, raw_text=text,
                       model=data.get("model") or body["model"],
                       usage={"input_tokens": tokens.get("input_tokens"),
                              "output_tokens": tokens.get("output_tokens")})


def call_cohere_1(request: AIRequest, settings: dict[str, Any]) -> dict[str, Any]:
    return call_cohere_api(request, settings, provider="cohere_api_1")


def call_cohere_2(request: AIRequest, settings: dict[str, Any]) -> dict[str, Any]:
    return call_cohere_api(request, settings, provider="cohere_api_2")


def call_cohere_3(request: AIRequest, settings: dict[str, Any]) -> dict[str, Any]:
    return call_cohere_api(request, settings, provider="cohere_api_3")
