"""
app/main.py
===========

Standalone FastAPI service for the JobBot recruiter e-mail auto-reply.
n8n (workflow n8n/04_recruiter_email_auto_reply.json) polls Gmail every 5
minutes and calls this API for every new inbox e-mail.

Start (from the project root):

    python -m app.main                  # uses API_HOST / API_PORT from .env (default 0.0.0.0:8090)
    uvicorn app.main:app --port 8090    # same thing

Swagger: http://localhost:8090/docs

Endpoints
    GET  /health                                 liveness + knowledge file check
    GET  /ai/providers/status                    which AI providers are enabled (never shows keys)
    GET  /ai/runs/{date}                         provider attempt metrics for a day (YYYY-MM-DD)
    POST /ai/recruiter-email-reply               full pipeline (n8n calls this)
    POST /ai/recruiter-email/mark-result         n8n reports SENT / DRAFTED / LABELED / FAILED
    GET  /ai/recruiter-email/knowledge-status    experience_details.md version vs Project copies
    POST /ai/recruiter-email/knowledge-uploaded  record a re-upload to the Claude / ChatGPT Project
"""

from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path
from threading import Lock
from typing import Any, Dict, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]

try:  # .env is optional when the variables are set another way
    from dotenv import load_dotenv

    load_dotenv(PROJECT_ROOT / ".env")
except ImportError:  # pragma: no cover
    pass

from fastapi import APIRouter, Depends, FastAPI, HTTPException  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

from ai.errors import AIConfigurationError, AllProvidersUnavailableError, JobBotAIError  # noqa: E402
from ai.observability import daily_metrics, redact, today_ist  # noqa: E402
from ai.provider_router import get_enabled_providers  # noqa: E402
from ai.settings import PROVIDER_FLAGS, PROVIDER_PRIORITY, load_ai_config  # noqa: E402


def setup_logging() -> None:
    """Console + logs/recruiter_api.log (values are never logged, only field names)."""
    logs = PROJECT_ROOT / "logs"
    logs.mkdir(exist_ok=True)
    root = logging.getLogger()
    if any(isinstance(h, RotatingFileHandler) for h in root.handlers):
        return
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s - %(message)s")
    file_handler = RotatingFileHandler(logs / "recruiter_api.log", maxBytes=5_000_000, backupCount=5,
                                       encoding="utf-8")
    file_handler.setFormatter(fmt)
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(file_handler)
    root.addHandler(console)
    root.setLevel(os.getenv("LOG_LEVEL", "INFO").upper())


router = APIRouter(prefix="/ai", tags=["recruiter-email"])

# Serializes the whole classifier+draft generation request, not just JSON ledger writes.
# Single uvicorn worker is required (as in scripts/start_api.bat); n8n production
# concurrency=1 additionally avoids two separate workflow executions overlapping.
_RECRUITER_PROCESSING_LOCK = Lock()


def ai_http_error(exc: JobBotAIError) -> HTTPException:
    """Controlled HTTP error for n8n: 503 = all providers down, 4xx/5xx = defect."""
    body = exc.to_dict()
    if isinstance(exc, AllProvidersUnavailableError):
        body["status"] = "ALL_PROVIDERS_UNAVAILABLE"
        body["manual_intervention_required"] = True
    if isinstance(exc, AIConfigurationError):
        body["status"] = "NO_AI_PROVIDER_ENABLED"
        body["manual_intervention_required"] = True
    body["message"] = redact(body.get("message"), limit=1000)
    return HTTPException(status_code=exc.http_status, detail=body)


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

@router.get("/providers/status")
def providers_status() -> Dict[str, Any]:
    config = load_ai_config()
    return {
        "priority_order": list(PROVIDER_PRIORITY),
        "flags": {PROVIDER_FLAGS[name]: config["flags"][name] for name in PROVIDER_PRIORITY},
        "enabled_providers": get_enabled_providers(config),
        "anthropic_api_key_present": bool(os.getenv("ANTHROPIC_API_KEY")),
        "openai_api_key_present": bool(os.getenv("OPENAI_API_KEY")),
        "claude_project_configured": bool(os.getenv("CLAUDE_PROJECT_URL", "").strip()),
        "chatgpt_project_configured": bool(os.getenv("CHATGPT_PROJECT_URL", "").strip()),
        "auto_send_recruiter_reply": os.getenv("AUTO_SEND_RECRUITER_REPLY", "false").lower() == "true",
    }


@router.get("/runs/{date_folder}")
def runs_for_day(date_folder: str) -> Dict[str, Any]:
    return {"date": date_folder, "metrics": daily_metrics(date_folder)}


# ---------------------------------------------------------------------------
# Recruiter e-mail
# ---------------------------------------------------------------------------

class RecruiterEmailRequest(BaseModel):
    message_id: str = Field(..., min_length=1, max_length=512)
    thread_id: str = Field(default="", max_length=512)
    from_: str = Field(..., alias="from", min_length=3, max_length=512)
    to: str = Field(default="", max_length=2000)
    subject: str = Field(default="", max_length=2000)
    body: str = Field(default="", max_length=200_000)
    received_at: Optional[str] = None
    headers: Dict[str, Any] = Field(default_factory=dict)

    model_config = {"populate_by_name": True}


class MarkResultRequest(BaseModel):
    message_id: str = Field(..., min_length=1, max_length=512)
    outcome: str = Field(..., pattern="^(SENT|DRAFTED|LABELED|FAILED)$")
    reply_ref: Optional[str] = Field(default=None, max_length=255)


class KnowledgeUploadedRequest(BaseModel):
    provider: str = Field(..., pattern="^(claude|chatgpt|both|claude_web|chatgpt_web)$")


def recruiter_dependencies() -> Dict[str, Any]:
    """FastAPI dependency: keyword arguments for process_recruiter_email (tests override it)."""
    from recruiter_email.ledger import get_ledger_engine

    return {"engine": get_ledger_engine()}


@router.post("/recruiter-email-reply")
def recruiter_email_reply(request: RecruiterEmailRequest,
                          deps: Dict[str, Any] = Depends(recruiter_dependencies)) -> Dict[str, Any]:
    from recruiter_email.service import IncomingEmail, process_recruiter_email

    if not request.subject.strip() and not request.body.strip():
        raise HTTPException(status_code=400, detail="subject and body are both empty")
    email = IncomingEmail(
        message_id=request.message_id.strip(),
        thread_id=(request.thread_id or request.message_id).strip(),
        sender=request.from_,
        subject=request.subject,
        body=request.body,
        to=request.to,
        received_at=request.received_at,
        headers=request.headers,
    )
    try:
        with _RECRUITER_PROCESSING_LOCK:
            return process_recruiter_email(email, **deps)
    except JobBotAIError as exc:
        raise ai_http_error(exc)
    except Exception as exc:
        from recruiter_email.ledger import LedgerError
        if isinstance(exc, LedgerError):
            raise HTTPException(status_code=503, detail='JSON ledger unavailable: no reply sent') from exc
        raise


@router.post("/recruiter-email/mark-result")
def recruiter_email_mark_result(request: MarkResultRequest,
                                deps: Dict[str, Any] = Depends(recruiter_dependencies)) -> Dict[str, Any]:
    from recruiter_email.ledger import mark_reply

    updated = mark_reply(deps["engine"], request.message_id, request.outcome, request.reply_ref)
    if not updated:
        raise HTTPException(status_code=404, detail="message_id not found in JSON ledger")
    return {"status": "ok", "message_id": request.message_id, "outcome": request.outcome, "date": today_ist()}


@router.get("/recruiter-email/ledger-summary")
def recruiter_ledger_summary() -> Dict[str, Any]:
    """Safe counts only: no sender hashes, raw email text, or facts."""
    from recruiter_email.ledger import LedgerError, get_ledger_engine, ledger_summary
    try:
        return ledger_summary(get_ledger_engine())
    except LedgerError as exc:
        raise HTTPException(status_code=503, detail='JSON ledger unavailable') from exc


@router.get("/recruiter-email/knowledge-status")
def recruiter_knowledge_status() -> Dict[str, Any]:
    """Local experience_details.md version/hash and whether each Project copy is known to be current."""
    from recruiter_email.knowledge_sync import knowledge_status

    return knowledge_status()


@router.post("/recruiter-email/knowledge-uploaded")
def recruiter_knowledge_uploaded(request: KnowledgeUploadedRequest) -> Dict[str, Any]:
    """Call after you re-uploaded the CURRENT experience_details.md to the Claude and/or ChatGPT Project."""
    from recruiter_email.knowledge_base import CandidateFactsError, load_knowledge
    from recruiter_email.knowledge_sync import knowledge_status, mark_uploaded

    try:
        kb = load_knowledge()
    except CandidateFactsError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    mark_uploaded(request.provider, kb)
    return knowledge_status(kb)


def create_app() -> FastAPI:
    application = FastAPI(
        title="JobBot Recruiter E-mail Auto-Reply API",
        version="3.0.0",
        description="Classifies recruiter e-mails, answers from config/experience_details.md and returns a "
                    "Gmail-ready reply for n8n. Draft-first by default.",
    )
    application.include_router(router)

    @application.get("/health", tags=["status"])
    def health() -> Dict[str, Any]:
        from recruiter_email.knowledge_sync import knowledge_status

        local = knowledge_status()["local"]
        return {"status": "ok" if local.get("available") else "degraded", "service": "jobbot-recruiter-email",
                "knowledge_file_available": bool(local.get("available")),
                "knowledge_revision": local.get("revision"), "ledger_type": "json", "date": today_ist()}

    return application


app = create_app()


def main() -> None:
    import uvicorn

    setup_logging()
    uvicorn.run(app, host=os.getenv("API_HOST", "0.0.0.0"), port=int(os.getenv("API_PORT", "8090")),
                log_level=os.getenv("LOG_LEVEL", "info").lower())


if __name__ == "__main__":
    main()
