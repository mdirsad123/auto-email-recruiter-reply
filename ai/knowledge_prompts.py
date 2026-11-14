"""
ai/knowledge_prompts.py
=======================

Project-knowledge helpers for the two WEB providers (pure functions).

When a request carries ``AIRequest.knowledge`` (recruiter e-mail operations):

* Claude Web opens the configured Claude Project (CLAUDE_PROJECT_URL) and
  ChatGPT Web opens the configured ChatGPT Project (CHATGPT_PROJECT_URL), so
  the new chat is created INSIDE that Project and can read the uploaded
  ``experience_details.md``.
* The page is verified to be that Project before anything is sent.
* The request file and the typed message reference the Project knowledge file
  explicitly; the request file also carries the local, non-confidential
  excerpts as the reliability fallback.

If no Project URL is configured the request still works from the local
excerpts (unless RECRUITER_REQUIRE_PROJECT_KNOWLEDGE=true, then the provider
reports itself unavailable and the router falls back to the next one).
"""

from __future__ import annotations

import os
import re
from typing import Any

PROVIDER_NAMES = {"claude_web": "Claude", "chatgpt_web": "ChatGPT"}

_PROJECT_ID = (
    re.compile(r"/g/(g-p-[0-9a-f]{8,})", re.IGNORECASE),              # ChatGPT: /g/g-p-<hex>-slug/project
    re.compile(r"/project/([A-Za-z0-9_\-]{6,})", re.IGNORECASE),      # Claude: /project/<uuid> or /code/project/<id>
)
_TRUE = {"1", "true", "yes", "on"}


def project_id_from_url(url: str) -> str | None:
    for pattern in _PROJECT_ID:
        match = pattern.search(url or "")
        if match:
            return match.group(1)
    return None


def project_config(provider: str, settings: dict[str, Any], env: dict[str, str] | None = None) -> dict[str, Any]:
    """Which Project a web provider must use for knowledge-backed requests."""
    source = os.environ if env is None else env
    url = (source.get(settings.get("project_url_env", ""), "") or "").strip()
    name = (source.get(settings.get("project_name_env", ""), "") or "").strip()
    require = (source.get("RECRUITER_REQUIRE_PROJECT_KNOWLEDGE", "") or "").strip().lower() in _TRUE
    return {
        "provider": provider,
        "url": url,
        "id": project_id_from_url(url),
        "name": name,
        "configured": bool(url),
        "required": require,
        "verify_file_visible": bool(settings.get("verify_knowledge_file_visible", False)),
    }


def verify_project_page(current_url: str, page_text: str, project: dict[str, Any], document: str) -> str | None:
    """None when the browser is inside the configured Project, else the reason it is not."""
    if not project.get("configured"):
        return None
    project_id = project.get("id")
    if project_id and project_id.lower() not in (current_url or "").lower():
        return f"browser is not inside the configured Project (expected id {project_id})"
    text = (page_text or "").lower()
    if project.get("name") and project["name"].lower() not in text:
        return f"Project name '{project['name']}' not visible on the page"
    if project.get("verify_file_visible") and document.lower() not in text:
        return f"knowledge file {document} not visible in the Project"
    return None


def knowledge_instruction(provider: str, knowledge: dict[str, Any], project: dict[str, Any] | None) -> str:
    """The explicit Project-knowledge instruction (spec wording)."""
    document = knowledge.get("document") or "experience_details.md"
    name = PROVIDER_NAMES.get(provider, provider)
    if not (project and project.get("configured")):
        return (f"No {name} Project is configured for this chat. Use ONLY the LOCAL KNOWLEDGE EXCERPTS of "
                f"`{document}` inside the attached request file as the candidate's professional knowledge source. "
                "Set knowledge_version_seen to null.")
    if provider == "chatgpt_web":
        head = (f"Use the latest `{document}` uploaded to this ChatGPT Project as the professional knowledge "
                f"source. Answer from {document} information. If a requested experience value is missing or "
                "ambiguous, report it as missing.")
    else:
        head = (f"Refer to the latest `{document}` uploaded to this Claude Project. Treat it as the candidate's "
                "professional knowledge base. Extract only information explicitly supported by that document.")
    return (head + " The attached request file also contains LOCAL KNOWLEDGE EXCERPTS from the newest local copy "
            f"(revision {knowledge.get('revision') or 'unknown'}); if the Project copy disagrees with them, the "
            "local excerpts are newer: follow them and report the Project version you saw in "
            "knowledge_version_seen.")


def request_preamble(provider: str, knowledge: dict[str, Any], project: dict[str, Any] | None) -> str:
    """Section placed at the top of the web request file."""
    if not knowledge:
        return ""
    purpose = knowledge.get("purpose", "")
    lines = ["## PROJECT KNOWLEDGE", knowledge_instruction(provider, knowledge, project), ""]
    if purpose == "reply":
        lines.append("For this reply-wording task do NOT copy facts from the knowledge file: software inserts "
                     "every verified fact. Use the knowledge only to understand the context.")
    else:
        lines.append(f"Local document version: {knowledge.get('version') or 'not set'}. "
                     "Report the version line of the PROJECT copy (e.g. 'Document version: ...') in "
                     "knowledge_version_seen, or null when you cannot open the Project file.")
    return "\n".join(lines)


def chat_message(provider: str, knowledge: dict[str, Any], request_file_name: str,
                 project: dict[str, Any] | None) -> str:
    """The short message typed into the chat composer."""
    base = (f"Read the attached file {request_file_name} completely and follow it exactly. "
            "Reply with ONE JSON object inside a single ```json code block and nothing else.")
    if not knowledge:
        return base
    return knowledge_instruction(provider, knowledge, project) + " " + base
