"""Entry point: a tenant-scoped session for one principal."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import psycopg

from .authz import Authorizer
from .changesets import SchemaService
from .context import Principal, Tenant
from .db import TenantConnection
from .metadata import MetadataRegistry, SchemaSnapshot
from .records import RecordService

if TYPE_CHECKING:
    from .packages import PackageService


class Session:
    """Engine access for one tenant and one principal.

    The connection must be in autocommit mode: the engine controls transactions
    itself, and some schema operations (concurrent index builds) cannot run inside
    a transaction.

    All database access goes through a ``TenantConnection``, the central check that
    every statement stays inside this tenant's schema (ARCHITECTURE.md §3.1).
    """

    def __init__(
        self, conn: psycopg.Connection | TenantConnection, tenant: Tenant, principal: Principal
    ) -> None:
        guarded = conn if isinstance(conn, TenantConnection) else TenantConnection(conn, tenant)
        if guarded.tenant != tenant:
            raise ValueError("The connection belongs to a different tenant.")
        # The only connection engine services ever see: every statement is checked
        # against this tenant before it runs (see db.py).
        self.conn = guarded
        self.tenant = tenant
        self.principal = principal
        self.metadata = MetadataRegistry(guarded, tenant)
        self.schema = SchemaService(guarded, tenant, principal)
        self.records = RecordService(guarded, tenant, principal)

    @property
    def skills(self) -> Any:
        """The tenant's skill library (Agent Skills format)."""
        from .skills import SkillService

        return SkillService(self)

    @property
    def authz(self) -> Authorizer:
        """Roles and permissions of the person behind this session."""
        return Authorizer(self.conn, self.tenant, self.principal)

    @property
    def packages(self) -> PackageService:
        from .packages import PackageService

        return PackageService(self)

    def snapshot(self) -> SchemaSnapshot:
        return self.metadata.snapshot()

    def describe_schema(self) -> dict[str, Any]:
        return self.metadata.snapshot().to_dict()

    def as_principal(self, principal: Principal) -> Session:
        return Session(self.conn, self.tenant, principal)
