"""Create a ready-to-use tenant: schema, core package, organization, first administrator."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import psycopg

from .context import Principal, Tenant
from .packages import DEFAULT_PACKAGES, PackageService
from .session import Session
from .tenant import provision_tenant


@dataclass(frozen=True)
class Bootstrap:
    tenant: Tenant
    organization_id: uuid.UUID
    admin_user_id: uuid.UUID

    @property
    def admin(self) -> Principal:
        return Principal(user_id=self.admin_user_id)


def bootstrap_tenant(
    conn: psycopg.Connection,
    *,
    organization_name: str,
    admin_email: str,
    admin_display_name: str,
    tenant_id: uuid.UUID | None = None,
) -> Bootstrap:
    """Provision a tenant with the core package, its organization, and an administrator."""
    tenant = provision_tenant(conn, tenant_id)
    system = Session(conn, tenant, Principal.system(source="bootstrap"))
    packages = PackageService(system)
    proposal = packages.propose_install(list(DEFAULT_PACKAGES))
    packages.apply(proposal.id, diff_hash=proposal.diff_hash)

    org = system.records.create(
        "organization",
        {"name": organization_name, "unit_type": "company"},
        idempotency_key="bootstrap:organization",
    )
    admin = system.records.create(
        "app_user",
        {
            "display_name": admin_display_name,
            "email": admin_email,
            "status": "active",
            "organization": org["id"],
        },
        idempotency_key="bootstrap:admin",
    )
    admin_role = system.records.list("role", where={"key": "admin"}, limit=1)[0]
    system.records.create(
        "user_role",
        {"app_user": admin["id"], "role": admin_role["id"]},
        idempotency_key="bootstrap:admin-role",
    )
    return Bootstrap(
        tenant=tenant,
        organization_id=uuid.UUID(org["id"]),
        admin_user_id=uuid.UUID(admin["id"]),
    )
