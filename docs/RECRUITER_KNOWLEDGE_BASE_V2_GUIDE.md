# Recruiter Knowledge Base v3 — authoritative candidate facts

`config/experience_details.md` is the ONLY source of factual answers sent to
recruiters. It is loaded on **every API request**, including backfill messages.
`recruiter_email.knowledge_base` parses the Markdown headings, prose,
`KEY=VALUE` entries, role-level experience and supporting evidence.
The AI classifier is not allowed to invent facts or replace the local values.

## Canonical candidate profile

The master CV provided in `specific.zip` (`specific/md_irsad.md`) was used to
correct the old, unrelated email-project profile. The candidate is **MD IRSAD**.
Total experience: **3+ years**. Verified chronology:

- 33 Kuber India Advisory LLP, Software Developer, Sep 2023–Jan 2025.
- Freelance Data Engineer, Feb 2025–present.
- Client project MOTOR: Feb–May 2025.
- Client project AUS Logistics / Printsoft: May 2025–present.

For verified technology-specific skills, use the user-requested **2 years**
rounded. Do not assert unverified skills, client employment, title, salary,
certification, or more experience. Current/expected CTC, notice period,
location and Azure/Databricks/AgentCore/other unknown years are blank / absent
so any request for them is routed to manual review.

## Edit and sync

Edit `config/experience_details.md` and bump `Document version`.
Remote Claude/ChatGPT Projects are *not* automatically updated by local code.
If web providers use Project knowledge, replace the old attachment in each
configured Project. Only afterwards run:

```bat
python -m recruiter_email.knowledge_sync status
python -m recruiter_email.knowledge_sync mark-uploaded both
```

A content SHA256 + version tracks parity to avoid stale Project knowledge.
The local file is authoritative even when the Project may be out of date;
version mismatch is flagged for manual review as required by safety gates.

## Relevant experience and safe replies

- Total Experience -> `TOTAL_EXPERIENCE_YEARS=3+` -> `3+ years`.
- Relevant Experience for AWS Data Engineer -> `AWS_DATA_ENGINEERING_EXPERIENCE_YEARS=2`.
- Relevant Experience for Data Engineer -> `DATA_ENGINEERING_EXPERIENCE_YEARS=2`.
- Relevant Experience for Python Developer -> `PYTHON_DEVELOPMENT_EXPERIENCE_YEARS=2`.
- Relevant Experience for AI Engineer -> `AI_ENGINEERING_EXPERIENCE_YEARS=2`.
- Exact tool experience -> corresponding `*_EXPERIENCE_YEARS=2` IF actually verified.
- Unverified tool -> manual review, never silently invent years.
- CTC, availability, preferred location -> leave blank and route to manual review.

Greetings and signature come from `config/recruiter_email_template.json`, not
AI prose. Python resolves fields deterministically and validates hallucinations,
number claims, role matches and automation/sensitive flags before any draft/send.

## Gmail ingestion / duplicate policy

New Gmail trigger polls 5 minutes. A separate 15-minute schedule uses Gmail
`Get Many` with a 3-day search, READ AND UNREAD, followed by a `Get` with full
payload. n8n now passes every candidate email to the API; it does NOT drop
emails using simplistic subject keywords. Automated mail is ignored in Python.

The file `data/recruiter_email_ledger.json` is created automatically. It uses
cross-process locking + atomic writes. `message_id` is the primary idempotency
key; sender address and normalized subject+body are stored as irreversible
hashes for duplicate-content suppression. Do NOT erase the file each run.
A previously IGNORED email from an older n8n execution with no JSON ledger
entry can be found by the 3-day scan. Confirmed draft/sent results never retry.

### Debugging

- Health: `GET http://localhost:8090/health`.
- AI flags: `GET http://localhost:8090/ai/providers/status`.
- Knowledge parity: `GET http://localhost:8090/ai/recruiter-email/knowledge-status`.
- Ledger summary: `GET http://localhost:8090/ai/recruiter-email/ledger-summary`.
- Local tests: `python -m pytest tests/ai -q`.
- If old messages are missing, check scheduled workflow activation, Gmail
  credential on the 2 backfill Gmail nodes, message labels and Gmail search.
- If no draft appears, check n8n execution history and `logs/recruiter_api.log`.
- Chrome debugger is reused at 127.0.0.1:9222 if it returns a valid DevTools
  `/json/version` response; the venv is taken from project then LinkedIn root.

This does not send emails merely from the presence of an address in a JD:
only Gmail inbox messages selected by the n8n workflow are considered.
