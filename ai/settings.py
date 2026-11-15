"""
ai/settings.py
==============

Central AI configuration.

* Which providers may run: ONLY the four .env flags USE_CLAUDE_API,
  USE_CHATGPT_API, USE_CLAUDE_WEB, USE_CHATGPT_WEB, in the fixed priority
  order Claude API -> ChatGPT API -> Claude Web -> ChatGPT Web.
* Models, browser selectors and retry tuning: config/ai_provider_settings.json.

Nothing in here ever reads, returns or logs an API key value; only whether a
key is present.
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SETTINGS_PATH = Path(
    os.getenv("JOBBOT_AI_SETTINGS_PATH", str(PROJECT_ROOT / "config" / "ai_provider_settings.json"))
)

# Operations handled by the router.
OP_GROUP_JOBS = "group_jobs"
OP_CV_MATCH = "cv_match"
OP_CV_GENERATE = "cv_generate"
OP_RECRUITER_CLASSIFY = "recruiter_email_classify"
OP_RECRUITER_REPLY = "recruiter_email_reply"

ALL_OPERATIONS = (
    OP_GROUP_JOBS,
    OP_CV_MATCH,
    OP_CV_GENERATE,
    OP_RECRUITER_CLASSIFY,
    OP_RECRUITER_REPLY,
)

PIPELINE_OPERATIONS = (OP_GROUP_JOBS, OP_CV_MATCH, OP_CV_GENERATE)

_TRUE = {"1", "true", "yes", "on"}


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in _TRUE


def env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "") or default)
    except ValueError:
        return default


def env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default


# ---------------------------------------------------------------------------
# Provider flags (the ONLY thing that decides which providers may be called)
# ---------------------------------------------------------------------------

# Fixed priority order. Never changed by config, n8n or the caller.
PROVIDER_PRIORITY = ("cohere_api_1", "cohere_api_2", "cohere_api_3", "claude_api", "chatgpt_api", "claude_web", "chatgpt_web")

PROVIDER_FLAGS = {
    "cohere_api_1": "USE_COHERE_API",
    "cohere_api_2": "USE_COHERE_API",
    "cohere_api_3": "USE_COHERE_API",
    "claude_api": "USE_CLAUDE_API",
    "chatgpt_api": "USE_CHATGPT_API",
    "claude_web": "USE_CLAUDE_WEB",
    "chatgpt_web": "USE_CHATGPT_WEB",
}

# "api" providers are retried with backoff; "web" providers are tried once.
PROVIDER_KINDS = {"cohere_api_1": "api", "cohere_api_2": "api", "cohere_api_3": "api", "claude_api": "api", "chatgpt_api": "api", "claude_web": "web", "chatgpt_web": "web"}

DEFAULT_RETRY = {
    "api_max_retries": 2,
    "web_max_retries": 0,
    "initial_delay_seconds": 2.0,
    "max_delay_seconds": 10.0,
}


def parse_flag(value: str | None, default: bool = True) -> bool:
    """'true'/'1'/'yes'/'on' -> True, 'false'/'0'/'no'/'off' -> False, unset -> default."""
    if value is None or not value.strip():
        return default
    return value.strip().lower() in _TRUE


def read_provider_flags(env: dict[str, str] | None = None) -> dict[str, bool]:
    """Pure: {provider: enabled} from USE_* variables. Unset flags count as true (as in .env.example)."""
    source = os.environ if env is None else env
    flags = {provider: parse_flag(source.get(var)) for provider, var in PROVIDER_FLAGS.items()}
    # Automatically enable configured Cohere key slots unless explicitly disabled.
    # Empty slots are skipped, so a user with only key 1 has no wasted attempts.
    for index in range(1, 4):
        provider = f"cohere_api_{index}"
        key_name = "COHERE_API_KEY" + (str(index) if index > 1 else "")
        flags[provider] = parse_flag(source.get("USE_COHERE_API"), default=True) and bool(source.get(key_name, "").strip())
    return flags


@lru_cache(maxsize=4)
def read_settings_file(path: str) -> dict[str, Any]:
    """Models, selectors and retry tuning from config/ai_provider_settings.json (read once)."""
    file_path = Path(path)
    if not file_path.is_file():
        return {}
    try:
        return json.loads(file_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        # Broken config is an application defect, not a provider outage.
        raise RuntimeError(f"Invalid JSON in AI settings file {file_path}: {exc}") from exc


def strip_comments(mapping: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in mapping.items() if not str(k).startswith("_")}


def load_ai_config(env: dict[str, str] | None = None, settings_path: Path | None = None) -> dict[str, Any]:
    """
    Build the router configuration dict:

        {"flags": {...}, "retry": {...}, "timeout_seconds": float,
         "providers": {name: {...model / selectors...}}, "web_unavailable_markers": [...]}
    """
    source = os.environ if env is None else env
    raw = read_settings_file(str(settings_path or SETTINGS_PATH))
    retry = {**DEFAULT_RETRY, **strip_comments(raw.get("retry", {}))}
    if source.get("JOBBOT_AI_MAX_RETRIES", "").strip():
        retry["api_max_retries"] = int(source["JOBBOT_AI_MAX_RETRIES"])
    try:
        timeout = float(source.get("JOBBOT_AI_TIMEOUT_SECONDS", "") or 120.0)
    except ValueError:
        timeout = 120.0
    return {
        "flags": read_provider_flags(source),
        "retry": retry,
        "timeout_seconds": timeout,
        "providers": {k: strip_comments(v) for k, v in strip_comments(raw.get("providers", {})).items()},
        "web_unavailable_markers": [m.lower() for m in raw.get("web_unavailable_markers", [])],
    }


def provider_settings(config: dict[str, Any], provider: str) -> dict[str, Any]:
    """Settings handed to one call_* function (never contains API keys)."""
    name = "cohere_api" if provider.startswith("cohere_api_") else provider
    return {
        **config.get("providers", {}).get(name, {}),
        **config.get("providers", {}).get(provider, {}),
        "timeout_seconds": config.get("timeout_seconds", 120.0),
        "markers": config.get("web_unavailable_markers", []),
    }
