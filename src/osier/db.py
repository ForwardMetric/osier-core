"""The single point through which the engine talks to the database for a tenant.

Every engine service receives a ``TenantConnection`` instead of a raw psycopg
connection. Before any statement runs, it is checked:

1. It must be composed with ``psycopg.sql`` (plain strings are rejected), so SQL
   text never contains interpolated data.
2. Every schema-qualified identifier must name *this* tenant's schema.
   Three-part names are rejected.
3. Literal SQL fragments may not contain quoted identifiers or dotted names, so
   qualification cannot be smuggled past check 2 in raw text.

Unqualified table names already fail at the database because sessions set an
empty ``search_path``. Together these make cross-tenant access through the
engine a loud error instead of a silent leak.

The check walks psycopg's ``Composed`` objects, which relies on psycopg
internals (``_obj``). ``tests/test_db_guard.py`` fails if those change.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from typing import Any

import psycopg
from psycopg import sql

from .context import Tenant
from .errors import OsierError

_DOTTED_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*\s*\.\s*[A-Za-z_\"]")


class TenantIsolationError(OsierError):
    """A statement tried to reach outside the session's tenant. Always a bug."""

    default_code = "TENANT_ISOLATION_VIOLATION"


def _walk(node: Any) -> Iterator[sql.Composable]:
    if isinstance(node, sql.Composed):
        for child in node._obj:
            yield from _walk(child)
    else:
        yield node


class TenantConnection:
    """A psycopg connection restricted to one tenant schema."""

    def __init__(self, conn: psycopg.Connection, tenant: Tenant) -> None:
        if not conn.autocommit:
            raise ValueError("The connection must be in autocommit mode.")
        self._conn = conn
        self.tenant = tenant
        # Defense in depth: unqualified names cannot resolve to any schema.
        conn.execute("SET search_path TO ''")

    # ---- the check -------------------------------------------------------------------

    def check(self, query: Any) -> None:
        if not isinstance(query, sql.Composable):
            raise TenantIsolationError(
                "Engine SQL must be composed with psycopg.sql, not passed as a string."
            )
        schema = self.tenant.schema
        for part in _walk(query):
            if isinstance(part, sql.Identifier):
                names = part._obj
                if len(names) > 2:
                    raise TenantIsolationError(
                        f"Three-part identifier {'.'.join(names)!r} is not allowed."
                    )
                if len(names) == 2 and names[0] != schema:
                    raise TenantIsolationError(
                        f"Statement references schema {names[0]!r} outside tenant {schema!r}."
                    )
            elif isinstance(part, sql.SQL):
                text = part._obj
                if '"' in text or _DOTTED_NAME.search(text):
                    raise TenantIsolationError(
                        "Literal SQL fragments may not contain quoted or dotted names; "
                        "use sql.Identifier(schema, table)."
                    )

    # ---- psycopg-like API --------------------------------------------------------------

    def execute(self, query: Any, params: Any = None) -> psycopg.Cursor:
        self.check(query)
        return self._conn.execute(query, params)

    def cursor(self, **kwargs: Any) -> _TenantCursor:
        return _TenantCursor(self, self._conn.cursor(**kwargs))

    def transaction(self) -> Any:
        return self._conn.transaction()

    def render(self, query: sql.Composable) -> str:
        """Render a statement as text for display (proposals show their DDL)."""
        self.check(query)
        return query.as_string(self._conn)

    @property
    def autocommit(self) -> bool:
        return self._conn.autocommit


class _TenantCursor:
    def __init__(self, guard: TenantConnection, cursor: psycopg.Cursor) -> None:
        self._guard = guard
        self._cursor = cursor

    def execute(self, query: Any, params: Any = None) -> _TenantCursor:
        self._guard.check(query)
        self._cursor.execute(query, params)
        return self

    def executemany(self, query: Any, params_seq: Iterable[Any]) -> None:
        self._guard.check(query)
        self._cursor.executemany(query, params_seq)

    def fetchone(self) -> Any:
        return self._cursor.fetchone()

    def fetchall(self) -> list[Any]:
        return self._cursor.fetchall()

    def __enter__(self) -> _TenantCursor:
        return self

    def __exit__(self, *exc: Any) -> None:
        self._cursor.close()

