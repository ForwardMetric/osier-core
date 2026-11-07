"""The tenant's own Claude API access: key, model, limits, and usage.

- **Bring your own key.** The key is entered by a person through the CLI, stored
  encrypted with ``CredentialBroker`` in ``meta_ai_settings``, and never returned by
  any tool, record, or log (rule 12).
- **The tenant chooses the model.** osier ships no default model; ``osier ai setup``
  asks for one.
- **Limits.** A monthly token cap per tenant (checked before every call) and a
  maximum number of turns per AI step. Every call's usage is recorded in
  ``ai_usage`` with its run and step.
- **Client injection.** Workers call ``configure_runtime`` once with the credential
  broker (and, in tests, a fake client factory).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from typing import Any, Literal

from psycopg import sql
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field
from pydantic import ValidationError as PydanticValidationError

from .errors import ApprovalError, OsierError, RecordValidationError


class AIError(OsierError):
    default_code = "AI_ERROR"


class AISettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str | None = Field(
        default=None, min_length=1, max_length=100, description="Claude model id to use."
    )
    default_effort: Literal["low", "medium", "high"] = Field(
        default="medium", description="Effort for AI steps that do not set one."
    )
    monthly_token_limit: int = Field(
        default=2_000_000,
        ge=10_000,
        le=500_000_000,
        description="Input plus output tokens per calendar month (UTC).",
    )
    refusal_fallback: bool = Field(
        default=True,
        description="Let the API retry a declined request on a fallback model "
        "(server-side fallbacks; only for models that support them).",
    )


def _default_client_factory(api_key: str) -> Any:
    import anthropic

    return anthropic.Anthropic(api_key=api_key, max_retries=2)


_runtime: dict[str, Any] = {"broker": None, "client_factory": _default_client_factory}


def configure_runtime(*, broker: Any, client_factory: Callable[[str], Any] | None = None) -> None:
    """Set the credential broker (and optionally a client factory) for this process."""
    _runtime["broker"] = broker
    if client_factory is not None:
        _runtime["client_factory"] = client_factory


class AIService:
    def __init__(self, session: Any, broker: Any = None) -> None:
        self._session = session
        self._conn = session.conn
        self._broker = broker
        self._settings = sql.Identifier(session.tenant.schema, "meta_ai_settings")
        self._usage = sql.Identifier(session.tenant.schema, "ai_usage")

    def _row(self) -> tuple[Any, ...] | None:
        return self._conn.execute(
            sql.SQL("SELECT settings, secret_ciphertext, key_id FROM {} WHERE id = 1").format(
                self._settings
            )
        ).fetchone()

    def settings(self) -> AISettings:
        row = self._row()
        return AISettings.model_validate(row[0] if row else {})

    def status(self) -> dict[str, Any]:
        """Settings and this month's usage. Never includes the key."""
        row = self._row()
        settings = self.settings()
        used = self.tokens_this_month()
        return {
            "settings": settings.model_dump(),
            "api_key_configured": bool(row and row[1]),
            "tokens_this_month": used,
            "tokens_left_this_month": max(0, settings.monthly_token_limit - used),
        }

    def _require_manager(self, doing: str) -> None:
        self._session.authz.require("ai.manage", doing)
        if not self._session.principal.is_human:
            raise ApprovalError(
                f"{doing.capitalize()} must be done by a person.", code="APPROVAL_REQUIRES_HUMAN"
            )

    def preview(self, changes: dict[str, Any]) -> dict[str, Any]:
        self._session.authz.require("ai.manage", "changing AI settings")
        current = self.settings()
        try:
            new = AISettings.model_validate({**current.model_dump(), **changes})
        except PydanticValidationError as exc:
            raise RecordValidationError(
                "Invalid AI settings.",
                code="AI_SETTINGS_INVALID",
                details={
                    "errors": [
                        {"field": ".".join(map(str, e["loc"])), "message": e["msg"]}
                        for e in exc.errors(include_url=False)
                    ]
                },
            ) from None
        diff = {
            k: {"from": getattr(current, k), "to": v}
            for k, v in new.model_dump().items()
            if getattr(current, k) != v
        }
        return {"settings": new.model_dump(), "changes": diff}

    def update(self, changes: dict[str, Any], *, api_key: str | None = None) -> dict[str, Any]:
        """Change settings and, optionally, the API key (CLI only: keys never pass
        through agents)."""
        self._require_manager("changing AI settings")
        preview = self.preview(changes)
        ciphertext, key_id = None, None
        if api_key is not None:
            if self._broker is None:
                raise AIError("No credential broker configured.", code="CREDENTIAL_ERROR")
            if not api_key.strip():
                raise RecordValidationError("The API key is empty.", field="api_key")
            ciphertext = self._broker.encrypt({"api_key": api_key.strip()})
            key_id = self._broker.key_id
        actor = self._session.principal.user_id
        with self._conn.transaction():
            self._conn.execute(
                sql.SQL(
                    "INSERT INTO {} (id, settings, updated_by) VALUES (1, %s, %s) "
                    "ON CONFLICT (id) DO NOTHING"
                ).format(self._settings),
                (Jsonb(preview["settings"]), actor),
            )
            self._conn.execute(
                sql.SQL(
                    "UPDATE {} SET settings = %s, updated_by = %s, updated_at = now() WHERE id = 1"
                ).format(self._settings),
                (Jsonb(preview["settings"]), actor),
            )
            if ciphertext is not None:
                self._conn.execute(
                    sql.SQL(
                        "UPDATE {} SET secret_ciphertext = %s, key_id = %s WHERE id = 1"
                    ).format(self._settings),
                    (ciphertext, key_id),
                )
        return {**preview, "api_key_configured": self.status()["api_key_configured"]}

    def remove_key(self) -> None:
        self._require_manager("removing the AI API key")
        self._conn.execute(
            sql.SQL(
                "UPDATE {} SET secret_ciphertext = NULL, key_id = NULL, updated_at = now() "
                "WHERE id = 1"
            ).format(self._settings)
        )

    # ---- for AI steps (engine use, no permission of its own) ---------------------------

    def tokens_this_month(self) -> int:
        start = dt.datetime.now(dt.UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        row = self._conn.execute(
            sql.SQL(
                "SELECT COALESCE(sum(input_tokens + output_tokens), 0) FROM {} WHERE at >= %s"
            ).format(self._usage),
            (start,),
        ).fetchone()
        return int(row[0])

    def check_budget(self) -> None:
        settings = self.settings()
        if self.tokens_this_month() >= settings.monthly_token_limit:
            raise AIError(
                "This organization has used its AI tokens for the month.",
                code="AI_BUDGET_EXCEEDED",
                hint="An administrator can raise monthly_token_limit with `osier ai settings`.",
            )

    def client(self) -> tuple[Any, AISettings]:
        """A Claude client with the tenant's key, and the settings to use."""
        settings = self.settings()
        row = self._row()
        if not settings.model or not row or not row[1]:
            raise AIError(
                "AI is not set up for this organization.",
                code="AI_NOT_CONFIGURED",
                hint="An administrator runs `osier ai setup` to add the API key and model.",
            )
        broker = self._broker or _runtime["broker"]
        if broker is None:
            raise AIError("No credential broker configured.", code="CREDENTIAL_ERROR")
        api_key = broker.decrypt(row[1], row[2])["api_key"]
        return _runtime["client_factory"](api_key), settings

    def record_usage(self, run_id: Any, step_id: str, model: str, usage: Any) -> dict[str, int]:
        used = {
            "input_tokens": int(getattr(usage, "input_tokens", 0) or 0),
            "output_tokens": int(getattr(usage, "output_tokens", 0) or 0),
            "cache_read_tokens": int(getattr(usage, "cache_read_input_tokens", 0) or 0),
        }
        self._conn.execute(
            sql.SQL(
                "INSERT INTO {} (run_id, step_id, model, input_tokens, output_tokens, "
                "cache_read_tokens) VALUES (%s, %s, %s, %s, %s, %s)"
            ).format(self._usage),
            (
                run_id,
                step_id,
                model,
                used["input_tokens"],
                used["output_tokens"],
                used["cache_read_tokens"],
            ),
        )
        return used
