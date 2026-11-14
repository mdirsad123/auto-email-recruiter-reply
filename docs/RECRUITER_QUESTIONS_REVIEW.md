# Recruiter answers and missing-question review

## Verified international visa and relocation values

The updated `config/experience_details.md` contains the user's confirmed facts:
no current visa for the destination country, passport available, currently based in India,
willingness to relocate for a suitable opportunity after authorization,
and a need for employer support with the destination country work visa process.
These entries are **not** evidence of a valid destination-country visa or work authorization.

When an email asks about any international immigration/authorization item, the
pipeline **creates a manual-review draft** and does not auto-send it.

## Future unknown or missing questions

The service writes short *labels* to `data/recruiter_questions_to_review.json`.
It does not store message bodies or automatically write invented values into
the authoritative Markdown. The queue uses the `messages` dictionary purely
as a record store, separate from `data/recruiter_email_ledger.json`.

To teach your bot a new answer:

1. Open the review queue. Inspect records with `review_status: pending`.
2. Verify the requested fact personally before publishing it.
3. Add a unique `KEY=VALUE` line under the appropriate section in
   `config/experience_details.md` (for a truly new question, add a regex alias
   in `recruiter_email/field_registry.py` so bare recruiter wording is detected).
4. Increment `Document version` and synchronize the updated file to any
   configured Claude/ChatGPT Project attachment. See `docs/RECRUITER_KNOWLEDGE_BASE_V2_GUIDE.md`.
5. Change the reviewed queue entry's `review_status` to `answered` after
   confirming a test draft. **Do not delete or reset the duplicate ledger.**

Existing already-processed emails won't automatically be re-sent after editing
knowledge. Review or edit any previously saved Gmail drafts manually.

## Common questions worth reviewing (do not guess answers)

- Work authorization in any destination country (including the USA)
- Passport expiration / validity date, visa class, immigration history
- Current / expected salary currency and compensation format
- Joining date, notice period negotiation, buyout eligibility
- Date of birth, citizenship, government IDs (handle privately)
- Relocation to a particular city/country, remote/hybrid preference
- Shift timings, employment type, full-time/contract/onsite
- Interview availability and time zone
- References / background checks / documents and payslips
- Industry/project-specific experience not listed in the CV
- Recruiter requests requiring action, signatures, legal commitments or attachments

Never put passport numbers, ID numbers, bank data or other secrets into
an automatically shared Markdown knowledge source. Handle these manually.

## Review queue found on 14 November 2026

The user's uploaded review JSON had only two unmapped labels:

- **Age:** `AGE=25` has been provisionally included from the previously supplied
  age for October/November 2026. Age changes over time; confirm before sending.
  Age requests now always require manual review, even when auto-send is enabled.
- **Date of relocation to US:** the correct existing international fact is
  `INTERNATIONAL_RELOCATION_STATUS`; it means the candidate is currently in
  India and has NOT relocated to the destination country. Do not invent a date.

The queue JSON is a *tracker*, not the source of truth. Put approved values in
`config/experience_details.md`, not in the queue. The legacy queued rows can be
marked `answered` after you verify the next draft. Never overwrite your live
`data/recruiter_questions_to_review.json` or email ledger with files from ZIP.

### Chrome tab behavior

After each Claude Chat 1 or Chat 2 response (including timeouts/errors), the
service closes its own newly opened Chrome tab, logs success/failure, and restores
the originally active tab. It does NOT close previously opened tabs or your
personal Chrome windows. If tabs still accumulate, restart the Python API with
this version, check logs for "tab remains open", and verify no other older copy
of the API is running against the same debug Chrome profile.
