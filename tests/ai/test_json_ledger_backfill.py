"""Regression checks for JSON persistence, safe idempotency and 3-day backfill.

No mailboxes, AI models, browsers or database servers are accessed.
"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from recruiter_email.ledger import (
    JSONLedger, LedgerError, claim_message, content_fingerprint, finish_message,
    get_message, ledger_summary, mark_reply, sender_recently_handled,
    thread_recently_auto_replied,
)
from recruiter_email.knowledge_base import load_knowledge
from recruiter_email.email_template import load_template

ROOT = Path(__file__).resolve().parents[2]


def ledger(tmp_path):
    return JSONLedger(tmp_path / 'ledger.json')


def test_json_claim_duplicate_and_durable_restart(tmp_path):
    one = ledger(tmp_path)
    ok, _ = claim_message(one, 'g1', 'thread1', 'hr@company.example', subject='Role', body='Please share CV')
    assert ok
    ok, row = claim_message(JSONLedger(one.path), 'g1', 'thread1', 'hr@company.example')
    assert not ok and row['attempts'] == 1
    assert ledger_summary(one)['total_messages'] == 1
    saved = one.path.read_text()
    assert 'hr@company.example' not in saved and 'Please share CV' not in saved
    assert 'company.example' in saved and len(row['sender_hash']) == 64


def test_json_no_sqlite_or_postgres_dependencies():
    source = (ROOT / 'recruiter_email' / 'ledger.py').read_text()
    requirements = (ROOT / 'requirements.txt').read_text()
    assert 'from sqlalchemy' not in source.lower()
    assert 'sqlalchemy' not in requirements.lower()
    assert 'psycopg' not in requirements.lower()


def test_atomic_claim_under_concurrent_threads(tmp_path):
    l = ledger(tmp_path)
    def one(_):
        return claim_message(l, 'same-id', 'thread', 'someone@company.example',
                             subject='Python role', body='Send resume')[0]
    with ThreadPoolExecutor(max_workers=18) as pool:
        results = list(pool.map(one, range(50)))
    assert results.count(True) == 1
    assert results.count(False) == 49
    assert ledger_summary(l)['total_messages'] == 1


def test_corrupt_json_fails_closed_and_does_not_overwrite(tmp_path):
    l = ledger(tmp_path)
    l.path.write_text('{bad json', encoding='utf-8')
    with pytest.raises(LedgerError):
        claim_message(l, 'x', 'y', 'hr@example.com')
    assert l.path.read_text() == '{bad json'


def test_transient_retry_expires_and_sent_never_retries(tmp_path):
    l = ledger(tmp_path)
    assert claim_message(l, 'x', 't', 'hr@a.example')[0]
    finish_message(l, 'x', status='PROVIDERS_UNAVAILABLE', action='LABEL_MANUAL_REVIEW')
    assert claim_message(l, 'x', 't', 'hr@a.example')[0]
    finish_message(l, 'x', status='ERROR')
    assert claim_message(l, 'x', 't', 'hr@a.example')[0]
    finish_message(l, 'x', status='ERROR')
    assert not claim_message(l, 'x', 't', 'hr@a.example')[0]
    assert mark_reply(l, 'x', 'SENT', 'gmail-id')
    assert not claim_message(l, 'x', 't', 'hr@a.example')[0]
    assert thread_recently_auto_replied(l, 't', hours=72)


def test_stale_processing_recovery_and_no_retry_if_drafted(tmp_path):
    l = ledger(tmp_path)
    assert claim_message(l, 'x', 't', 'hr@a.example')[0]
    state = json.loads(l.path.read_text())
    state['messages']['x']['updated_at'] = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    l.path.write_text(json.dumps(state))
    assert claim_message(l, 'x', 't', 'hr@a.example')[0]
    assert mark_reply(l, 'x', 'DRAFTED', 'draft-1')
    assert not claim_message(l, 'x', 't', 'hr@a.example')[0]
    assert mark_reply(l, 'x', 'SENT')  # late result should not override confirmed draft
    assert get_message(l, 'x')['reply_outcome'] == 'DRAFTED'


def test_sender_hash_duplicate_content_not_different_question(tmp_path):
    l = ledger(tmp_path)
    claim_message(l, 'm1', 't1', 'hr@example.com', subject='Re: AWS Engineer', body='Share CV')
    finish_message(l, 'm1', status='READY_TO_SEND', action='CREATE_DRAFT')
    mark_reply(l, 'm1', 'DRAFTED', 'draft1')
    claim_message(l, 'm2', 't2', 'hr@example.com', subject='Fwd: AWS Engineer', body='  share    cv  ')
    assert sender_recently_handled(l, 'm2', 72)
    claim_message(l, 'm3', 't3', 'hr@example.com', subject='Re: AWS Engineer', body='Share CTC')
    assert not sender_recently_handled(l, 'm3', 72)
    claim_message(l, 'm4', 't4', 'different@example.com', subject='AWS Engineer', body='Share CV')
    assert not sender_recently_handled(l, 'm4', 72)
    assert content_fingerprint('Re: Role', 'Hi') == content_fingerprint('Fwd: Role', 'hi')


def test_startup_backfill_and_live_poll_are_sequential():
    w = json.loads((ROOT / 'n8n' / '04_recruiter_email_auto_reply.json').read_text())
    nodes = {n['name']: n for n in w['nodes']}
    trigger = nodes['Catch-up once: n8n started or workflow published']
    assert trigger['type'] == 'n8n-nodes-base.n8nTrigger'
    assert {'init', 'activate'}.issubset(set(trigger['parameters']['events']))
    assert not any(n['type'] == 'n8n-nodes-base.scheduleTrigger' for n in w['nodes'])
    live = nodes['Gmail Trigger (new inbox mail)']
    assert live['parameters']['pollTimes']['item'][0]['value'] == 5
    scan = nodes['Gmail: Backfill last 7 days (startup only)']
    assert scan['parameters']['returnAll'] is True
    assert scan['parameters']['filters']['readStatus'] == 'both'
    assert 'newer_than:7d' in scan['parameters']['filters']['q']
    assert 'JobBot/Recruiter/Ignored' not in scan['parameters']['filters']['q']
    loop = nodes['Process emails one by one (batch 1)']
    assert loop['type'] == 'n8n-nodes-base.splitInBatches' and loop['parameters']['batchSize'] == 1
    def follows(src, target, output=None):
        groups = w['connections'][src]['main']
        indexes = range(len(groups)) if output is None else [output]
        return any(e['node'] == target for i in indexes for e in groups[i])
    assert follows(trigger['name'], 'Wait for Python API before catch-up')
    assert follows('Wait for Python API before catch-up', scan['name'])
    assert follows(scan['name'], 'Gmail: Fetch complete older email')
    assert follows('Gmail: Fetch complete older email', 'Normalize Email')
    assert follows(live['name'], 'Normalize Email')
    assert follows('Normalize Email', loop['name'])
    assert follows(loop['name'], 'Already replied/drafted in Gmail?', output=1)
    assert follows(loop['name'], 'Batch finished', output=0)
    for terminal in ('Audit log: mark result', 'Ignore (duplicate / not recruiter / no reply needed)',
                     'Skip: Gmail already contains reply/draft label', 'Gmail: Label manual review (API error)'):
        assert follows(terminal, loop['name'])


def test_canonical_local_knowledge_and_signature():
    kb = load_knowledge()
    assert kb['conflicts'] == {}
    assert kb['facts']['FULL_NAME'] == 'Md Irsad'
    assert kb['facts']['TOTAL_EXPERIENCE_YEARS'] == '3+'
    for key in ['PYTHON_EXPERIENCE_YEARS', 'AWS_EXPERIENCE_YEARS',
                'DATA_ENGINEERING_EXPERIENCE_YEARS', 'PYSPARK_EXPERIENCE_YEARS',
                'AI_ENGINEERING_EXPERIENCE_YEARS']:
        assert kb['facts'][key] == '2'
    assert 'AZURE_EXPERIENCE_YEARS' not in kb['facts']
    assert 'AGENTCORE_EXPERIENCE' not in kb['facts']
    assert 'Md Irsad' in load_template()['signature_lines'][0]
    assert 'mdirsadtech7305@gmail.com' in '\n'.join(load_template()['signature_lines'])
