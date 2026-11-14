"""
ai/providers/chatgpt_web.py
===========================

ChatGPT web provider (enabled by USE_CHATGPT_WEB), the last fallback.

Writes the canonical request (same instructions + input + JSON schema the APIs
get) to a file, attaches it in a new ChatGPT chat in the debugger Chrome, waits
for the answer and parses the JSON. Synchronous, so the result is COMPLETED and
is validated exactly like API output.

Entry point: ``call_chatgpt_web(request, settings, chat_runner=None)``.
"""

from __future__ import annotations

import os
from typing import Any, Callable

from ai.provider_result import AIRequest
from ai.providers.browser_chat import run_chat_json_request, run_web_request

PROVIDER = "chatgpt_web"


def chatgpt_url(settings: dict[str, Any]) -> str:
    return os.getenv(settings.get("chat_url_env", "CHATGPT_PROJECT_URL"), "").strip() \
        or settings.get("default_chat_url", "https://chatgpt.com/")


def call_chatgpt_web(request: AIRequest, settings: dict[str, Any], *,
                     chat_runner: Callable[..., tuple[str, str]] = run_chat_json_request) -> dict[str, Any]:
    """
    New chat at CHATGPT_PROJECT_URL (a ChatGPT Project page) or chatgpt.com.
    Recruiter e-mail requests (request.knowledge set) must run inside the Project:
    the page is verified and the prompt references the Project's experience_details.md.
    """
    settings = {"project_url_env": "CHATGPT_PROJECT_URL", "project_name_env": "CHATGPT_PROJECT_NAME",
                "response_timeout_seconds": 1200, **settings}
    return run_web_request(PROVIDER, request, settings, chatgpt_url(settings), chat_runner)
