> **2026-11-15 UPDATE:** For Cohere-first fallback and `AUTO_SEND_RECRUITER_REPLY=true` forced sending, read `docs/COHERE_FORCE_AUTOSEND_UBUNTU.md` first. Older safety/manual-review instructions below describe the previous mode.

# JobBot Recruiter Email Automation — JSON ledger + one-time startup catch-up

Standalone FastAPI + n8n project (no PostgreSQL, SQLite or SQLAlchemy database).

# n8n workflow screenshot
<img width="1325" height="568" alt="n8n-workflow" src="https://github.com/user-attachments/assets/1c54f731-5b13-41cc-9919-b243dc10d0fd" />


**Windows installation directory:** `D:\ird-projects\sahil_projects\jobbot-recruiter-email`
**Live email intake:** Gmail trigger every 5 minutes. If several new messages arrived
since the previous poll, the trigger can output multiple emails; all are queued
through `Process emails one by one (batch 1)` and handled sequentially.

**Offline recovery:** On first workflow publication/activation or n8n instance
startup/restart, `Catch-up once: n8n started or workflow published` waits for
Python API readiness (up to 15 minutes), then retrieves all
matching messages in the last **7 days** (read + unread). It does NOT scan the
last few days every 15 minutes. Change `newer_than:7d` to `newer_than:15d` in
`Gmail: Backfill last 7 days (startup only)` for planned longer shutdowns. The
7-day lookback covers ordinary nights and weekends but not an absence beyond
7 days; plan a longer recovery window in that case.

**Concurrency:** n8n `N8N_CONCURRENCY_PRODUCTION_LIMIT=1`, plus a Python in-process
lock around `process_recruiter_email`, prevent simultaneous Claude operations
from this service. Temporary Claude automation tabs close after replies, leaving
the original Chrome tabs open. Do not run separate manual backfills while a
production execution is running; the n8n concurrency setting excludes manual
executions. Keep the API running before starting/restarting n8n so that the
one-time recovery execution can complete (and use one uvicorn worker).

The old n8n-side recruiter keyword IF filters have been removed: every eligible
message now reaches the Python API for automated-mail detection and recruiter
classification. Role terms in the Python filter include Python, AWS/Data
Engineering, AI/GenAI and common recruiter/hiring phrases. This prevents the
previous n8n false negatives from being permanently ignored. To classify even
emails with no pre-filter signal via AI, set `RECRUITER_PREFILTER_REQUIRED=false`
(uses substantially more AI/browser calls and exposes more mailbox content to
that configured AI provider).

**Safety:** `AUTO_SEND_RECRUITER_REPLY=false` is preserved. Valid recruiter
responses are drafts unless you explicitly turn on auto-send in BOTH the Python
`.env` and n8n Docker environment. Manual-review gates always override it.

## What's in this project

- `app/main.py`: API on port 8090 (`/docs`, `/health`).
- `recruiter_email/ledger.py`: atomic on-disk JSON duplicate ledger. No server needed.
- `data/recruiter_email_ledger.json`: existing on-disk deduplication history (preserve it).
- `config/experience_details.md`: canonical recruiter RAG knowledge file, updated from `specific/md_irsad.md`.
- `config/recruiter_email_template.json`: correct name/contact signature.
- `n8n/04_recruiter_email_auto_reply.json`: 5-minute new-mail polling + startup recovery, sequential per item.
- `start_recruiter_email.bat`: one-click Windows entry point.
- `scripts/start_api.bat`: starts Chrome remote-debugger only if no valid endpoint exists,
  activates `.venv` from THIS project if present, otherwise falls back to
  `D:\ird-projects\sahil_projects\my_jobbots_automation\.venv`, starts FastAPI.
- `scripts/start_chrome_debug.bat`: reuse/open `127.0.0.1:9222` (`C:\chromedebug` profile).
- `scripts/run_tests.bat`: offline unit tests.

## Install & start on Windows

1. Extract ZIP to `D:\ird-projects\sahil_projects\` so that the path ends
   with `jobbot-recruiter-email\app\main.py` (do not nest an extra folder).
2. Backup your old `.env` and any existing `data/` ledger before replacing the project.
   This delivered ZIP includes the user's supplied `.env` settings. Protect it: it may
   contain configured provider URLs/credentials.
3. Run `scripts\setup_windows.bat` if packages are missing. Or activate your existing
   shared LinkedIn venv and run `python -m pip install -r requirements.txt`.
4. Run `start_recruiter_email.bat` (or `scripts\start_api.bat`). It will use
   the DevTools JSON endpoint at `http://127.0.0.1:9222/json/version` to REUSE a
   running debugging Chrome. If absent, launch Chrome from standard install paths
   with `--user-data-dir="C:\chromedebug"`. It **does not** open a second debugger
   when an existing valid Chrome DevTools endpoint is available.
5. Confirm `http://localhost:8090/health` and `http://localhost:8090/docs`.

## n8n Gmail setup (import updated workflow; replace old active workflow)

1. From `n8n\`, run `docker compose up -d` and open `http://localhost:5678`.
2. **Deactivate the old recruiter workflow** before activating this revised one;
   do not leave both running. Import `n8n/04_recruiter_email_auto_reply.json`.
3. Select your **existing Gmail OAuth2 credential** on each Gmail node including
   `Gmail Trigger (new inbox mail)`, `Gmail: Backfill last 7 days (startup only)`,
   `Gmail: Fetch complete older email`, `Gmail: Reply in thread`,
   `Gmail: Create draft in thread`, `Gmail: Mark / label message`,
   `Gmail: Label manual review (API error)`.
4. In `Normalize Email` JavaScript, replace the 4 `REPLACE_LABEL_ID_*` placeholders
   with your Gmail label IDs (or use the IDs from the previous working workflow):
   `JobBot/Recruiter/AutoReplied`, `JobBot/Recruiter/DraftReady`,
   `JobBot/Recruiter/ManualReview`, `JobBot/Recruiter/Ignored`.
5. Keep `AUTO_SEND_RECRUITER_REPLY=false` in **both** `.env` and
   `n8n/docker-compose.yml` while testing. The 5-minute Gmail trigger stays
   active. Catch-up runs once when the workflow is first published/activated
   and when n8n restarts. Start Python API first and confirm `/health`, then
   start/restart Docker n8n. To manually test recovery, execute its n8n Trigger
   node, but do not overlap an existing run.
6. Startup Gmail search excludes Spam, Trash, Sent, Draft and threads tagged with
   AutoReplied/DraftReady; **Ignored is intentionally NOT excluded** so previous
   missed recruiter emails can be considered. Both read and unread messages are
   included. For other mail labels, Gmail search may be adjusted as needed.
7. The Python JSON ledger stops repeated processing if both live and backfill
   encounter the same Gmail `message_id`; it also checks hashed sender + hashed
   normalized email content to prevent the same recruiter sending identical
   emails under different Gmail IDs. Different opportunities or different
   follow-up questions from the same sender remain eligible.

## JSON ledger behavior and recovery

- File: `data/recruiter_email_ledger.json` (override with `RECRUITER_EMAIL_LEDGER_PATH`).
- Contents: message IDs, thread IDs, statuses, hashed sender address and hashed
  email content, attempt counts, outcome metadata and timestamps. **No raw
  email bodies, candidates' values or plaintext sender addresses.**
- Cross-process locks: sidecar `.lock` file and atomic JSON writes, no server.
- Retryable classifier/provider failure may be retried up to 3 attempts. A
  `PROCESSING` lease only expires after configured minutes (default 120).
- `DRAFTED` or `SENT` email must not be regenerated or sent again. Keep the JSON
  file when updating the project; don't delete it to make the scan "start over".
- New JSON ledger starts EMPTY; it cannot automatically import old PostgreSQL/SQLite
  processed rows. The Gmail label exclusions protect previously labelled drafts
  and replies. Before activating auto-send, review old drafts/labels and test
  draft-only, especially if any old mail was processed but not labelled.
- If the JSON ledger is corrupt or unwritable, the API fails closed (503) and
  will NOT safely generate a new reply. Restore a backup or fix file access.
- For a single message test, use a new Gmail `message_id` in the API request.

## Correct candidate knowledge (from canonical CV)

- **MD IRSAD**, B.Sc. IT, University of Mumbai (2023).
- **3+ total professional years**; employer dates are Sep 2023–Jan 2025 for
  33 Kuber India Advisory LLP and Feb 2025–present for freelance client work.
- **2 years** is the requested conservative rounded figure for verified
  technology/role-specific experience (Python, AWS, PySpark, backend, LLM
  application integration, etc.); unverified Azure, Databricks, LangGraph,
  AgentCore etc. are NOT invented.
- Work: MOTOR finance pipeline, AUS Logistics / Printsoft Replacement, and
  33 Kuber JazzTrack. Freelance is not direct client employment.
- Signature/contact come from the canonical user CV, not the unrelated 11-year
  candidate content in the original email project.
- Unknown CTC, notice period and location are deliberately blank. These require
  manual review, not guessed answers. Knowledge is read fresh for every email.

**Important RAG sync:** When using `USE_CLAUDE_WEB` or `USE_CHATGPT_WEB` with
configured Project knowledge, upload the *NEW* `config/experience_details.md`
into each applicable Claude/ChatGPT Project (replace old attachment). **Only
after the upload succeeds**, run:

```bat
python -m recruiter_email.knowledge_sync mark-uploaded both
```

Do not mark uploaded before updating the external Project: that would falsely
claim knowledge parity. API providers use local knowledge directly.

## Test (no live Gmail or AI needed)

```bat
python -m pytest tests\ai -q
```

Real end-to-end Gmail OAuth delivery, n8n executions, Chrome Web UI and actual
AI provider performance need validation on your Windows machine with signed-in
accounts; offline checks cannot assert those external services are working.
Use the draft-only workflow and `docs/RECRUITER_TEST_EMAILS.md` for smoke tests.

---------------------------------------------
# Run Debbuger port in RustDesk Ubuntu terminal
cd ~/ird-projects/auto-email-recruiter-reply
chmod +x start_chromium_debug.sh
./start_chromium_debug.sh

-- check debbuger port: curl http://127.0.0.1:9222/json/version

# Run email fast api in Ubuntu terminal
cd ~/ird-projects/auto-email-recruiter-reply
chmod +x start_recruiter_email.sh
./start_recruiter_email.sh

-- check api health: curl http://localhost:8090/health
