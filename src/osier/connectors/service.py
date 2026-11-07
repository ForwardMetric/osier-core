"""Connector service: connections, routing, approvals, execution, and audit."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pydantic import BaseModel
from pydantic import ValidationError as PydanticValidationError

from ..authz import Authorizer
from ..context import ActorType, Principal
from ..errors import (
    ApprovalError,
    IntegrityError,
    NotFoundError,
    OsierError,
    RecordValidationError,
)
from .base import ConnectorType, ExecutionContext
from .email import SendEmail
from .mcp_service import McpConnectorsMixin
from .vault import CredentialBroker

ENGINE_SERVICE = "connectors"

# Where a sent email is logged, per related object: (object, link field, extra values).
_EMAIL_LOG_TARGETS: dict[str, tuple[str, str, dict[str, Any]]] = {
    "lead": ("activity", "lead", {}),
    "contact": ("activity", "contact", {}),
    "account": ("activity", "account", {}),
    "opportunity": ("activity", "opportunity", {}),
    "support_case": ("case_comment", "support_case", {"is_public": True}),
}


def _validation_error(message: str, exc: PydanticValidationError) -> RecordValidationError:
    return RecordValidationError(
        message,
        code="CONNECTOR_INPUT_INVALID",
        details={
            "errors": [
                {"field": ".".join(str(p) for p in e["loc"]), "message": e["msg"]}
                for e in exc.errors(include_url=False)
            ]
        },
    )


class ConnectorService(McpConnectorsMixin):
    def __init__(
        self,
        session: Any,  # Session (avoids an import cycle)
        broker: CredentialBroker,
        *,
        allow_private_network: bool = False,
        allow_local_processes: bool = False,
        mcp_client_factory: Any = None,
    ) -> None:
        from . import CONNECTOR_TYPES

        self._session = session
        self._conn = session.conn
        self._schema = session.tenant.schema
        self._principal: Principal = session.principal
        self._broker = broker
        self._types = CONNECTOR_TYPES
        self._records = session.records.for_engine(ENGINE_SERVICE)
        self._allow_private_network = allow_private_network
        self._authz = Authorizer(session.conn, session.tenant, session.principal)
        self._allow_local_processes = allow_local_processes
        self._mcp_client_factory = mcp_client_factory

    # ---- helpers ---------------------------------------------------------------------

    def _t(self, name: str) -> sql.Identifier:
        return sql.Identifier(self._schema, name)

    def _type(self, key: str) -> ConnectorType:
        connector = self._types.get(key)
        if connector is None:
            raise NotFoundError(
                f"There is no connector type {key!r}.",
                code="CONNECTOR_TYPE_NOT_FOUND",
                expected=sorted(self._types),
            )
        return connector

    def _require_human(self, what: str) -> None:
        """Managing connections: a person (never an agent) with connections.manage."""
        if not (self._principal.is_human or self._principal.actor_type is ActorType.SYSTEM):
            raise ApprovalError(
                f"{what} must be done by a person, not an agent.",
                code="APPROVAL_REQUIRES_HUMAN",
                hint="Ask the user to confirm it themselves.",
            )
        self._authz.require("connections.manage", what.lower())

    def _require_decider(self, row: dict[str, Any], what: str) -> None:
        """Approving an agent's action: a person who is the one the agent acted for, or
        who manages connections."""
        if not (self._principal.is_human or self._principal.actor_type is ActorType.SYSTEM):
            raise ApprovalError(
                f"{what} must be done by a person, not an agent.",
                code="APPROVAL_REQUIRES_HUMAN",
                hint="Ask the user to confirm it themselves.",
            )
        if self._principal.actor_type is ActorType.SYSTEM:
            return
        requester = row["on_behalf_of"] if row["actor_type"] == "agent" else row["actor_id"]
        if requester != self._principal.user_id:
            self._authz.require("connections.manage", "deciding on someone else's action")

    def _acting_user(self) -> uuid.UUID:
        return self._principal.on_behalf_of or self._principal.user_id

    def _visible(self, connection: dict[str, Any]) -> bool:
        """May the person behind this request have agents use the connection?"""
        if self._principal.actor_type is ActorType.SYSTEM:
            return True
        visibility = connection.get("visibility") or "organization"
        if visibility == "organization":
            return True
        if connection["owner_user"] == str(self._acting_user()):
            return True
        if visibility == "roles":
            return bool(set(self._visible_roles(connection["id"])) & set(self._authz.roles()))
        return False

    def _visible_roles(self, connection_id: str) -> list[str]:
        return [
            r[0]
            for r in self._conn.execute(
                sql.SQL("SELECT role_key FROM {} WHERE connection_id = %s ORDER BY 1").format(
                    self._t("connector_visibility")
                ),
                (connection_id,),
            ).fetchall()
        ]

    def _check_visibility(self, visibility: str | None, roles: list[str] | None, scope: str) -> str:
        visibility = visibility or ("private" if scope == "user" else "organization")
        if visibility not in ("private", "roles", "organization"):
            raise RecordValidationError(
                f"Invalid visibility {visibility!r}.",
                field="visibility",
                expected=["private", "roles", "organization"],
            )
        if (visibility == "roles") != bool(roles):
            raise RecordValidationError(
                "List the roles exactly when visibility is 'roles'.", field="visible_roles"
            )
        if roles:
            known = {r["key"] for r in self._session.records.list("role", limit=200)}
            unknown = sorted(set(roles) - known)
            if unknown:
                raise RecordValidationError(
                    f"Unknown roles {unknown}.", field="visible_roles", expected=sorted(known)
                )
        return visibility

    def _store_visibility(self, connection_id: str, roles: list[str] | None) -> None:
        self._conn.execute(
            sql.SQL("DELETE FROM {} WHERE connection_id = %s").format(
                self._t("connector_visibility")
            ),
            (connection_id,),
        )
        for role in roles or []:
            self._conn.execute(
                sql.SQL("INSERT INTO {} (connection_id, role_key) VALUES (%s, %s)").format(
                    self._t("connector_visibility")
                ),
                (connection_id, role),
            )

    def set_visibility(
        self, connection_name: str, visibility: str, visible_roles: list[str] | None = None
    ) -> dict[str, Any]:
        """Change who may use a connection (people with connections.manage)."""
        self._require_human("Sharing a connection")
        connection = self._connection_by_name(connection_name, manage=True)
        visibility = self._check_visibility(visibility, visible_roles, connection["scope"])
        with self._conn.transaction():
            updated = self._records.update(
                "connection",
                connection["id"],
                {"visibility": visibility},
                expected_row_version=connection["row_version"],
            )
            self._store_visibility(connection["id"], visible_roles if visibility == "roles" else [])
        return self._describe_connection(updated)

    @staticmethod
    def _sharing_warning(scope: str, visibility: str) -> str | None:
        if scope == "user" and visibility != "private":
            return (
                "This connection uses a person's own account. Everyone it is shared with "
                "will act as that account (e.g. emails come from that person). Prefer a "
                "shared company account for shared connections."
            )
        return None

    # ---- connector types and connections -------------------------------------------

    def connector_types(self) -> list[dict[str, Any]]:
        return [c.describe() for c in self._types.values()]

    def create_connection(
        self,
        connector_type: str,
        name: str,
        *,
        purpose: str = "transactional",
        config: dict[str, Any] | None = None,
        secrets: dict[str, Any] | None = None,
        scope: str = "organization",
        owner_user: uuid.UUID | None = None,
        visibility: str | None = None,
        visible_roles: list[str] | None = None,
        is_default: bool = False,
        approval: str = "required",
        max_recipients: int = 10,
        verify: bool = True,
    ) -> dict[str, Any]:
        """Create a connection. People only: credentials are entered outside the chat."""
        self._require_human("Connecting an external account")
        connector = self._type(connector_type)
        try:
            config_obj = connector.config_model.model_validate(config or {})
            secret_obj = (
                connector.secret_model.model_validate(secrets or {})
                if connector.secret_model
                else None
            )
        except PydanticValidationError as exc:
            raise _validation_error("Invalid connection settings.", exc) from None
        visibility = self._check_visibility(visibility, visible_roles, scope)
        if owner_user is None and (scope == "user" or visibility != "organization"):
            owner_user = self._acting_user()

        if verify:
            connector.verify(
                ExecutionContext(
                    config=config_obj,
                    secrets=secret_obj,
                    connection={"name": name},
                    allow_private_network=self._allow_private_network,
                )
            )

        with self._conn.transaction():
            if is_default:
                self._clear_defaults(purpose, connector)
            record = self._records.create(
                "connection",
                {
                    "name": name,
                    "connector_type": connector.key,
                    "purpose": purpose,
                    "scope": scope,
                    "visibility": visibility,
                    "owner_user": owner_user,
                    "is_default": is_default,
                    "status": "active",
                    "connected_at": dt.datetime.now(dt.UTC),
                },
            )
            self._conn.execute(
                sql.SQL(
                    "INSERT INTO {} (connection_id, config, secret_ciphertext, key_id) "
                    "VALUES (%s, %s, %s, %s)"
                ).format(self._t("connector_credentials")),
                (
                    record["id"],
                    Jsonb(config_obj.model_dump(mode="json")),
                    self._broker.encrypt(secret_obj.model_dump()) if secret_obj else None,
                    self._broker.key_id if secret_obj else None,
                ),
            )
            self._conn.execute(
                sql.SQL(
                    "INSERT INTO {} (connection_id, approval, max_recipients, updated_by) "
                    "VALUES (%s, %s, %s, %s)"
                ).format(self._t("connector_policies")),
                (record["id"], approval, max_recipients, self._principal.user_id),
            )
            self._store_visibility(record["id"], visible_roles)
        described = self._describe_connection(record)
        warning = self._sharing_warning(scope, visibility)
        return {**described, "warning": warning} if warning else described

    def _clear_defaults(self, purpose: str, connector: ConnectorType) -> None:
        for other in self._session.records.list(
            "connection", where={"purpose": purpose, "is_default": True}, limit=200
        ):
            other_type = self._types.get(other["connector_type"])
            if other_type and set(other_type.capabilities) & set(connector.capabilities):
                self._records.update(
                    "connection",
                    other["id"],
                    {"is_default": False},
                    expected_row_version=other["row_version"],
                )

    def _policy(self, connection_id: str) -> dict[str, Any]:
        with self._conn.cursor(row_factory=dict_row) as cur:
            row = cur.execute(
                sql.SQL("SELECT approval, max_recipients FROM {} WHERE connection_id = %s").format(
                    self._t("connector_policies")
                ),
                (connection_id,),
            ).fetchone()
        return row or {"approval": "required", "max_recipients": 10}

    def _describe_connection(self, record: dict[str, Any]) -> dict[str, Any]:
        connector = self._types.get(record["connector_type"])
        policy = self._policy(record["id"])
        extra: dict[str, Any] = {}
        if record["connector_type"].startswith("c__"):
            ctype = self._mcp_type(record["connector_type"])
            extra = {
                "runtime": "mcp",
                "server_status": ctype["status"],
                "tools": [t["name"] for t in ctype["tools"]],
                "hint": "Use list_connector_tools and call_connector_tool with this connection.",
            }
        return {
            **extra,
            "id": record["id"],
            "name": record["name"],
            "connector_type": record["connector_type"],
            "purpose": record["purpose"],
            "scope": record["scope"],
            "visibility": record.get("visibility") or "organization",
            "visible_roles": self._visible_roles(record["id"]),
            "owner_user": record["owner_user"],
            "is_default": record["is_default"],
            "status": record["status"],
            "last_error": record["last_error"],
            "capabilities": connector.capabilities if connector else [],
            "actions": [a.name for a in connector.actions] if connector else [],
            "approval": policy["approval"],
            "max_recipients": policy["max_recipients"],
        }

    def list_connections(
        self, *, capability: str | None = None, purpose: str | None = None
    ) -> list[dict[str, Any]]:
        where: dict[str, Any] = {}
        if purpose:
            where["purpose"] = purpose
        result = []
        for record in self._session.records.list("connection", where=where, limit=200):
            if not self._visible(record) or record["status"] == "revoked":
                continue
            described = self._describe_connection(record)
            if capability and capability not in described["capabilities"]:
                continue
            result.append(described)
        return result

    def resolve(
        self, capability: str, purpose: str, connection_name: str | None = None
    ) -> dict[str, Any]:
        """Pick the connection for a call: named, else the default, else the only match."""
        candidates = self.list_connections(capability=capability, purpose=purpose)
        if connection_name is not None:
            named = [c for c in candidates if c["name"] == connection_name]
            if not named:
                raise NotFoundError(
                    f"No {purpose} connection named {connection_name!r} provides {capability}.",
                    code="CONNECTION_NOT_FOUND",
                    expected=[c["name"] for c in candidates],
                )
            return named[0]
        if not candidates:
            raise NotFoundError(
                f"No {purpose} connection provides {capability}.",
                code="NO_CONNECTION",
                hint="Ask the user to connect an account with 'osier connect'.",
            )
        defaults = [c for c in candidates if c["is_default"]]
        if len(candidates) == 1:
            return candidates[0]
        if len(defaults) == 1:
            return defaults[0]
        raise IntegrityError(
            f"Several {purpose} connections provide {capability}; say which one to use.",
            code="CONNECTION_AMBIGUOUS",
            field="connection",
            expected=[c["name"] for c in candidates],
        )

    # ---- calls -----------------------------------------------------------------------

    def send_email(
        self,
        arguments: dict[str, Any],
        *,
        connection: str | None = None,
        idempotency_key: str | None = None,
        request_id: uuid.UUID | None = None,
    ) -> dict[str, Any]:
        return self.call(
            "email.send",
            arguments,
            connection=connection,
            idempotency_key=idempotency_key,
            request_id=request_id,
        )

    def call(
        self,
        capability: str,
        arguments: dict[str, Any],
        *,
        connection: str | None = None,
        idempotency_key: str | None = None,
        request_id: uuid.UUID | None = None,
    ) -> dict[str, Any]:
        """Run a capability through a connection, or queue it for a person's approval."""
        self._authz.require("actions.request", "having agents send email or use apps")
        if idempotency_key is not None:
            existing = self._call_by_key(idempotency_key)
            if existing is not None:
                return self._call_view(existing)

        purpose = arguments.get("purpose", "transactional") if isinstance(arguments, dict) else None
        target = self.resolve(capability, purpose or "general", connection)
        connector = self._type(target["connector_type"])
        action = connector.action_for(capability)
        if action is None:  # pragma: no cover - resolve() filters by capability
            raise NotFoundError(f"{connector.key} cannot do {capability}.")
        try:
            args = action.input_model.model_validate(arguments)
        except PydanticValidationError as exc:
            raise _validation_error(f"Invalid arguments for {capability}.", exc) from None

        recipients = None
        summary = f"{capability} via {target['name']}"
        if isinstance(args, SendEmail):
            if args.purpose == "marketing":
                raise OsierError(
                    "Marketing email (consent checks, unsubscribe links, bulk approval) is "
                    "not available yet.",
                    code="NOT_SUPPORTED_YET",
                    hint="Send one-to-one messages with purpose 'transactional'.",
                )
            recipients = args.recipient_count
            summary = (
                f"Email to {', '.join(args.to)}"
                + (f" (cc {', '.join(args.cc)})" if args.cc else "")
                + f": {args.subject!r} via {target['name']}"
            )
            self._check_related(args)
        policy = self._policy(target["id"])
        if recipients is not None and recipients > policy["max_recipients"]:
            raise OsierError(
                f"{recipients} recipients exceed this connection's limit of "
                f"{policy['max_recipients']}.",
                code="RECIPIENT_LIMIT",
                field="to",
            )

        needs_approval = action.side_effect == "external_send" and policy["approval"] == "required"
        call_id = uuid.uuid4()
        p = self._principal
        self._conn.execute(
            sql.SQL(
                "INSERT INTO {} (id, connection_id, connector_type, action, capability, purpose, "
                "status, summary, arguments, recipients, actor_id, actor_type, on_behalf_of, "
                "agent_id, source, request_id, idempotency_key) VALUES (%s, %s, %s, %s, %s, %s, "
                "%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
            ).format(self._t("connector_calls")),
            (
                call_id,
                target["id"],
                connector.key,
                action.name,
                capability,
                purpose,
                "awaiting_approval" if needs_approval else "running",
                summary,
                Jsonb(args.model_dump(mode="json")),
                recipients,
                p.user_id,
                p.actor_type.value,
                p.on_behalf_of,
                p.agent_id,
                p.source,
                request_id,
                idempotency_key,
            ),
        )
        if not needs_approval:
            self._execute(call_id)
        return self._call_view(self._get_call(call_id))

    def _check_related(self, args: SendEmail) -> None:
        if (args.related_object is None) != (args.related_record_id is None):
            raise RecordValidationError(
                "related_object and related_record_id must be given together.",
                field="related_record_id",
            )
        if args.related_object is not None:
            # Raises OBJECT_NOT_FOUND / RECORD_NOT_FOUND / RECORD_RETIRED with details.
            self._session.records.get(args.related_object, args.related_record_id)

    def pending_calls(self) -> list[dict[str, Any]]:
        with self._conn.cursor(row_factory=dict_row) as cur:
            rows = cur.execute(
                sql.SQL(
                    "SELECT * FROM {} WHERE status = 'awaiting_approval' ORDER BY created_at"
                ).format(self._t("connector_calls"))
            ).fetchall()
        return [self._call_view(r, include_arguments=True) for r in rows]

    def get_call(self, call_id: Any) -> dict[str, Any]:
        return self._call_view(self._get_call(call_id))

    def approve(self, call_id: Any) -> dict[str, Any]:
        self._require_decider(self._get_call(call_id), "Approving an external action")
        row = self._decide(call_id, "running")
        self._execute(row["id"])
        return self._call_view(self._get_call(row["id"]))

    def reject(self, call_id: Any) -> dict[str, Any]:
        self._require_decider(self._get_call(call_id), "Rejecting an external action")
        row = self._decide(call_id, "rejected")
        return self._call_view(self._get_call(row["id"]))

    def _decide(self, call_id: Any, new_status: str) -> dict[str, Any]:
        with self._conn.transaction():
            row = self._get_call(call_id, lock=True)
            if row["status"] != "awaiting_approval":
                raise ApprovalError(
                    f"This action is {row['status']}, not awaiting approval.",
                    code="CALL_NOT_PENDING",
                )
            self._conn.execute(
                sql.SQL(
                    "UPDATE {} SET status = %s, decided_by = %s, decided_at = now() WHERE id = %s"
                ).format(self._t("connector_calls")),
                (new_status, self._principal.user_id, row["id"]),
            )
        return row

    def _get_call(self, call_id: Any, *, lock: bool = False) -> dict[str, Any]:
        try:
            cid = uuid.UUID(str(call_id))
        except ValueError:
            raise RecordValidationError("Invalid call id.", field="call_id") from None
        query = sql.SQL("SELECT * FROM {} WHERE id = %s").format(self._t("connector_calls"))
        if lock:
            query += sql.SQL(" FOR UPDATE")
        with self._conn.cursor(row_factory=dict_row) as cur:
            row = cur.execute(query, (cid,)).fetchone()
        if row is None:
            raise NotFoundError(f"No connector call {cid}.", code="CALL_NOT_FOUND")
        return row

    def _call_by_key(self, key: str) -> dict[str, Any] | None:
        with self._conn.cursor(row_factory=dict_row) as cur:
            return cur.execute(
                sql.SQL("SELECT * FROM {} WHERE idempotency_key = %s").format(
                    self._t("connector_calls")
                ),
                (key,),
            ).fetchone()

    def _call_view(self, row: dict[str, Any], *, include_arguments: bool = False) -> dict[str, Any]:
        view = {
            "call_id": str(row["id"]),
            "status": row["status"],
            "summary": row["summary"],
            "capability": row["capability"],
            "connector_type": row["connector_type"],
            "result": row["result"],
            "error": row["error"],
            "requested_by": str(row["actor_id"]),
            "requested_by_type": row["actor_type"],
            "created_at": row["created_at"].isoformat(),
        }
        if row["status"] == "awaiting_approval":
            view["next_step"] = (
                "Nothing has been sent or run. A person must approve this with "
                f"'osier approvals approve {row['id']}'."
            )
        if include_arguments:
            view["arguments"] = row["arguments"]
        return view

    def _execute(self, call_id: uuid.UUID) -> None:
        row = self._get_call(call_id)
        connection = self._session.records.get("connection", row["connection_id"])
        if row["connector_type"].startswith("c__"):
            self._execute_mcp(row, connection)
            return
        connector = self._type(row["connector_type"])
        action = connector.action(row["action"])
        assert action is not None
        try:
            ctx = self._context(connector, connection)
            args = action.input_model.model_validate(row["arguments"])
            result = connector.execute(action, args, ctx)
        except OsierError as exc:
            self._finish(row, connection, "failed", error=exc.to_dict())
            return
        except Exception as exc:  # noqa: BLE001 - connector bugs must not leak secrets
            self._finish(
                row,
                connection,
                "failed",
                error={"code": "CONNECTOR_FAILED", "message": exc.__class__.__name__},
            )
            return
        logged = self._log_to_crm(row, args)
        if logged:
            result = {**result, "logged_as": logged}
        self._finish(row, connection, "succeeded", result=result)

    def _context(self, connector: ConnectorType, connection: dict[str, Any]) -> ExecutionContext:
        with self._conn.cursor(row_factory=dict_row) as cur:
            cred = cur.execute(
                sql.SQL("SELECT * FROM {} WHERE connection_id = %s").format(
                    self._t("connector_credentials")
                ),
                (connection["id"],),
            ).fetchone()
        if cred is None or cred["revoked_at"] is not None:
            raise OsierError("This connection has no credentials.", code="CONNECTION_REVOKED")
        secrets = None
        if connector.secret_model is not None and cred["secret_ciphertext"] is not None:
            secrets = connector.secret_model.model_validate(
                self._broker.decrypt(cred["secret_ciphertext"], cred["key_id"])
            )
        return ExecutionContext(
            config=connector.config_model.model_validate(cred["config"]),
            secrets=secrets,
            connection={"id": connection["id"], "name": connection["name"]},
            allow_private_network=self._allow_private_network,
        )

    def _finish(
        self,
        row: dict[str, Any],
        connection: dict[str, Any],
        status: str,
        *,
        result: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
    ) -> None:
        with self._conn.transaction():
            self._conn.execute(
                sql.SQL(
                    "UPDATE {} SET status = %s, result = %s, error = %s, completed_at = now() "
                    "WHERE id = %s"
                ).format(self._t("connector_calls")),
                (
                    status,
                    Jsonb(result) if result else None,
                    Jsonb(error) if error else None,
                    row["id"],
                ),
            )
            update: dict[str, Any] = {"last_used_at": dt.datetime.now(dt.UTC)}
            if status == "failed" and (error or {}).get("code") != "TOOL_RETURNED_ERROR":
                update |= {"status": "error", "last_error": (error or {}).get("message")}
            elif connection["status"] != "active":
                update |= {"status": "active", "last_error": None}
            self._records.update(
                "connection",
                connection["id"],
                update,
                expected_row_version=connection["row_version"],
            )

    def _log_to_crm(self, row: dict[str, Any], args: BaseModel) -> dict[str, Any] | None:
        """Log a sent email on the related record, if that package is installed."""
        if not isinstance(args, SendEmail) or args.related_object is None:
            return None
        target = _EMAIL_LOG_TARGETS.get(args.related_object)
        snapshot = self._session.snapshot()
        if target is None or target[0] not in snapshot.objects:
            return None
        object_name, link_field, extra = target
        requester = Principal(
            user_id=row["actor_id"],
            actor_type=ActorType(row["actor_type"]),
            on_behalf_of=row["on_behalf_of"],
            agent_id=row["agent_id"],
            source=row["source"],
        )
        records = self._session.as_principal(requester).records
        header = f"To: {', '.join(args.to)}\nSubject: {args.subject}\n\n"
        if object_name == "activity":
            values: dict[str, Any] = {
                "subject": f"Email: {args.subject}"[:255],
                "activity_type": "email",
                "status": "completed",
                "end_time": dt.datetime.now(dt.UTC),
                "description": header + args.body_text,
                link_field: str(args.related_record_id),
            }
            if args.related_object == "contact":
                contact = records.get("contact", args.related_record_id)
                if contact.get("account"):
                    values["account"] = contact["account"]
        else:
            values = {
                **extra,
                "body": header + args.body_text,
                link_field: str(args.related_record_id),
            }
        logged = records.create(object_name, values, idempotency_key=f"connector-call:{row['id']}")
        return {"object": object_name, "id": logged["id"]}
