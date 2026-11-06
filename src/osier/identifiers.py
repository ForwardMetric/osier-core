"""Naming rules for objects, fields, and tenant schemas.

Physical database names are derived from validated API names and are always
passed to SQL as quoted identifiers (``psycopg.sql.Identifier``). User or AI
supplied strings are never interpolated into SQL text.
"""

from __future__ import annotations

import re
import uuid
from enum import StrEnum

from .errors import InvalidNameError

NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,47}$")
MAX_NAME_LENGTH = 48
CUSTOM_PREFIX = "c_"


class Owner(StrEnum):
    STANDARD = "standard"  # platform-owned
    CUSTOM = "custom"  # tenant-owned


# Columns present on every object table.
SYSTEM_COLUMNS = frozenset(
    {
        "id",
        "created_at",
        "created_by",
        "updated_at",
        "updated_by",
        "owner_id",
        "row_version",
        "retired_at",
        "retired_by",
    }
)

# Engine tables inside each tenant schema.
ENGINE_TABLES = frozenset(
    {
        "meta_schema_version",
        "meta_objects",
        "meta_fields",
        "meta_change_sets",
        "meta_packages",
        "meta_idempotency",
        "field_history",
        "field_history_default",
        "record_events",
    }
)

RESERVED_PREFIXES = ("meta_", "pg_", "osier_", "sys_")

# A conservative set of SQL keywords. Identifiers are always quoted, so these are
# rejected for clarity (and to keep raw SQL written by humans readable), not safety.
SQL_KEYWORDS = frozenset(
    """
    all analyse analyze and any array as asc asymmetric authorization between binary both
    case cast check collate column constraint create cross current_date current_role
    current_time current_timestamp current_user default deferrable desc distinct do else
    end except false fetch for foreign freeze from full grant group having ilike in
    initially inner intersect into is isnull join lateral leading left like limit
    localtime localtimestamp natural not notnull null offset on only or order outer
    overlaps placing primary references returning right select session_user similar
    some symmetric table tablesample then to trailing true union unique user using
    variadic verbose when where window with
    """.split()
)


def validate_api_name(name: object, *, kind: str, owner: Owner) -> str:
    """Validate an object or field API name and return it.

    ``kind`` is ``"object"`` or ``"field"`` and only affects messages and the
    reserved-name checks.
    """
    if not isinstance(name, str):
        raise InvalidNameError(f"The {kind} name must be a string.", field="api_name")

    if not NAME_PATTERN.fullmatch(name):
        raise InvalidNameError(
            f"Invalid {kind} name {name!r}.",
            field="api_name",
            expected=f"lowercase letters, digits and underscores, starting with a letter, "
            f"at most {MAX_NAME_LENGTH} characters",
            hint=f"Try {suggest_name(name, owner)!r}.",
        )
    if "__" in name or name.endswith("_"):
        raise InvalidNameError(
            f"Invalid {kind} name {name!r}: no double or trailing underscores.",
            field="api_name",
            hint=f"Try {suggest_name(name, owner)!r}.",
        )
    if name in SQL_KEYWORDS:
        raise InvalidNameError(
            f"{name!r} is a reserved word.", field="api_name", hint=f"Try '{name}_value'."
        )
    if name.startswith(RESERVED_PREFIXES):
        raise InvalidNameError(
            f"{kind.capitalize()} names may not start with {', '.join(RESERVED_PREFIXES)}.",
            field="api_name",
        )
    if kind == "field" and name in SYSTEM_COLUMNS:
        raise InvalidNameError(
            f"{name!r} is a system field present on every object.", field="api_name"
        )
    if kind == "object" and name in ENGINE_TABLES:
        raise InvalidNameError(f"{name!r} is reserved by the platform.", field="api_name")

    is_custom_name = name.startswith(CUSTOM_PREFIX)
    if owner is Owner.CUSTOM and not is_custom_name:
        raise InvalidNameError(
            f"Custom {kind} names must start with {CUSTOM_PREFIX!r}.",
            field="api_name",
            hint=f"Use {suggest_name(name, owner)!r}.",
        )
    if owner is Owner.STANDARD and is_custom_name:
        raise InvalidNameError(
            f"Standard {kind} names may not start with {CUSTOM_PREFIX!r}.", field="api_name"
        )
    return name


def suggest_name(raw: str, owner: Owner) -> str:
    """Best-effort conversion of free text (e.g. a label) into a valid API name."""
    text = re.sub(r"[^a-z0-9]+", "_", str(raw).lower()).strip("_")
    text = re.sub(r"_+", "_", text) or "field"
    if not text[0].isalpha():
        text = f"x_{text}"
    if owner is Owner.CUSTOM and not text.startswith(CUSTOM_PREFIX):
        text = CUSTOM_PREFIX + text
    if owner is Owner.STANDARD and text.startswith(CUSTOM_PREFIX):
        text = text[len(CUSTOM_PREFIX) :]
    return text[:MAX_NAME_LENGTH].rstrip("_")


def new_tenant_schema_name() -> str:
    """Generate a tenant schema name. Never derived from user input."""
    return f"t_{uuid.uuid4().hex}"


TENANT_SCHEMA_PATTERN = re.compile(r"^t_[0-9a-f]{32}$")


def validate_tenant_schema(name: str) -> str:
    if not TENANT_SCHEMA_PATTERN.fullmatch(name):
        raise InvalidNameError(f"Invalid tenant schema name {name!r}.")
    return name


def index_name(prefix: str, field_id: uuid.UUID) -> str:
    """Stable, length-safe name for an index or constraint tied to a field."""
    return f"{prefix}_{field_id.hex}"
