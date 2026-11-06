"""Record service: create, read, update, and retire records of any object.

All writes are validated against the current metadata snapshot, run in a single
transaction together with their history and event rows, and are attributed to
the acting principal.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import uuid
from decimal import Decimal
from typing import Any, Literal

import psycopg
from psycopg import errors as pg_errors
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, create_model
from pydantic import ValidationError as PydanticValidationError

from .context import Principal, Tenant
from .db import TenantConnection
from .errors import (
    ApprovalError,
    ConflictError,
    IntegrityError,
    NotFoundError,
    OsierError,
    RecordValidationError,
)
from .fieldtypes import REGISTRY, FieldType, OnDelete
from .identifiers import SYSTEM_COLUMNS
from .metadata import FieldDef, MetadataRegistry, ObjectDef, SchemaSnapshot

MAX_LIST_LIMIT = 200
MAX_CASCADE_DEPTH = 10
_SYSTEM_OUTPUT = (
    "id",
    "created_at",
    "created_by",
    "updated_at",
    "updated_by",
    "owner_id",
    "row_version",
    "retired_at",
)


class Provenance(BaseModel):
    """Where a value came from. Stored with history and events."""

    model_config = ConfigDict(extra="forbid")

    source_type: Literal[
        "user_entered", "agent_extraction", "agent_inference", "integration", "import", "system"
    ]
    source_ref: str | None = Field(default=None, max_length=500)
    confidence: float | None = Field(default=None, ge=0, le=1)
    verified: bool = False
    notes: str | None = Field(default=None, max_length=1000)


def to_jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dt.datetime | dt.date):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    return value


# --------------------------------------------------------------------------------------
# Validators generated from metadata
# --------------------------------------------------------------------------------------


def _validator(snapshot: SchemaSnapshot, obj: ObjectDef, mode: str) -> type[BaseModel]:
    key = (mode, obj.api_name)
    cached = snapshot._validator_cache.get(key)
    if cached is not None:
        return cached
    definitions: dict[str, Any] = {"owner_id": (uuid.UUID | None, None)}
    for f in obj.active_fields.values():
        python_type = REGISTRY[f.type].python_type(f.options)
        if mode == "create" and f.required and f.default is None:
            definitions[f.api_name] = (python_type, ...)
        elif f.required:
            # May be omitted (default applies / unchanged on update) but never set to null.
            definitions[f.api_name] = (python_type, None)
        else:
            definitions[f.api_name] = (python_type | None, None)
    model = create_model(  # type: ignore[call-overload]
        f"{obj.api_name}_{mode}",
        __config__=ConfigDict(extra="forbid"),
        **definitions,
    )
    snapshot._validator_cache[key] = model
    return model


def _validation_error(
    exc: PydanticValidationError, snapshot: SchemaSnapshot, obj: ObjectDef
) -> RecordValidationError:
    problems = []
    for err in exc.errors(include_url=False):
        name = str(err["loc"][0]) if err["loc"] else None
        f = obj.fields.get(name) if name else None
        if err["type"] == "extra_forbidden":
            if f is not None and not f.active:
                problem = {"field": name, "code": "FIELD_RETIRED", "message": "field is retired"}
            elif name in SYSTEM_COLUMNS:
                problem = {
                    "field": name,
                    "code": "READ_ONLY_FIELD",
                    "message": "system field; set by the platform",
                }
            else:
                problem = {
                    "field": name,
                    "code": "UNKNOWN_FIELD",
                    "message": "no such field",
                    "expected": sorted(obj.active_fields),
                }
        elif err["type"] == "missing":
            problem = {"field": name, "code": "REQUIRED_FIELD_MISSING", "message": "required"}
        else:
            problem = {
                "field": name,
                "code": "INVALID_VALUE",
                "message": err["msg"],
                "expected": f.describe_expected(snapshot) if f else None,
            }
        problems.append(problem)
    first = problems[0]
    return RecordValidationError(
        f"{len(problems)} invalid value(s) for {obj.api_name!r}.",
        field=first.get("field"),
        expected=first.get("expected"),
        details={"errors": problems},
        hint="Fix the listed fields and send the request again. Call describe_schema for "
        "field types and allowed values.",
    )


# --------------------------------------------------------------------------------------
# Service
# --------------------------------------------------------------------------------------


class RecordService:
    def __init__(self, conn: TenantConnection, tenant: Tenant, principal: Principal) -> None:
        self._conn = conn
        self._tenant = tenant
        self._principal = principal
        self._registry = MetadataRegistry(conn, tenant)

    # ---- helpers -------------------------------------------------------------------

    def _engine_table(self, name: str) -> sql.Identifier:
        return sql.Identifier(self._tenant.schema, name)

    def _validate(
        self, snapshot: SchemaSnapshot, obj: ObjectDef, values: Any, mode: str
    ) -> dict[str, Any]:
        if not isinstance(values, dict):
            raise RecordValidationError("Record values must be an object (field: value).")
        model = _validator(snapshot, obj, mode)
        try:
            validated = model.model_validate(values)
        except PydanticValidationError as exc:
            raise _validation_error(exc, snapshot, obj) from None
        return {k: getattr(validated, k) for k in validated.model_fields_set}

    def _check_lookups(
        self, snapshot: SchemaSnapshot, obj: ObjectDef, data: dict[str, Any]
    ) -> None:
        for name, value in data.items():
            f = obj.fields.get(name)
            if f is None or f.type is not FieldType.LOOKUP or value is None:
                continue
            target = snapshot.objects_by_id[f.target_object_id]  # type: ignore[index]
            row = self._conn.execute(
                sql.SQL("SELECT retired_at FROM {} WHERE id = %s").format(snapshot.table(target)),
                (value,),
            ).fetchone()
            if row is None or row[0] is not None:
                raise IntegrityError(
                    f"No active {target.api_name!r} record with id {value}.",
                    code="LOOKUP_TARGET_NOT_FOUND",
                    field=name,
                    expected=f.describe_expected(snapshot),
                )

    def _check_unique(
        self,
        snapshot: SchemaSnapshot,
        obj: ObjectDef,
        data: dict[str, Any],
        exclude_id: uuid.UUID | None = None,
    ) -> None:
        # The unique index is the real guarantee; this check gives a precise error and
        # covers the window while a new unique index is still being built.
        for name, value in data.items():
            f = obj.fields.get(name)
            if f is None or not f.unique or value is None:
                continue
            query = sql.SQL("SELECT id FROM {} WHERE {} = %s").format(
                snapshot.table(obj), sql.Identifier(name)
            )
            params: tuple[Any, ...] = (value,)
            if exclude_id is not None:
                query += sql.SQL(" AND id <> %s")
                params = (value, exclude_id)
            row = self._conn.execute(query + sql.SQL(" LIMIT 1"), params).fetchone()
            if row is not None:
                raise _unique_error(obj, f, existing_id=row[0])

    def _map_db_error(
        self, exc: psycopg.Error, snapshot: SchemaSnapshot, obj: ObjectDef
    ) -> OsierError:
        diag = exc.diag
        if isinstance(exc, pg_errors.UniqueViolation) and diag.constraint_name:
            f = _field_for_constraint(obj, diag.constraint_name)
            if f is not None:
                return _unique_error(obj, f)
            if diag.constraint_name.startswith("uq_"):
                return IntegrityError(
                    "Another record already has this combination of values.",
                    code="UNIQUE_VIOLATION",
                )
        if isinstance(exc, pg_errors.ForeignKeyViolation):
            return IntegrityError(
                "A lookup refers to a record that does not exist.", code="LOOKUP_TARGET_NOT_FOUND"
            )
        if isinstance(exc, pg_errors.NotNullViolation) and diag.column_name:
            return RecordValidationError(
                f"Field {diag.column_name!r} is required.",
                code="REQUIRED_FIELD_MISSING",
                field=diag.column_name,
            )
        if isinstance(exc, pg_errors.SerializationFailure | pg_errors.DeadlockDetected):
            return ConflictError("The write conflicted with another write.", retryable=True)
        return OsierError("The database rejected the write.", code="DATABASE_ERROR")

    def _output(self, obj: ObjectDef, row: dict[str, Any]) -> dict[str, Any]:
        result = {k: to_jsonable(row[k]) for k in _SYSTEM_OUTPUT}
        for name in obj.active_fields:
            result[name] = to_jsonable(row.get(name))
        result["object"] = obj.api_name
        return result

    def _fetch(
        self, snapshot: SchemaSnapshot, obj: ObjectDef, record_id: Any, *, lock: bool = False
    ) -> dict[str, Any]:
        try:
            rid = uuid.UUID(str(record_id))
        except ValueError:
            raise RecordValidationError(
                f"{record_id!r} is not a valid record id.", field="id", expected="UUID"
            ) from None
        query = sql.SQL("SELECT * FROM {} WHERE id = %s").format(snapshot.table(obj))
        if lock:
            query += sql.SQL(" FOR UPDATE")
        with self._conn.cursor(row_factory=dict_row) as cur:
            row = cur.execute(query, (rid,)).fetchone()
        if row is None:
            raise NotFoundError(
                f"No {obj.api_name!r} record with id {rid}.", code="RECORD_NOT_FOUND", field="id"
            )
        return row

    def _actor_columns(self) -> tuple[Any, ...]:
        p = self._principal
        return (p.user_id, p.actor_type.value, p.on_behalf_of, p.agent_id, p.source)

    def _write_event(
        self,
        obj: ObjectDef,
        record_id: uuid.UUID,
        event: str,
        request_id: uuid.UUID | None,
        provenance: Provenance | None,
    ) -> None:
        self._conn.execute(
            sql.SQL(
                "INSERT INTO {} (object_id, record_id, event, actor_id, actor_type, on_behalf_of, "
                "agent_id, source, request_id, provenance) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
            ).format(self._engine_table("record_events")),
            (
                obj.id,
                record_id,
                event,
                *self._actor_columns(),
                request_id,
                Jsonb(provenance.model_dump(mode="json")) if provenance else None,
            ),
        )

    def _write_history(
        self,
        obj: ObjectDef,
        record_id: uuid.UUID,
        changes: list[tuple[FieldDef, Any, Any]],
        request_id: uuid.UUID | None,
        provenance: Provenance | None,
    ) -> None:
        tracked = [(f, old, new) for f, old, new in changes if f.track_history]
        if not tracked:
            return
        prov = Jsonb(provenance.model_dump(mode="json")) if provenance else None
        with self._conn.cursor() as cur:
            cur.executemany(
                sql.SQL(
                    "INSERT INTO {} (object_id, record_id, field_id, old_value, new_value, "
                    "actor_id, actor_type, on_behalf_of, agent_id, source, request_id, provenance) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
                ).format(self._engine_table("field_history")),
                [
                    (
                        obj.id,
                        record_id,
                        f.id,
                        Jsonb(to_jsonable(old)),
                        Jsonb(to_jsonable(new)),
                        *self._actor_columns(),
                        request_id,
                        prov,
                    )
                    for f, old, new in tracked
                ],
            )

    @staticmethod
    def _provenance(value: Any) -> Provenance | None:
        if value is None or isinstance(value, Provenance):
            return value
        try:
            return Provenance.model_validate(value)
        except PydanticValidationError as exc:
            raise RecordValidationError(
                "Invalid provenance.",
                field="provenance",
                details={"errors": [e["msg"] for e in exc.errors(include_url=False)]},
            ) from None

    def _guard_protected(self, obj: ObjectDef) -> None:
        if obj.protected and not (
            self._principal.is_human or self._principal.actor_type.value == "system"
        ):
            raise ApprovalError(
                f"{obj.label} records control access and can only be changed by a person.",
                code="AGENT_WRITE_NOT_ALLOWED",
                hint="Ask the user to make this change, or propose it for their approval.",
            )

    def _check_unique_together(
        self,
        snapshot: SchemaSnapshot,
        obj: ObjectDef,
        merged: dict[str, Any],
        exclude_id: uuid.UUID | None = None,
    ) -> None:
        for combo in obj.unique_together:
            values = [merged.get(f.api_name) for f in combo]
            if any(v is None for v in values):
                continue  # NULLs never conflict in a unique index
            conditions = sql.SQL(" AND ").join(
                sql.SQL("{} = %s").format(sql.Identifier(f.api_name)) for f in combo
            )
            query = sql.SQL("SELECT id FROM {} WHERE ").format(snapshot.table(obj)) + conditions
            params: list[Any] = list(values)
            if exclude_id is not None:
                query += sql.SQL(" AND id <> %s")
                params.append(exclude_id)
            row = self._conn.execute(query + sql.SQL(" LIMIT 1"), params).fetchone()
            if row is not None:
                raise _unique_together_error(obj, combo, row[0])

    # ---- public API ----------------------------------------------------------------

    def create(
        self,
        object: str,
        values: dict[str, Any],
        *,
        idempotency_key: str | None = None,
        provenance: Provenance | dict[str, Any] | None = None,
        request_id: uuid.UUID | None = None,
    ) -> dict[str, Any]:
        snapshot = self._registry.snapshot()
        obj = snapshot.get_object(object)
        self._guard_protected(obj)
        prov = self._provenance(provenance)
        data = self._validate(snapshot, obj, values, "create")
        owner_id = (
            data.pop("owner_id", None) or self._principal.on_behalf_of or self._principal.user_id
        )
        request_hash = _request_hash(obj.api_name, data, owner_id)

        try:
            with self._conn.transaction():
                if idempotency_key is not None:
                    existing = self._claim_idempotency_key(obj, idempotency_key, request_hash)
                    if existing is not None:
                        return self._output(obj, self._fetch(snapshot, obj, existing))
                self._check_lookups(snapshot, obj, data)
                self._check_unique(snapshot, obj, data)
                self._check_unique_together(snapshot, obj, data)
                columns = [*data, "created_by", "updated_by", "owner_id"]
                params = [
                    *data.values(),
                    self._principal.user_id,
                    self._principal.user_id,
                    owner_id,
                ]
                with self._conn.cursor(row_factory=dict_row) as cur:
                    row = cur.execute(
                        sql.SQL("INSERT INTO {} ({}) VALUES ({}) RETURNING *").format(
                            snapshot.table(obj),
                            sql.SQL(", ").join(sql.Identifier(c) for c in columns),
                            sql.SQL(", ").join(sql.Placeholder() for _ in columns),
                        ),
                        params,
                    ).fetchone()
                assert row is not None
                self._write_event(obj, row["id"], "created", request_id, prov)
                if idempotency_key is not None:
                    self._conn.execute(
                        sql.SQL("UPDATE {} SET record_id = %s WHERE key = %s").format(
                            self._engine_table("meta_idempotency")
                        ),
                        (row["id"], idempotency_key),
                    )
        except psycopg.Error as exc:
            raise self._map_db_error(exc, snapshot, obj) from exc
        return self._output(obj, row)

    def _claim_idempotency_key(
        self, obj: ObjectDef, key: str, request_hash: str
    ) -> uuid.UUID | None:
        if not 1 <= len(key) <= 200:
            raise RecordValidationError(
                "Idempotency keys must be 1-200 characters.", field="idempotency_key"
            )
        table = self._engine_table("meta_idempotency")
        inserted = self._conn.execute(
            sql.SQL(
                "INSERT INTO {} (key, object_id, request_hash) VALUES (%s, %s, %s) "
                "ON CONFLICT (key) DO NOTHING RETURNING key"
            ).format(table),
            (key, obj.id, request_hash),
        ).fetchone()
        if inserted is not None:
            return None
        row = self._conn.execute(
            sql.SQL("SELECT object_id, request_hash, record_id FROM {} WHERE key = %s").format(
                table
            ),
            (key,),
        ).fetchone()
        assert row is not None
        if row[0] != obj.id or row[1] != request_hash:
            raise IntegrityError(
                "This idempotency key was already used for a different request.",
                code="IDEMPOTENCY_KEY_REUSED",
                field="idempotency_key",
                hint="Use a new key for a new request.",
            )
        return row[2]

    def get(self, object: str, record_id: Any, *, include_retired: bool = False) -> dict[str, Any]:
        snapshot = self._registry.snapshot()
        obj = snapshot.get_object(object)
        row = self._fetch(snapshot, obj, record_id)
        if row["retired_at"] is not None and not include_retired:
            raise NotFoundError(
                f"{obj.api_name!r} record {row['id']} is retired.",
                code="RECORD_RETIRED",
                field="id",
                hint="Pass include_retired to read retired records.",
            )
        return self._output(obj, row)

    def list(
        self,
        object: str,
        *,
        where: dict[str, Any] | None = None,
        limit: int = 50,
        include_retired: bool = False,
    ) -> list[dict[str, Any]]:
        """Simple equality filtering. The full structured query language comes in M2."""
        snapshot = self._registry.snapshot()
        obj = snapshot.get_object(object)
        if not 1 <= limit <= MAX_LIST_LIMIT:
            raise RecordValidationError(
                f"limit must be between 1 and {MAX_LIST_LIMIT}.", field="limit"
            )
        conditions: list[sql.Composable] = []
        params: list[Any] = []
        for name, value in (where or {}).items():
            if name == "id" or name == "owner_id":
                python_type: Any = uuid.UUID
            else:
                f = obj.active_fields.get(name)
                if f is None:
                    raise RecordValidationError(
                        f"Unknown field {name!r}.",
                        code="UNKNOWN_FIELD",
                        field=name,
                        expected=sorted(obj.active_fields),
                    )
                python_type = REGISTRY[f.type].python_type(f.options)
            if value is None:
                conditions.append(sql.SQL("{} IS NULL").format(sql.Identifier(name)))
                continue
            try:
                params.append(TypeAdapter(python_type).validate_python(value))
            except PydanticValidationError:
                raise RecordValidationError(
                    f"Invalid filter value for {name!r}.", code="INVALID_VALUE", field=name
                ) from None
            conditions.append(sql.SQL("{} = %s").format(sql.Identifier(name)))
        if not include_retired:
            conditions.append(sql.SQL("retired_at IS NULL"))
        query = sql.SQL("SELECT * FROM {}").format(snapshot.table(obj))
        if conditions:
            query += sql.SQL(" WHERE ") + sql.SQL(" AND ").join(conditions)
        query += sql.SQL(" ORDER BY created_at, id LIMIT {}").format(sql.Literal(limit))
        with self._conn.cursor(row_factory=dict_row) as cur:
            rows = cur.execute(query, params).fetchall()
        return [self._output(obj, r) for r in rows]

    def update(
        self,
        object: str,
        record_id: Any,
        values: dict[str, Any],
        *,
        expected_row_version: int,
        provenance: Provenance | dict[str, Any] | None = None,
        request_id: uuid.UUID | None = None,
    ) -> dict[str, Any]:
        snapshot = self._registry.snapshot()
        obj = snapshot.get_object(object)
        self._guard_protected(obj)
        prov = self._provenance(provenance)
        data = self._validate(snapshot, obj, values, "update")
        if not data:
            raise RecordValidationError("No fields to update.", code="NO_CHANGES")
        try:
            with self._conn.transaction():
                current = self._fetch(snapshot, obj, record_id, lock=True)
                if current["retired_at"] is not None:
                    raise IntegrityError(
                        "Retired records cannot be updated.", code="RECORD_RETIRED", field="id"
                    )
                if current["row_version"] != expected_row_version:
                    raise ConflictError(
                        "The record was changed by someone else since you read it.",
                        field="row_version",
                        expected=current["row_version"],
                        hint="Read the record again, re-apply your change, and send it with the "
                        "new row_version.",
                        details={"current_row_version": current["row_version"]},
                    )
                changed = {k: v for k, v in data.items() if current[k] != v}
                if not changed:
                    return self._output(obj, current)
                self._check_lookups(snapshot, obj, changed)
                self._check_unique(snapshot, obj, changed, exclude_id=current["id"])
                self._check_unique_together(
                    snapshot, obj, {**current, **changed}, exclude_id=current["id"]
                )
                assignments = [sql.SQL("{} = %s").format(sql.Identifier(k)) for k in changed] + [
                    sql.SQL("updated_at = now()"),
                    sql.SQL("updated_by = %s"),
                    sql.SQL("row_version = row_version + 1"),
                ]
                with self._conn.cursor(row_factory=dict_row) as cur:
                    row = cur.execute(
                        sql.SQL("UPDATE {} SET {} WHERE id = %s RETURNING *").format(
                            snapshot.table(obj), sql.SQL(", ").join(assignments)
                        ),
                        [*changed.values(), self._principal.user_id, current["id"]],
                    ).fetchone()
                assert row is not None
                history = [(obj.fields[k], current[k], row[k]) for k in changed if k in obj.fields]
                self._write_history(obj, row["id"], history, request_id, prov)
        except psycopg.Error as exc:
            raise self._map_db_error(exc, snapshot, obj) from exc
        return self._output(obj, row)

    def retire(
        self,
        object: str,
        record_id: Any,
        *,
        expected_row_version: int,
        request_id: uuid.UUID | None = None,
    ) -> dict[str, Any]:
        """Soft-delete a record, applying each incoming lookup's on_delete rule."""
        snapshot = self._registry.snapshot()
        obj = snapshot.get_object(object)
        self._guard_protected(obj)
        report: dict[str, list[Any]] = {"retired": [], "cleared": []}
        try:
            with self._conn.transaction():
                current = self._fetch(snapshot, obj, record_id, lock=True)
                if current["retired_at"] is not None:
                    raise IntegrityError(
                        "The record is already retired.", code="RECORD_RETIRED", field="id"
                    )
                if current["row_version"] != expected_row_version:
                    raise ConflictError(
                        "The record was changed by someone else since you read it.",
                        field="row_version",
                        expected=current["row_version"],
                        details={"current_row_version": current["row_version"]},
                    )
                self._retire_tree(snapshot, obj, [current["id"]], request_id, report, set(), 0)
        except psycopg.Error as exc:
            raise self._map_db_error(exc, snapshot, obj) from exc
        return report

    def _retire_tree(
        self,
        snapshot: SchemaSnapshot,
        obj: ObjectDef,
        ids: list[uuid.UUID],
        request_id: uuid.UUID | None,
        report: dict[str, list[Any]],
        visited: set[uuid.UUID],
        depth: int,
    ) -> None:
        ids = [i for i in ids if i not in visited]
        if not ids:
            return
        if depth > MAX_CASCADE_DEPTH:
            raise IntegrityError(
                "Cascade too deep; retire child records first.", code="CASCADE_TOO_DEEP"
            )
        visited.update(ids)
        for child_obj, f in snapshot.lookups_targeting(obj):
            column = sql.Identifier(f.api_name)
            children = [
                r[0]
                for r in self._conn.execute(
                    sql.SQL(
                        "SELECT id FROM {} WHERE {} = ANY(%s) AND retired_at IS NULL "
                        "AND NOT (id = ANY(%s)) FOR UPDATE"
                    ).format(snapshot.table(child_obj), column),
                    (ids, list(visited)),
                ).fetchall()
            ]
            if not children:
                continue
            rule = f.options["on_delete"]
            if rule == OnDelete.RESTRICT.value:
                raise IntegrityError(
                    f"{len(children)} active {child_obj.api_name!r} record(s) still refer to this "
                    f"record through {f.api_name!r}.",
                    code="RECORD_HAS_DEPENDENTS",
                    details={
                        "object": child_obj.api_name,
                        "field": f.api_name,
                        "count": len(children),
                    },
                    hint=f"Retire or reassign those {child_obj.plural_label} first.",
                )
            if rule == OnDelete.SET_NULL.value:
                with self._conn.cursor() as cur:
                    old = dict(
                        cur.execute(
                            sql.SQL("SELECT id, {} FROM {} WHERE id = ANY(%s)").format(
                                column, snapshot.table(child_obj)
                            ),
                            (children,),
                        ).fetchall()
                    )
                self._conn.execute(
                    sql.SQL(
                        "UPDATE {} SET {} = NULL, updated_at = now(), updated_by = %s, "
                        "row_version = row_version + 1 WHERE id = ANY(%s)"
                    ).format(snapshot.table(child_obj), column),
                    (self._principal.user_id, children),
                )
                for child_id in children:
                    self._write_history(
                        child_obj, child_id, [(f, old[child_id], None)], request_id, None
                    )
                report["cleared"].append(
                    {"object": child_obj.api_name, "field": f.api_name, "count": len(children)}
                )
            elif rule == OnDelete.CASCADE.value:
                self._retire_tree(
                    snapshot, child_obj, children, request_id, report, visited, depth + 1
                )
        self._conn.execute(
            sql.SQL(
                "UPDATE {} SET retired_at = now(), retired_by = %s, updated_at = now(), "
                "updated_by = %s, row_version = row_version + 1 WHERE id = ANY(%s)"
            ).format(snapshot.table(obj)),
            (self._principal.user_id, self._principal.user_id, ids),
        )
        for rid in ids:
            self._write_event(obj, rid, "retired", request_id, None)
            report["retired"].append({"object": obj.api_name, "id": str(rid)})

    def history(self, object: str, record_id: Any) -> list[dict[str, Any]]:
        snapshot = self._registry.snapshot()
        obj = snapshot.get_object(object)
        row = self._fetch(snapshot, obj, record_id)
        with self._conn.cursor(row_factory=dict_row) as cur:
            rows = cur.execute(
                sql.SQL("SELECT * FROM {} WHERE record_id = %s ORDER BY changed_at, id").format(
                    self._engine_table("field_history")
                ),
                (row["id"],),
            ).fetchall()
        result = []
        for r in rows:
            f = obj.fields_by_id.get(r["field_id"])
            result.append(
                {
                    "field": f.api_name if f else str(r["field_id"]),
                    "old_value": r["old_value"],
                    "new_value": r["new_value"],
                    "actor_id": str(r["actor_id"]),
                    "actor_type": r["actor_type"],
                    "on_behalf_of": to_jsonable(r["on_behalf_of"]),
                    "agent_id": r["agent_id"],
                    "source": r["source"],
                    "request_id": to_jsonable(r["request_id"]),
                    "provenance": r["provenance"],
                    "changed_at": r["changed_at"].isoformat(),
                }
            )
        return result


def _field_for_constraint(obj: ObjectDef, constraint: str) -> FieldDef | None:
    for f in obj.fields.values():
        if constraint.endswith(f.id.hex):
            return f
    return None


def _unique_error(obj: ObjectDef, f: FieldDef, existing_id: Any = None) -> IntegrityError:
    return IntegrityError(
        f"Another {obj.api_name!r} record already has this {f.api_name!r}.",
        code="UNIQUE_VIOLATION",
        field=f.api_name,
        details={"existing_record_id": str(existing_id)} if existing_id else {},
        hint="Use a different value, or update the existing record instead.",
    )


def _unique_together_error(
    obj: ObjectDef, combo: list[FieldDef], existing_id: Any
) -> IntegrityError:
    names = [f.api_name for f in combo]
    return IntegrityError(
        f"Another {obj.api_name!r} record already has this combination of {names}.",
        code="UNIQUE_VIOLATION",
        field=",".join(names),
        details={"fields": names, "existing_record_id": str(existing_id)},
        hint="Use a different combination, or update the existing record instead.",
    )


def _request_hash(object_name: str, data: dict[str, Any], owner_id: Any) -> str:
    payload = json.dumps(
        {
            "object": object_name,
            "data": {k: to_jsonable(v) for k, v in data.items()},
            "owner": str(owner_id),
        },
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode()).hexdigest()
