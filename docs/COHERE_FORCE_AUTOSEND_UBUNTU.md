# Recruiter email bot — Cohere-first and forced auto-send (Ubuntu)

Version: 2026-11-15. Source: the user's uploaded `auto-email-recruiter-reply.zip`.

## Behavior

- `AUTO_SEND_RECRUITER_REPLY=true`: **every identified recruiter email for which the bot produces a reply** returns `READY_TO_SEND` + `SEND_REPLY`, even if the classifier asks for review, no candidate details were requested, confidence is low, role is ambiguous, etc. Salary / contract messages receive **neutral acknowledgement**, not acceptance or negotiation.
- `AUTO_SEND_RECRUITER_REPLY=false`: keeps existing draft/manual-review logic.
- Existing prefilters for automated senders, no-reply, non-recruiter messages, rejections/receipts, and exact duplicate Gmail message IDs remain. Do not turn them off; they prevent loops and accidental mass messaging.
- Missing details are **not invented**. The email omits missing values and asks for more information. All-models-down scenario falls back to a deterministic generic acknowledgement of recruiter-like emails so the API can still return `SEND_REPLY`.
- A successful API `SEND_REPLY` is only a **decision**: the Gmail send happens in n8n, and n8n must be active, authenticated, and able to reach FastAPI. Never assume `200 OK` from FastAPI means Gmail actually sent the email.

## Routing order (both classifier and reply writer)

1. Cohere API 1 (`COHERE_API_KEY`)
2. Cohere API 2 (`COHERE_API_KEY2`)
3. Cohere API 3 (`COHERE_API_KEY3`)
4. Claude Web (if `USE_CLAUDE_WEB=true`)
5. ChatGPT Web (if `USE_CHATGPT_WEB=true`)

The old optional Claude and OpenAI paid APIs remain available when their `USE_*` flags are enabled, **but disable them** to get the exact sequence above. Each Cohere key gets one attempt per operation; rate-limit, invalid/expired key, request/network timeout, server error, or invalid model output tries the next key without waiting for the original key to recover. Cohere requests have a default 20-second timeout. Browser fallbacks wait up to 75 seconds for a response. No live API/Gmail requests were performed in this build.

We use Cohere's current REST v2 endpoint (`POST https://api.cohere.com/v2/chat`) with JSON mode using `httpx`. This avoids depending on the obsolete v1 `co.chat(message=...)` example. The user's email subject, new message body, and local relevant knowledge excerpts can be sent to the selected Cohere account for processing; check your data-handling requirements.

## Safe upgrade on your existing Oracle Ubuntu server

**1. Stop FastAPI** (Ctrl+C in its terminal). You can leave n8n's Docker container running, but disable the workflow while upgrading so no scheduled emails arrive during the brief changeover.

**2. Back up the live project.** From Ubuntu terminal:

```bash
cd ~/ird-projects
cp -a auto-email-recruiter-reply auto-email-recruiter-reply.backup-$(date +%Y%m%d-%H%M%S)
```

**3. Upload the ZIP** (`auto-email-recruiter-reply_cohere_force_send.zip`) into `~/ird-projects/` with VS Code Remote SSH; use VS Code's Explorer to upload. Extract onto the existing project root:

```bash
cd ~/ird-projects
unzip -o auto-email-recruiter-reply_cohere_force_send.zip -d auto-email-recruiter-reply
```

This ZIP deliberately contains **no `.env`**, `.git` history, prior email ledger JSON, personal login/session/profile, or logs. It includes the source code and original n8n workflow JSON. Do not delete your existing project before extraction.

**4. Add to the existing `.env` file** (not `n8n/docker-compose.yml`):

```ini
AUTO_SEND_RECRUITER_REPLY=true
USE_COHERE_API=true
COHERE_API_KEY=PASTE_YOUR_COMPLETE_KEY_1_HERE
COHERE_API_KEY2=PASTE_YOUR_COMPLETE_KEY_2_HERE
COHERE_API_KEY3=PASTE_YOUR_COMPLETE_KEY_3_HERE
COHERE_MODEL=command-a-plus-05-2026
COHERE_TIMEOUT_SECONDS=20
USE_CLAUDE_API=false
USE_CHATGPT_API=false
USE_CLAUDE_WEB=true
USE_CHATGPT_WEB=true
```

Use actual complete keys in your **private local** `.env`; never paste them in chat, commit them to Git, or put them in n8n workflow exports. `COHERE_API_KEY2` and `COHERE_API_KEY3` can remain empty; empty slots are skipped. If only the first key is configured, Claude Web is next after its failure.

**5. Keep n8n Compose auto-send set to true** (your existing file already contains this):

```yaml
environment:
  - AUTO_SEND_RECRUITER_REPLY=true
```

Do not overwrite existing Compose networking or volume settings. The original n8n flow already routes `SEND_REPLY` through an additional check of the Compose environment flag and the API's `auto_send=true`. Since both are true, Gmail will take the **Reply in thread** path. An active already-imported workflow does not need to be reimported solely for Python changes. If you changed the Compose file, from `n8n/` run `docker compose up -d`.

**6. Restart FastAPI:**

```bash
cd ~/ird-projects/auto-email-recruiter-reply
chmod +x start_recruiter_email.sh
./start_recruiter_email.sh
```

**7. Verify configuration from another Ubuntu terminal:**

```bash
curl -s http://127.0.0.1:8090/health
curl -s http://127.0.0.1:8090/ai/providers/status | python3 -m json.tool
```

Check `enabled_providers` begins with `cohere_api_1`, `cohere_api_2`, `cohere_api_3` and continues to `claude_web`, `chatgpt_web`. Confirm `auto_send_recruiter_reply: true` and `cohere_api_keys_present: {key1: true, key2: true, key3: true}`. These endpoints only confirm configuration, not live Cohere key validity.

**8. Re-enable the n8n workflow** and send a **NEW** interview email from a test account:

Subject: `Python Developer opportunity`

Body: `Tomorrow is the interview. Can you confirm?`

Within the next successful n8n poll, confirm an actual message appears under **Gmail > Sent** and thread receives `JobBot/Recruiter/AutoReplied`. The reply should acknowledge the interview and ask for time/platform details; it must NOT falsely confirm that the candidate can attend. Review one run under n8n **Executions** before turning back on high-volume polling.

## Troubleshooting

- Still draft: Check `AUTO_SEND_RECRUITER_REPLY=true` in **both** the FastAPI `.env` and active n8n container (`docker exec n8n-recruiter-email printenv AUTO_SEND_RECRUITER_REPLY`). An edited Docker Compose file is not necessarily deployed yet. Ensure the FastAPI **process was restarted** after modifying `.env`.
- Cohere skipped: Check status `cohere_api_keys_present` and `enabled_providers`; empty keys or `USE_COHERE_API=false` skip it.
- Cohere failed: Check `logs/recruiter_api.log` for `cohere_api_1` / `FALLBACK` and failure type (RATE_LIMIT, AUTH, TIMEOUT, OUTPUT_VALIDATION). Never print `.env` or token values into logs.
- Browser verification popup: The browser is a fallback, not primary. If Cohere works, Chrome is not needed for those requests. If both browsers are unusable, the bot can send a generic deterministic acknowledgement for recruiter-like emails.
- Reply not actually sent: FastAPI `SEND_REPLY` is not Gmail delivery. Check n8n **Gmail: Reply in thread**, its credential authorization, execution failure, and `Audit log: mark result`.
- Existing drafts: old Gmail drafts are NOT automatically sent or relabeled retroactively. The duplicate ledger prevents blind resends. For a clean test, use a **new** Gmail message ID.
- Powered-off n8n or expired Gmail authorization can still miss emails. Startup backfill currently searches the last 7 days, not infinite history. Monitor n8n failures if uninterrupted coverage matters.

## Tests

`python3 -m pytest -q tests/ai/test_cohere_failover.py`

Run full tests in your project's `.venv` after installing `requirements.txt`.

No live API key test or actual Gmail send can be performed in an offline code-only verification.
