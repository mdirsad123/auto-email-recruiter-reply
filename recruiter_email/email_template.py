"""
recruiter_email/email_template.py
=================================

Deterministic greeting + signature for every recruiter reply, from ONE place:
config/recruiter_email_template.json (override: RECRUITER_EMAIL_TEMPLATE_PATH).

    Dear Recruiter,

    <opening paragraph written by the AI, fact-free>

    <Label: value lines built by Python from verified facts>

    <closing paragraph written by the AI, fact-free>

    Best regards,
    <signature lines, exactly as configured>

Any greeting or sign-off the AI adds anyway is stripped, so the reply never
contains two greetings or two signatures, whichever of the four providers wrote it.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TEMPLATE_PATH = PROJECT_ROOT / "config" / "recruiter_email_template.json"

DEFAULT_TEMPLATE: dict[str, Any] = {
    "greeting": "Dear Recruiter,",
    "closing": "Best regards,",
    "signature_lines": [
        "Md Irsad",
        "+91-8409718735 | mdirsadtech7305@gmail.com",
        "LinkedIn: https://www.linkedin.com/in/md-irsad-17ab4b218",
        "GitHub: https://github.com/mdirsad123",
    ],
}

_GREETING_LINE = re.compile(
    r"^\s*(?:dear|hi|hello|hey|greetings|good\s+(?:morning|afternoon|evening))\b[^\n.!?]{0,60}[,!:]?\s*$",
    re.IGNORECASE)
_GREETING_PREFIX = re.compile(
    r"^\s*(?:dear\s+(?:recruiter|hiring\s+(?:manager|team)|sir(?:\s*/\s*madam)?|madam|team|\w+)|hi(?:\s+\w+)?|"
    r"hello(?:\s+\w+)?|greetings)\s*[,!:]\s*", re.IGNORECASE)
_SIGN_OFF = re.compile(
    r"(?:^|\n|(?<=[.!?])\s+)\s*(?:best\s+regards|warm\s+regards|kind\s+regards|regards|thanks\s*(?:&|and)\s*regards|"
    r"sincerely|yours\s+(?:sincerely|truly|faithfully)|thanks|thank\s+you|cheers)\s*[,.!]?\s*(?:\n.*)?$",
    re.IGNORECASE | re.DOTALL)


def template_path() -> Path:
    raw = os.getenv("RECRUITER_EMAIL_TEMPLATE_PATH", "").strip()
    path = Path(raw) if raw else DEFAULT_TEMPLATE_PATH
    return path if path.is_absolute() else PROJECT_ROOT / path


def load_template(path: Path | None = None) -> dict[str, Any]:
    """Configured template merged over the defaults (read on every call; tiny file)."""
    path = path or template_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except (OSError, json.JSONDecodeError):
        raw = {}
    merged = {**DEFAULT_TEMPLATE, **{k: v for k, v in raw.items() if not k.startswith("_")}}
    merged["signature_lines"] = [str(line) for line in merged.get("signature_lines") or []]
    return merged


def template_email(template: dict[str, Any]) -> str | None:
    """The candidate's own e-mail from the signature (used for self-sent detection)."""
    for line in template.get("signature_lines", []):
        match = re.search(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}", line)
        if match:
            return match.group(0)
    return None


def strip_greeting(text: str) -> str:
    """Remove a greeting the AI put at the start of a paragraph."""
    lines = (text or "").strip().splitlines()
    while lines and _GREETING_LINE.match(lines[0]):
        lines.pop(0)
    return _GREETING_PREFIX.sub("", "\n".join(lines).strip(), count=1).strip()


def strip_sign_off(text: str, template: dict[str, Any] | None = None) -> str:
    """Remove a sign-off / signature block the AI put at the end of a paragraph."""
    cleaned = _SIGN_OFF.sub("", (text or "").strip()).strip()
    for line in (template or DEFAULT_TEMPLATE).get("signature_lines", []):
        cleaned = cleaned.replace(line, "").strip()
    return cleaned


def compose_email(opening: str, detail_lines: list[str], closing: str,
                  template: dict[str, Any] | None = None) -> str:
    """Final reply body: fixed greeting, AI paragraphs (cleaned), fact lines, fixed signature."""
    template = template or load_template()
    opening = strip_sign_off(strip_greeting(opening), template)
    closing = strip_sign_off(strip_greeting(closing), template)
    parts = [template["greeting"], ""]
    if opening:
        parts += [opening, ""]
    if detail_lines:
        parts += detail_lines + [""]
    if closing:
        parts += [closing, ""]
    parts += [template["closing"], *template["signature_lines"]]
    return "\n".join(parts).strip() + "\n"
