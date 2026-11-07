"""Structured record queries: the only way agents read many records.

Agents never write SQL. They send a small JSON query that is validated against
the metadata and compiled to SQL with bound parameters:

    {"object": "lead",
     "filters": [{"field": "status", "op": "eq", "value": "new"},
                 {"field": "company_name", "op": "contains", "value": "acme"}],
     "order_by": [{"field": "created_at", "direction": "desc"}],
     "limit": 20,
     "expand": ["converted_account"]}
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any, Literal

from psycopg import sql
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter
from pydantic import ValidationError as PydanticValidationError

from .errors import RecordValidationError
from .fieldtypes import REGISTRY, FieldType
from .metadata import FieldDef, ObjectDef, SchemaSnapshot

MAX_LIMIT = 200
MAX_OFFSET = 10_000
MAX_IN_VALUES = 100

Operator = Literal[
    "eq", "ne", "gt", "gte", "lt", "lte", "in", "not_in", "contains", "starts_with", "is_null"
]
_COMPARISON = {"eq": "=", "ne": "<>", "gt": ">", "gte": ">=", "lt": "<", "lte": "<="}
_TEXT_TYPES = {
    FieldType.TEXT,
    FieldType.LONG_TEXT,
    FieldType.EMAIL,
    FieldType.URL,
    FieldType.PICKLIST,
}
_ORDERED_TYPES = {
    FieldType.INTEGER,
    FieldType.DECIMAL,
    FieldType.DATE,
    FieldType.DATETIME,
    FieldType.TEXT,
    FieldType.EMAIL,
    FieldType.URL,
    FieldType.PICKLIST,
}
# System columns that may be filtered and sorted on, with their value types.
_SYSTEM_FIELDS: dict[str, Any] = {
    "id": uuid.UUID,
    "created_at": dt.datetime,
    "updated_at": dt.datetime,
    "created_by": uuid.UUID,
    "updated_by": uuid.UUID,
    "owner_id": uuid.UUID,
    "row_version": int,
}


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Filter(_Model):
    field: str
    op: Operator = "eq"
    value: Any = None


class OrderBy(_Model):
    field: str
    direction: Literal["asc", "desc"] = "asc"


class Query(_Model):
    object: str
    filters: list[Filter] = Field(default_factory=list, max_length=20)
    order_by: list[OrderBy] = Field(default_factory=list, max_length=3)
    limit: int = Field(default=50, ge=1, le=MAX_LIMIT)
    offset: int = Field(default=0, ge=0, le=MAX_OFFSET)
    expand: list[str] = Field(default_factory=list, max_length=10)
    include_retired: bool = False


def parse_query(raw: Any) -> Query:
    try:
        return Query.model_validate(raw)
    except PydanticValidationError as exc:
        raise RecordValidationError(
            "The query is not well-formed.",
            code="QUERY_MALFORMED",
            details={
                "errors": [
                    {"location": ".".join(str(p) for p in e["loc"]), "message": e["msg"]}
                    for e in exc.errors(include_url=False)
                ]
            },
            hint="See the query_records tool description for the query format.",
        ) from None


def _resolve_field(obj: ObjectDef, name: str) -> tuple[FieldDef | None, Any]:
    if name in _SYSTEM_FIELDS:
        return None, _SYSTEM_FIELDS[name]
    f = obj.active_fields.get(name)
    if f is None:
        raise RecordValidationError(
            f"{obj.api_name!r} has no field {name!r}.",
            code="UNKNOWN_FIELD",
            field=name,
            expected=sorted([*obj.active_fields, *_SYSTEM_FIELDS]),
        )
    python_type = REGISTRY[f.type].python_type(f.options)
    if f.type is FieldType.PICKLIST:
        python_type = str  # filtering by inactive values is allowed
    return f, python_type


def _coerce(name: str, python_type: Any, value: Any, expected: str) -> Any:
    try:
        return TypeAdapter(python_type).validate_python(value)
    except PydanticValidationError:
        raise RecordValidationError(
            f"Invalid filter value for {name!r}.",
            code="INVALID_VALUE",
            field=name,
            expected=expected,
        ) from None


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def compile_query(
    snapshot: SchemaSnapshot, query: Query
) -> tuple[ObjectDef, sql.Composable, list[Any]]:
    obj = snapshot.get_object(query.object)
    conditions: list[sql.Composable] = []
    params: list[Any] = []

    for flt in query.filters:
        f, python_type = _resolve_field(obj, flt.field)
        column = sql.Identifier(flt.field)
        ftype = f.type if f else None
        expected = f.describe_expected(snapshot) if f else python_type.__name__

        if flt.op == "is_null":
            if not isinstance(flt.value, bool):
                raise RecordValidationError(
                    "is_null needs value true or false.", field=flt.field, code="INVALID_VALUE"
                )
            conditions.append(
                sql.SQL("{} IS NULL" if flt.value else "{} IS NOT NULL").format(column)
            )
        elif flt.op in ("contains", "starts_with"):
            if ftype not in _TEXT_TYPES:
                raise RecordValidationError(
                    f"{flt.op} only works on text fields.", field=flt.field, code="INVALID_OPERATOR"
                )
            if not isinstance(flt.value, str) or not flt.value:
                raise RecordValidationError(
                    f"{flt.op} needs a non-empty string.", field=flt.field, code="INVALID_VALUE"
                )
            pattern = _escape_like(flt.value)
            pattern = f"%{pattern}%" if flt.op == "contains" else f"{pattern}%"
            conditions.append(sql.SQL("{} ILIKE %s ESCAPE '\\'").format(column))
            params.append(pattern)
        elif flt.op in ("in", "not_in"):
            if not isinstance(flt.value, list) or not 1 <= len(flt.value) <= MAX_IN_VALUES:
                raise RecordValidationError(
                    f"{flt.op} needs a list of 1 to {MAX_IN_VALUES} values.",
                    field=flt.field,
                    code="INVALID_VALUE",
                )
            values = [_coerce(flt.field, python_type, v, expected) for v in flt.value]
            template = "{} = ANY(%s)" if flt.op == "in" else "NOT ({} = ANY(%s))"
            conditions.append(sql.SQL(template).format(column))
            params.append(values)
        else:
            if flt.value is None:
                raise RecordValidationError(
                    "Use op 'is_null' to filter on empty values.",
                    field=flt.field,
                    code="INVALID_VALUE",
                )
            if flt.op not in ("eq", "ne") and ftype is not None and ftype not in _ORDERED_TYPES:
                raise RecordValidationError(
                    f"{flt.op} does not apply to {ftype.value} fields.",
                    field=flt.field,
                    code="INVALID_OPERATOR",
                )
            conditions.append(sql.SQL("{} " + _COMPARISON[flt.op] + " %s").format(column))
            params.append(_coerce(flt.field, python_type, flt.value, expected))

    if not query.include_retired:
        conditions.append(sql.SQL("retired_at IS NULL"))

    order: list[sql.Composable] = []
    for ob in query.order_by:
        f, _ = _resolve_field(obj, ob.field)
        if f is not None and f.type not in _ORDERED_TYPES | {FieldType.BOOLEAN}:
            raise RecordValidationError(
                f"Cannot sort by {f.type.value} field {ob.field!r}.",
                field=ob.field,
                code="INVALID_OPERATOR",
            )
        order.append(
            sql.SQL(
                "{} " + ("DESC NULLS LAST" if ob.direction == "desc" else "ASC NULLS LAST")
            ).format(sql.Identifier(ob.field))
        )
    order += [sql.SQL("created_at ASC"), sql.SQL("id ASC")]  # stable paging

    for name in query.expand:
        f = obj.active_fields.get(name)
        if f is None or f.type is not FieldType.LOOKUP:
            raise RecordValidationError(
                f"{name!r} is not a lookup field of {obj.api_name!r}.",
                code="INVALID_EXPAND",
                field=name,
                expected=sorted(
                    n for n, x in obj.active_fields.items() if x.type is FieldType.LOOKUP
                ),
            )

    statement = sql.SQL("SELECT * FROM {}").format(snapshot.table(obj))
    if conditions:
        statement += sql.SQL(" WHERE ") + sql.SQL(" AND ").join(conditions)
    statement += sql.SQL(" ORDER BY ") + sql.SQL(", ").join(order)
    # Fetch one extra row to report has_more without a count query.
    statement += sql.SQL(" LIMIT {} OFFSET {}").format(
        sql.Literal(query.limit + 1), sql.Literal(query.offset)
    )
    return obj, statement, params
