"""Field type registry.

Each field type defines:
- how its options are validated and normalized,
- the PostgreSQL column type it compiles to,
- the Python/Pydantic type used to validate incoming values,
- a short human/agent-readable description of what values are expected.
"""

from __future__ import annotations

import datetime as dt
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Any, Literal

from psycopg import sql
from pydantic import AfterValidator, AwareDatetime, Field, StrictBool, StrictInt, StrictStr

from .errors import SchemaChangeError


class FieldType(StrEnum):
    TEXT = "text"
    LONG_TEXT = "long_text"
    INTEGER = "integer"
    DECIMAL = "decimal"
    BOOLEAN = "boolean"
    DATE = "date"
    DATETIME = "datetime"
    EMAIL = "email"
    URL = "url"
    PICKLIST = "picklist"
    LOOKUP = "lookup"


class OnDelete(StrEnum):
    RESTRICT = "restrict"  # retiring the parent is blocked while active children exist
    CASCADE = "cascade"  # retiring the parent retires the children
    SET_NULL = "set_null"  # retiring the parent clears the lookup on children


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_URL_RE = re.compile(r"^https?://[^\s/$.?#][^\s]*$", re.IGNORECASE)
_PICKLIST_VALUE_RE = re.compile(r"^[a-z0-9][a-z0-9_]{0,99}$")


def _check_email(value: str) -> str:
    if not _EMAIL_RE.fullmatch(value):
        raise ValueError("not a valid email address")
    return value


def _check_url(value: str) -> str:
    if not _URL_RE.fullmatch(value):
        raise ValueError("not a valid http(s) URL")
    return value


@dataclass(frozen=True)
class TypeSpec:
    normalize_options: Callable[[dict[str, Any]], dict[str, Any]]
    column_type: Callable[[dict[str, Any]], sql.Composable]
    python_type: Callable[[dict[str, Any]], Any]
    describe: Callable[[dict[str, Any]], str]
    indexable: bool = True


def _expect_keys(options: dict[str, Any], allowed: set[str], type_name: str) -> None:
    unknown = set(options) - allowed
    if unknown:
        raise SchemaChangeError(
            f"Unknown option(s) for type {type_name!r}: {sorted(unknown)}.",
            field="options",
            expected=sorted(allowed),
        )


def _int_option(options: dict[str, Any], key: str, default: int, lo: int, hi: int) -> int:
    value = options.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool) or not lo <= value <= hi:
        raise SchemaChangeError(
            f"Option {key!r} must be an integer between {lo} and {hi}.",
            field=f"options.{key}",
            expected=f"integer {lo}..{hi}",
        )
    return value


# --- text ---------------------------------------------------------------------------------


def _text_options(options: dict[str, Any]) -> dict[str, Any]:
    _expect_keys(options, {"max_length"}, "text")
    return {"max_length": _int_option(options, "max_length", 255, 1, 10_000)}


def _long_text_options(options: dict[str, Any]) -> dict[str, Any]:
    _expect_keys(options, {"max_length"}, "long_text")
    return {"max_length": _int_option(options, "max_length", 100_000, 1, 1_000_000)}


# --- decimal ------------------------------------------------------------------------------


def _decimal_options(options: dict[str, Any]) -> dict[str, Any]:
    _expect_keys(options, {"precision", "scale"}, "decimal")
    precision = _int_option(options, "precision", 18, 1, 38)
    scale = _int_option(options, "scale", 2, 0, precision)
    return {"precision": precision, "scale": scale}


# --- picklist -----------------------------------------------------------------------------


def normalize_picklist_values(values: Any) -> list[dict[str, Any]]:
    """Accept ["a", "b"] or [{"value": "a", "label": "A"}, ...]; return normalized entries."""
    if not isinstance(values, list) or not values:
        raise SchemaChangeError(
            "Picklist values must be a non-empty list.",
            field="options.values",
            expected='["value", ...] or [{"value": "...", "label": "..."}]',
        )
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in values:
        if isinstance(entry, str):
            entry = {"value": entry}
        if not isinstance(entry, dict) or "value" not in entry:
            raise SchemaChangeError("Invalid picklist entry.", field="options.values")
        value = entry["value"]
        if not isinstance(value, str) or not _PICKLIST_VALUE_RE.fullmatch(value):
            raise SchemaChangeError(
                f"Invalid picklist value {value!r}.",
                field="options.values",
                expected="lowercase letters, digits and underscores, starting with a letter "
                "or digit, at most 100 characters",
            )
        if value in seen:
            raise SchemaChangeError(f"Duplicate picklist value {value!r}.", field="options.values")
        seen.add(value)
        label = entry.get("label") or value.replace("_", " ").capitalize()
        result.append(
            {"value": value, "label": str(label), "active": bool(entry.get("active", True))}
        )
    return result


def _picklist_options(options: dict[str, Any]) -> dict[str, Any]:
    _expect_keys(options, {"values"}, "picklist")
    return {"values": normalize_picklist_values(options.get("values"))}


def active_picklist_values(options: dict[str, Any]) -> list[str]:
    return [v["value"] for v in options["values"] if v["active"]]


def _picklist_type(options: dict[str, Any]) -> Any:
    values = tuple(active_picklist_values(options))
    if not values:
        # No active values: nothing can be written. Literal[()] is invalid, so use a
        # string type that always fails validation.
        return Annotated[StrictStr, AfterValidator(_reject_all)]
    return Literal[values]  # type: ignore[valid-type]


def _reject_all(value: str) -> str:
    raise ValueError("this picklist has no active values")


# --- lookup -------------------------------------------------------------------------------


def _lookup_options(options: dict[str, Any]) -> dict[str, Any]:
    _expect_keys(options, {"target", "target_object_id", "on_delete"}, "lookup")
    target = options.get("target")
    if target is None and "target_object_id" not in options:
        raise SchemaChangeError(
            "Lookup fields need a 'target' object.",
            field="options.target",
            expected="object api_name",
        )
    on_delete = options.get("on_delete", OnDelete.RESTRICT.value)
    try:
        on_delete = OnDelete(on_delete).value
    except ValueError:
        raise SchemaChangeError(
            f"Invalid on_delete {on_delete!r}.",
            field="options.on_delete",
            expected=[e.value for e in OnDelete],
        ) from None
    result: dict[str, Any] = {"on_delete": on_delete}
    if target is not None:
        result["target"] = target
    if "target_object_id" in options:
        result["target_object_id"] = str(options["target_object_id"])
    return result


def _no_options(type_name: str) -> Callable[[dict[str, Any]], dict[str, Any]]:
    def normalize(options: dict[str, Any]) -> dict[str, Any]:
        _expect_keys(options, set(), type_name)
        return {}

    return normalize


def _fixed(sql_type: str) -> Callable[[dict[str, Any]], sql.Composable]:
    return lambda _options: sql.SQL(sql_type)  # constant strings only


REGISTRY: dict[FieldType, TypeSpec] = {
    FieldType.TEXT: TypeSpec(
        normalize_options=_text_options,
        column_type=lambda o: sql.SQL("varchar({})").format(sql.Literal(o["max_length"])),
        python_type=lambda o: Annotated[StrictStr, Field(max_length=o["max_length"])],
        describe=lambda o: f"string, at most {o['max_length']} characters",
    ),
    FieldType.LONG_TEXT: TypeSpec(
        normalize_options=_long_text_options,
        column_type=_fixed("text"),
        python_type=lambda o: Annotated[StrictStr, Field(max_length=o["max_length"])],
        describe=lambda o: f"string, at most {o['max_length']} characters",
        indexable=False,
    ),
    FieldType.INTEGER: TypeSpec(
        normalize_options=_no_options("integer"),
        column_type=_fixed("bigint"),
        python_type=lambda o: Annotated[StrictInt, Field(ge=-(2**63), le=2**63 - 1)],
        describe=lambda o: "integer",
    ),
    FieldType.DECIMAL: TypeSpec(
        normalize_options=_decimal_options,
        column_type=lambda o: sql.SQL("numeric({}, {})").format(
            sql.Literal(o["precision"]), sql.Literal(o["scale"])
        ),
        python_type=lambda o: Annotated[
            Decimal,
            Field(max_digits=o["precision"], decimal_places=o["scale"], allow_inf_nan=False),
        ],
        describe=lambda o: (
            f"number with at most {o['precision']} digits, "
            f"{o['scale']} after the decimal point (send as a string for exact values)"
        ),
    ),
    FieldType.BOOLEAN: TypeSpec(
        normalize_options=_no_options("boolean"),
        column_type=_fixed("boolean"),
        python_type=lambda o: StrictBool,
        describe=lambda o: "true or false",
    ),
    FieldType.DATE: TypeSpec(
        normalize_options=_no_options("date"),
        column_type=_fixed("date"),
        python_type=lambda o: dt.date,
        describe=lambda o: "date, ISO 8601 (YYYY-MM-DD)",
    ),
    FieldType.DATETIME: TypeSpec(
        normalize_options=_no_options("datetime"),
        column_type=_fixed("timestamptz"),
        python_type=lambda o: AwareDatetime,
        describe=lambda o: "date-time with time zone, ISO 8601 (e.g. 2026-01-31T09:00:00Z)",
    ),
    FieldType.EMAIL: TypeSpec(
        normalize_options=_no_options("email"),
        column_type=_fixed("varchar(320)"),
        python_type=lambda o: Annotated[
            StrictStr, Field(max_length=320), AfterValidator(_check_email)
        ],
        describe=lambda o: "email address",
    ),
    FieldType.URL: TypeSpec(
        normalize_options=_no_options("url"),
        column_type=_fixed("varchar(2048)"),
        python_type=lambda o: Annotated[
            StrictStr, Field(max_length=2048), AfterValidator(_check_url)
        ],
        describe=lambda o: "http or https URL",
    ),
    FieldType.PICKLIST: TypeSpec(
        normalize_options=_picklist_options,
        column_type=_fixed("varchar(100)"),
        python_type=_picklist_type,
        describe=lambda o: f"one of {active_picklist_values(o)}",
    ),
    FieldType.LOOKUP: TypeSpec(
        normalize_options=_lookup_options,
        column_type=_fixed("uuid"),
        python_type=lambda o: uuid.UUID,
        describe=lambda o: f"id (UUID) of an active {o.get('target', 'target')} record",
    ),
}


def parse_field_type(value: Any) -> FieldType:
    try:
        return FieldType(value)
    except ValueError:
        raise SchemaChangeError(
            f"Unknown field type {value!r}.",
            field="type",
            expected=[t.value for t in FieldType],
        ) from None
