"""Metadata registry: an immutable, versioned snapshot of a tenant's objects and fields."""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from functools import cached_property
from typing import Any

from psycopg import sql
from psycopg.rows import dict_row

from .context import Tenant
from .db import TenantConnection
from .errors import NotFoundError
from .fieldtypes import REGISTRY, FieldType
from .identifiers import Owner


@dataclass(frozen=True)
class FieldDef:
    id: uuid.UUID
    object_id: uuid.UUID
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
    index_status: str | None
    position: int
    semantic_ref: str | None = None

    @property
    def active(self) -> bool:
        return self.status == "active"

    @property
    def target_object_id(self) -> uuid.UUID | None:
        if self.type is FieldType.LOOKUP:
            return uuid.UUID(self.options["target_object_id"])
        return None

    def describe_expected(self, snapshot: SchemaSnapshot | None = None) -> str:
        if self.type is FieldType.LOOKUP and snapshot is not None:
            target = snapshot.objects_by_id.get(self.target_object_id)  # type: ignore[arg-type]
            name = target.api_name if target else "target"
            return f"id (UUID) of an active {name} record"
        return REGISTRY[self.type].describe(self.options)


@dataclass(frozen=True)
class ObjectDef:
    id: uuid.UUID
    api_name: str
    label: str
    plural_label: str
    description: str | None
    owner: Owner
    status: str
    fields: dict[str, FieldDef]  # all fields, including retired, keyed by api_name
    semantic_ref: str | None = None
    options: dict[str, Any] = field(default_factory=dict)
    package: str | None = None

    @property
    def access(self) -> str:
        """open | protected (people only) | engine_managed (owning engine service only)."""
        return str(self.options.get("access", "open"))

    @property
    def managed_by(self) -> str | None:
        return self.options.get("managed_by")

    @cached_property
    def unique_together(self) -> list[list[FieldDef]]:
        by_id = {str(f.id): f for f in self.fields.values()}
        return [[by_id[i] for i in combo] for combo in self.options.get("unique_together", [])]

    @property
    def active(self) -> bool:
        return self.status == "active"

    @cached_property
    def active_fields(self) -> dict[str, FieldDef]:
        return {k: f for k, f in self.fields.items() if f.active}

    @cached_property
    def fields_by_id(self) -> dict[uuid.UUID, FieldDef]:
        return {f.id: f for f in self.fields.values()}


@dataclass(frozen=True)
class SchemaSnapshot:
    tenant: Tenant
    version: int
    objects: dict[str, ObjectDef]  # all objects, including retired, keyed by api_name
    _validator_cache: dict[Any, Any] = field(default_factory=dict, compare=False, repr=False)

    @cached_property
    def objects_by_id(self) -> dict[uuid.UUID, ObjectDef]:
        return {o.id: o for o in self.objects.values()}

    def get_object(self, api_name: str, *, include_retired: bool = False) -> ObjectDef:
        obj = self.objects.get(api_name)
        if obj is None or (not obj.active and not include_retired):
            available = sorted(o.api_name for o in self.objects.values() if o.active)
            raise NotFoundError(
                f"Object {api_name!r} does not exist"
                + (" (it is retired)." if obj is not None else "."),
                code="OBJECT_NOT_FOUND",
                field="object",
                expected=available,
                hint="Call describe_schema to list available objects.",
            )
        return obj

    def table(self, obj: ObjectDef) -> sql.Identifier:
        return sql.Identifier(self.tenant.schema, obj.api_name)

    def lookups_targeting(self, obj: ObjectDef) -> list[tuple[ObjectDef, FieldDef]]:
        """Active lookup fields on active objects that point at ``obj``."""
        result = []
        for other in self.objects.values():
            if not other.active:
                continue
            for f in other.active_fields.values():
                if f.type is FieldType.LOOKUP and f.target_object_id == obj.id:
                    result.append((other, f))
        return result

    def to_dict(self) -> dict[str, Any]:
        """Agent-friendly description of active objects and fields."""
        objects = []
        for obj in sorted(self.objects.values(), key=lambda o: o.api_name):
            if not obj.active:
                continue
            fields = []
            for f in sorted(obj.active_fields.values(), key=lambda f: f.position):
                entry: dict[str, Any] = {
                    "api_name": f.api_name,
                    "label": f.label,
                    "description": f.description,
                    "semantic_ref": f.semantic_ref,
                    "type": f.type.value,
                    "required": f.required,
                    "unique": f.unique,
                    "track_history": f.track_history,
                    "owner": f.owner.value,
                    "expected": f.describe_expected(self),
                }
                if f.type is FieldType.LOOKUP:
                    target = self.objects_by_id[f.target_object_id]  # type: ignore[index]
                    entry["target"] = target.api_name
                    entry["on_delete"] = f.options["on_delete"]
                if f.type is FieldType.PICKLIST:
                    entry["values"] = [v for v in f.options["values"] if v["active"]]
                if f.default is not None:
                    entry["default"] = f.default
                fields.append(entry)
            objects.append(
                {
                    "api_name": obj.api_name,
                    "label": obj.label,
                    "plural_label": obj.plural_label,
                    "owner": obj.owner.value,
                    "package": obj.package,
                    "description": obj.description,
                    "semantic_ref": obj.semantic_ref,
                    "access": obj.access,
                    "managed_by": obj.managed_by,
                    "unique_together": [
                        [f.api_name for f in combo] for combo in obj.unique_together
                    ],
                    "fields": fields,
                }
            )
        return {"schema_version": self.version, "objects": objects}


class MetadataRegistry:
    """Loads snapshots and caches them per (tenant schema, schema version)."""

    _cache: dict[tuple[str, int], SchemaSnapshot] = {}
    _lock = threading.Lock()

    def __init__(self, conn: TenantConnection, tenant: Tenant) -> None:
        self._conn = conn
        self._tenant = tenant

    def current_version(self, *, lock: str | None = None) -> int:
        query = sql.SQL("SELECT version FROM {}").format(
            sql.Identifier(self._tenant.schema, "meta_schema_version")
        )
        if lock == "share":
            query += sql.SQL(" FOR SHARE")
        elif lock == "update":
            query += sql.SQL(" FOR UPDATE")
        row = self._conn.execute(query).fetchone()
        assert row is not None
        return int(row[0])

    def snapshot(self) -> SchemaSnapshot:
        version = self.current_version()
        key = (self._tenant.schema, version)
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        # Hold a share lock on the version row while loading so a concurrent schema
        # change (which takes FOR UPDATE) cannot interleave with the read.
        with self._conn.transaction():
            version = self.current_version(lock="share")
            snapshot = self._load(version)
        key = (self._tenant.schema, version)
        with self._lock:
            # Keep only the newest snapshot per tenant schema.
            for stale in [k for k in self._cache if k[0] == self._tenant.schema]:
                del self._cache[stale]
            self._cache[key] = snapshot
        return snapshot

    def _load(self, version: int) -> SchemaSnapshot:
        schema = self._tenant.schema
        with self._conn.cursor(row_factory=dict_row) as cur:
            objects_rows = cur.execute(
                sql.SQL("SELECT * FROM {}").format(sql.Identifier(schema, "meta_objects"))
            ).fetchall()
            field_rows = cur.execute(
                sql.SQL("SELECT * FROM {} ORDER BY position").format(
                    sql.Identifier(schema, "meta_fields")
                )
            ).fetchall()
        fields_by_object: dict[uuid.UUID, dict[str, FieldDef]] = {}
        for r in field_rows:
            fields_by_object.setdefault(r["object_id"], {})[r["api_name"]] = FieldDef(
                id=r["id"],
                object_id=r["object_id"],
                api_name=r["api_name"],
                label=r["label"],
                description=r["description"],
                type=FieldType(r["type"]),
                required=r["required"],
                unique=r["is_unique"],
                indexed=r["indexed"],
                track_history=r["track_history"],
                default=r["default_value"],
                options=r["options"],
                owner=Owner(r["owner"]),
                status=r["status"],
                index_status=r["index_status"],
                position=r["position"],
                semantic_ref=r["semantic_ref"],
            )
        objects = {
            r["api_name"]: ObjectDef(
                id=r["id"],
                api_name=r["api_name"],
                label=r["label"],
                plural_label=r["plural_label"],
                description=r["description"],
                owner=Owner(r["owner"]),
                status=r["status"],
                fields=fields_by_object.get(r["id"], {}),
                semantic_ref=r["semantic_ref"],
                options=r["options"],
                package=r["package"],
            )
            for r in objects_rows
        }
        return SchemaSnapshot(tenant=self._tenant, version=version, objects=objects)
