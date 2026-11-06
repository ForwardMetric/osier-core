"""Packages: installable bundles of platform-owned (standard) objects.

A package is metadata only (objects, fields, seed records), never code. Packages
are installed and uninstalled through ordinary change sets with source
``package``; the engine rebuilds the expected operations from this registry and
rejects any change set that does not match exactly, so tenants and agents cannot
use the package path to create arbitrary standard objects.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from psycopg import sql

from ..changesets import ApplyResult, Proposal, parse_operations
from ..context import Principal, Tenant
from ..db import TenantConnection
from ..errors import NotFoundError, SchemaChangeError
from ..metadata import SchemaSnapshot
from . import case_management, core, crm


@dataclass(frozen=True)
class Package:
    name: str
    version: int
    label: str
    description: str
    depends: tuple[str, ...]
    operations: list[dict[str, Any]]
    seeds: list[tuple[str, str, dict[str, Any]]] = field(default_factory=list)
    removable: bool = True

    @property
    def object_names(self) -> list[str]:
        return [op["api_name"] for op in self.operations if op["op"] == "create_object"]

    def install_operations(self) -> list[dict[str, Any]]:
        return [
            {"op": "register_package", "package": self.name, "version": self.version},
            *self.operations,
        ]

    def uninstall_operations(self) -> list[dict[str, Any]]:
        # Children first, so lookups between the package's own objects don't block.
        retire = [{"op": "retire_object", "object": n} for n in reversed(self.object_names)]
        return [*retire, {"op": "unregister_package", "package": self.name}]


def _from_module(module: Any, *, removable: bool = True) -> Package:
    return Package(
        name=module.NAME,
        version=module.VERSION,
        label=module.LABEL,
        description=module.DESCRIPTION,
        depends=tuple(module.DEPENDS),
        operations=module.OPERATIONS,
        seeds=list(module.SEEDS),
        removable=removable,
    )


REGISTRY: dict[str, Package] = {
    p.name: p
    for p in (
        _from_module(core, removable=False),
        _from_module(crm),
        _from_module(case_management),
    )
}
DEFAULT_PACKAGES = ("core",)


def get_package(name: str) -> Package:
    package = REGISTRY.get(name)
    if package is None:
        raise NotFoundError(
            f"There is no package called {name!r}.",
            code="PACKAGE_NOT_FOUND",
            expected=sorted(REGISTRY),
        )
    return package


def _package_states(conn: TenantConnection, tenant: Tenant) -> dict[str, str]:
    rows = conn.execute(
        sql.SQL("SELECT package, status FROM {}").format(
            sql.Identifier(tenant.schema, "meta_packages")
        )
    ).fetchall()
    return {r[0]: r[1] for r in rows}


def verify_package_operations(
    conn: TenantConnection, snapshot: SchemaSnapshot, operations: list[Any]
) -> None:
    """Check a package change set against the registry and the installed packages."""
    states = _package_states(conn, snapshot.tenant)
    installed = {name for name, status in states.items() if status == "installed"}
    expected: list[dict[str, Any]] = []
    for op in operations:
        if op.op == "register_package":
            package = get_package(op.package)
            if op.version != package.version:
                raise SchemaChangeError(
                    f"Package {op.package!r} version {op.version} is not available.",
                    code="PACKAGE_MISMATCH",
                )
            if op.package in installed:
                raise SchemaChangeError(
                    f"Package {op.package!r} is already installed.",
                    code="PACKAGE_ALREADY_INSTALLED",
                )
            if states.get(op.package) == "uninstalled":
                raise SchemaChangeError(
                    f"Package {op.package!r} was uninstalled earlier; reinstalling is not "
                    "supported yet.",
                    code="PACKAGE_REINSTALL_NOT_SUPPORTED",
                )
            missing = [d for d in package.depends if d not in installed]
            if missing:
                raise SchemaChangeError(
                    f"Package {op.package!r} requires {missing} to be installed first.",
                    code="PACKAGE_DEPENDENCY_MISSING",
                    expected=missing,
                )
            installed.add(op.package)
            expected.extend(package.install_operations())
        elif op.op == "unregister_package":
            package = get_package(op.package)
            if op.package not in installed:
                raise SchemaChangeError(
                    f"Package {op.package!r} is not installed.", code="PACKAGE_NOT_INSTALLED"
                )
            if not package.removable:
                raise SchemaChangeError(
                    f"Package {op.package!r} is required by the platform and cannot be removed.",
                    code="PACKAGE_NOT_REMOVABLE",
                )
            installed.discard(op.package)
            dependents = sorted(n for n in installed if op.package in REGISTRY[n].depends)
            if dependents:
                raise SchemaChangeError(
                    f"Package {op.package!r} is required by {dependents}; uninstall those first.",
                    code="PACKAGE_HAS_DEPENDENTS",
                    expected=dependents,
                )
            expected.extend(package.uninstall_operations())
    actual = [op.model_dump(mode="json", exclude_unset=True) for op in operations]
    wanted = (
        [op.model_dump(mode="json", exclude_unset=True) for op in parse_operations(expected)]
        if expected
        else []
    )
    if actual != wanted:
        raise SchemaChangeError(
            "A package change set must contain exactly the package's own operations.",
            code="PACKAGE_MISMATCH",
        )


def _resolve(names: list[str], installed: set[str]) -> list[Package]:
    """Packages to install, dependencies first (depth-first topological order)."""
    order: list[Package] = []
    seen: set[str] = set()

    def visit(name: str, chain: tuple[str, ...]) -> None:
        if name in chain:
            raise SchemaChangeError(f"Package dependency cycle: {[*chain, name]}.")
        if name in seen or name in installed:
            return
        package = get_package(name)
        for dep in package.depends:
            visit(dep, (*chain, name))
        seen.add(name)
        order.append(package)

    for name in names:
        visit(name, ())
    return order


class PackageService:
    """Install, uninstall, and list packages for one tenant session."""

    def __init__(self, session: Any) -> None:  # Session (avoids an import cycle)
        self._session = session

    def states(self) -> dict[str, str]:
        return _package_states(self._session.conn, self._session.tenant)

    def available(self) -> list[dict[str, Any]]:
        states = self.states()
        return [
            {
                "package": p.name,
                "label": p.label,
                "description": p.description,
                "version": p.version,
                "depends": list(p.depends),
                "objects": p.object_names,
                "removable": p.removable,
                "status": states.get(p.name, "available"),
            }
            for p in REGISTRY.values()
        ]

    def propose_install(self, names: list[str]) -> Proposal:
        """Propose installing packages, adding any missing dependencies."""
        states = self.states()
        installed = {n for n, s in states.items() if s == "installed"}
        order = _resolve(list(names), installed)
        if not order:
            raise SchemaChangeError(
                f"{sorted(names)} already installed.", code="PACKAGE_ALREADY_INSTALLED"
            )
        operations = [op for p in order for op in p.install_operations()]
        return self._session.schema.propose(operations, source="package")

    def propose_uninstall(self, name: str) -> Proposal:
        return self._session.schema.propose(
            get_package(name).uninstall_operations(), source="package"
        )

    def apply(self, change_set_id: uuid.UUID, *, diff_hash: str) -> ApplyResult:
        """Apply an install or uninstall proposal, then load seed records."""
        result = self._session.schema.apply(change_set_id, diff_hash=diff_hash)
        row = self._session.conn.execute(
            sql.SQL("SELECT operations FROM {} WHERE id = %s").format(
                sql.Identifier(self._session.tenant.schema, "meta_change_sets")
            ),
            (change_set_id,),
        ).fetchone()
        for op in row[0] if row else []:
            if op.get("op") == "register_package":
                self.seed(op["package"])
        return result

    def seed(self, name: str) -> None:
        """Create the package's seed records. Safe to run repeatedly."""
        system = self._session.as_principal(Principal.system(source="package"))
        for object_name, key, values in get_package(name).seeds:
            system.records.create(object_name, values, idempotency_key=f"package:{name}:{key}")
