"""Durable JSON-file message deduplication and audit trail (no database).

The ledger holds message IDs, thread IDs, status and hashed sender metadata;
no body, AI response, candidate details, or plain sender address is written.

All operations serialize via an OS-level lock file and use atomic os.replace().
This works across multiple n8n executions / API worker processes on the same
local Windows or Linux filesystem. Keep this file on local disk (not NFS/SMB).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterator

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_JSON_PATH = PROJECT_ROOT / 'data' / 'recruiter_email_ledger.json'
PROCESSING = 'PROCESSING'
RETRYABLE_STATUSES = frozenset({'ERROR', 'PROVIDERS_UNAVAILABLE'})
SENT = 'SENT'
DRAFTED = 'DRAFTED'
_ALLOWED_FIELDS = frozenset({'status','action','provider','role','intent','confidence',
                             'requested_fields','missing_fields','reasons'})


class LedgerError(RuntimeError):
    """Do not process/send if duplicate protection cannot be trusted."""


@dataclass(frozen=True)
class JSONLedger:
    path: Path


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso_now() -> str:
    return _now().isoformat(timespec='microseconds')


def _parse_time(value: str | None) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def sender_fingerprint(address: str) -> tuple[str, str]:
    address = (address or '').strip().lower()
    domain = address.split('@')[-1] if '@' in address else ''
    return domain, hashlib.sha256(address.encode('utf-8')).hexdigest()


def content_fingerprint(subject: str, body: str) -> str:
    """Hash normalized email content; never persist subject or body itself."""
    normalized_subject = re.sub(r"^(?:\s*(?:re|fw|fwd)\s*:\s*)+", "", subject or "", flags=re.I)
    normalized = re.sub(r"\s+", " ", normalized_subject.strip().casefold() + "\n" + (body or "").strip().casefold())
    return hashlib.sha256(normalized.encode('utf-8')).hexdigest()


def sender_recently_handled(ledger: JSONLedger, message_id: str, hours: float) -> bool:
    """Suppress same sender + same subject/body re-sent under a new Gmail ID.

    Different opportunities or follow-up questions from one recruiter remain
    eligible, even if their subjects are the same.
    """
    if hours <= 0:
        return False
    since = _now() - timedelta(hours=hours)
    with _locked(ledger) as state:
        current = state['messages'].get(message_id)
        if not current:
            return False
        for key, row in state['messages'].items():
            if key == message_id or not isinstance(row, dict):
                continue
            if not row.get('content_hash') or row['content_hash'] != current.get('content_hash'):
                continue
            if row.get('sender_hash') != current.get('sender_hash'):
                continue
            when = _parse_time(row.get('created_at'))
            if when and when >= since and (row.get('reply_outcome') in {SENT, DRAFTED} or
                row.get('action') in {'CREATE_DRAFT', 'SEND_REPLY', 'CREATE_DRAFT_MANUAL_REVIEW'}):
                return True
    return False


def ledger_path() -> Path:
    raw = os.getenv('RECRUITER_EMAIL_LEDGER_PATH', '').strip()
    path = Path(raw).expanduser() if raw else DEFAULT_JSON_PATH
    return path if path.is_absolute() else PROJECT_ROOT / path


@lru_cache(maxsize=1)
def get_ledger_engine() -> JSONLedger:
    """Legacy function name kept for API/service compatibility; returns JSONLedger."""
    return JSONLedger(ledger_path())


def ensure_ledger_table(ledger: JSONLedger) -> JSONLedger:
    """Compatibility alias (no SQL table exists)."""
    if not isinstance(ledger, JSONLedger):
        raise TypeError('Only JSONLedger is supported; configure RECRUITER_EMAIL_LEDGER_PATH')
    return ledger


@contextmanager
def _locked(ledger: JSONLedger) -> Iterator[dict[str, Any]]:
    path = ledger.path
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_file = path.with_name(path.name + '.lock')
    # Keep this sidecar lock file: never delete while the process is running.
    with open(lock_file, 'a+b') as lock:
        lock.seek(0, os.SEEK_END)
        if lock.tell() == 0:
            lock.write(b'0')
            lock.flush()
        lock.seek(0)
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            if not path.exists():
                state: dict[str, Any] = {'schema_version': 1, 'messages': {}}
            else:
                try:
                    state = json.loads(path.read_text(encoding='utf-8'))
                    if (not isinstance(state, dict) or state.get('schema_version') != 1
                            or not isinstance(state.get('messages'), dict)):
                        raise ValueError('schema does not match expected JSON ledger v1')
                except (OSError, ValueError, TypeError) as exc:
                    raise LedgerError(f'JSON ledger unreadable/corrupt: {path.name} ({type(exc).__name__})') from exc
            yield state
        finally:
            lock.seek(0)
            if os.name == 'nt':
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _save(ledger: JSONLedger, state: dict[str, Any]) -> None:
    data = json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + '\n'
    temp = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=ledger.path.parent,
                                         prefix='.recruiter-ledger-', suffix='.tmp', delete=False) as f:
            temp = Path(f.name)
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, ledger.path)
    finally:
        if temp is not None and temp.exists():
            temp.unlink(missing_ok=True)


def claim_message(ledger: JSONLedger, message_id: str, thread_id: str, sender: str,
                  max_attempts: int = 3, *, subject: str = "", body: str = "") -> tuple[bool, dict[str, Any] | None]:
    """Atomically claim message. Retries transient errors / expired PROCESSING lease only."""
    if not message_id or not str(message_id).strip():
        raise LedgerError('Gmail message ID is required for safe deduplication')
    domain, digest = sender_fingerprint(sender)
    with _locked(ledger) as state:
        existing = state['messages'].get(message_id)
        if existing is not None:
            if not isinstance(existing, dict):
                raise LedgerError('Invalid message entry in JSON ledger')
            prior = dict(existing)
            stale = existing.get('status') == PROCESSING and (
                (t := _parse_time(existing.get('updated_at'))) is not None and
                _now() - t > timedelta(minutes=max(5, int(os.getenv('RECRUITER_PROCESSING_LEASE_MINUTES','120'))))
            )
            # A draft or sent reply cannot be reclaimed even when the worker crashed.
            retryable = (existing.get('status') in RETRYABLE_STATUSES or stale) and not existing.get('reply_outcome')
            if not retryable or int(existing.get('attempts', 1)) >= max_attempts:
                return False, prior
            existing.update(status=PROCESSING, attempts=int(existing.get('attempts', 1)) + 1,
                            updated_at=_iso_now())
            _save(ledger, state)
            return True, prior
        now = _iso_now()
        state['messages'][message_id] = {
            'message_id': message_id, 'thread_id': thread_id, 'sender_domain': domain,
            'sender_hash': digest, 'content_hash': content_fingerprint(subject, body),
            'status': PROCESSING, 'action': None,
            'attempts': 1, 'created_at': now, 'updated_at': now,
            'reply_at': None, 'reply_outcome': None, 'reply_ref': None,
        }
        _save(ledger, state)
        return True, None


def finish_message(ledger: JSONLedger, message_id: str, **fields: Any) -> None:
    with _locked(ledger) as state:
        row = state['messages'].get(message_id)
        if row is None:
            raise LedgerError('Cannot finish unclaimed message')
        if row.get('reply_outcome') == SENT:
            return  # Never undo confirmed send.
        row.update({k: v for k, v in fields.items() if k in _ALLOWED_FIELDS})
        row['updated_at'] = _iso_now()
        _save(ledger, state)


def mark_reply(ledger: JSONLedger, message_id: str, outcome: str, reply_ref: str | None = None) -> bool:
    if outcome not in {SENT, DRAFTED, 'LABELED', 'FAILED'}:
        raise ValueError('Invalid Gmail result')
    with _locked(ledger) as state:
        row = state['messages'].get(message_id)
        if row is None:
            return False
        if row.get('reply_outcome') in {SENT, DRAFTED}:
            return True  # Idempotent; do not allow a stale result to downgrade an outcome.
        row.update(reply_outcome=outcome, reply_ref=reply_ref, updated_at=_iso_now())
        if outcome in {SENT, DRAFTED}:
            row['reply_at'] = _iso_now()
        if outcome == SENT:
            row['status'] = 'REPLIED'
        _save(ledger, state)
        return True


def thread_recently_auto_replied(ledger: JSONLedger, thread_id: str, hours: float) -> bool:
    if not thread_id:
        return False
    since = _now() - timedelta(hours=hours)
    with _locked(ledger) as state:
        for row in state['messages'].values():
            when = _parse_time(row.get('reply_at'))
            if row.get('thread_id') == thread_id and row.get('reply_outcome') == SENT and when and when >= since:
                return True
    return False


def get_message(ledger: JSONLedger, message_id: str) -> dict[str, Any] | None:
    with _locked(ledger) as state:
        row = state['messages'].get(message_id)
        return dict(row) if row else None


def ledger_summary(ledger: JSONLedger) -> dict[str, Any]:
    with _locked(ledger) as state:
        counts: dict[str, int] = {}
        for row in state['messages'].values():
            status = str(row.get('status', 'UNKNOWN'))
            counts[status] = counts.get(status, 0) + 1
        return {'type': 'json', 'total_messages': len(state['messages']), 'statuses': counts}
