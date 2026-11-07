"""The tenant's AI model connections, model profiles, limits, and usage.

Models are reached through LiteLLM (MIT), so any provider it supports works:
Anthropic, OpenAI, OpenRouter, Azure, Groq, a self-hosted OpenAI-compatible server,
and more.

- **AI connections**: several per tenant. Each has a LiteLLM provider, an optional API
  base URL, the models it may use, an encrypted API key (entered by a person through
  the CLI, never returned by any tool, record, or log), optional prices, and monthly
  limits (tokens, and dollars when prices are known).
- **Model profiles**: named choices such as ``fast`` or ``careful`` that map to a
  connection and model. AI steps name a profile (or use the default), so the model
  behind a profile can change without editing workflows.
- Changing connections or profiles needs ``ai.manage`` and a person.
- Workers call ``configure_runtime`` once with the credential broker (tests also pass
  a fake completion function).
"""

from __future__ import annotations

import datetime as dt
import os
import re
import uuid
from collections.abc import Callable
from decimal import Decimal
from typing import Any, Literal
from urllib.parse import urlparse

from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field
from pydantic import ValidationError as PydanticValidationError

from .errors import ApprovalError, NotFoundError, OsierError, RecordValidationError

NAME = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


class AIError(OsierError):
    default_code = "AI_ERROR"


def _litellm() -> Any:
    # Use LiteLLM's bundled model price list instead of downloading it on import.
    os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    import litellm

    litellm.suppress_debug_info = True
    return litellm


def known_providers() -> list[str]:
    return sorted(str(getattr(p, "value", p)) for p in _litellm().provider_list)


class ConnectionSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    models: list[str] = Field(min_length=1, max_length=50)
    input_usd_per_million: Decimal | None = Field(default=None, ge=0, le=10_000)
    output_usd_per_million: Decimal | None = Field(default=None, ge=0, le=10_000)
    monthly_token_limit: int = Field(default=2_000_000, ge=10_000, le=1_000_000_000)
    monthly_budget_usd: Decimal | None = Field(default=None, ge=0, le=1_000_000)


class Profile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=40)
    connection: str
    model: str = Field(min_length=1, max_length=200)
    effort: Literal["low", "medium", "high"] = "medium"
    description: str = Field(default="", max_length=500)


_runtime: dict[str, Any] = {"broker": None, "completion": None, "allow_private_network": False}


def configure_runtime(
    *,
    broker: Any,
    completion: Callable[..., Any] | None = None,
    allow_private_network: bool | None = None,
) -> None:
    """Set the credential broker (and, in tests, a fake completion) for this process."""
    _runtime["broker"] = broker
    _runtime["completion"] = completion
    if allow_private_network is not None:
        _runtime["allow_private_network"] = allow_private_network


def _validation(exc: PydanticValidationError, code: str) -> RecordValidationError:
    return RecordValidationError(
        "Invalid AI settings.",
        code=code,
        details={
            "errors": [
                {"field": ".".join(map(str, e["loc"])), "message": e["msg"]}
                for e in exc.errors(include_url=False)
            ]
        },
    )


class AIService:
    def __init__(self, session: Any, broker: Any = None) -> None:
        self._session = session
        self._conn = session.conn
        self._broker = broker
        schema = session.tenant.schema
        self._connections = sql.Identifier(schema, "ai_connections")
        self._profiles = sql.Identifier(schema, "ai_profiles")
        self._settings = sql.Identifier(schema, "meta_ai_settings")
        self._usage = sql.Identifier(schema, "ai_usage")

    def _require_manager(self, doing: str) -> None:
        self._session.authz.require("ai.manage", doing)
        if not self._session.principal.is_human:
            raise ApprovalError(
                f"{doing.capitalize()} must be done by a person.", code="APPROVAL_REQUIRES_HUMAN"
            )

    # ---- reading --------------------------------------------------------------------

    def _connection_rows(self) -> list[dict[str, Any]]:
        with self._conn.cursor(row_factory=dict_row) as cur:
            return cur.execute(
                sql.SQL("SELECT * FROM {} WHERE status <> 'removed' ORDER BY name").format(
                    self._connections
                )
            ).fetchall()

    def _connection(self, name: str) -> dict[str, Any]:
        for row in self._connection_rows():
            if row["name"] == name:
                return row
        raise NotFoundError(
            f"No AI connection named {name!r}.",
            code="AI_CONNECTION_NOT_FOUND",
            expected=[r["name"] for r in self._connection_rows()],
        )

    def connections(self) -> list[dict[str, Any]]:
        out = []
        for row in self._connection_rows():
            settings = ConnectionSettings.model_validate(row["settings"])
            used = self.usage_this_month(row["id"])
            out.append(
                {
                    "name": row["name"],
                    "provider": row["provider"],
                    "api_base": row["api_base"],
                    "status": row["status"],
                    "api_key_configured": row["secret_ciphertext"] is not None,
                    **settings.model_dump(mode="json"),
                    "tokens_this_month": used["tokens"],
                    "cost_usd_this_month": used["cost_usd"],
                }
            )
        return out

    def profiles(self) -> list[dict[str, Any]]:
        names = {r["id"]: r["name"] for r in self._connection_rows()}
        with self._conn.cursor(row_factory=dict_row) as cur:
            rows = cur.execute(
                sql.SQL(
                    "SELECT name, connection_id, model, effort, description FROM {} ORDER BY name"
                ).format(self._profiles)
            ).fetchall()
        default = self.default_profile()
        return [
            {
                "name": r["name"],
                "connection": names.get(r["connection_id"]),
                "model": r["model"],
                "effort": r["effort"],
                "description": r["description"],
                "default": r["name"] == default,
            }
            for r in rows
        ]

    def default_profile(self) -> str | None:
        row = self._conn.execute(
            sql.SQL("SELECT settings FROM {} WHERE id = 1").format(self._settings)
        ).fetchone()
        return (row[0] or {}).get("default_profile") if row else None

    def status(self) -> dict[str, Any]:
        """Connections (never keys), profiles, and usage this month."""
        return {
            "connections": self.connections(),
            "profiles": self.profiles(),
            "default_profile": self.default_profile(),
        }

    # ---- connections (CLI: keys never pass through agents) ----------------------------

    def _check_api_base(self, api_base: str | None) -> str | None:
        if not api_base:
            return None
        parsed = urlparse(api_base)
        private_ok = _runtime["allow_private_network"]
        if parsed.scheme != "https" and not (private_ok and parsed.scheme == "http"):
            raise RecordValidationError("The API base URL must use https.", field="api_base")
        from .connectors.netguard import check_destination

        check_destination(
            parsed.hostname or "", parsed.port or 443, allow_private_network=private_ok
        )
        return api_base

    def connect(
        self,
        name: str,
        *,
        provider: str,
        models: list[str],
        api_key: str | None,
        api_base: str | None = None,
        **settings: Any,
    ) -> dict[str, Any]:
        """Add or replace an AI connection."""
        self._require_manager("connecting AI models")
        if not NAME.fullmatch(name) or len(name) > 40:
            raise RecordValidationError(
                f"Invalid connection name {name!r}.",
                field="name",
                expected="lowercase letters, digits, single hyphens",
            )
        if provider not in known_providers():
            raise RecordValidationError(
                f"Unknown provider {provider!r}.",
                field="provider",
                code="AI_PROVIDER_UNKNOWN",
                hint="Use a LiteLLM provider name, e.g. openai, "
                "anthropic, openrouter, azure, groq, ollama.",
            )
        try:
            parsed = ConnectionSettings.model_validate({"models": models, **settings})
        except PydanticValidationError as exc:
            raise _validation(exc, "AI_SETTINGS_INVALID") from None
        api_base = self._check_api_base(api_base)
        ciphertext = key_id = None
        if api_key:
            broker = self._broker or _runtime["broker"]
            if broker is None:
                raise AIError("No credential broker configured.", code="CREDENTIAL_ERROR")
            ciphertext, key_id = broker.encrypt({"api_key": api_key.strip()}), broker.key_id
        actor = self._session.principal.user_id
        with self._conn.transaction():
            existing = self._conn.execute(
                sql.SQL("SELECT id FROM {} WHERE name = %s").format(self._connections), (name,)
            ).fetchone()
            if existing:
                self._conn.execute(
                    sql.SQL(
                        "UPDATE {} SET provider = %s, api_base = %s, settings = %s, "
                        "secret_ciphertext = %s, key_id = %s, status = 'active', "
                        "updated_by = %s, updated_at = now() WHERE id = %s"
                    ).format(self._connections),
                    (
                        provider,
                        api_base,
                        Jsonb(parsed.model_dump(mode="json")),
                        ciphertext,
                        key_id,
                        actor,
                        existing[0],
                    ),
                )
            else:
                self._conn.execute(
                    sql.SQL(
                        "INSERT INTO {} (id, name, provider, api_base, settings, "
                        "secret_ciphertext, key_id, created_by, updated_by) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)"
                    ).format(self._connections),
                    (
                        uuid.uuid4(),
                        name,
                        provider,
                        api_base,
                        Jsonb(parsed.model_dump(mode="json")),
                        ciphertext,
                        key_id,
                        actor,
                        actor,
                    ),
                )
        return next(c for c in self.connections() if c["name"] == name)

    def update_connection(self, name: str, changes: dict[str, Any]) -> dict[str, Any]:
        """Change models, prices, or limits (not the key; reconnect for that)."""
        self._require_manager("changing AI connections")
        row = self._connection(name)
        try:
            parsed = ConnectionSettings.model_validate({**row["settings"], **changes})
        except PydanticValidationError as exc:
            raise _validation(exc, "AI_SETTINGS_INVALID") from None
        self._conn.execute(
            sql.SQL(
                "UPDATE {} SET settings = %s, updated_by = %s, updated_at = now() WHERE id = %s"
            ).format(self._connections),
            (Jsonb(parsed.model_dump(mode="json")), self._session.principal.user_id, row["id"]),
        )
        return next(c for c in self.connections() if c["name"] == name)

    def disconnect(self, name: str) -> dict[str, Any]:
        self._require_manager("removing AI connections")
        row = self._connection(name)
        in_use = [p["name"] for p in self.profiles() if p["connection"] == name]
        if in_use:
            raise AIError(
                f"Profiles {in_use} use this connection; change them first.",
                code="AI_CONNECTION_IN_USE",
            )
        self._conn.execute(
            sql.SQL(
                "UPDATE {} SET status = 'removed', secret_ciphertext = NULL, key_id = NULL, "
                "updated_at = now() WHERE id = %s"
            ).format(self._connections),
            (row["id"],),
        )
        return {"name": name, "status": "removed"}

    # ---- profiles (no secrets: also possible in chat, after confirmation) ---------------

    def preview_profile(self, **fields: Any) -> dict[str, Any]:
        self._session.authz.require("ai.manage", "changing AI model profiles")
        try:
            profile = Profile.model_validate(fields)
        except PydanticValidationError as exc:
            raise _validation(exc, "AI_PROFILE_INVALID") from None
        if not NAME.fullmatch(profile.name):
            raise RecordValidationError(
                f"Invalid profile name {profile.name!r}.", field="name", code="AI_PROFILE_INVALID"
            )
        connection = self._connection(profile.connection)
        allowed = ConnectionSettings.model_validate(connection["settings"]).models
        if profile.model not in allowed:
            raise RecordValidationError(
                f"Connection {profile.connection!r} does not allow model {profile.model!r}.",
                code="AI_MODEL_NOT_ALLOWED",
                field="model",
                expected=allowed,
            )
        current = next((p for p in self.profiles() if p["name"] == profile.name), None)
        return {
            "profile": profile.model_dump(),
            "current": current,
            "action": "update" if current else "create",
        }

    def set_profile(self, *, default: bool = False, **fields: Any) -> dict[str, Any]:
        self._require_manager("changing AI model profiles")
        preview = self.preview_profile(**fields)
        p = preview["profile"]
        connection_id = self._connection(p["connection"])["id"]
        actor = self._session.principal.user_id
        with self._conn.transaction():
            self._conn.execute(
                sql.SQL("DELETE FROM {} WHERE name = %s").format(self._profiles), (p["name"],)
            )
            self._conn.execute(
                sql.SQL(
                    "INSERT INTO {} (name, connection_id, model, effort, description, "
                    "updated_by) VALUES (%s, %s, %s, %s, %s, %s)"
                ).format(self._profiles),
                (p["name"], connection_id, p["model"], p["effort"], p["description"], actor),
            )
            if default or self.default_profile() is None:
                self._set_default(p["name"], actor)
        return next(x for x in self.profiles() if x["name"] == p["name"])

    def _set_default(self, name: str, actor: Any) -> None:
        self._conn.execute(
            sql.SQL(
                "INSERT INTO {} (id, settings, updated_by) VALUES (1, %s, %s) "
                "ON CONFLICT (id) DO NOTHING"
            ).format(self._settings),
            (Jsonb({}), actor),
        )
        self._conn.execute(
            sql.SQL(
                "UPDATE {} SET settings = settings || %s, updated_by = %s, "
                "updated_at = now() WHERE id = 1"
            ).format(self._settings),
            (Jsonb({"default_profile": name}), actor),
        )

    def remove_profile(self, name: str) -> dict[str, Any]:
        self._require_manager("changing AI model profiles")
        self._conn.execute(
            sql.SQL("DELETE FROM {} WHERE name = %s").format(self._profiles), (name,)
        )
        return {"name": name, "removed": True}

    # ---- for AI steps (engine use) ------------------------------------------------------

    def resolve(self, profile_name: str | None) -> dict[str, Any]:
        """The profile, connection, and key an AI step uses."""
        name = profile_name or self.default_profile()
        profile = next((p for p in self.profiles() if p["name"] == name), None) if name else None
        if profile is None:
            raise AIError(
                f"No AI model profile {name!r}." if name else "No AI model is set up.",
                code="AI_NOT_CONFIGURED",
                hint="An administrator adds a connection with `osier ai connect` and a "
                "profile with `osier ai profile`.",
                expected=[p["name"] for p in self.profiles()],
            )
        row = self._connection(profile["connection"])
        if row["status"] != "active":
            raise AIError(f"AI connection {row['name']!r} is not active.", code="AI_NOT_CONFIGURED")
        settings = ConnectionSettings.model_validate(row["settings"])
        api_key = None
        if row["secret_ciphertext"] is not None:
            broker = self._broker or _runtime["broker"]
            if broker is None:
                raise AIError("No credential broker configured.", code="CREDENTIAL_ERROR")
            api_key = broker.decrypt(row["secret_ciphertext"], row["key_id"])["api_key"]
        return {
            "profile": profile,
            "connection_id": row["id"],
            "connection": row["name"],
            "provider": row["provider"],
            "api_base": row["api_base"],
            "api_key": api_key,
            "settings": settings,
        }

    def completion(self) -> Callable[..., Any]:
        return _runtime["completion"] or _litellm().completion

    def usage_this_month(self, connection_id: Any = None) -> dict[str, Any]:
        start = dt.datetime.now(dt.UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        where = sql.SQL("at >= %s")
        params: list[Any] = [start]
        if connection_id is not None:
            where += sql.SQL(" AND connection_id = %s")
            params.append(connection_id)
        row = self._conn.execute(
            sql.SQL(
                "SELECT COALESCE(sum(input_tokens + output_tokens), 0), sum(cost_usd) "
                "FROM {} WHERE {}"
            ).format(self._usage, where),
            params,
        ).fetchone()
        return {"tokens": int(row[0]), "cost_usd": float(row[1]) if row[1] is not None else None}

    def check_budget(self, resolved: dict[str, Any]) -> None:
        settings: ConnectionSettings = resolved["settings"]
        used = self.usage_this_month(resolved["connection_id"])
        if used["tokens"] >= settings.monthly_token_limit:
            raise AIError(
                f"AI connection {resolved['connection']!r} has used its tokens for the month.",
                code="AI_BUDGET_EXCEEDED",
                hint="Raise monthly_token_limit with `osier ai update`.",
            )
        if (
            settings.monthly_budget_usd is not None
            and used["cost_usd"] is not None
            and Decimal(str(used["cost_usd"])) >= settings.monthly_budget_usd
        ):
            raise AIError(
                f"AI connection {resolved['connection']!r} has reached its monthly budget.",
                code="AI_BUDGET_EXCEEDED",
            )

    def cost(
        self, resolved: dict[str, Any], response: Any, input_tokens: int, output_tokens: int
    ) -> float | None:
        settings: ConnectionSettings = resolved["settings"]
        if settings.input_usd_per_million is not None and settings.output_usd_per_million:
            return float(
                (
                    settings.input_usd_per_million * input_tokens
                    + settings.output_usd_per_million * output_tokens
                )
                / 1_000_000
            )
        try:
            return float(_litellm().completion_cost(completion_response=response))
        except Exception:  # noqa: BLE001 - unknown models have no price
            return None

    def record_usage(
        self, resolved: dict[str, Any], run_id: Any, step_id: str, response: Any
    ) -> dict[str, Any]:
        usage = getattr(response, "usage", None)
        used = {
            "input_tokens": int(getattr(usage, "prompt_tokens", 0) or 0),
            "output_tokens": int(getattr(usage, "completion_tokens", 0) or 0),
        }
        cost = self.cost(resolved, response, used["input_tokens"], used["output_tokens"])
        self._conn.execute(
            sql.SQL(
                "INSERT INTO {} (run_id, step_id, connection_id, profile, model, input_tokens, "
                "output_tokens, cost_usd) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"
            ).format(self._usage),
            (
                run_id,
                step_id,
                resolved["connection_id"],
                resolved["profile"]["name"],
                resolved["profile"]["model"],
                used["input_tokens"],
                used["output_tokens"],
                cost,
            ),
        )
        return {**used, "cost_usd": cost}
