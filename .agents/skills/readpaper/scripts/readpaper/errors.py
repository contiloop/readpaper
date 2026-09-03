"""Structured fail-closed errors."""

from __future__ import annotations

from enum import StrEnum
from typing import Any


class ErrorCode(StrEnum):
    INVALID_ARGUMENT = "INVALID_ARGUMENT"
    UNSUPPORTED_SOURCE = "UNSUPPORTED_SOURCE"
    ACCESS_DENIED = "ACCESS_DENIED"
    FETCH_FAILED = "FETCH_FAILED"
    TIMEOUT = "TIMEOUT"
    CANCELLED = "CANCELLED"
    CORRUPT_ARTIFACT = "CORRUPT_ARTIFACT"
    OCR_REQUIRED = "OCR_REQUIRED"
    UNSUPPORTED_ARTIFACT = "UNSUPPORTED_ARTIFACT"
    OUTPUT_BUDGET_EXCEEDED = "OUTPUT_BUDGET_EXCEEDED"
    OUTPUT_TRUNCATED = "OUTPUT_TRUNCATED"
    NOT_FOUND = "NOT_FOUND"
    ID_MISMATCH = "ID_MISMATCH"
    ACTIVE_RUN_CONFLICT = "ACTIVE_RUN_CONFLICT"
    ANSWER_NOT_STARTED = "ANSWER_NOT_STARTED"
    ANSWER_PENDING = "ANSWER_PENDING"
    ANSWER_INTERRUPTED = "ANSWER_INTERRUPTED"
    OBSERVER_UNAVAILABLE = "OBSERVER_UNAVAILABLE"
    COVERAGE_INCOMPLETE = "COVERAGE_INCOMPLETE"
    AUDIT_INCOMPLETE = "AUDIT_INCOMPLETE"
    STATE_CONFLICT = "STATE_CONFLICT"
    UNSUPPORTED_MODEL_CONFIG = "UNSUPPORTED_MODEL_CONFIG"
    DELETE_CONFIRMATION_REQUIRED = "DELETE_CONFIRMATION_REQUIRED"
    DELETE_SCOPE_CHANGED = "DELETE_SCOPE_CHANGED"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class ReadPaperError(RuntimeError):
    def __init__(self, code: ErrorCode, message: str, *, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code.value, "message": str(self), "details": self.details}
