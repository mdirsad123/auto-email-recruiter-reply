"""
Shared helpers for the AI provider / recruiter e-mail tests.

No test calls a real API, opens Chrome or touches PostgreSQL: provider
functions are fakes, the ledger uses in-memory SQLite and run manifests go to
tmp_path. Run:  pytest tests/ai -q
"""

from __future__ import annotations

import sys
from functools import partial
from pathlib import Path
from typing import Any, Callable

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ai.errors import ProviderUnavailableError  # noqa: E402
from ai.provider_result import COMPLETED, make_result  # noqa: E402
from ai.provider_router import execute_ai_task  # noqa: E402
from ai.settings import DEFAULT_RETRY, PROVIDER_PRIORITY  # noqa: E402

ALL_PROVIDERS = list(PROVIDER_PRIORITY)


def fake_provider(name: str, responses: list[Any], status: str = COMPLETED,
                  handoff: dict | None = None) -> Callable:
    """
    Scripted provider function with the real signature fn(request, settings).
    Each response is data, an exception to raise, or a callable(request) -> data.
    The last response repeats. ``fn.calls`` lists the requests received.
    """
    calls: list = []

    def provider(request, settings):
        calls.append(request)
        item = responses[min(len(calls), len(responses)) - 1]
        if isinstance(item, BaseException):
            raise item
        if callable(item):
            item = item(request)
        return make_result(name, request.operation, data=item, status=status, handoff=handoff)

    provider.calls = calls
    provider.provider_name = name
    return provider


def must_not_call(name: str) -> Callable:
    def provider(request, settings):
        raise AssertionError(f"disabled provider {name} was called")

    provider.calls = []
    return provider


def unavailable(kind: str = "RATE_LIMIT", retryable: bool = True) -> ProviderUnavailableError:
    return ProviderUnavailableError(f"simulated {kind}", failure_type=kind, retryable=retryable)


def make_config(enabled: list[str] | None = None, api_retries: int = 2) -> dict[str, Any]:
    """Router config with the given providers enabled (all others false) and zero backoff."""
    enabled = ALL_PROVIDERS if enabled is None else enabled
    return {
        "flags": {name: name in enabled for name in ALL_PROVIDERS},
        "retry": {**DEFAULT_RETRY, "api_max_retries": api_retries, "initial_delay_seconds": 0.0,
                  "max_delay_seconds": 0.0},
        "timeout_seconds": 1.0,
        "providers": {},
        "web_unavailable_markers": [],
    }


def make_execute(providers: dict[str, Callable], enabled: list[str] | None = None, api_retries: int = 2,
                 log: list | None = None) -> Callable[..., dict]:
    """
    execute_ai_task bound to fake providers. ``enabled`` defaults to the providers given;
    every provider not given is wired to must_not_call().
    """
    functions = {name: providers.get(name) or must_not_call(name) for name in ALL_PROVIDERS}
    sink = log if log is not None else []
    return partial(execute_ai_task, config=make_config(enabled or list(providers), api_retries),
                   provider_functions=functions, sleep=lambda _s: None, log=sink.append)


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    """Manifests + attempt logs go to tmp; flags/env never leak in from the machine."""
    monkeypatch.setenv("JOBBOT_AI_RUNS_ROOT", str(tmp_path / "ai_runs"))
    monkeypatch.setenv("JOBBOT_AI_ATTEMPT_LOG", "false")
    for name in ("USE_CLAUDE_API", "USE_CHATGPT_API", "USE_CLAUDE_WEB", "USE_CHATGPT_WEB",
                 "AUTO_SEND_RECRUITER_REPLY", "CV_MATCH_MIN_SCORE", "CV_MATCH_REQUIRE_NO_CRITICAL_GAPS",
                 "RECRUITER_SELF_ADDRESSES", "RECRUITER_BLOCKED_SENDER_DOMAINS", "ANTHROPIC_API_KEY",
                 "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    return tmp_path / "ai_runs"


def execute_with(*fakes: Callable, log: list | None = None) -> Callable[..., dict]:
    """Shortcut: enable exactly the given fake providers (priority order still applies)."""
    return make_execute({fake.provider_name: fake for fake in fakes}, log=log)
