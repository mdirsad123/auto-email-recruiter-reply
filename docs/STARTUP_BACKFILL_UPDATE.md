# Nov 14 update: startup-only catch-up and sequential Claude

**Main behavior**

- Every five minutes the Gmail Trigger looks for new inbox emails, and can return more than one per poll.
- On first workflow activation/publication and every n8n startup/restart, an API-readiness check (up to ~15 minutes of retries) precedes one catch-up searching the last 7 days, including read messages.
- A `Loop Over Items` node with batch size 1 completes email A (through draft/send, label, audit) before starting email B.
- Python has an in-process lock around the recruiter request, preventing concurrent classifier/writer Chrome runs from this service.
- Chrome tabs created by the browser automator are now closed after the response; original user tabs remain open.
- Both Python and Docker are configured with AUTO_SEND_RECRUITER_REPLY=false by default. The tool only creates drafts for eligible emails until both values are explicitly enabled.

**Deploy (Windows)**

1. Back up your project folder, `.env` and `data/recruiter_email_ledger.json`.
2. Stop any running n8n workflow execution. Unpublish/deactivate the old version. Don't run two copies.
3. Copy this update over the existing project. Preserve the existing Gmail OAuth credentials in n8n and its Docker volume.
4. Start FastAPI with `start_recruiter_email.bat`; ensure `http://localhost:8090/health` reports status.
5. From `n8n`, run `docker compose up -d --force-recreate`. n8n startup triggers a one-time catch-up for already published workflows. If importing the workflow instead, publish/activate it after setting Gmail credentials and label IDs.
6. Import `n8n/04_recruiter_email_auto_reply.json` into n8n, then select the existing Gmail credential on every Gmail node. In `Normalize Email`, replace all four `REPLACE_LABEL_ID_*` values. Only publish the new workflow after the old one is inactive.
7. Test with two fresh eligible recruiter emails. Check that the workflow processes both sequentially and creates two drafts while auto-send is false.

**Important limitations**

- n8n does not run while Windows/Docker is off. n8n and FastAPI must both start again for recovery to work. The built-in n8n startup trigger executes once, not continuously. The catch-up waits for the API (up to about 15 minutes) to start. If FastAPI is still down, resolve the API issue and manually retry that execution; a second recovery will occur on the next n8n restart. For unattended recovery, configure both apps to auto-start on Windows.
- A 7-day catch-up cannot recover emails older than seven days. Change Gmail query `newer_than:7d` to `newer_than:15d` before the next long offline period if needed.
- If a Gmail draft/send node fails after the classifier has already recorded a decision, manual review may be necessary to avoid duplicate drafts. Do not delete the JSON ledger to force retries.
- Concurrency=1 affects *production* executions within this n8n instance, not manually executed workflows. Multiple independently running Python API worker processes would each have their own lock; keep the configured one-worker startup script.
- Actual Gmail OAuth, browser AI, and n8n processing need a live end-to-end test on your PC; tests here are offline/unit tests.
