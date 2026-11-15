"""execute_ai_task: USE_* flags, fixed priority, retry, fallback and the no-fallback rules."""

from __future__ import annotations

import pytest

from ai.errors import (
    AIConfigurationError,
    AllProvidersUnavailableError,
    ApplicationError,
    FailureType,
    InputValidationError,
    InvalidRequestError,
    OutputValidationError,
    ProviderUnavailableError,
    provider_not_configured,
)
from ai.provider_result import SUBMITTED, AIRequest
from ai.provider_router import (
    backoff_seconds,
    get_enabled_providers,
    handle_provider_fallback,
    validate_ai_response,
)
from ai.settings import OP_GROUP_JOBS, load_ai_config, read_provider_flags
from tests.ai.conftest import ALL_PROVIDERS, fake_provider, make_config, make_execute, unavailable

DATA = {"ok": True}


def req() -> AIRequest:
    return AIRequest(operation=OP_GROUP_JOBS, system_prompt="s", user_prompt="u",
                     log_context={"run_date": "2026-11-05"})


# ------------------------------------------------------------------ flags
def flags(api_c, api_o, web_c, web_o):
    return {"USE_CLAUDE_API": api_c, "USE_CHATGPT_API": api_o, "USE_CLAUDE_WEB": web_c, "USE_CHATGPT_WEB": web_o}


@pytest.mark.parametrize("env,expected", [
    (flags("true", "true", "true", "true"), ["claude_api", "chatgpt_api", "claude_web", "chatgpt_web"]),
    (flags("false", "true", "false", "true"), ["chatgpt_api", "chatgpt_web"]),
    (flags("false", "false", "true", "false"), ["claude_web"]),
    (flags("true", "false", "false", "false"), ["claude_api"]),
    (flags("false", "false", "false", "true"), ["chatgpt_web"]),
    (flags("FALSE", "0", "no", "off"), []),
    (flags("TRUE", "1", "yes", "on"), ["claude_api", "chatgpt_api", "claude_web", "chatgpt_web"]),
])
def test_enabled_providers_follow_flags_in_fixed_order(env, expected):
    assert get_enabled_providers(load_ai_config(env)) == expected


def test_unset_flag_defaults_to_true():
    flags = read_provider_flags({"USE_CLAUDE_API": "false"})
    assert all(not flags[name] for name in ("cohere_api_1", "cohere_api_2", "cohere_api_3"))
    assert {k: flags[k] for k in ("claude_api", "chatgpt_api", "claude_web", "chatgpt_web")} == {
        "claude_api": False, "chatgpt_api": True, "claude_web": True, "chatgpt_web": True}


def test_flags_read_from_process_env(monkeypatch):
    monkeypatch.setenv("USE_CLAUDE_API", "false")
    monkeypatch.setenv("USE_CLAUDE_WEB", "false")
    assert get_enabled_providers(load_ai_config()) == ["chatgpt_api", "chatgpt_web"]


def test_user_example_openai_then_chatgpt_web_then_controlled_failure():
    """USE_CLAUDE_API=false, USE_CHATGPT_API=true, USE_CLAUDE_WEB=false, USE_CHATGPT_WEB=true."""
    o = fake_provider("chatgpt_api", [unavailable()])
    w = fake_provider("chatgpt_web", [unavailable(FailureType.WEB_USAGE_LIMIT, retryable=False)])
    log: list = []
    execute = make_execute({"chatgpt_api": o, "chatgpt_web": w}, log=log)  # Claude ones = must_not_call
    with pytest.raises(AllProvidersUnavailableError) as info:
        execute(req())
    assert [a["provider"] for a in log] == ["chatgpt_api"] * 3 + ["chatgpt_web"]
    assert info.value.http_status == 503
    assert {a["provider"] for a in info.value.attempts} == {"chatgpt_api", "chatgpt_web"}


def test_user_example_success_on_chatgpt_web():
    execute = make_execute({"chatgpt_api": fake_provider("chatgpt_api", [unavailable()]),
                            "chatgpt_web": fake_provider("chatgpt_web", [DATA])})
    out = execute(req())
    assert out["provider"] == "chatgpt_web" and out["enabled_providers"] == ["chatgpt_api", "chatgpt_web"]


def test_only_one_provider_enabled_uses_only_that_provider():
    w = fake_provider("claude_web", [None], status=SUBMITTED, handoff={"collector": "/cloude-download-cvs"})
    out = make_execute({"claude_web": w})(req())
    assert out["provider"] == "claude_web" and out["status"] == SUBMITTED and len(w.calls) == 1


def test_only_one_provider_enabled_and_it_fails_is_controlled_failure():
    with pytest.raises(AllProvidersUnavailableError):
        make_execute({"claude_api": fake_provider("claude_api", [unavailable()])})(req())


def test_all_flags_false_is_configuration_error():
    execute = make_execute({}, enabled=[])
    with pytest.raises(AIConfigurationError) as info:
        execute(req())
    assert info.value.failure_type == "NO_PROVIDER_ENABLED" and "USE_CLAUDE_API" in str(info.value)


def test_disabled_provider_is_never_called_even_if_registered():
    claude = fake_provider("claude_api", [DATA])
    openai = fake_provider("chatgpt_api", [DATA])
    out = make_execute({"claude_api": claude, "chatgpt_api": openai}, enabled=["chatgpt_api"])(req())
    assert out["provider"] == "chatgpt_api" and claude.calls == []


# ------------------------------------------------------------------ fallback
def test_first_provider_success_no_fallback():
    a, b = fake_provider("claude_api", [DATA]), fake_provider("chatgpt_api", [DATA])
    out = make_execute({"claude_api": a, "chatgpt_api": b})(req())
    assert out["provider"] == "claude_api" and out["data"] == DATA and b.calls == []
    assert [x["result"] for x in out["attempts"]] == ["SUCCESS"]


def test_rate_limit_retries_then_succeeds_on_same_provider():
    a = fake_provider("claude_api", [unavailable(), DATA])
    out = make_execute({"claude_api": a})(req())
    assert len(a.calls) == 2 and [x["result"] for x in out["attempts"]] == ["RETRY", "SUCCESS"]


@pytest.mark.parametrize("failure", [FailureType.RATE_LIMIT, FailureType.SERVER_ERROR, FailureType.OVERLOADED,
                                     FailureType.TIMEOUT, FailureType.NETWORK, FailureType.PROVIDER_OUTAGE])
def test_availability_errors_fall_back_to_next_enabled(failure):
    a = fake_provider("claude_api", [unavailable(failure)])
    b = fake_provider("chatgpt_api", [DATA])
    log: list = []
    out = make_execute({"claude_api": a, "chatgpt_api": b}, log=log)(req())
    assert out["provider"] == "chatgpt_api" and len(a.calls) == 3  # 1 + 2 retries
    assert [x["result"] for x in out["attempts"]] == ["RETRY", "RETRY", "FALLBACK", "SUCCESS"]
    assert out["attempts"][2]["fallback_reason"] == failure
    assert {"operation", "provider", "attempt", "result", "duration_ms", "fallback_reason"} <= set(log[0])


def test_quota_is_not_retried_but_falls_back():
    a = fake_provider("claude_api", [unavailable(FailureType.QUOTA_EXHAUSTED, retryable=False)])
    out = make_execute({"claude_api": a, "chatgpt_api": fake_provider("chatgpt_api", [DATA])})(req())
    assert len(a.calls) == 1 and out["provider"] == "chatgpt_api"


def test_enabled_but_missing_key_is_skipped():
    a = fake_provider("claude_api", [provider_not_configured("ANTHROPIC_API_KEY is not set.")])
    out = make_execute({"claude_api": a, "chatgpt_api": fake_provider("chatgpt_api", [DATA])})(req())
    assert out["provider"] == "chatgpt_api" and out["attempts"][0]["result"] == "SKIPPED"


def test_full_chain_order_and_web_not_retried():
    providers = {
        "claude_api": fake_provider("claude_api", [unavailable()]),
        "chatgpt_api": fake_provider("chatgpt_api", [unavailable(FailureType.SERVER_ERROR)]),
        "claude_web": fake_provider("claude_web", [unavailable(FailureType.BROWSER_SESSION_EXPIRED, False)]),
        "chatgpt_web": fake_provider("chatgpt_web", [DATA]),
    }
    log: list = []
    out = make_execute(providers, log=log)(req())
    assert out["provider"] == "chatgpt_web"
    assert [a["provider"] for a in log] == ["claude_api"] * 3 + ["chatgpt_api"] * 3 + ["claude_web", "chatgpt_web"]


@pytest.mark.parametrize("error", [InputValidationError("input file missing"),
                                   InvalidRequestError("HTTP 400 bad schema"),
                                   OutputValidationError("schema mismatch")])
def test_defects_never_fall_back(error):
    a = fake_provider("claude_api", [error])
    b = fake_provider("chatgpt_api", [DATA])
    with pytest.raises(type(error)):
        make_execute({"claude_api": a, "chatgpt_api": b})(req())
    assert b.calls == [] and len(a.calls) == 1


def test_validator_failure_stops_without_fallback():
    b = fake_provider("chatgpt_api", [DATA])

    def validator(data, result):
        raise OutputValidationError("missing job ids", provider=result["provider"])

    with pytest.raises(OutputValidationError) as info:
        make_execute({"claude_api": fake_provider("claude_api", [{"bad": 1}]), "chatgpt_api": b})(
            req(), validator=validator)
    assert b.calls == [] and info.value.details["attempts"][-1]["result"] == "FAILED"


def test_programming_bug_becomes_application_error():
    b = fake_provider("chatgpt_api", [DATA])
    with pytest.raises(ApplicationError):
        make_execute({"claude_api": fake_provider("claude_api", [KeyError("web_payload")]), "chatgpt_api": b})(req())
    assert b.calls == []


def test_errors_are_redacted_in_attempt_log():
    leak = ProviderUnavailableError("401 for key sk-ant-api03-SECRETSECRETSECRET user jane@corp.example",
                                    failure_type=FailureType.AUTH, retryable=False)
    log: list = []
    make_execute({"claude_api": fake_provider("claude_api", [leak]),
                  "chatgpt_api": fake_provider("chatgpt_api", [DATA])}, log=log)(req())
    assert "SECRETSECRET" not in str(log) and "jane@corp.example" not in str(log)


# ------------------------------------------------------------------ pure helpers
def test_handle_provider_fallback_decisions():
    assert handle_provider_fallback(unavailable(), attempt=1, max_attempts=3) == "retry"
    assert handle_provider_fallback(unavailable(), attempt=3, max_attempts=3) == "fallback"
    assert handle_provider_fallback(unavailable(retryable=False), attempt=1, max_attempts=3) == "fallback"


def test_backoff_is_exponential_capped_and_honours_retry_after():
    retry = make_config()["retry"] | {"initial_delay_seconds": 2.0, "max_delay_seconds": 10.0}
    no_jitter = lambda: 0.0  # noqa: E731
    assert [backoff_seconds(n, unavailable(), retry, no_jitter) for n in (1, 2, 3, 4)] == [2.0, 4.0, 8.0, 10.0]
    slow = ProviderUnavailableError("429", retry_after_seconds=7)
    assert backoff_seconds(1, slow, retry, no_jitter) == 7.0


def test_validate_ai_response_skips_submitted_results():
    submitted = {"status": SUBMITTED, "data": None, "provider": "claude_web"}
    assert validate_ai_response(submitted, lambda d, r: 1 / 0) is submitted
