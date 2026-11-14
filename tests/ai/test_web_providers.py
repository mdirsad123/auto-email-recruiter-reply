"""Claude web / ChatGPT web provider functions (no real browser)."""

from __future__ import annotations

import pytest

from ai.errors import FailureType, InputValidationError, ProviderUnavailableError
from ai.provider_result import COMPLETED, AIRequest
from ai.providers.browser_chat import ensure_browser, map_browser_exception
from ai.providers.chatgpt_web import call_chatgpt_web
from ai.providers.claude_web import call_claude_web
from ai.settings import OP_RECRUITER_CLASSIFY


def browser_ok(provider):
    return None


def test_browser_errors_are_mapped():
    assert isinstance(map_browser_exception(FileNotFoundError("no file"), "claude_web"), InputValidationError)
    expired = map_browser_exception(RuntimeError("page requires login"), "claude_web")
    assert expired.failure_type == FailureType.BROWSER_SESSION_EXPIRED
    assert isinstance(map_browser_exception(TimeoutError("slow"), "claude_web"), ProviderUnavailableError)


def test_browser_down_is_unavailable(monkeypatch):
    monkeypatch.setattr("ai.providers.browser_chat.debugger_reachable", lambda address=None: False)
    with pytest.raises(ProviderUnavailableError) as info:
        ensure_browser("claude_web", bootstrap=lambda: False)
    assert info.value.failure_type == FailureType.BROWSER_UNAVAILABLE


def test_claude_web_generic_chat_for_recruiter_ops():
    def runner(**kwargs):
        return '{"is_recruiter_email": false}', "https://claude.ai/chat/1"

    result = call_claude_web(AIRequest(operation=OP_RECRUITER_CLASSIFY, system_prompt="s", user_prompt="u",
                                       output_schema={"type": "object"}), {}, chat_runner=runner,
                             browser_check=browser_ok)
    assert result["status"] == COMPLETED and result["data"] == {"is_recruiter_email": False}


def test_chatgpt_web_returns_completed_json():
    seen = {}

    def runner(**kwargs):
        seen.update(kwargs)
        return '```json\n{"is_recruiter_email": true}\n```', "https://chatgpt.com/c/abc"

    result = call_chatgpt_web(AIRequest(operation=OP_RECRUITER_CLASSIFY, system_prompt="s", user_prompt="u",
                                        output_schema={"type": "object"}), {}, chat_runner=runner)
    assert result["status"] == COMPLETED and result["data"] == {"is_recruiter_email": True}
    assert seen["request_file"].is_file()
