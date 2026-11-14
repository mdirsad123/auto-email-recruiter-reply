"""
ai/providers/claude_web.py
==========================

Claude web provider (enabled by USE_CLAUDE_WEB): Selenium on the already
running debugger Chrome (127.0.0.1:9222).

Recruiter e-mail requests (classification + reply wording) carry
``request.knowledge``: the chat is opened inside the Claude Project configured
in CLAUDE_PROJECT_URL (verified first), and the prompt explicitly references
the Project's ``experience_details.md``; local excerpts are attached as fallback.

Entry point: ``call_claude_web(request, settings, chat_runner=None, browser_check=None)``.
"""

from __future__ import annotations

import os
from typing import Any, Callable

from ai.provider_result import AIRequest
from ai.providers.browser_chat import ensure_browser, run_chat_json_request, run_web_request

PROVIDER = "claude_web"


def call_claude_web(request: AIRequest, settings: dict[str, Any], *,
                    chat_runner: Callable[..., tuple[str, str]] = run_chat_json_request,
                    browser_check: Callable[[str], None] = ensure_browser,
                    **_ignored: Any) -> dict[str, Any]:
    browser_check(PROVIDER)
    url = os.getenv(settings.get("chat_url_env", "CLAUDE_WEB_GENERIC_CHAT_URL"), "").strip() \
        or settings.get("default_chat_url", "https://claude.ai/new")
    settings = {"project_url_env": "CLAUDE_PROJECT_URL", "project_name_env": "CLAUDE_PROJECT_NAME",
                "response_timeout_seconds": 900, **settings}
    return run_web_request(PROVIDER, request, settings, url, chat_runner)
