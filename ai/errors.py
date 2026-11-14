"""
ai/errors.py
============

Shared error hierarchy for the JobBot AI provider layer.

The single most important rule of the router lives in these classes:

* ``ProviderUnavailableError`` (and subclasses) mean "this provider cannot
  serve the request right now". The router may retry the same provider and
  then move to the NEXT provider.

* Every other ``JobBotAIError`` subclass (input, request, output-validation,
  application errors) means "the request or our own code is wrong". The router
  NEVER falls back on these; it stops, logs and re-raises so that a real
  defect is not hidden behind a browser fallback.
"""

from __future__ import annotations

from typing import Any


class FailureType:
    """String constants used in logs, manifests and HTTP error bodies."""

    # Availability problems -> fallback allowed
    RATE_LIMIT = "RATE_LIMIT"
    QUOTA_EXHAUSTED = "QUOTA_EXHAUSTED"
    SERVER_ERROR = "SERVER_ERROR"
    OVERLOADED = "OVERLOADED"
    TIMEOUT = "TIMEOUT"
    NETWORK = "NETWORK"
    PROVIDER_OUTAGE = "PROVIDER_OUTAGE"
    NOT_CONFIGURED = "NOT_CONFIGURED"
    AUTH = "AUTH"
    BROWSER_UNAVAILABLE = "BROWSER_UNAVAILABLE"
    BROWSER_SESSION_EXPIRED = "BROWSER_SESSION_EXPIRED"
    BROWSER_UI_TIMEOUT = "BROWSER_UI_TIMEOUT"
    WEB_USAGE_LIMIT = "WEB_USAGE_LIMIT"
    PROJECT_KNOWLEDGE_UNAVAILABLE = "PROJECT_KNOWLEDGE_UNAVAILABLE"  # wrong / inaccessible Claude or ChatGPT Project

    # Defects -> never fall back
    INVALID_INPUT = "INVALID_INPUT"
    INVALID_REQUEST = "INVALID_REQUEST"
    OUTPUT_VALIDATION = "OUTPUT_VALIDATION"
    OUTPUT_TRUNCATED = "OUTPUT_TRUNCATED"
    MODEL_REFUSAL = "MODEL_REFUSAL"
    APPLICATION_ERROR = "APPLICATION_ERROR"
    ALL_PROVIDERS_UNAVAILABLE = "ALL_PROVIDERS_UNAVAILABLE"
    NO_PROVIDER_ENABLED = "NO_PROVIDER_ENABLED"


class JobBotAIError(Exception):
    """Base class. ``failure_type`` is always one of ``FailureType``."""

    failure_type: str = FailureType.APPLICATION_ERROR
    fallback_allowed: bool = False
    http_status: int = 500

    def __init__(
        self,
        message: str,
        *,
        failure_type: str | None = None,
        provider: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        if failure_type:
            self.failure_type = failure_type
        self.provider = provider
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "error_type": type(self).__name__,
            "failure_type": self.failure_type,
            "provider": self.provider,
            "message": str(self),
            "fallback_allowed": self.fallback_allowed,
            "details": self.details,
        }


# ---------------------------------------------------------------------------
# Availability errors (fallback allowed)
# ---------------------------------------------------------------------------

class ProviderUnavailableError(JobBotAIError):
    """Provider cannot serve the request right now -> try the next provider."""

    failure_type = FailureType.PROVIDER_OUTAGE
    fallback_allowed = True
    http_status = 503

    def __init__(
        self,
        message: str,
        *,
        failure_type: str | None = None,
        provider: str | None = None,
        retryable: bool = True,
        retry_after_seconds: float | None = None,
        status_code: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            message,
            failure_type=failure_type,
            provider=provider,
            details=details,
        )
        # retryable=True  -> the router may retry the SAME provider first.
        # retryable=False -> skip straight to the next provider
        #                    (e.g. key missing, quota exhausted, browser down).
        self.retryable = retryable
        self.retry_after_seconds = retry_after_seconds
        self.status_code = status_code


def provider_not_configured(message: str, provider: str | None = None) -> ProviderUnavailableError:
    """Enabled by its USE_* flag but unusable (API key or package missing): skip to the next provider."""
    return ProviderUnavailableError(message, failure_type=FailureType.NOT_CONFIGURED, provider=provider,
                                    retryable=False)


class AllProvidersUnavailableError(JobBotAIError):
    """Every configured provider was unavailable. Controlled failure."""

    failure_type = FailureType.ALL_PROVIDERS_UNAVAILABLE
    fallback_allowed = False
    http_status = 503

    def __init__(
        self,
        operation: str,
        attempts: list[dict[str, Any]],
    ) -> None:
        summary = ", ".join(
            f"{a.get('provider')}:{a.get('failure_type') or a.get('result')}"
            for a in attempts
        ) or "no providers configured"
        super().__init__(
            f"All enabled AI providers are unavailable for operation '{operation}' "
            f"({summary}). Manual intervention required; no output was written.",
            details={"operation": operation, "attempts": attempts},
        )
        self.operation = operation
        self.attempts = attempts


# ---------------------------------------------------------------------------
# Defect errors (NEVER fall back)
# ---------------------------------------------------------------------------

class InputValidationError(JobBotAIError):
    """Missing/invalid input file, path, JD, template, date, etc."""

    failure_type = FailureType.INVALID_INPUT
    http_status = 400


class InvalidRequestError(JobBotAIError):
    """The provider rejected the request structure (HTTP 400/404/413/422)."""

    failure_type = FailureType.INVALID_REQUEST
    http_status = 502


class OutputValidationError(JobBotAIError):
    """Model output failed JSON parsing, schema or business validation."""

    failure_type = FailureType.OUTPUT_VALIDATION
    http_status = 502


class ApplicationError(JobBotAIError):
    """Unexpected programming error inside JobBot code."""

    failure_type = FailureType.APPLICATION_ERROR
    http_status = 500


class AIConfigurationError(JobBotAIError):
    """Every USE_* provider flag is false: nothing may be called."""

    failure_type = FailureType.NO_PROVIDER_ENABLED
    http_status = 500


def is_fallback_error(exc: BaseException) -> bool:
    """True only for provider-availability problems (the router may try the next provider)."""
    return isinstance(exc, ProviderUnavailableError)
