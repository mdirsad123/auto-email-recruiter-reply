> **2026-11-15 UPDATE:** For Cohere-first fallback and `AUTO_SEND_RECRUITER_REPLY=true` forced sending, read `docs/COHERE_FORCE_AUTOSEND_UBUNTU.md` first. Older safety/manual-review instructions below describe the previous mode.

# Missing recruiter questions + Claude tab cleanup patch (14 November 2026)

- Added `AGE=25` based on candidate's last stated age in October/November 2026;
  verify it before any age response. Age answers always go to ManualReview.
- Existing generic international-relocation answer handles "date of relocation to US"
  without inventing a date. Known questions no longer create false unmapped rows
  just because the classifier says they are unsupported.
- Each automation-owned Claude/ChatGPT Chrome tab is closed after response OR
  failure; the original tab is restored. Cleanup failures are logged.
- `config/experience_details.md` version advanced to 2026-11-14.4. Upload the new
  file to any configured Claude/ChatGPT Project and update project sync state.

## Install on existing Windows project

1. Backup your project, `.env`, `data/recruiter_questions_to_review.json`,
   `data/recruiter_email_ledger.json`, and existing n8n workflow.
2. Stop the Python API, then copy code and config from this ZIP into your project.
   **Do not delete or overwrite your live `.env`, data/ JSON files, Gmail labels,
   credentials, or published n8n workflow.** There are no live data files in ZIP.
3. Restart the Python API using your existing `.bat`, leaving one uvicorn worker.
   Your current n8n workflow does NOT need reimport for these changes.
4. Sync the new `experience_details.md` to your Claude/ChatGPT Project. The local
   MD alone does not update the remote Project attachment.
5. Send a test recruiter email asking Age and Date of Relocation to US. Check
   that the draft says 25 (after verifying) and no US relocation date, and
   Gmail stays in ManualReview.
6. Inspect the Python terminal for `Closed recruiter AI automation tab`.

Existing Gmail drafts are not edited retroactively. Existing Claude tabs must
be closed manually once; the change only closes future automation-created tabs.

# International recruiter questions + missing-answer review (November 14, 2026)

## What changed

- `config/experience_details.md`: verified, country-neutral visa, passport and relocation answers. Version `2026-11-14.3`.
- `recruiter_email/field_registry.py`: identifies these questions from bare recruiter lines (including the sample "When did you Relocate to US", plus UAE, Saudi Arabia, Japan and other countries).
- `recruiter_email/service.py`: International visa/relocation questions always create **manual-review drafts**, never auto-send, including when auto-send is switched ON.
- `recruiter_email/pending_questions.py`: captures missing/unmapped recruiter question labels into a separate JSON review queue; does not invent or fill answers.
- `scripts/review_missing_questions.bat`: opens the review queue in Notepad after it exists.
- `docs/RECRUITER_QUESTIONS_REVIEW.md`: explains how to add approved answers later.

## Safe installation

1. Back up the project and especially your CURRENT `.env`, n8n workflow, and `data/recruiter_email_ledger.json`.
2. Stop the Python API, then merge the code in this ZIP into the existing project. **Do not overwrite your live `.env`, JSON ledger, Gmail credentials or any existing data.** The ZIP intentionally omits those runtime files.
3. Start the Python service again using your existing startup BAT.
4. The authoritative MD is read on each API call. If you use a Claude or ChatGPT Project knowledge file, replace the old remote attachment with the updated `config/experience_details.md` and update knowledge sync per `docs/RECRUITER_KNOWLEDGE_BASE_V2_GUIDE.md`.
5. Test a recruiter message asking for full name, location, visa status and international relocation history. Expect a Gmail draft + ManualReview label, *not* an auto-sent message.
6. After future messages ask unanswered questions, run `scripts/review_missing_questions.bat` to read `data/recruiter_questions_to_review.json`.

**Note:** An already drafted message won't be rewritten just because the MD changes; manually edit old drafts.

## Tests

127 local tests passed (excluding `tests/ai/test_providers.py`, whose `anthropic` dependency is absent in this isolated test environment). Live browser/Google/Gmail authorization was not tested.
