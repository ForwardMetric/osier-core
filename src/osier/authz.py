"""Authorization: what the person behind a request may do.

Permissions are fixed by the platform; each tenant decides which roles hold them
(``meta_role_permissions``). Users get roles through ``user_role`` records. An agent
has exactly the permissions of the person it acts for (``on_behalf_of``); the
platform's system principal has all of them.

Finer, per-object and per-field permissions come later (ARCHITECTURE.md §8).
"""

from __future__ import annotations

import uuid
from typing import Any

from psycopg import sql

from .context import ActorType, Principal, Tenant
from .db import TenantConnection
from .errors import OsierError

PERMISSIONS: dict[str, str] = {
    "schema.manage": "create and change objects and fields",
    "packages.manage": "install, upgrade, and uninstall packages",
    "connections.manage": "connect apps and accounts, share them, and set tool policies",
    "users.manage": "invite users and assign roles",
    "records.use": "read and write business records",
    "actions.request": "have agents send email and use connected apps",
}

DEFAULT_ROLE_PERMISSIONS: dict[str, tuple[str, ...]] = {
    "admin": tuple(PERMISSIONS),
    "developer": (
        "schema.manage",
        "packages.manage",
        "connections.manage",
        "records.use",
        "actions.request",
    ),
    "member": ("records.use", "actions.request"),
}


class PermissionDenied(OsierError):
    default_code = "MISSING_PERMISSION"


def acting_person(principal: Principal) -> uuid.UUID | None:
    """The human whose permissions apply: the person an agent acts for, or the user."""
    if principal.actor_type is ActorType.AGENT:
        return principal.on_behalf_of
    return principal.user_id


class Authorizer:
    def __init__(self, conn: TenantConnection, tenant: Tenant, principal: Principal) -> None:
        self._conn = conn
        self._tenant = tenant
        self._principal = principal
        self._cache: tuple[list[str], set[str]] | None = None

    def _t(self, name: str) -> sql.Identifier:
        return sql.Identifier(self._tenant.schema, name)

    def _load(self) -> tuple[list[str], set[str]]:
        if self._cache is not None:
            return self._cache
        person = acting_person(self._principal)
        if person is None:
            self._cache = ([], set())
            return self._cache
        # No table aliases: the tenant guard rejects dotted names in SQL text.
        rows = self._conn.execute(
            sql.SQL(
                "SELECT DISTINCT key FROM {r} WHERE retired_at IS NULL AND id IN ("
                "SELECT role FROM {ur} WHERE app_user = %s AND retired_at IS NULL "
                "AND (start_date IS NULL OR start_date <= current_date) "
                "AND (end_date IS NULL OR end_date >= current_date)) ORDER BY key"
            ).format(ur=self._t("user_role"), r=self._t("role")),
            (person,),
        ).fetchall()
        roles = [r[0] for r in rows]
        perms = {
            r[0]
            for r in self._conn.execute(
                sql.SQL("SELECT permission FROM {} WHERE role_key = ANY(%s)").format(
                    self._t("meta_role_permissions")
                ),
                (roles,),
            ).fetchall()
        }
        self._cache = (roles, perms)
        return self._cache

    def roles(self) -> list[str]:
        return self._load()[0]

    def permissions(self) -> set[str]:
        if self._principal.actor_type is ActorType.SYSTEM:
            return set(PERMISSIONS)
        return self._load()[1]

    def has(self, permission: str) -> bool:
        return permission in self.permissions()

    def require(self, permission: str, doing: str) -> None:
        if self.has(permission):
            return
        granting = sorted(
            r[0]
            for r in self._conn.execute(
                sql.SQL("SELECT role_key FROM {} WHERE permission = %s").format(
                    self._t("meta_role_permissions")
                ),
                (permission,),
            ).fetchall()
        )
        labels = self._role_labels(granting)
        raise PermissionDenied(
            f"{doing.capitalize()} requires "
            + (
                f"the {labels[0]} role. "
                if len(labels) == 1
                else f"one of these roles: {', '.join(labels)}. "
                if labels
                else f"the {permission} permission, which no role has. "
            )
            + f"Your roles: {', '.join(self._role_labels(self.roles())) or 'none'}.",
            field=permission,
            expected=granting,
            hint=self._who_can_grant(),
            details={
                "permission": permission,
                "permission_means": PERMISSIONS.get(permission),
                "your_roles": self.roles(),
                "roles_with_permission": granting,
            },
        )

    def _role_labels(self, keys: list[str]) -> list[str]:
        if not keys:
            return []
        rows = self._conn.execute(
            sql.SQL("SELECT key, name FROM {} WHERE key = ANY(%s) ORDER BY key").format(
                self._t("role")
            ),
            (keys,),
        ).fetchall()
        names = {r[0]: r[1] for r in rows}
        return [names.get(k, k) for k in keys]

    def _who_can_grant(self) -> str:
        rows = self._conn.execute(
            sql.SQL(
                "SELECT display_name FROM {u} WHERE retired_at IS NULL AND status = 'active' "
                "AND id IN (SELECT app_user FROM {ur} WHERE retired_at IS NULL AND role IN ("
                "SELECT id FROM {r} WHERE retired_at IS NULL AND key IN ("
                "SELECT role_key FROM {rp} WHERE permission = %s))) "
                "ORDER BY display_name LIMIT 5"
            ).format(
                u=self._t("app_user"),
                ur=self._t("user_role"),
                r=self._t("role"),
                rp=self._t("meta_role_permissions"),
            ),
            ("users.manage",),
        ).fetchall()
        names = [r[0] for r in rows]
        if not names:
            return "Ask an administrator to grant it."
        return f"Ask an administrator ({', '.join(names)}) to grant it."


def describe(authorizer: Authorizer) -> dict[str, Any]:
    return {
        "roles": authorizer.roles(),
        "permissions": sorted(authorizer.permissions()),
    }
