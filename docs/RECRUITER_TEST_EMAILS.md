# Recruiter Automation — Offline & Gmail Smoke Tests

**Important:** These are example messages, not real candidate details. Send test
emails from a DIFFERENT Gmail account to your configured n8n inbox. Activate
the revised n8n workflow in draft-only mode (`AUTO_SEND_RECRUITER_REPLY=false`).

1. Subject: `Python Backend Developer opportunity`. Body: `Please share your
   total experience, Python experience and updated profile.` Expected: draft
   (or manual draft if AI confidence insufficient); Total `3+ years`, Python
   `2 years`, correct MD IRSAD signature.
2. Subject: `AWS Data Engineer position`. Body: `Your relevant experience in
   AWS Data Engineering?` Expected: `2 years` verified relevant experience.
3. Subject: `Data Engineer vacancy`. Body: `Please send total years and PySpark
   experience.` Expected: `3+ years` total, PySpark `2 years`.
4. Subject: `AI Engineer application`. Body: `What is your LLM experience?`
   Expected: `2 years`, with no invented ML research claims.
5. Subject: `Recruiter details request`. Body: `Please confirm current CTC,
   expected CTC and notice period`. Expected: manual review / incomplete draft,
   NOT invented salary or joining claims.
6. Subject: `Azure/Databricks engineer`. Body: `How many years in Azure and
   Databricks?` Expected: manual review (no fabricated years).
7. Subject: `Job alert newsletter`; sender: `noreply@job-board.example`, with
   List-Unsubscribe header. Expected: SKIPPED_AUTOMATED, no AI call/draft.
8. Subject: `Python Engineer interview`. Body: `Would you confirm availability
   tomorrow?` Expected: review if availability isn't verified.
9. Send test #1 twice with identical Gmail message ID by API (or re-trigger n8n):
   expected second API result `DUPLICATE_SKIPPED`, no second draft.
10. Send a NEW Gmail message ID with identical sender, subject and body shortly
    after a draft: expected sender-content deduplication; no duplicate draft.
11. Send same sender/subject but a different question: it is a new message and
    should not be suppressed by sender-content deduplication.
12. Mark an earlier recruiter email as READ, and run the `Schedule: revisit last
    3 days` path. It should be included even if the latest message was not from
    a recruiter. Repeat scan: ledger should skip existing IDs.

**Verify before enabling auto-send:** Gmail OAuth credentials, correct
normalization (full body, not only snippet), labels, n8n execution logs, JSON
ledger persistence, fixed signature, Project knowledge upload and no old
workflow running in parallel. Tests in `tests/ai` provide mocked coverage;
actual Gmail/Chrome execution requires connected accounts on Windows.
