"""
recruiter_email/knowledge_sync.py
=================================

Keeps track of WHICH copy of experience_details.md was uploaded to the
Claude Project and the ChatGPT Project, so the pipeline can detect a known
version mismatch (local file edited, Project copy not replaced yet).

State file (default config/knowledge_sync_state.json, override with
RECRUITER_KNOWLEDGE_SYNC_PATH):

    {"claude_web":  {"sha256": "...", "version": "2026-11-09.1", "uploaded_at": "..."},
     "chatgpt_web": {...}}

Command line (run from the project root):

    python -m recruiter_email.knowledge_sync status
    python -m recruiter_email.knowledge_sync mark-uploaded claude     # after re-uploading to Claude
    python -m recruiter_email.knowledge_sync mark-uploaded chatgpt    # after re-uploading to ChatGPT
    python -m recruiter_email.knowledge_sync mark-uploaded both
    python -m recruiter_email.knowledge_sync migrate                  # experience_details.txt -> .md

The same status/mark actions exist in Swagger:
    GET  /ai/recruiter-email/knowledge-status
    POST /ai/recruiter-email/knowledge-uploaded   {"provider": "claude_web" | "chatgpt_web" | "both"}

Every recruiter reply logs the knowledge revision it used to
data/ai_runs/<date>/recruiter_knowledge_usage.jsonl (no values).
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from recruiter_email.knowledge_base import (
    PROJECT_ROOT,
    CandidateFactsError,
    knowledge_path,
    load_knowledge,
    parse_knowledge,
)

WEB_PROVIDERS = ("claude_web", "chatgpt_web")
IN_SYNC = "in_sync"
OUTDATED = "outdated"
UNKNOWN = "unknown"
NOT_APPLICABLE = "not_applicable"   # API providers read the local file directly

_ALIASES = {"claude": ("claude_web",), "claude_web": ("claude_web",), "chatgpt": ("chatgpt_web",),
            "chatgpt_web": ("chatgpt_web",), "openai": ("chatgpt_web",), "both": WEB_PROVIDERS,
            "all": WEB_PROVIDERS}


def sync_state_path() -> Path:
    raw = os.getenv("RECRUITER_KNOWLEDGE_SYNC_PATH", "").strip()
    path = Path(raw) if raw else PROJECT_ROOT / "config" / "knowledge_sync_state.json"
    return path if path.is_absolute() else PROJECT_ROOT / path


def read_sync_state(path: Path | None = None) -> dict[str, Any]:
    path = path or sync_state_path()
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except (OSError, json.JSONDecodeError):
        return {}


def providers_for(name: str) -> tuple[str, ...]:
    try:
        return _ALIASES[name.strip().lower()]
    except KeyError:
        raise ValueError(f"unknown provider {name!r}: use claude, chatgpt or both") from None


def mark_uploaded(provider: str, kb: dict[str, Any], path: Path | None = None) -> dict[str, Any]:
    """Record that the CURRENT local file was uploaded to the given Project(s)."""
    path = path or sync_state_path()
    state = read_sync_state(path)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for name in providers_for(provider):
        state[name] = {"sha256": kb["sha256"], "version": kb.get("version"), "revision": kb["revision"],
                       "uploaded_at": now}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    return state


def project_status(provider: str, kb: dict[str, Any], state: dict[str, Any] | None = None) -> str:
    """Pure (given state): in_sync | outdated | unknown | not_applicable."""
    if provider not in WEB_PROVIDERS:
        return NOT_APPLICABLE
    record = (state if state is not None else read_sync_state()).get(provider)
    if not record or not record.get("sha256"):
        return UNKNOWN
    return IN_SYNC if record["sha256"] == kb["sha256"] else OUTDATED


def version_seen_mismatch(kb: dict[str, Any], seen: str | None) -> bool:
    """The AI reported a Project document version that differs from the local one."""
    if not seen or not kb.get("version"):
        return False
    return seen.strip().lower() not in {kb["version"].strip().lower(), kb["revision"].lower()}


def knowledge_status(kb: dict[str, Any] | None = None, state: dict[str, Any] | None = None) -> dict[str, Any]:
    """What /ai/recruiter-email/knowledge-status returns (no values, only metadata)."""
    try:
        kb = kb or load_knowledge()
    except CandidateFactsError as exc:
        return {"local": {"available": False, "error": str(exc), "path": str(knowledge_path())},
                "projects": {}, "action_required": ["Create config/experience_details.md"]}
    state = read_sync_state() if state is None else state
    projects = {name: {"status": project_status(name, kb, state), **(state.get(name) or {})}
                for name in WEB_PROVIDERS}
    actions = [f"Re-upload {kb['document']} to the {'Claude' if name == 'claude_web' else 'ChatGPT'} Project, "
               f"then run: python -m recruiter_email.knowledge_sync mark-uploaded "
               f"{'claude' if name == 'claude_web' else 'chatgpt'}"
               for name, info in projects.items() if info["status"] != IN_SYNC]
    return {
        "local": {"available": True, "path": kb["path"], "document": kb["document"], "version": kb.get("version"),
                  "sha256": kb["sha256"], "revision": kb["revision"], "fact_keys": sorted(kb["facts"]),
                  "conflicting_keys": sorted(kb.get("conflicts", {})), "empty_keys": kb.get("empty_keys", [])},
        "projects": projects,
        "action_required": actions,
    }


def log_usage(message_id: str, kb: dict[str, Any] | None, *, provider: str | None, status: str,
              project_state: str | None = None, version_seen: str | None = None) -> dict[str, Any]:
    """One JSON line per recruiter reply: which knowledge revision was used (never values)."""
    from ai.observability import ai_runs_root, logger, redact, today_ist

    record = {
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "message_id": redact(message_id, 80),
        "document": (kb or {}).get("document"),
        "revision": (kb or {}).get("revision"),
        "version": (kb or {}).get("version"),
        "sha256": (kb or {}).get("sha256"),
        "provider": provider,
        "project_knowledge": project_state,
        "project_version_seen": version_seen,
        "status": status,
    }
    logger.info("recruiter_email knowledge revision=%s provider=%s project=%s status=%s",
                record["revision"], provider, project_state, status)
    try:
        directory = ai_runs_root() / today_ist()
        directory.mkdir(parents=True, exist_ok=True)
        with (directory / "recruiter_knowledge_usage.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")
    except OSError as exc:  # logging must never break the reply path
        logger.warning("knowledge usage log not written: %s", exc)
    return record


# ---------------------------------------------------------------------------
# Migration: legacy experience_details.txt -> experience_details.md
# ---------------------------------------------------------------------------

def migrate_txt_to_md(text: str, version: str) -> str:
    """Pure: legacy KEY=VALUE text -> Markdown knowledge document (signature lines are dropped)."""
    prose: list[str] = []
    kv: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue  # comments and the old hand-written signature (now config/recruiter_email_template.json)
        key, value = (part.strip() for part in line.split("=", 1))
        if key in {"PROFILE_SUMMARY", "SUMMARY", "ABOUT_ME"} and value:
            prose.append(value if value.endswith(".") else value + ".")
        else:
            kv.append(f"{key}={value}")
    parts = ["# Professional Profile", "", f"Document version: {version}", ""]
    parts += [p + "\n" for p in prose]
    if kv:
        parts += ["## Structured facts", "", "Optional KEY=VALUE lines (migrated from experience_details.txt).", ""]
        parts += kv
    return "\n".join(parts).rstrip() + "\n"


def migrate(txt_path: Path, md_path: Path, version: str | None = None) -> Path:
    if md_path.exists():
        raise FileExistsError(f"{md_path} already exists; edit it directly.")
    version = version or datetime.now(timezone.utc).strftime("%Y-%m-%d") + ".1"
    md_path.write_text(migrate_txt_to_md(txt_path.read_text(encoding="utf-8-sig"), version), encoding="utf-8")
    parse_knowledge(md_path.read_text(encoding="utf-8"), md_path)  # sanity check
    return md_path


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    command = args[0] if args else "status"
    if command == "status":
        print(json.dumps(knowledge_status(), indent=2))
        return 0
    if command == "mark-uploaded":
        if len(args) < 2:
            print("usage: python -m recruiter_email.knowledge_sync mark-uploaded claude|chatgpt|both")
            return 2
        kb = load_knowledge()
        mark_uploaded(args[1], kb)
        print(f"Recorded {kb['revision']} as uploaded to: {', '.join(providers_for(args[1]))}")
        return 0
    if command == "migrate":
        md = knowledge_path()
        txt = Path(args[1]) if len(args) > 1 else md.with_suffix(".txt")
        print(f"Wrote {migrate(txt, md)}")
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
