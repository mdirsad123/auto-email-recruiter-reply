"""Review queue for missing recruiter facts: metadata only, never invented answers.

Pending rows contain short field names/AI labels and message-ID hashes, never
raw emails, email bodies, contact details, or speculative candidate values.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from recruiter_email.ledger import JSONLedger, _locked
from recruiter_email.field_registry import label_for

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PATH = ROOT / "data" / "recruiter_questions_to_review.json"


def _queue_path() -> Path:
    supplied = os.getenv("RECRUITER_QUESTION_REVIEW_PATH", "").strip()
    path = Path(supplied).expanduser() if supplied else DEFAULT_PATH
    return path if path.is_absolute() else ROOT / path


def _safe_label(value: str) -> str | None:
    # Avoid persisting arbitrary input snippets, email addresses or credentials.
    clean = re.sub(r"\s+", " ", str(value or "").strip())[:100]
    if not (2 <= len(clean) <= 100):
        return None
    if "@" in clean or "http" in clean.lower() or re.search(r"\d{6,}", clean):
        return None
    if not re.fullmatch(r"[\w .,/()?#:+\-]+", clean, re.UNICODE):
        return None
    return clean


def capture_pending_questions(result: dict) -> None:
    """Persist unique missing/unmapped question labels for the user's review."""
    observations = []
    for key in result.get("missing_fields") or []:
        label = _safe_label(label_for(str(key)))
        if label:
            observations.append(("missing_verified_answer", str(key), label))
    for label in result.get("unmapped_requests") or []:
        safe = _safe_label(label)
        if safe:
            observations.append(("unmapped_question", "", safe))
    if not observations:
        return

    path = _queue_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Reuse durable OS-level locking + schema-checked atomic IO from ledger.
    # The queue is stored as a separate schema-v1 JSON file under 'messages' for
    # compatibility with the shared locking helper; it is NOT the mail ledger.
    with _locked(JSONLedger(path)) as state:
        entries = state["messages"]
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        msg = hashlib.sha256(str(result.get("message_id") or "").encode()).hexdigest()[:20]
        for kind, key, label in dict.fromkeys(observations):
            row_id = hashlib.sha256((kind + ":" + key.casefold() + ":" + label.casefold()).encode()).hexdigest()[:24]
            old = entries.get(row_id, {})
            seen = old.get("message_hashes", [])
            if msg in seen:
                continue
            entries[row_id] = {
                "kind": kind, "field_key": key or None, "question_label": label,
                # A newly missing field reopens an item previously marked answered.
                "review_status": "pending" if old.get("review_status") == "answered" else old.get("review_status", "pending"),
                "first_seen_at": old.get("first_seen_at", now), "last_seen_at": now,
                "count": int(old.get("count", 0)) + 1,
                "message_hashes": (seen + [msg])[-100:],
                "review_note": old.get("review_note", ""),
            }
        data = json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                             prefix=".recruiter-questions-", suffix=".tmp", delete=False) as file:
                temporary = Path(file.name)
                file.write(data)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
