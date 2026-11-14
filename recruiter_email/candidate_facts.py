"""
recruiter_email/candidate_facts.py
==================================

Small, stable API over the candidate knowledge base.

The AUTHORITATIVE source is now config/experience_details.md (natural English
Markdown, see recruiter_email/knowledge_base.py). This module keeps the old
function names so existing callers and tests keep working:

    load_facts()            -> {KEY: value} from the latest experience_details.md
    has_fact / get_fact     -> read one value (empty / TBD / N/A count as missing)
    parse_facts(text, src)  -> .md (or any non-.txt) source: lenient Markdown parse
                               .txt source: the legacy strict KEY=VALUE parser

The file is read fresh on every request so edits apply immediately, and its
values are never logged.
"""

from __future__ import annotations

import re
from pathlib import Path

from recruiter_email.knowledge_base import (
    DEFAULT_PATH,
    PROJECT_ROOT,
    CandidateFactsError,
    is_missing,
    knowledge_path,
    load_knowledge,
    parse_knowledge,
)

__all__ = ["CandidateFactsError", "DEFAULT_PATH", "PROJECT_ROOT", "facts_path", "get_fact", "has_fact",
           "load_facts", "parse_facts", "parse_legacy_txt"]

_KEY = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")

# Facts are a plain dict {KEY: raw value}; use has_fact()/get_fact() to read them.
CandidateFacts = dict


def has_fact(facts: dict[str, str], key: str) -> bool:
    return not is_missing((facts or {}).get(key, ""))


def get_fact(facts: dict[str, str], key: str) -> str | None:
    return facts[key].strip() if has_fact(facts, key) else None


def facts_path() -> Path:
    return knowledge_path()


def parse_legacy_txt(text: str, source: Path) -> dict[str, str]:
    """The original strict KEY=VALUE parser (config/experience_details.txt)."""
    values: dict[str, str] = {}
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise CandidateFactsError(f"{source.name}:{number}: expected KEY=VALUE")
        key, value = line.split("=", 1)
        key = key.strip()
        if not _KEY.match(key):
            raise CandidateFactsError(f"{source.name}:{number}: invalid key {key!r} (use UPPER_SNAKE_CASE)")
        if key in values:
            raise CandidateFactsError(f"{source.name}:{number}: duplicate key {key}")
        values[key] = value.strip()
    return values


def parse_facts(text: str, source: Path) -> dict[str, str]:
    """Text -> {KEY: value}. Markdown is parsed leniently; a .txt source keeps the legacy strict rules."""
    if Path(source).suffix.lower() == ".txt":
        return parse_legacy_txt(text, Path(source))
    return parse_knowledge(text, source)["facts"]


def load_facts(path: Path | None = None) -> dict[str, str]:
    return load_knowledge(path)["facts"]
