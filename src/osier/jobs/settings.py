"""Per-tenant job settings, independent of the queue backend."""

from __future__ import annotations

from typing import Any, Literal

from psycopg import sql
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field
from pydantic import ValidationError as PydanticValidationError

from ..errors import ApprovalError, RecordValidationError


class JobSettings(BaseModel):
    """A tenant's job settings. The bounds are platform limits that protect shared
    workers; plans may raise them later."""

    model_config = ConfigDict(extra="forbid")

    concurrency: int = Field(default=2, ge=1, le=10, description="Jobs run at the same time.")
    max_attempts: int = Field(default=3, ge=1, le=5, description="Tries per job.")
    retry_wait_seconds: int = Field(default=10, ge=1, le=3600, description="Wait before retry.")
    retry_backoff: Literal["fixed", "linear"] = Field(
        default="linear", description="fixed: same wait each time; linear: wait x attempt."
    )
    paused: bool = Field(default=False, description="Queue jobs but do not run them.")
    keep_finished_days: int = Field(
        default=14, ge=1, le=90, description="Days finished jobs are kept."
    )


def describe_settings() -> dict[str, Any]:
    """Each setting with its default, limits, and meaning."""
    keep = ("default", "minimum", "maximum", "enum", "description")
    props = JobSettings.model_json_schema()["properties"]
    return {name: {k: v for k, v in p.items() if k in keep} for name, p in props.items()}


class JobSettingsService:
    def __init__(self, session: Any) -> None:
        self._session = session
        self._conn = session.conn
        self._table = sql.Identifier(session.tenant.schema, "meta_job_settings")

    def get(self) -> JobSettings:
        row = self._conn.execute(
            sql.SQL("SELECT settings FROM {} WHERE id = 1").format(self._table)
        ).fetchone()
        return JobSettings.model_validate(row[0] if row else {})

    def preview(self, changes: dict[str, Any]) -> dict[str, Any]:
        self._session.authz.require("jobs.manage", "changing background job settings")
        current = self.get()
        try:
            new = JobSettings.model_validate({**current.model_dump(), **changes})
        except PydanticValidationError as exc:
            raise RecordValidationError(
                "Invalid job settings.",
                code="JOB_SETTINGS_INVALID",
                details={
                    "errors": [
                        {"field": ".".join(map(str, e["loc"])), "message": e["msg"]}
                        for e in exc.errors(include_url=False)
                    ]
                },
                expected=describe_settings(),
            ) from None
        diff = {
            k: {"from": getattr(current, k), "to": v}
            for k, v in new.model_dump().items()
            if getattr(current, k) != v
        }
        return {"settings": new.model_dump(), "changes": diff}

    def update(self, changes: dict[str, Any]) -> dict[str, Any]:
        preview = self.preview(changes)
        if not self._session.principal.is_human:
            raise ApprovalError(
                "Changing job settings must be confirmed by a person.",
                code="APPROVAL_REQUIRES_HUMAN",
            )
        self._conn.execute(
            sql.SQL(
                "INSERT INTO {} (id, settings, updated_by) VALUES (1, %s, %s) "
                "ON CONFLICT (id) DO UPDATE SET settings = %s, updated_by = %s, "
                "updated_at = now()"
            ).format(self._table),
            (
                Jsonb(preview["settings"]),
                self._session.principal.user_id,
                Jsonb(preview["settings"]),
                self._session.principal.user_id,
            ),
        )
        return preview
