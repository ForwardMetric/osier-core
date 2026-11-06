"""Structured errors.

Every error the engine raises carries a stable ``code`` and enough detail for an
AI agent to either correct its request or stop. ``retryable`` means "the same
request may succeed if retried later"; it is *not* an invitation to retry an
invalid request unchanged.
"""

from __future__ import annotations

from typing import Any


class OsierError(Exception):
    """Base class for all engine errors."""

    default_code = "OSIER_ERROR"
    default_retryable = False

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        field: str | None = None,
        expected: Any = None,
        retryable: bool | None = None,
        hint: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code or self.default_code
        self.field = field
        self.expected = expected
        self.retryable = self.default_retryable if retryable is None else retryable
        self.hint = hint
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "field": self.field,
            "expected": self.expected,
            "retryable": self.retryable,
            "hint": self.hint,
        }
        if self.details:
            data["details"] = self.details
        return data

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.code!r}, {self.message!r})"


class InvalidNameError(OsierError):
    default_code = "INVALID_NAME"


class SchemaChangeError(OsierError):
    """A proposed schema change is invalid or cannot be applied."""

    default_code = "SCHEMA_CHANGE_INVALID"


class NotFoundError(OsierError):
    default_code = "NOT_FOUND"


class RecordValidationError(OsierError):
    """One or more field values are invalid. ``details['errors']`` lists each problem."""

    default_code = "VALIDATION_FAILED"


class ConflictError(OsierError):
    """The record changed since it was read (optimistic concurrency)."""

    default_code = "CONCURRENT_UPDATE"
    default_retryable = True


class IntegrityError(OsierError):
    """A uniqueness, relationship, or dependency rule would be violated."""

    default_code = "INTEGRITY_VIOLATION"


class ApprovalError(OsierError):
    """A change was applied without a valid approval."""

    default_code = "APPROVAL_INVALID"
