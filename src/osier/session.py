"""Entry point: a tenant-scoped session for one principal."""

from __future__ import annotations

from typing import Any

import psycopg

from .changesets import SchemaService
from .context import Principal, Tenant
from .metadata import MetadataRegistry, SchemaSnapshot
from .records import RecordService


class Session:
    """Engine access for one tenant and one principal.

    The connection must be in autocommit mode: the engine controls transactions
    itself, and some schema operations (concurrent index builds) cannot run inside
    a transaction.

    The session sets an empty ``search_path`` so that any SQL referring to a table
    without its tenant schema fails loudly instead of silently reaching the wrong
    schema (ARCHITECTURE.md §3.1).
    """

    def __init__(self, conn: psycopg.Connection, tenant: Tenant, principal: Principal) -> None:
        if not conn.autocommit:
            raise ValueError("The connection must be in autocommit mode.")
        conn.execute("SET search_path TO ''")
        self.conn = conn
        self.tenant = tenant
        self.principal = principal
        self.metadata = MetadataRegistry(conn, tenant)
        self.schema = SchemaService(conn, tenant, principal)
        self.records = RecordService(conn, tenant, principal)

    def snapshot(self) -> SchemaSnapshot:
        return self.metadata.snapshot()

    def describe_schema(self) -> dict[str, Any]:
        return self.metadata.snapshot().to_dict()

    def as_principal(self, principal: Principal) -> Session:
        return Session(self.conn, self.tenant, principal)
