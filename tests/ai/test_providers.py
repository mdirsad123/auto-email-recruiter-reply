"""Anthropic / OpenAI error classification and provider behaviour with fake clients."""

from __future__ import annotations

from types import SimpleNamespace

import anthropic
import httpx
import openai
import pytest

from ai.errors import FailureType, InvalidRequestError, OutputValidationError, ProviderUnavailableError
from ai.provider_result import AIRequest
from ai.providers.anthropic_api import call_claude_api, classify_anthropic_error
from ai.providers.openai_api import call_openai_api, classify_openai_error
from ai.settings import OP_CV_MATCH

REQ = httpx.Request("POST", "https://api.example.invalid/v1")
SCHEMA = {"type": "object", "additionalProperties": False, "required": ["a"],
          "properties": {"a": {"type": "integer", "minimum": 0}}}


def status_error(module, status: int, message: str, headers: dict | None = None, body: dict | None = None):
    response = httpx.Response(status, request=REQ, headers=headers or {}, json=body or {"error": {"message": message}})
    cls = module.APIStatusError
    for name, code in (("RateLimitError", 429), ("InternalServerError", 500), ("BadRequestError", 400),
                       ("AuthenticationError", 401), ("PermissionDeniedError", 403)):
        if status == code:
            cls = getattr(module, name)
    return cls(message, response=response, body=body)


def request(schema=SCHEMA) -> AIRequest:
    return AIRequest(operation=OP_CV_MATCH, system_prompt="sys", user_prompt="user", output_schema=schema)


# ---------------------------------------------------------------- Anthropic
@pytest.mark.parametrize("status,message,failure,retryable", [
    (429, "rate_limit_error: too many requests", FailureType.RATE_LIMIT, True),
    (429, "Your credit balance is too low", FailureType.QUOTA_EXHAUSTED, False),
    (529, "Overloaded", FailureType.OVERLOADED, True),
    (500, "internal error", FailureType.SERVER_ERROR, True),
    (503, "service unavailable", FailureType.SERVER_ERROR, True),
    (401, "invalid x-api-key", FailureType.AUTH, False),
])
def test_anthropic_availability_errors(status, message, failure, retryable):
    mapped = classify_anthropic_error(status_error(anthropic, status, message))
    assert isinstance(mapped, ProviderUnavailableError)
    assert mapped.failure_type == failure and mapped.retryable is retryable


def test_anthropic_retry_after_header_is_honoured():
    mapped = classify_anthropic_error(status_error(anthropic, 429, "slow down", headers={"retry-after": "7"}))
    assert mapped.retry_after_seconds == 7


def test_anthropic_network_and_timeout():
    assert classify_anthropic_error(anthropic.APIConnectionError(request=REQ)).failure_type == FailureType.NETWORK
    assert classify_anthropic_error(anthropic.APITimeoutError(request=REQ)).failure_type == FailureType.TIMEOUT


def test_anthropic_bad_request_is_not_fallback():
    mapped = classify_anthropic_error(status_error(anthropic, 400, "messages.0.content: invalid"))
    assert isinstance(mapped, InvalidRequestError) and not mapped.fallback_allowed


class _Stream:
    def __init__(self, message=None, error=None):
        self.message, self.error = message, error

    def __enter__(self):
        if self.error:
            raise self.error
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        return self.message


def fake_anthropic(message=None, error=None):
    captured = {}

    def stream(**kwargs):
        captured.update(kwargs)
        return _Stream(message, error)

    client = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(stream=stream)),
                             messages=SimpleNamespace(stream=stream))
    return client, captured


def msg(text, stop_reason="end_turn"):
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], stop_reason=stop_reason,
                           model="claude-opus-5-5", usage=SimpleNamespace(input_tokens=1, output_tokens=1,
                                                                          cache_read_input_tokens=0))


def test_anthropic_provider_structured_output_request_shape():
    client, captured = fake_anthropic(msg('{"a": 3}'))
    result = call_claude_api(request(), {}, client=client)
    assert result["data"] == {"a": 3} and result["provider"] == "claude_api"
    fmt = captured["output_config"]["format"]
    assert fmt["type"] == "json_schema"
    assert "minimum" not in fmt["schema"]["properties"]["a"]  # stripped for the provider
    assert captured["system"][0]["cache_control"] == {"type": "ephemeral"}


def test_anthropic_refusal_and_truncation_do_not_fall_back():
    for stop, failure in (("refusal", FailureType.MODEL_REFUSAL), ("max_tokens", FailureType.OUTPUT_TRUNCATED)):
        client, _ = fake_anthropic(msg("", stop_reason=stop))
        with pytest.raises(OutputValidationError) as info:
            call_claude_api(request(), {}, client=client)
        assert info.value.failure_type == failure


def test_anthropic_invalid_json_is_output_validation_error():
    client, _ = fake_anthropic(msg("not json at all"))
    with pytest.raises(OutputValidationError):
        call_claude_api(request(), {}, client=client)


def test_anthropic_sdk_error_is_mapped_during_execute():
    client, _ = fake_anthropic(error=status_error(anthropic, 529, "Overloaded"))
    with pytest.raises(ProviderUnavailableError) as info:
        call_claude_api(request(), {}, client=client)
    assert info.value.failure_type == FailureType.OVERLOADED


def test_anthropic_missing_key_is_not_configured(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(ProviderUnavailableError) as info:
        call_claude_api(request(), {})
    assert info.value.failure_type == FailureType.NOT_CONFIGURED


# ---------------------------------------------------------------- OpenAI
@pytest.mark.parametrize("status,body,failure,retryable", [
    (429, {"error": {"code": "rate_limit_exceeded", "message": "slow"}}, FailureType.RATE_LIMIT, True),
    (429, {"error": {"code": "insufficient_quota", "message": "You exceeded your current quota"}},
     FailureType.QUOTA_EXHAUSTED, False),
    (500, {"error": {"message": "server"}}, FailureType.SERVER_ERROR, True),
    (401, {"error": {"message": "bad key"}}, FailureType.AUTH, False),
])
def test_openai_availability_errors(status, body, failure, retryable):
    mapped = classify_openai_error(status_error(openai, status, body["error"]["message"], body=body))
    assert isinstance(mapped, ProviderUnavailableError)
    assert mapped.failure_type == failure and mapped.retryable is retryable


def test_openai_bad_request_is_not_fallback():
    mapped = classify_openai_error(status_error(openai, 400, "Invalid schema for response_format"))
    assert isinstance(mapped, InvalidRequestError)


def fake_openai(response=None, error=None):
    captured = {}

    def create(**kwargs):
        captured.update(kwargs)
        if error:
            raise error
        return response

    return SimpleNamespace(responses=SimpleNamespace(create=create)), captured


def test_openai_responses_api_request_shape():
    client, captured = fake_openai(SimpleNamespace(status="completed", output_text='{"a": 1}', usage=None))
    result = call_openai_api(request(), {}, client=client)
    assert result["data"] == {"a": 1} and result["provider"] == "chatgpt_api"
    assert captured["text"]["format"]["type"] == "json_schema" and captured["text"]["format"]["strict"] is True
    assert captured["store"] is False


def test_openai_incomplete_and_failed_status():
    client, _ = fake_openai(SimpleNamespace(status="incomplete", output_text="",
                                            incomplete_details=SimpleNamespace(reason="max_output_tokens")))
    with pytest.raises(OutputValidationError) as info:
        call_openai_api(request(), {}, client=client)
    assert info.value.failure_type == FailureType.OUTPUT_TRUNCATED

    client, _ = fake_openai(SimpleNamespace(status="failed", output_text="",
                                            error=SimpleNamespace(message="server_error")))
    with pytest.raises(ProviderUnavailableError):
        call_openai_api(request(), {}, client=client)


def test_openai_missing_key_is_not_configured(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(ProviderUnavailableError) as info:
        call_openai_api(request(), {})
    assert info.value.failure_type == FailureType.NOT_CONFIGURED
