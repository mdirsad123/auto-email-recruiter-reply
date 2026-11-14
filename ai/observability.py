"""
ai/observability.py
===================

Structured, privacy-safe logging for every provider attempt.

* One JSON line per attempt (operation, provider, attempt, result, duration,
  fallback reason) via the standard ``logging`` module.
* The same line is appended to ``data/ai_runs/<date>/ai_attempts.jsonl``
  so daily metrics can be computed without a log stack.
* ``redact()`` strips API keys, e-mails, phone numbers and long digit runs from
  any free text before it is logged.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ai.settings import PROJECT_ROOT

IST = timezone(timedelta(hours=5, minutes=30))
DEFAULT_AI_RUNS_ROOT = PROJECT_ROOT / "data" / "ai_runs"


def ai_runs_root() -> Path:
    """Where manifests, attempt logs and web request files go (JOBBOT_AI_RUNS_ROOT overrides)."""
    return Path(os.getenv("JOBBOT_AI_RUNS_ROOT", "").strip() or DEFAULT_AI_RUNS_ROOT)

logger = logging.getLogger("jobbot_recruiter_email")

_write_lock = threading.Lock()

_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"), "[REDACTED_KEY]"),
    (re.compile(r"sk-(?:proj-|or-v1-)?[A-Za-z0-9_\-]{12,}"), "[REDACTED_KEY]"),
    (re.compile(r"(?i)(api[_-]?key|authorization|x-api-key)\s*[:=]\s*\S+"), r"\1=[REDACTED]"),
    (re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{10,}"), "Bearer [REDACTED]"),
    (re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"), "[EMAIL]"),
    (re.compile(r"\+?\d[\d\s\-()]{8,}\d"), "[NUMBER]"),
)


def redact(text: Any, limit: int = 500) -> str:
    """Remove secrets and personal data from a free-text message."""
    value = "" if text is None else str(text)
    for pattern, replacement in _REDACTIONS:
        value = pattern.sub(replacement, value)
    value = " ".join(value.split())
    return value[:limit]


def today_ist() -> str:
    return datetime.now(IST).strftime("%Y-%m-%d")


def safe_context(context: dict[str, Any] | None) -> dict[str, Any]:
    """Keep only small scalar context values; never full prompts or bodies."""
    allowed: dict[str, Any] = {}
    for key, value in (context or {}).items():
        if isinstance(value, (int, float, bool)) or value is None:
            allowed[key] = value
        elif isinstance(value, str):
            allowed[key] = redact(value, limit=120)
        elif isinstance(value, (list, tuple)) and len(value) <= 20:
            allowed[key] = [redact(v, limit=60) for v in value]
    return allowed


def log_attempt(record: dict[str, Any]) -> None:
    """Emit one attempt record to the logger and the daily JSONL file."""
    payload = dict(record)
    payload["error"] = redact(payload.get("error")) if payload.get("error") else None
    payload["context"] = safe_context(payload.get("context"))
    payload["ts"] = datetime.now(IST).isoformat(timespec="seconds")
    line = json.dumps(payload, ensure_ascii=False, sort_keys=True)

    level = logging.INFO
    if payload.get("result") in {"FAILED"}:
        level = logging.ERROR
    elif payload.get("result") in {"RETRY", "FALLBACK"}:
        level = logging.WARNING
    logger.log(level, "ai_attempt %s", line)

    if os.getenv("JOBBOT_AI_ATTEMPT_LOG", "true").strip().lower() in {"0", "false", "no", "off"}:
        return
    try:
        day_dir = ai_runs_root() / today_ist()
        day_dir.mkdir(parents=True, exist_ok=True)
        with _write_lock, (day_dir / "ai_attempts.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError as exc:  # logging must never break the pipeline
        logger.warning("Could not append AI attempt log: %s", exc)


def daily_metrics(day: str | None = None) -> dict[str, Any]:
    """Aggregate ai_attempts.jsonl into the counters suggested in the guide."""
    day = day or today_ist()
    path = ai_runs_root() / day / "ai_attempts.jsonl"
    metrics: dict[str, Any] = {"date": day, "attempts": 0, "by_operation": {}, "by_provider": {}}
    if not path.is_file():
        return metrics
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        metrics["attempts"] += 1
        op = metrics["by_operation"].setdefault(row.get("operation", "?"), {})
        op[row.get("result", "?")] = op.get(row.get("result", "?"), 0) + 1
        prov = metrics["by_provider"].setdefault(row.get("provider", "?"), {})
        prov[row.get("result", "?")] = prov.get(row.get("result", "?"), 0) + 1
    return metrics
