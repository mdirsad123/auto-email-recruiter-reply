"""
recruiter_email/knowledge_base.py
=================================

Loads config/experience_details.md - the candidate's professional knowledge
base written in natural English - and turns it into:

* ``facts``     {FIELD_KEY: value} the recruiter pipeline can answer from,
* ``evidence``  {FIELD_KEY: [source sentence, ...]} for every fact,
* ``conflicts`` {FIELD_KEY: [value, value]} when the document contradicts itself
  (a conflicting key is left OUT of ``facts`` so it goes to manual review),
* ``chunks``    heading-aware paragraphs used for retrieval,
* ``version`` / ``sha256`` / ``revision`` to know exactly which copy was used.

Supported content (mix freely, the file can grow over time):

    # Professional Profile
    Document version: 2026-11-09.1                 <- optional, recommended
    My full name is Md Irsad.
    I have overall 3+ years of professional experience.
    My relevant experience in AWS Data Engineering, Azure Data Engineering,
    and AI Engineering is 2 years.
    I have 3 years of experience in LLMs, GenAI and AI agents.
    - Python experience: 10 years                   <- bullets / "Label: value"
    | Field | Value |                               <- two-column tables
    NOTICE_PERIOD_DAYS=30                           <- optional KEY=VALUE

The file is read fresh for every recruiter e-mail, so edits apply to the next
request. Values are never logged.

Functional style: every function is pure except ``load_knowledge``.
"""

from __future__ import annotations

import hashlib
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from recruiter_email.knowledge_aliases import ROLE_AREAS, SKILLS, key_for_item, normalise_item

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PATH = PROJECT_ROOT / "config" / "experience_details.md"
DOCUMENT_NAME = "experience_details.md"

KV_LINE = re.compile(r"^\s*(?:[-*]\s+)?`?([A-Z][A-Z0-9_]{1,63})`?\s*=\s*(.*?)\s*$")
MISSING_MARKERS = {"", "tbd", "todo", "?", "n/a", "na", "none", "null", "-", "<value>", "xxx"}
_VERSION = re.compile(
    r"^\s*(?:<!--\s*)?(?:[-*]\s+)?\**(?:kb[-_ ]|knowledge[-_ ]base[-_ ]|document[-_ ])?(?:version|revision)\**"
    r"\s*[:=]\s*\**\s*([A-Za-z0-9][\w.+\-]{0,40})", re.IGNORECASE | re.MULTILINE)

NUM = r"(\d{1,2}(?:\.\d{1,2})?\s*\+?)"
YRS = r"\s*\+?\s*(?:years?|yrs?)\b"
_APPROX = r"(?:around\s+|about\s+|approximately\s+|approx\.?\s+|nearly\s+|over\s+|more\s+than\s+)?"

TOTAL_PATTERNS = (
    re.compile(r"\b(?:overall|total)\s+(?:professional\s+|work\s+|it\s+|industry\s+)?(?:experience\s*(?:is|of|:|-)?\s*)?"
               r"(?:of\s+)?" + _APPROX + NUM + YRS, re.IGNORECASE),
    re.compile(NUM + YRS + r"\s+of\s+(?:overall|total)\s+(?:professional\s+|work\s+|it\s+|industry\s+)?experience",
               re.IGNORECASE),
    re.compile(_APPROX + NUM + YRS + r"\s+of\s+(?:professional|industry|it|work)\s+experience(?!\s+(?:in|with|on|as)\b)",
               re.IGNORECASE),
)
RELEVANT_PATTERNS = (
    # "My relevant experience in AWS Data Engineering is 2 years"
    re.compile(r"\brelevant\s+experience\s+(?:in|as|for|across)\s+(?P<list>.+?)\s+(?:is|=|:|of|-)\s*"
               + _APPROX + NUM + YRS, re.IGNORECASE),
    # "Relevant experience (AWS Data Engineering): 2 years"
    re.compile(r"\brelevant\s+experience\s*[\(\[]\s*(?:in\s+|for\s+|as\s+)?(?P<list>[^)\]]+)[\)\]]\s*\**\s*"
               r"(?:is|=|:|-)?\s*\**\s*" + _APPROX + NUM + YRS, re.IGNORECASE),
)
AREA_PATTERNS = (
    # "I have 3 years of experience in LLMs, GenAI and AI agents"
    re.compile(_APPROX + NUM + YRS + r"\s+of\s+(?:hands[- ]on\s+|professional\s+|strong\s+)?experience\s+"
               r"(?:in|with|on|as|across)\s+(?P<list>[^.;\n]+)", re.IGNORECASE),
    # "Python experience: 10 years" / "My AWS experience is 6 years"
    re.compile(r"(?:^|\bmy\s+|[-*]\s+)(?P<list>[A-Za-z][\w/ ,&+.#-]{0,80}?)\s+experience\s*(?:is|=|:|-|of)?\s*"
               + _APPROX + NUM + YRS, re.IGNORECASE),
    # "10 years in Python" / "6 years with AWS"
    re.compile(NUM + YRS + r"\s+(?:in|with|on)\s+(?P<list>[^.;\n]+)", re.IGNORECASE),
)
NAME_PATTERN = re.compile(
    r"(?:^|\b(?i:my)\s+)\**(?i:full\s+)?(?i:name)\**\s*(?i:is|:|-)\s*\**\s*([A-Z][A-Za-z.'-]*(?:\s+[A-Z][A-Za-z.'-]*){0,5})")


class CandidateFactsError(Exception):
    """experience_details.md is missing, unreadable or unusable (input defect)."""


# ---------------------------------------------------------------------------
# Small pure helpers
# ---------------------------------------------------------------------------

_PLACEHOLDER_VALUE = re.compile(r"<[^<>]{1,40}>")


def is_missing(value: str | None) -> bool:
    """Empty, TBD/N/A-style markers and template placeholders such as '<N> years' count as missing."""
    clean = (value or "").strip()
    return clean.lower() in MISSING_MARKERS or bool(_PLACEHOLDER_VALUE.search(clean))


def normalise_number(value: str) -> str:
    """'3+ years' -> '3+', '2 yrs' -> '2', 'Yes - 2 years' unchanged."""
    clean = (value or "").strip()
    match = re.fullmatch(r"(\d{1,2}(?:\.\d{1,2})?)\s*(\+?)\s*(?:\+?\s*(?:years?|yrs?))?\.?", clean, re.IGNORECASE)
    return f"{match.group(1)}{match.group(2)}" if match else clean


def comparable(value: str) -> str:
    return re.sub(r"\s+", " ", normalise_number(value)).strip().lower()


def normalise_text(text: str) -> str:
    """For evidence checks: lowercase, no markdown punctuation, single spaces."""
    value = re.sub(r"[*_`#>|]", " ", text or "").lower()
    return re.sub(r"\s+", " ", value).strip()


def content_hash(text: str) -> str:
    return hashlib.sha256((text or "").replace("\r\n", "\n").encode("utf-8")).hexdigest()


def find_version(text: str) -> str | None:
    """Explicit document version: 'Document version: 2026-11-09.1', front matter 'version:' or VERSION=..."""
    kv = re.search(r"^\s*(?:DOCUMENT_VERSION|KB_VERSION)\s*=\s*(\S+)", text or "", re.MULTILINE)
    if kv:
        return kv.group(1).strip()
    match = _VERSION.search("\n".join((text or "").splitlines()[:40]))
    return match.group(1).strip().rstrip(".") if match else None


def make_revision(version: str | None, sha256: str) -> str:
    return f"{version or 'unversioned'}#{sha256[:12]}"


# ---------------------------------------------------------------------------
# Markdown -> blocks / chunks
# ---------------------------------------------------------------------------

def split_blocks(text: str) -> list[dict[str, str]]:
    """
    Markdown -> list of {"heading", "kind", "text"} blocks, kind in
    prose | bullet | table | kv. Paragraph lines are joined so sentences that
    wrap over several lines are read as one sentence.
    """
    blocks: list[dict[str, str]] = []
    headings: list[str] = []
    paragraph: list[str] = []
    in_code = False
    in_comment = False

    def heading() -> str:
        return " > ".join(headings)

    def flush() -> None:
        if paragraph:
            blocks.append({"heading": heading(), "kind": "prose", "text": " ".join(paragraph)})
            paragraph.clear()

    lines = (text or "").replace("\r\n", "\n").split("\n")
    if lines and lines[0].strip() == "---":  # YAML front matter
        end = next((i for i, line in enumerate(lines[1:], start=1) if line.strip() == "---"), None)
        if end:
            lines = lines[end + 1:]
    for raw in lines:
        line = raw.strip()
        if line.startswith("```"):
            flush()
            in_code = not in_code
            continue
        if in_code:
            continue
        if in_comment or line.startswith("<!--"):
            in_comment = "-->" not in line
            flush()
            continue
        if not line:
            flush()
            continue
        heading_match = re.match(r"^(#{1,6})\s+(.*)$", line)
        if heading_match:
            flush()
            level = len(heading_match.group(1))
            del headings[level - 1:]
            headings.extend([""] * (level - 1 - len(headings)))
            headings.append(heading_match.group(2).strip().strip("#").strip())
            headings[:] = [h for h in headings if h]
            continue
        if KV_LINE.match(line):
            flush()
            blocks.append({"heading": heading(), "kind": "kv", "text": line})
            continue
        if line.startswith("|"):
            flush()
            if not re.fullmatch(r"\|?[\s:|-]+\|?", line):  # skip |---|---| separators
                blocks.append({"heading": heading(), "kind": "table", "text": line})
            continue
        if re.match(r"^(?:[-*+]|\d+[.)])\s+", line):
            flush()
            blocks.append({"heading": heading(), "kind": "bullet",
                           "text": re.sub(r"^(?:[-*+]|\d+[.)])\s+", "", line)})
            continue
        if blocks and blocks[-1]["kind"] == "bullet" and raw.startswith(("  ", "\t")) and not paragraph:
            blocks[-1]["text"] += " " + line  # wrapped bullet
            continue
        paragraph.append(line)
    flush()
    return blocks


def sentences(block_text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9*\"'(])", block_text.strip())
    return [p.strip() for p in parts if p.strip()]


def table_cells(row: str) -> list[str]:
    return [cell.strip().strip("*").strip() for cell in row.strip().strip("|").split("|")]


# ---------------------------------------------------------------------------
# Fact extraction (pure)
# ---------------------------------------------------------------------------

def split_list(text: str) -> list[str]:
    """'AWS Data Engineering, Azure Data Engineering, and AI Engineering' -> 3 items; '/' splits as fallback."""
    items: list[str] = []
    for piece in re.split(r",|;|&|\band\b|\bor\b|\bas well as\b", text, flags=re.IGNORECASE):
        if not piece.strip():
            continue
        if key_for_item(piece) or "/" not in piece:
            items.append(piece)
        else:
            items.extend(p for p in piece.split("/") if p.strip())
    return items


def label_index(field_labels: dict[str, str]) -> dict[str, str]:
    """Normalised label/alias -> key, from the field registry labels and the alias tables."""
    index = {normalise_item(label): key for key, label in field_labels.items()}
    for table in (ROLE_AREAS, SKILLS):
        for key, (label, aliases) in table.items():
            index.setdefault(normalise_item(label), key)
            for alias in aliases:
                index.setdefault(normalise_item(f"{alias} experience"), key)
    index.update({
        "name": "FULL_NAME", "full name": "FULL_NAME", "candidate name": "FULL_NAME",
        "total experience": "TOTAL_EXPERIENCE_YEARS", "overall experience": "TOTAL_EXPERIENCE_YEARS",
        "total exp": "TOTAL_EXPERIENCE_YEARS", "phone": "MOBILE_NUMBER", "mobile": "MOBILE_NUMBER",
        "email": "EMAIL_ID", "e-mail": "EMAIL_ID", "linkedin": "LINKEDIN_PROFILE_URL",
        "github": "GITHUB_PROFILE_URL", "notice period": "NOTICE_PERIOD_DAYS", "location": "CURRENT_LOCATION",
    })
    return index


def extract_from_sentence(sentence: str) -> list[tuple[str, str]]:
    """Natural-language sentence -> [(KEY, normalised value)]."""
    found: list[tuple[str, str]] = []
    for pattern in TOTAL_PATTERNS:
        match = pattern.search(sentence)
        if match and "relevant" not in sentence[:match.start()].lower()[-40:]:
            found.append(("TOTAL_EXPERIENCE_YEARS", normalise_number(match.group(1))))
            break
    relevant = next((m for m in (p.search(sentence) for p in RELEVANT_PATTERNS) if m), None)
    if relevant:
        value = normalise_number(relevant.groups()[-1])
        for item in split_list(relevant.group("list")):
            key = key_for_item(item)
            if key:
                found.append((key, value))
    else:
        for pattern in AREA_PATTERNS:
            for match in pattern.finditer(sentence):
                value = normalise_number(match.group(1) if pattern is not AREA_PATTERNS[1] else match.group(2))
                for item in split_list(match.group("list")):
                    key = key_for_item(item)
                    if key:
                        found.append((key, value))
    name = NAME_PATTERN.search(sentence)
    if name:
        found.append(("FULL_NAME", name.group(1).strip().rstrip(".")))
    return found


def extract_labelled(text: str, labels: dict[str, str]) -> list[tuple[str, str]]:
    """'Notice Period: 30 days' / '**Total Experience** - 3+ years' when the label is known."""
    match = re.match(r"^\**\s*([A-Za-z][\w /&().+-]{1,60}?)\s*\**\s*[:\-–]\s*\**\s*(.+?)\s*$", text)
    if not match:
        return []
    key = labels.get(normalise_item(match.group(1)))
    if not key:
        return []
    value = match.group(2).strip().rstrip(".")
    return [(key, normalise_number(value) if key.endswith(("_YEARS", "_EXPERIENCE")) else value)]


def add_fact(state: dict[str, Any], key: str, value: str, evidence: str) -> None:
    if is_missing(value):
        state["empty"].add(key)
        return
    values = state["candidates"].setdefault(key, {})
    values.setdefault(comparable(value), value)
    state["evidence"].setdefault(key, [])
    if evidence not in state["evidence"][key]:
        state["evidence"][key].append(evidence)


def extract_facts(blocks: list[dict[str, str]], field_labels: dict[str, str]) -> dict[str, Any]:
    """Blocks -> {"facts", "evidence", "conflicts", "empty_keys"}."""
    labels = label_index(field_labels)
    state: dict[str, Any] = {"candidates": {}, "evidence": {}, "empty": set()}
    for block in blocks:
        kind, text = block["kind"], block["text"]
        if kind == "kv":
            key, value = KV_LINE.match(text).groups()  # type: ignore[union-attr]
            if key in {"DOCUMENT_VERSION", "KB_VERSION"}:
                continue
            add_fact(state, key, value, text)
            # free text in a KV value is still natural language (e.g. PROFILE_SUMMARY=...)
            for nl_key, nl_value in extract_from_sentence(value):
                if nl_key != key:
                    add_fact(state, nl_key, nl_value, text)
            continue
        if kind == "table":
            cells = table_cells(text)
            if len(cells) >= 2 and cells[1]:
                for key, value in extract_labelled(f"{cells[0]}: {cells[1]}", labels):
                    add_fact(state, key, value, text)
            continue
        for key, value in extract_labelled(text, labels) if kind == "bullet" or ":" in text[:60] else []:
            add_fact(state, key, value, text)
        for sentence in sentences(text):
            for key, value in extract_from_sentence(sentence):
                add_fact(state, key, value, sentence)

    facts: dict[str, str] = {}
    conflicts: dict[str, list[str]] = {}
    for key, values in state["candidates"].items():
        if len(values) == 1:
            facts[key] = next(iter(values.values()))
        else:
            conflicts[key] = list(values.values())
    return {"facts": facts, "evidence": state["evidence"], "conflicts": conflicts,
            "empty_keys": sorted(state["empty"] - set(facts) - set(conflicts))}


def parse_knowledge(text: str, source: Path | str = DOCUMENT_NAME,
                    field_labels: dict[str, str] | None = None) -> dict[str, Any]:
    """Pure: Markdown text -> knowledge dict (see module docstring)."""
    if field_labels is None:
        from recruiter_email.field_registry import FIELDS

        field_labels = {key: spec.label for key, spec in FIELDS.items()}
    blocks = split_blocks(text)
    extracted = extract_facts(blocks, field_labels)
    sha = content_hash(text)
    version = find_version(text)
    return {
        "path": str(source),
        "document": Path(str(source)).name or DOCUMENT_NAME,
        "version": version,
        "sha256": sha,
        "revision": make_revision(version, sha),
        "loaded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "text": text,
        "blocks": blocks,
        **extracted,
    }


def knowledge_from_facts(facts: dict[str, str], source: str = "facts") -> dict[str, Any]:
    """Wrap a plain {KEY: value} dict (legacy loaders, tests) as a knowledge dict."""
    text = "\n".join(f"{k}={v}" for k, v in facts.items())
    return parse_knowledge(text, source)


# ---------------------------------------------------------------------------
# I/O
# ---------------------------------------------------------------------------

def knowledge_path() -> Path:
    raw = os.getenv("CANDIDATE_FACTS_PATH", "").strip()
    path = Path(raw) if raw else DEFAULT_PATH
    return path if path.is_absolute() else PROJECT_ROOT / path


def load_knowledge(path: Path | None = None) -> dict[str, Any]:
    """Read the latest local experience_details.md (fresh on every call)."""
    path = path or knowledge_path()
    if not path.is_file():
        legacy = path.with_suffix(".txt")
        hint = (f" A legacy {legacy.name} exists: run `python -m recruiter_email.knowledge_sync migrate` "
                "to convert it.") if legacy.is_file() else ""
        raise CandidateFactsError(
            f"Candidate knowledge file not found: {path}. Copy config/experience_details.example.md to "
            f"config/experience_details.md and fill it in.{hint}")
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        raise CandidateFactsError(f"Cannot read {path}: {exc}") from exc
    if not text.strip():
        raise CandidateFactsError(f"{path} is empty.")
    return parse_knowledge(text, path)
