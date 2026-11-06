"""Schema change sets: propose -> validate -> impact analysis -> approve -> apply.

All schema changes (by tenants through AI, or by the platform) are expressed as a
list of structured operations. Nothing here accepts SQL from callers: the planner
compiles operations to SQL using only validated identifiers and bound literals.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal

import psycopg
from psycopg import errors as pg_errors
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter
from pydantic import ValidationError as PydanticValidationError

from .context import ActorType, Principal, Tenant
from .errors import ApprovalError, InvalidNameError, NotFoundError, SchemaChangeError
from .fieldtypes import (
    REGISTRY,
    FieldType,
    OnDelete,
    normalize_picklist_values,
    parse_field_type,
)
from .identifiers import Owner, index_name, validate_api_name
from .metadata import MetadataRegistry, SchemaSnapshot

LOCK_TIMEOUT = "5s"
STATEMENT_TIMEOUT = "60s"

# --------------------------------------------------------------------------------------
# Operations (the only input format for schema changes)
# --------------------------------------------------------------------------------------


class _Op(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FieldSpec(_Op):
    api_name: str
    label: str = Field(min_length=1, max_length=200)
    type: str
    description: str | None = Field(default=None, max_length=2000)
    required: bool = False
    unique: bool = False
    indexed: bool = False
    track_history: bool = False
    default: Any = None
    options: dict[str, Any] = Field(default_factory=dict)


class CreateObject(_Op):
    op: Literal["create_object"]
    api_name: str
    label: str = Field(min_length=1, max_length=200)
    plural_label: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    fields: list[FieldSpec] = Field(default_factory=list)


class UpdateObject(_Op):
    op: Literal["update_object"]
    object: str
    label: str | None = Field(default=None, min_length=1, max_length=200)
    plural_label: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)


class RetireObject(_Op):
    op: Literal["retire_object"]
    object: str


class AddField(_Op):
    op: Literal["add_field"]
    object: str
    field: FieldSpec


class UpdateField(_Op):
    op: Literal["update_field"]
    object: str
    field: str
    label: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    required: bool | None = None
    track_history: bool | None = None
    default: Any = None  # only applied if explicitly provided; null removes the default
    add_picklist_values: list[Any] | None = None
    deactivate_picklist_values: list[str] | None = None


class RetireField(_Op):
    op: Literal["retire_field"]
    object: str
    field: str


Operation = Annotated[
    CreateObject | UpdateObject | RetireObject | AddField | UpdateField | RetireField,
    Field(discriminator="op"),
]
_OPERATIONS_ADAPTER = TypeAdapter(list[Operation])


def parse_operations(raw: Any) -> list[Any]:
    try:
        ops = _OPERATIONS_ADAPTER.validate_python(raw)
    except PydanticValidationError as exc:
        raise SchemaChangeError(
            "The change set is not well-formed.",
            code="CHANGE_SET_MALFORMED",
            details={
                "errors": [
                    {"location": ".".join(str(p) for p in e["loc"]), "message": e["msg"]}
                    for e in exc.errors(include_url=False)
                ]
            },
            hint="Each operation needs an 'op' of: create_object, update_object, "
            "retire_object, add_field, update_field, retire_field.",
        ) from None
    if not ops:
        raise SchemaChangeError(
            "A change set needs at least one operation.", code="CHANGE_SET_EMPTY"
        )
    return ops


# --------------------------------------------------------------------------------------
# Planning
# --------------------------------------------------------------------------------------


@dataclass
class _WField:
    id: uuid.UUID
    api_name: str
    label: str
    description: str | None
    type: FieldType
    required: bool
    unique: bool
    indexed: bool
    track_history: bool
    default: Any
    options: dict[str, Any]
    owner: Owner
    status: str
    position: int
    is_new: bool = False


@dataclass
class _WObject:
    id: uuid.UUID
    api_name: str
    label: str
    plural_label: str
    description: str | None
    owner: Owner
    status: str
    fields: dict[str, _WField]
    is_new: bool = False


@dataclass
class _PostStep:
    """Work that must run after the transaction commits (e.g. concurrent index builds)."""

    statement: sql.Composable
    field_id: uuid.UUID
    index: str
    description: str


@dataclass
class Plan:
    statements: list[tuple[sql.Composable, Any]] = field(default_factory=list)
    ddl: list[sql.Composable] = field(default_factory=list)
    post_commit: list[_PostStep] = field(default_factory=list)
    summary: list[str] = field(default_factory=list)
    impact: list[dict[str, Any]] = field(default_factory=list)
    blockers: list[dict[str, Any]] = field(default_factory=list)

    def run(self, statement: sql.Composable, params: Any = None, *, ddl: bool = False) -> None:
        self.statements.append((statement, params))
        if ddl:
            self.ddl.append(statement)


_ON_DELETE_SQL = {
    OnDelete.RESTRICT.value: sql.SQL("RESTRICT"),
    OnDelete.CASCADE.value: sql.SQL("CASCADE"),
    OnDelete.SET_NULL.value: sql.SQL("SET NULL"),
}


class _Planner:
    def __init__(
        self,
        conn: psycopg.Connection,
        snapshot: SchemaSnapshot,
        change_set_id: uuid.UUID,
        source: str,
    ) -> None:
        self.conn = conn
        self.snapshot = snapshot
        self.schema = snapshot.tenant.schema
        self.change_set_id = change_set_id
        self.tenant_change = source == "tenant"
        self.owner = Owner.CUSTOM if self.tenant_change else Owner.STANDARD
        self.plan = Plan()
        self.objects: dict[str, _WObject] = {
            name: _WObject(
                id=o.id,
                api_name=o.api_name,
                label=o.label,
                plural_label=o.plural_label,
                description=o.description,
                owner=o.owner,
                status=o.status,
                fields={
                    fname: _WField(
                        id=f.id,
                        api_name=f.api_name,
                        label=f.label,
                        description=f.description,
                        type=f.type,
                        required=f.required,
                        unique=f.unique,
                        indexed=f.indexed,
                        track_history=f.track_history,
                        default=f.default,
                        options=dict(f.options),
                        owner=f.owner,
                        status=f.status,
                        position=f.position,
                    )
                    for fname, f in o.fields.items()
                },
            )
            for name, o in snapshot.objects.items()
        }

    # ---- helpers -------------------------------------------------------------------

    def _id(self, *parts: Any) -> uuid.UUID:
        """Deterministic ids so re-planning at apply time yields identical results."""
        return uuid.uuid5(self.change_set_id, ":".join(str(p) for p in parts))

    def _t(self, name: str) -> sql.Identifier:
        return sql.Identifier(self.schema, name)

    def _meta(self, name: str) -> sql.Identifier:
        return sql.Identifier(self.schema, name)

    def _object(self, name: str, op_index: int) -> _WObject:
        obj = self.objects.get(name)
        if obj is None or obj.status != "active":
            raise SchemaChangeError(
                f"Operation {op_index}: object {name!r} does not exist"
                + (" (it is retired)." if obj else "."),
                code="OBJECT_NOT_FOUND",
                field="object",
                expected=sorted(o.api_name for o in self.objects.values() if o.status == "active"),
            )
        return obj

    def _field(self, obj: _WObject, name: str, op_index: int) -> _WField:
        f = obj.fields.get(name)
        if f is None or f.status != "active":
            raise SchemaChangeError(
                f"Operation {op_index}: field {name!r} does not exist on {obj.api_name!r}"
                + (" (it is retired)." if f else "."),
                code="FIELD_NOT_FOUND",
                field="field",
                expected=sorted(k for k, v in obj.fields.items() if v.status == "active"),
            )
        return f

    def _protect_standard(self, owner: Owner, what: str, op_index: int) -> None:
        if self.tenant_change and owner is Owner.STANDARD:
            raise SchemaChangeError(
                f"Operation {op_index}: {what} is a standard (platform-owned) item and cannot "
                "be changed this way.",
                code="STANDARD_ITEM_PROTECTED",
                hint="You can add custom fields (prefixed 'c_') to standard objects, and change "
                "labels, help text, picklist values, required, and history tracking.",
            )

    def _row_count(self, obj: _WObject, where: sql.Composable | None = None) -> int:
        if obj.is_new:
            return 0
        query = sql.SQL("SELECT count(*) FROM {}").format(self._t(obj.api_name))
        if where is not None:
            query += sql.SQL(" WHERE ") + where
        row = self.conn.execute(query).fetchone()
        assert row is not None
        return int(row[0])

    def _blocker(self, message: str, **extra: Any) -> None:
        self.plan.blockers.append({"severity": "blocker", "message": message, **extra})

    def _info(self, message: str, **extra: Any) -> None:
        self.plan.impact.append({"severity": "info", "message": message, **extra})

    # ---- field specs ---------------------------------------------------------------

    def _build_field(self, obj: _WObject, spec: FieldSpec, op_index: int) -> _WField:
        where = f"Operation {op_index}"
        try:
            name = validate_api_name(spec.api_name, kind="field", owner=self.owner)
        except InvalidNameError as exc:
            exc.message = f"{where}: {exc.message}"
            exc.args = (exc.message,)
            raise
        if name in obj.fields:
            raise SchemaChangeError(
                f"{where}: field {name!r} already exists on {obj.api_name!r}"
                + (
                    " (retired; names cannot be reused)."
                    if obj.fields[name].status != "active"
                    else "."
                ),
                code="FIELD_EXISTS",
                field="api_name",
            )
        ftype = parse_field_type(spec.type)
        type_spec = REGISTRY[ftype]
        options = type_spec.normalize_options(dict(spec.options))

        if ftype is FieldType.LOOKUP:
            target_name = options.pop("target", None)
            if target_name is not None:
                target = self.objects.get(target_name)
                if target is None or target.status != "active":
                    raise SchemaChangeError(
                        f"{where}: lookup target {target_name!r} does not exist.",
                        code="OBJECT_NOT_FOUND",
                        field="options.target",
                        expected=sorted(
                            o.api_name for o in self.objects.values() if o.status == "active"
                        ),
                    )
                options["target_object_id"] = str(target.id)
            if spec.default is not None:
                raise SchemaChangeError(f"{where}: lookup fields cannot have a default.")
            if spec.unique:
                raise SchemaChangeError(
                    f"{where}: unique lookups (one-to-one) are not supported yet.",
                    code="NOT_SUPPORTED",
                )

        if (spec.unique or spec.indexed) and not type_spec.indexable:
            raise SchemaChangeError(
                f"{where}: fields of type {ftype.value!r} cannot be unique or indexed.",
                field="unique",
            )

        default = None
        if spec.default is not None:
            default = _validate_default(ftype, options, spec.default, where)

        position = max((f.position for f in obj.fields.values()), default=0) + 1
        return _WField(
            id=self._id("field", obj.api_name, name),
            api_name=name,
            label=spec.label,
            description=spec.description,
            type=ftype,
            required=spec.required,
            unique=spec.unique,
            indexed=spec.indexed or spec.unique or ftype is FieldType.LOOKUP,
            track_history=spec.track_history,
            default=default,
            options=options,
            owner=self.owner,
            status="active",
            position=position,
            is_new=True,
        )

    def _column_def(self, f: _WField, *, include_not_null: bool) -> sql.Composable:
        parts: list[sql.Composable] = [
            sql.Identifier(f.api_name),
            REGISTRY[f.type].column_type(f.options),
        ]
        if include_not_null and f.required:
            parts.append(sql.SQL("NOT NULL"))
        if f.default is not None:
            parts.append(sql.SQL("DEFAULT {}").format(_default_literal(f)))
        if f.type is FieldType.LOOKUP:
            target = next(
                o for o in self.objects.values() if str(o.id) == f.options["target_object_id"]
            )
            parts.append(
                sql.SQL("CONSTRAINT {} REFERENCES {} (id) ON DELETE {}").format(
                    sql.Identifier(index_name("fk", f.id)),
                    self._t(target.api_name),
                    _ON_DELETE_SQL[f.options["on_delete"]],
                )
            )
        return sql.SQL(" ").join(parts)

    def _index_statement(
        self, obj: _WObject, f: _WField, *, concurrently: bool
    ) -> tuple[str, sql.Composable]:
        name = index_name("ux" if f.unique else "ix", f.id)
        statement = sql.SQL("CREATE {unique}INDEX {conc}{name} ON {table} ({column})").format(
            unique=sql.SQL("UNIQUE " if f.unique else ""),
            conc=sql.SQL("CONCURRENTLY " if concurrently else ""),
            name=sql.Identifier(name),
            table=self._t(obj.api_name),
            column=sql.Identifier(f.api_name),
        )
        return name, statement

    def _insert_field_meta(self, obj: _WObject, f: _WField, index_status: str | None) -> None:
        self.plan.run(
            sql.SQL(
                "INSERT INTO {} (id, object_id, api_name, label, description, type, required, "
                "is_unique, indexed, track_history, default_value, options, owner, status, "
                "index_status, position) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
                "%s, 'active', %s, %s)"
            ).format(self._meta("meta_fields")),
            (
                f.id,
                obj.id,
                f.api_name,
                f.label,
                f.description,
                f.type.value,
                f.required,
                f.unique,
                f.indexed,
                f.track_history,
                Jsonb(f.default) if f.default is not None else None,
                Jsonb(f.options),
                f.owner.value,
                index_status,
                f.position,
            ),
        )

    # ---- operations ----------------------------------------------------------------

    def create_object(self, op: CreateObject, i: int) -> None:
        name = validate_api_name(op.api_name, kind="object", owner=self.owner)
        if name in self.objects:
            raise SchemaChangeError(
                f"Operation {i}: object {name!r} already exists"
                + (
                    " (retired; names cannot be reused)."
                    if self.objects[name].status != "active"
                    else "."
                ),
                code="OBJECT_EXISTS",
                field="api_name",
            )
        obj = _WObject(
            id=self._id("object", name),
            api_name=name,
            label=op.label,
            plural_label=op.plural_label or f"{op.label}s",
            description=op.description,
            owner=self.owner,
            status="active",
            fields={},
            is_new=True,
        )
        self.objects[name] = obj  # registered first so self-referencing lookups resolve

        new_fields = []
        for spec in op.fields:
            f = self._build_field(obj, spec, i)
            obj.fields[f.api_name] = f
            new_fields.append(f)

        columns = [
            sql.SQL("id uuid PRIMARY KEY DEFAULT gen_random_uuid()"),
            sql.SQL("created_at timestamptz NOT NULL DEFAULT now()"),
            sql.SQL("created_by uuid NOT NULL"),
            sql.SQL("updated_at timestamptz NOT NULL DEFAULT now()"),
            sql.SQL("updated_by uuid NOT NULL"),
            sql.SQL("owner_id uuid NOT NULL"),
            sql.SQL("row_version integer NOT NULL DEFAULT 1"),
            sql.SQL("retired_at timestamptz"),
            sql.SQL("retired_by uuid"),
        ] + [self._column_def(f, include_not_null=True) for f in new_fields]
        self.plan.run(
            sql.SQL("CREATE TABLE {} ({})").format(self._t(name), sql.SQL(", ").join(columns)),
            ddl=True,
        )
        self.plan.run(
            sql.SQL(
                "INSERT INTO {} (id, api_name, label, plural_label, description, owner) "
                "VALUES (%s, %s, %s, %s, %s, %s)"
            ).format(self._meta("meta_objects")),
            (obj.id, name, obj.label, obj.plural_label, obj.description, obj.owner.value),
        )
        for f in new_fields:
            if f.indexed:
                _, statement = self._index_statement(obj, f, concurrently=False)
                self.plan.run(statement, ddl=True)
            self._insert_field_meta(obj, f, "active" if f.indexed else None)
        field_list = ", ".join(f"{f.api_name} ({f.type.value})" for f in new_fields) or "no fields"
        self.plan.summary.append(f"Create object {name!r} with {field_list}.")

    def update_object(self, op: UpdateObject, i: int) -> None:
        obj = self._object(op.object, i)
        changes = {
            k: v for k, v in op.model_dump(exclude_unset=True).items() if k not in ("op", "object")
        }
        if not changes:
            raise SchemaChangeError(f"Operation {i}: nothing to change.", code="NO_CHANGE")
        for key, value in changes.items():
            setattr(obj, key, value)
        self.plan.run(
            sql.SQL(
                "UPDATE {} SET label = %s, plural_label = %s, description = %s, "
                "updated_at = now() WHERE id = %s"
            ).format(self._meta("meta_objects")),
            (obj.label, obj.plural_label, obj.description, obj.id),
        )
        self.plan.summary.append(f"Update {sorted(changes)} on object {obj.api_name!r}.")

    def retire_object(self, op: RetireObject, i: int) -> None:
        obj = self._object(op.object, i)
        self._protect_standard(obj.owner, f"object {obj.api_name!r}", i)
        referencing = [
            f"{o.api_name}.{f.api_name}"
            for o in self.objects.values()
            if o.status == "active" and o.api_name != obj.api_name
            for f in o.fields.values()
            if f.status == "active"
            and f.type is FieldType.LOOKUP
            and f.options["target_object_id"] == str(obj.id)
        ]
        if referencing:
            self._blocker(
                f"Object {obj.api_name!r} is referenced by active lookup fields; "
                "retire them first.",
                fields=sorted(referencing),
            )
        obj.status = "retired"
        self.plan.run(
            sql.SQL("UPDATE {} SET status = 'retired', updated_at = now() WHERE id = %s").format(
                self._meta("meta_objects")
            ),
            (obj.id,),
        )
        count = self._row_count(obj)
        self._info(
            f"Object {obj.api_name!r} will be hidden. Its {count} record(s) are kept, not deleted.",
            records=count,
        )
        self.plan.summary.append(f"Retire object {obj.api_name!r}.")

    def add_field(self, op: AddField, i: int) -> None:
        obj = self._object(op.object, i)
        f = self._build_field(obj, op.field, i)
        obj.fields[f.api_name] = f

        existing_rows = self._row_count(obj)
        not_null_inline = f.required and (f.default is not None or existing_rows == 0)
        if f.required and not not_null_inline:
            self._blocker(
                f"{obj.api_name!r} has {existing_rows} record(s); a new required field needs a "
                "default value for them.",
                field=f.api_name,
                records=existing_rows,
                hint="Add a 'default', or add the field as optional, fill it in, then make it "
                "required.",
            )
        if f.unique and f.default is not None and existing_rows > 1:
            self._blocker(
                "A unique field with a default cannot be added to an object with more than one "
                "record (every record would get the same value).",
                field=f.api_name,
            )

        self.plan.run(
            sql.SQL("ALTER TABLE {} ADD COLUMN {}").format(
                self._t(obj.api_name), self._column_def(f, include_not_null=f.required)
            ),
            ddl=True,
        )
        index_status = None
        if f.indexed:
            if obj.is_new:
                _, statement = self._index_statement(obj, f, concurrently=False)
                self.plan.run(statement, ddl=True)
                index_status = "active"
            else:
                index, statement = self._index_statement(obj, f, concurrently=True)
                self.plan.ddl.append(statement)
                self.plan.post_commit.append(
                    _PostStep(
                        statement, f.id, index, f"Build index for {obj.api_name}.{f.api_name}"
                    )
                )
                index_status = "building"
                self._info(
                    f"The index for {f.api_name!r} is built in the background after the change "
                    "is applied.",
                    field=f.api_name,
                )
        self._insert_field_meta(obj, f, index_status)
        if existing_rows and f.default is not None:
            self._info(
                f"{existing_rows} existing record(s) will get the default value.",
                field=f.api_name,
                records=existing_rows,
            )
        self.plan.summary.append(
            f"Add field {f.api_name!r} ({f.type.value}{', required' if f.required else ''}"
            f"{', unique' if f.unique else ''}) to {obj.api_name!r}."
        )

    def update_field(self, op: UpdateField, i: int) -> None:
        obj = self._object(op.object, i)
        f = self._field(obj, op.field, i)
        provided = op.model_fields_set - {"op", "object", "field"}
        if not provided:
            raise SchemaChangeError(f"Operation {i}: nothing to change.", code="NO_CHANGE")
        if "default" in provided:
            self._protect_standard(f.owner, f"the default of {obj.api_name}.{f.api_name}", i)
        table = self._t(obj.api_name)
        column = sql.Identifier(f.api_name)
        changed: list[str] = []

        for attr in ("label", "description", "track_history"):
            if attr in provided and getattr(op, attr) != getattr(f, attr):
                setattr(f, attr, getattr(op, attr))
                changed.append(attr)

        if "required" in provided and op.required is not None and op.required != f.required:
            if op.required:
                missing = self._row_count(obj, sql.SQL("{} IS NULL").format(column))
                if missing:
                    self._blocker(
                        f"{missing} record(s) of {obj.api_name!r} have no value for "
                        f"{f.api_name!r}.",
                        field=f.api_name,
                        records=missing,
                        hint="Fill in the missing values first, then make the field required.",
                    )
                self.plan.run(
                    sql.SQL("ALTER TABLE {} ALTER COLUMN {} SET NOT NULL").format(table, column),
                    ddl=True,
                )
            else:
                self.plan.run(
                    sql.SQL("ALTER TABLE {} ALTER COLUMN {} DROP NOT NULL").format(table, column),
                    ddl=True,
                )
            f.required = op.required
            changed.append("required")

        if "default" in provided:
            if f.type is FieldType.LOOKUP:
                raise SchemaChangeError(f"Operation {i}: lookup fields cannot have a default.")
            new_default = (
                None
                if op.default is None
                else _validate_default(f.type, f.options, op.default, f"Operation {i}")
            )
            if new_default != f.default:
                f.default = new_default
                if new_default is None:
                    statement = sql.SQL("ALTER TABLE {} ALTER COLUMN {} DROP DEFAULT").format(
                        table, column
                    )
                else:
                    statement = sql.SQL("ALTER TABLE {} ALTER COLUMN {} SET DEFAULT {}").format(
                        table, column, _default_literal(f)
                    )
                self.plan.run(statement, ddl=True)
                changed.append("default")

        if op.add_picklist_values or op.deactivate_picklist_values:
            if f.type is not FieldType.PICKLIST:
                raise SchemaChangeError(
                    f"Operation {i}: {f.api_name!r} is not a picklist.", field="field"
                )
            values = [dict(v) for v in f.options["values"]]
            by_value = {v["value"]: v for v in values}
            additions = (
                normalize_picklist_values(op.add_picklist_values) if op.add_picklist_values else []
            )
            for entry in additions:
                existing = by_value.get(entry["value"])
                if existing is None:
                    values.append(entry)
                    by_value[entry["value"]] = entry
                elif not existing["active"]:
                    existing["active"] = True
                else:
                    raise SchemaChangeError(
                        f"Operation {i}: picklist value {entry['value']!r} already exists.",
                        code="PICKLIST_VALUE_EXISTS",
                    )
            for value in op.deactivate_picklist_values or []:
                existing = by_value.get(value)
                if existing is None or not existing["active"]:
                    raise SchemaChangeError(
                        f"Operation {i}: picklist value {value!r} is not an active value.",
                        expected=[v["value"] for v in values if v["active"]],
                    )
                existing["active"] = False
            if f.default is not None and not by_value[f.default]["active"]:
                raise SchemaChangeError(
                    f"Operation {i}: cannot deactivate {f.default!r}; it is the field's default."
                )
            f.options = {**f.options, "values": values}
            changed.append("picklist values")

        if not changed:
            raise SchemaChangeError(f"Operation {i}: nothing to change.", code="NO_CHANGE")

        self.plan.run(
            sql.SQL(
                "UPDATE {} SET label = %s, description = %s, required = %s, track_history = %s, "
                "default_value = %s, options = %s, updated_at = now() WHERE id = %s"
            ).format(self._meta("meta_fields")),
            (
                f.label,
                f.description,
                f.required,
                f.track_history,
                Jsonb(f.default) if f.default is not None else None,
                Jsonb(f.options),
                f.id,
            ),
        )
        self.plan.summary.append(
            f"Update {', '.join(changed)} on field {obj.api_name}.{f.api_name}."
        )

    def retire_field(self, op: RetireField, i: int) -> None:
        obj = self._object(op.object, i)
        f = self._field(obj, op.field, i)
        self._protect_standard(f.owner, f"field {obj.api_name}.{f.api_name}", i)
        if f.required:
            # A retired column must not block inserts.
            self.plan.run(
                sql.SQL("ALTER TABLE {} ALTER COLUMN {} DROP NOT NULL").format(
                    self._t(obj.api_name), sql.Identifier(f.api_name)
                ),
                ddl=True,
            )
        f.status = "retired"
        self.plan.run(
            sql.SQL(
                "UPDATE {} SET status = 'retired', required = false, updated_at = now() "
                "WHERE id = %s"
            ).format(self._meta("meta_fields")),
            (f.id,),
        )
        self._info(
            f"Field {f.api_name!r} will be hidden. Existing values and history are kept.",
            field=f.api_name,
        )
        self.plan.summary.append(f"Retire field {obj.api_name}.{f.api_name}.")


def _validate_default(ftype: FieldType, options: dict[str, Any], value: Any, where: str) -> Any:
    adapter = TypeAdapter(REGISTRY[ftype].python_type(options))
    try:
        validated = adapter.validate_python(value)
    except PydanticValidationError:
        raise SchemaChangeError(
            f"{where}: invalid default {value!r}.",
            field="default",
            expected=REGISTRY[ftype].describe(options),
        ) from None
    return adapter.dump_python(validated, mode="json")


def _default_literal(f: _WField) -> sql.Composable:
    adapter = TypeAdapter(REGISTRY[f.type].python_type(f.options))
    return sql.Literal(adapter.validate_python(f.default))


def _plan(
    conn: psycopg.Connection,
    snapshot: SchemaSnapshot,
    change_set_id: uuid.UUID,
    source: str,
    operations: list[Any],
) -> Plan:
    planner = _Planner(conn, snapshot, change_set_id, source)
    for index, op in enumerate(operations, start=1):
        getattr(planner, op.op)(op, index)
    return planner.plan


# --------------------------------------------------------------------------------------
# Proposals and application
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Proposal:
    id: uuid.UUID
    status: str
    summary: list[str]
    ddl: list[str]
    impact: list[dict[str, Any]]
    blockers: list[dict[str, Any]]
    diff_hash: str
    base_schema_version: int

    @property
    def can_apply(self) -> bool:
        return not self.blockers

    def to_dict(self) -> dict[str, Any]:
        return {
            "change_set_id": str(self.id),
            "status": self.status,
            "can_apply": self.can_apply,
            "summary": self.summary,
            "ddl": self.ddl,
            "impact": self.impact,
            "blockers": self.blockers,
            "diff_hash": self.diff_hash,
            "base_schema_version": self.base_schema_version,
        }


@dataclass(frozen=True)
class ApplyResult:
    change_set_id: uuid.UUID
    schema_version: int
    summary: list[str]
    post_apply: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "change_set_id": str(self.change_set_id),
            "status": "applied",
            "schema_version": self.schema_version,
            "summary": self.summary,
            "post_apply": self.post_apply,
        }


def _diff_hash(
    change_set_id: uuid.UUID, base_version: int, operations: list[dict[str, Any]]
) -> str:
    canonical = json.dumps(
        {"id": str(change_set_id), "base": base_version, "ops": operations},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


class SchemaService:
    def __init__(self, conn: psycopg.Connection, tenant: Tenant, principal: Principal) -> None:
        self._conn = conn
        self._tenant = tenant
        self._principal = principal
        self._registry = MetadataRegistry(conn, tenant)

    def _table(self, name: str) -> sql.Identifier:
        return sql.Identifier(self._tenant.schema, name)

    def propose(self, operations: Any, *, source: str = "tenant") -> Proposal:
        """Validate a change set, analyse its impact, and store it as a proposal.

        Raises ``SchemaChangeError`` for invalid requests. Data-dependent problems
        (e.g. existing records missing a value) are returned as ``blockers``.
        """
        if source not in ("tenant", "platform", "upgrade"):
            raise SchemaChangeError(f"Invalid source {source!r}.")
        if source != "tenant" and self._principal.actor_type is not ActorType.SYSTEM:
            raise ApprovalError("Only the platform may propose platform or upgrade changes.")
        ops = parse_operations(operations)
        op_dicts = [op.model_dump(mode="json", exclude_unset=True) for op in ops]
        change_set_id = uuid.uuid4()

        snapshot = self._registry.snapshot()
        plan = _plan(self._conn, snapshot, change_set_id, source, ops)
        diff_hash = _diff_hash(change_set_id, snapshot.version, op_dicts)
        ddl = [s.as_string(self._conn) for s in plan.ddl]

        self._conn.execute(
            sql.SQL(
                "INSERT INTO {} (id, status, source, operations, summary, ddl, impact, blockers, "
                "diff_hash, base_schema_version, proposed_by, proposed_by_type, proposed_by_agent) "
                "VALUES (%s, 'proposed', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
            ).format(self._table("meta_change_sets")),
            (
                change_set_id,
                source,
                Jsonb(op_dicts),
                Jsonb(plan.summary),
                Jsonb(ddl),
                Jsonb(plan.impact),
                Jsonb(plan.blockers),
                diff_hash,
                snapshot.version,
                self._principal.user_id,
                self._principal.actor_type.value,
                self._principal.agent_id,
            ),
        )
        return Proposal(
            id=change_set_id,
            status="proposed",
            summary=plan.summary,
            ddl=ddl,
            impact=plan.impact,
            blockers=plan.blockers,
            diff_hash=diff_hash,
            base_schema_version=snapshot.version,
        )

    def apply(self, change_set_id: uuid.UUID, *, diff_hash: str) -> ApplyResult:
        """Apply a proposed change set, approved by this service's principal.

        Tenant changes must be approved by a human. In M3 approval additionally requires
        a single-use confirmation token issued by the approval UI (ARCHITECTURE.md §10.2).
        """
        approver = self._principal
        try:
            with self._conn.transaction():
                self._conn.execute(
                    sql.SQL("SET LOCAL lock_timeout = {}").format(sql.Literal(LOCK_TIMEOUT))
                )
                self._conn.execute(
                    sql.SQL("SET LOCAL statement_timeout = {}").format(
                        sql.Literal(STATEMENT_TIMEOUT)
                    )
                )
                version = self._registry.current_version(lock="update")
                with self._conn.cursor(row_factory=dict_row) as cur:
                    row = cur.execute(
                        sql.SQL("SELECT * FROM {} WHERE id = %s FOR UPDATE").format(
                            self._table("meta_change_sets")
                        ),
                        (change_set_id,),
                    ).fetchone()
                if row is None:
                    raise NotFoundError(
                        f"Change set {change_set_id} does not exist.", code="CHANGE_SET_NOT_FOUND"
                    )
                self._check_approval(row, approver, diff_hash, version)

                ops = parse_operations(row["operations"])
                snapshot = self._registry._load(version)
                plan = _plan(self._conn, snapshot, change_set_id, row["source"], ops)
                if plan.blockers:
                    raise SchemaChangeError(
                        "The change set cannot be applied because of blockers.",
                        code="CHANGE_SET_BLOCKED",
                        details={"blockers": plan.blockers},
                    )
                for statement, params in plan.statements:
                    self._conn.execute(statement, params)
                new_version = version + 1
                self._conn.execute(
                    sql.SQL("UPDATE {} SET version = %s").format(
                        self._table("meta_schema_version")
                    ),
                    (new_version,),
                )
                self._conn.execute(
                    sql.SQL(
                        "UPDATE {} SET status = 'applied', applied_schema_version = %s, "
                        "approved_by = %s, approved_by_type = %s, applied_at = now() WHERE id = %s"
                    ).format(self._table("meta_change_sets")),
                    (new_version, approver.user_id, approver.actor_type.value, change_set_id),
                )
        except _Superseded:
            with self._conn.transaction():
                self._conn.execute(
                    sql.SQL("UPDATE {} SET status = 'superseded' WHERE id = %s").format(
                        self._table("meta_change_sets")
                    ),
                    (change_set_id,),
                )
            raise SchemaChangeError(
                "The schema changed after this proposal was made.",
                code="CHANGE_SET_STALE",
                hint="Propose the change again against the current schema and re-approve it.",
            ) from None
        except pg_errors.LockNotAvailable:
            raise SchemaChangeError(
                "The schema change could not acquire its locks in time (the object is busy).",
                code="LOCK_TIMEOUT",
                retryable=True,
                hint="Apply the same change set again in a moment.",
            ) from None
        except psycopg.Error as exc:
            self._record_failure(change_set_id, {"sqlstate": exc.sqlstate, "message": str(exc)})
            raise SchemaChangeError(
                "The schema change failed in the database and was rolled back.",
                code="CHANGE_SET_FAILED",
                details={"sqlstate": exc.sqlstate},
            ) from exc

        post_apply = self._run_post_commit(change_set_id, plan.post_commit)
        return ApplyResult(change_set_id, new_version, plan.summary, post_apply)

    def _check_approval(
        self, row: dict[str, Any], approver: Principal, diff_hash: str, version: int
    ) -> None:
        if row["status"] != "proposed":
            raise SchemaChangeError(
                f"Change set is {row['status']}, not proposed.", code="CHANGE_SET_NOT_PENDING"
            )
        if row["diff_hash"] != diff_hash:
            raise ApprovalError(
                "The approval does not match the proposed change.",
                code="DIFF_HASH_MISMATCH",
                hint="Approve the exact proposal that was shown.",
            )
        if row["source"] == "tenant" and not approver.is_human:
            raise ApprovalError(
                "Schema changes must be approved by a person, not an agent.",
                code="APPROVAL_REQUIRES_HUMAN",
                hint="Show the proposal to the user and ask them to approve it.",
            )
        if row["source"] != "tenant" and approver.actor_type is not ActorType.SYSTEM:
            raise ApprovalError("Platform changes are applied by the platform.")
        if row["base_schema_version"] != version:
            raise _Superseded(row["id"])

    def _record_failure(self, change_set_id: uuid.UUID, error: dict[str, Any]) -> None:
        with self._conn.transaction():
            self._conn.execute(
                sql.SQL(
                    "UPDATE {} SET status = 'failed', error = %s WHERE id = %s AND "
                    "status = 'proposed'"
                ).format(self._table("meta_change_sets")),
                (Jsonb(error), change_set_id),
            )

    def _run_post_commit(
        self, change_set_id: uuid.UUID, steps: list[_PostStep]
    ) -> list[dict[str, Any]]:
        results = []
        for step in steps:
            status = "active"
            error = None
            try:
                self._conn.execute(step.statement)  # CONCURRENTLY: must run outside a transaction
            except psycopg.Error as exc:
                status = "failed"
                error = str(exc)
                # A failed concurrent build leaves an INVALID index behind.
                self._conn.execute(
                    sql.SQL("DROP INDEX CONCURRENTLY IF EXISTS {}").format(
                        sql.Identifier(self._tenant.schema, step.index)
                    )
                )
            self._conn.execute(
                sql.SQL("UPDATE {} SET index_status = %s WHERE id = %s").format(
                    self._table("meta_fields")
                ),
                (status, step.field_id),
            )
            results.append({"step": step.description, "status": status, "error": error})
        if results:
            self._conn.execute(
                sql.SQL("UPDATE {} SET post_apply = %s WHERE id = %s").format(
                    self._table("meta_change_sets")
                ),
                (Jsonb(results), change_set_id),
            )
        return results


class _Superseded(Exception):
    def __init__(self, change_set_id: uuid.UUID) -> None:
        self.change_set_id = change_set_id
