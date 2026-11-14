"""
recruiter_email/field_registry.py
=================================

Every candidate field a recruiter may ask for, with:
  * label    - how the value is shown in the reply ("Notice Period: 30 days")
  * unit     - appended to purely numeric values ("years", "days")
  * aliases  - regexes used by the deterministic detector (in addition to the LLM)
  * sensitive- if ONLY the deterministic detector (not the LLM) saw the request,
               the e-mail goes to manual review instead of over-sharing.

Any extra KEY=VALUE you add to config/experience_details.md that is not listed
here automatically becomes an answerable field (label derived from the key),
e.g. REASON_FOR_JOB_CHANGE=..., VISA_STATUS=...

Skill and role-area fields (PySpark, Databricks, AWS Data Engineering, ...) are
generated from recruiter_email/knowledge_aliases.py, so a recruiter asking for
"Gen AI exp" or "years in Spark" maps to the same key the Markdown parser
fills from "I have 3 years of experience in Generative AI".

RELEVANT_EXPERIENCE_YEARS is answered per role (see ROLE_RELEVANT_KEYS), never
from one generic number.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from recruiter_email.knowledge_aliases import ROLE_AREAS, SKILLS, alias_pattern


@dataclass(frozen=True)
class FieldSpec:
    key: str
    label: str
    unit: str = ""
    aliases: tuple[str, ...] = field(default_factory=tuple)
    sensitive: bool = False


FIELDS: dict[str, FieldSpec] = {
    spec.key: spec
    for spec in (
        FieldSpec("FULL_NAME", "Full Name", aliases=(r"\b(?:full|candidate'?s?)\s+name\b", r"^\W*name\s*[:\-?]")),
        FieldSpec("AGE", "Age", sensitive=True, aliases=(
            r"\bage\b", r"\bhow\s+old\s+are\s+you\b",
            r"\bcandidate(?:'s)?\s+age\b")),
        FieldSpec(
            "TOTAL_EXPERIENCE_YEARS", "Total Experience", "years",
            aliases=(r"total\s+(?:work\s+|it\s+)?exp(?:erience)?", r"overall\s+exp(?:erience)?",
                     r"exp(?:erience)?\s+in\s+years", r"years\s+of\s+(?:total\s+)?experience",
                     r"^\W*(?:total\s+)?exp(?:erience)?\s*[:\-?]"),
        ),
        FieldSpec("RELEVANT_EXPERIENCE_YEARS", "Relevant Experience", "years",
                  aliases=(r"\brelevant\s+(?:work\s+|it\s+)?exp(?:erience)?", r"\brel(?:evant)?\.?\s+exp\b")),
        FieldSpec("LLM_EXPERIENCE_YEARS", "LLM Experience", "years",
                  aliases=(r"\bllms?\b[^\n]{0,25}exp", r"exp[^\n]{0,25}\bllms?\b")),
        FieldSpec("GENAI_EXPERIENCE_YEARS", "GenAI Experience", "years",
                  aliases=(r"gen\s*-?\s*ai[^\n]{0,25}exp", r"generative\s+ai[^\n]{0,25}exp",
                           r"exp[^\n]{0,25}gen\s*-?\s*ai")),
        FieldSpec("AI_AGENT_EXPERIENCE_YEARS", "AI Agent Experience", "years",
                  aliases=(r"(?:ai\s+)?agents?\b[^\n]{0,20}exp", r"agentic[^\n]{0,25}exp")),
        FieldSpec("LANGGRAPH_EXPERIENCE", "LangGraph Experience", aliases=(r"lang\s*-?\s*graph",)),
        FieldSpec("AGENTCORE_EXPERIENCE", "AgentCore Experience", aliases=(r"agent\s*-?\s*core",)),
        FieldSpec("PYTHON_EXPERIENCE_YEARS", "Python Experience", "years",
                  aliases=(r"python[^\n]{0,20}exp", r"exp[^\n]{0,20}python")),
        FieldSpec("DATA_ENGINEERING_EXPERIENCE_YEARS", "Data Engineering Experience", "years",
                  aliases=(r"data\s+engineering[^\n]{0,20}exp",)),
        FieldSpec("AWS_EXPERIENCE_YEARS", "AWS Experience", "years", aliases=(r"\baws\b[^\n]{0,20}exp",)),
        FieldSpec("AZURE_EXPERIENCE_YEARS", "Azure Experience", "years", aliases=(r"\bazure\b[^\n]{0,20}exp",)),
        FieldSpec("NOTICE_PERIOD_DAYS", "Notice Period", "days",
                  aliases=(r"notice\s*-?\s*period", r"\bnp\b\s*[:\-?]", r"how\s+soon\s+can\s+you\s+join",
                           r"\bjoining\s+(?:time|date|availability)\b")),
        FieldSpec("CURRENT_CTC", "Current CTC", sensitive=True,
                  aliases=(r"\bcurrent\s+ctc\b", r"\bcctc\b", r"current\s+(?:salary|package|compensation)",
                           r"^\W*ctc\s*[:\-?]")),
        FieldSpec("EXPECTED_CTC", "Expected CTC", sensitive=True,
                  aliases=(r"\bexpected\s+ctc\b", r"\bectc\b", r"expected\s+(?:salary|package|compensation)")),
        FieldSpec("CURRENT_LOCATION", "Current Location",
                  aliases=(r"current\s+location", r"present\s+location", r"^\W*location\s*[:\-?]",
                           r"where\s+are\s+you\s+(?:currently\s+)?(?:based|located)")),
        FieldSpec("PREFERRED_LOCATION", "Preferred Location", aliases=(r"preferred\s+location",)),
        FieldSpec("LINKEDIN_PROFILE_URL", "LinkedIn Profile",
                  aliases=(r"linked\s*-?\s*in\s+(?:profile|url|link|id)", r"^\W*linked\s*-?\s*in\s*[:\-?]")),
        FieldSpec("MOBILE_NUMBER", "Mobile Number", sensitive=True,
                  aliases=(r"\bmobile\b", r"\bphone\s+(?:no|number)", r"contact\s+(?:no|number)",
                           r"whats\s*app\s+(?:no|number)")),
        FieldSpec("EMAIL_ID", "Email ID", aliases=(r"e-?mail\s*(?:id|address)", r"\bmail\s+id\b")),
        # Generic INTERNATIONAL immigration/relocation facts; never infer work rights.
        # Review-required even when auto-send is enabled (service safety gate).
        FieldSpec("VISA_STATUS", "Visa Status", aliases=(
            r"\bvisa\s+(?:status|type|category|details)\b",
            r"\b(?:have|hold|got|possess)\b[^\n]{0,35}\b(?:visa|entry\s+permit)\b",
            r"\b(?:us|u\.?s\.?|usa|uae|japan|saudi\s+arabia|qatar|dubai)\s+(?:work\s+)?visa\b",
            r"^\W*visa\s*[:?]")),
        FieldSpec("PASSPORT_AVAILABLE", "Passport Available", aliases=(
            r"\b(?:do\s+you\s+have\s+(?:a\s+)?|have\s+(?:a\s+)?)passport\b",
            r"\bpassport\s+(?:available|status|holder)\b", r"^\W*passport\s*[:?]")),
        FieldSpec("INTERNATIONAL_RELOCATION_STATUS", "International Relocation Status", aliases=(
            r"\bwhen\s+(?:did|have)\s+you\s+(?:move|moved|relocate|relocated)\s+to\b",
            r"\b(?:relocation|relocated)\s+(?:date|history|status)\b",
            r"\b(?:date|when)\b[^\n]{0,55}\brelocat(?:e|ed|ion)\b",
            r"\bhave\s+you\s+(?:already\s+)?relocated\s+to\b")),
        FieldSpec("WILLING_TO_RELOCATE_INTERNATIONALLY", "Willing to Relocate Internationally", aliases=(
            r"\b(?:willing|open|ready|able)\s+to\s+relocat(?:e|ion)\b",
            r"\b(?:can|would)\s+you\s+relocate\b",
            r"\b(?:relocation|relocate)\s+(?:willingness|preference)\b",
            r"\bopen\s+to\s+(?:international|overseas|foreign)\s+(?:relocation|roles|jobs)\b")),
        # Authorization is country-specific and unverified; keep it UNANSWERED.
        FieldSpec("WORK_AUTHORIZATION_DESTINATION", "Destination Country Work Authorization", aliases=(
            r"\b(?:authorized|eligible|permitted)\s+to\s+work\s+in\b",
            r"\b(?:work|employment)\s+(?:authorization|eligibility|permission|permit|rights)\b",
            r"\blegally\s+(?:allowed|able|eligible|authorized)\s+to\s+work\b")),
        FieldSpec("VISA_SPONSORSHIP_REQUIRED", "Visa Sponsorship Required", aliases=(
            r"\b(?:visa|work\s+(?:visa|permit|authorization)|employer)\s+sponsorship\b",
            r"\b(?:sponsor|sponsorship)\b[^\n]{0,40}\b(?:visa|work|permit|relocat)\b",
            r"\b(?:need|require|seeking|request)\b[^\n]{0,40}\b(?:visa\s+support|sponsor(?:ship)?|work\s+visa)\b")),
        FieldSpec("CURRENT_COMPANY", "Current Company",
                  aliases=(r"current\s+(?:company|employer|organi[sz]ation)",)),
        FieldSpec("CURRENT_ROLE", "Current Role",
                  aliases=(r"current\s+(?:role|designation|title|position)",)),
    )
}



def _alias_spec(key: str, label: str, aliases: tuple[str, ...]) -> FieldSpec:
    words = alias_pattern(aliases)
    unit = "years" if key.endswith("_YEARS") else ""
    return FieldSpec(key, label, unit, aliases=(words + r"[^\n]{0,25}\bexp", r"\bexp\w*[^\n]{0,25}" + words,
                                               r"years?\s+(?:of\s+exp\w*\s+)?(?:in|with|on)\s+" + words))


# Role areas first: their matches are consumed before single skills are checked,
# so "AWS Data Engineering experience" is not also read as "AWS experience".
ROLE_AREA_FIELDS: dict[str, FieldSpec] = {
    key: _alias_spec(key, label, aliases) for key, (label, aliases) in ROLE_AREAS.items()
}
for _key, (_label, _aliases) in SKILLS.items():
    _generated = _alias_spec(_key, _label, _aliases)
    _existing = FIELDS.get(_key)
    FIELDS[_key] = _generated if _existing is None else FieldSpec(
        _key, _existing.label, _existing.unit, aliases=_existing.aliases + _generated.aliases,
        sensitive=_existing.sensitive)
for _key, _spec in ROLE_AREA_FIELDS.items():
    FIELDS.setdefault(_key, _spec)

_COMPILED = {
    key: tuple(re.compile(pattern, re.IGNORECASE | re.MULTILINE) for pattern in spec.aliases)
    for key, spec in FIELDS.items()
}
_RELEVANT = re.compile(r"\brelevant\s+(?:work\s+|it\s+)?exp\w*|\brel\.?\s+exp\b", re.IGNORECASE)
_ROLE_AREA_COMPILED = {
    key: tuple(re.compile(pattern, re.IGNORECASE) for pattern in spec.aliases)
    for key, spec in ROLE_AREA_FIELDS.items()
}


def label_for(key: str) -> str:
    spec = FIELDS.get(key)
    if spec:
        return spec.label
    return key.replace("_", " ").title().replace("Ctc", "CTC").replace("Url", "URL").replace("Id", "ID")


def unit_for(key: str) -> str:
    spec = FIELDS.get(key)
    if spec:
        return spec.unit
    if key.endswith("_YEARS"):
        return "years"
    if key.endswith("_DAYS"):
        return "days"
    return ""


def is_sensitive(key: str) -> bool:
    spec = FIELDS.get(key)
    return bool(spec and spec.sensitive)


def format_value(key: str, value: str) -> str:
    """Deterministic formatting. Never changes the value itself."""
    unit = unit_for(key)
    clean = value.strip()
    if unit and re.fullmatch(r"\d+(?:\.\d+)?\+?", clean):
        return f"{clean} {unit}"
    return clean


def detect_fields(request_text: str) -> list[str]:
    """
    Deterministic field detection over the recruiter's request lines.
    Line by line: role-area phrases ("AWS Data Engineering exp") are matched and
    blanked out first so the single-skill patterns do not also fire on them.
    """
    found: list[str] = []
    for line in (request_text or "").splitlines() or [""]:
        remaining, area_found = line, False
        for key, patterns in _ROLE_AREA_COMPILED.items():
            for pattern in patterns:
                match = pattern.search(remaining)
                if match:
                    found.append(key)
                    area_found = True
                    remaining = remaining[:match.start()] + " " * (match.end() - match.start()) + remaining[match.end():]
        relevant = _RELEVANT.search(remaining)
        if relevant:
            remaining = _RELEVANT.sub(" ", remaining)
            if not area_found:  # "Relevant experience in AWS Data Engineering" -> the area key only
                found.append("RELEVANT_EXPERIENCE_YEARS")
        for key, patterns in _COMPILED.items():
            if key in ROLE_AREA_FIELDS or key == "RELEVANT_EXPERIENCE_YEARS":
                continue
            if any(pattern.search(remaining) for pattern in patterns):
                found.append(key)
    return list(dict.fromkeys(found))
