# Generic international visa and relocation update

This update replaces `US_RELOCATION_DATE`, `WILLING_TO_RELOCATE_TO_US`,
`US_VISA_SPONSORSHIP_REQUIRED` and `US_WORK_AUTHORIZATION` with
`INTERNATIONAL_RELOCATION_STATUS`, `WILLING_TO_RELOCATE_INTERNATIONALLY`,
`VISA_SPONSORSHIP_REQUIRED` and `WORK_AUTHORIZATION_DESTINATION`.
`VISA_STATUS` and `PASSPORT_AVAILABLE` remain and have country-neutral answers.

Questions mentioning the US, UAE, Saudi Arabia, Japan, or any other named
country are recognized by generic wording, not a fixed country list.

Every immigration and overseas relocation reply still requires ManualReview,
even if auto-send is enabled. `WORK_AUTHORIZATION_DESTINATION` is intentionally
unanswered until country-specific legal permission has been verified. Missing
questions continue to enter `data/recruiter_questions_to_review.json`.

To apply: stop the API; back up the live project and `.env` plus JSON ledger;
merge the ZIP, leaving all live credentials/data intact; restart the API.
If using Claude/ChatGPT Projects, replace the attached
`experience_details.md` with the newer version (`2026-11-14.3`) and update
knowledge synchronization. The old already-generated drafts will not be
rewritten automatically.
