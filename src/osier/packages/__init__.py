"""Packages: installable bundles of platform-owned (standard) objects.

A package is metadata only (objects, fields, seed records), never code. Packages
are installed, upgraded, and uninstalled through ordinary change sets with source
``package``; the engine rebuilds the expected operations from this registry and
rejects any change set that does not match exactly, so tenants and agents cannot
use the package path to create arbitrary standard objects.

**System packages** (e.g. ``core``) are installed when a tenant is created, can
never be uninstalled, and may depend only on other system packages. **Optional
packages** (e.g. ``crm``) are chosen by the tenant.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from psycopg import sql

from ..changesets import ApplyResult, Proposal, parse_operations
from ..context import Principal, Tenant
from ..db import TenantConnection
from ..errors import IntegrityError, NotFoundError, SchemaChangeError
from ..metadata import SchemaSnapshot
from . import case_management, core, crm, integrations
from .upgrade import compute_upgrade


@dataclass(frozen=True)
class Package:
    name: str
    version: int
    label: str
    description: str
    depends: tuple[str, ...]
    operations: list[dict[str, Any]]
    seeds: list[tuple[str, str, dict[str, Any]]] = field(default_factory=list)
    system: bool = False

    @property
    def removable(self) -> bool:
        return not self.system

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


def _from_module(module: Any, *, system: bool = False) -> Package:
    return Package(
        name=module.NAME,
        version=module.VERSION,
        label=module.LABEL,
        description=module.DESCRIPTION,
        depends=tuple(module.DEPENDS),
        operations=module.OPERATIONS,
        seeds=list(module.SEEDS),
        system=system,
    )


def validate_registry(registry: dict[str, Package]) -> None:
    """Registry invariants: known dependencies, no cycles, system packages depend only
    on system packages."""
    for package in registry.values():
        for dep in package.depends:
            if dep not in registry:
                raise ValueError(f"{package.name} depends on unknown package {dep}")
            if package.system and not registry[dep].system:
                raise ValueError(f"system package {package.name} depends on optional {dep}")
    for name in registry:
        _resolve_in(registry, [name], set())  # raises on cycles


REGISTRY: dict[str, Package] = {
    p.name: p
    for p in (
        _from_module(core, system=True),
        _from_module(integrations, system=True),
        _from_module(crm),
        _from_module(case_management),
    )
}


def system_package_names() -> list[str]:
    return [p.name for p in REGISTRY.values() if p.system]


def get_package(name: str) -> Package:
    package = REGISTRY.get(name)
    if package is None:
        raise NotFoundError(
            f"There is no package called {name!r}.",
            code="PACKAGE_NOT_FOUND",
            expected=sorted(REGISTRY),
        )
    return package


@dataclass(frozen=True)
class _Installed:
    status: str
    version: int
    definition: list[dict[str, Any]]


def _package_rows(conn: TenantConnection, tenant: Tenant) -> dict[str, _Installed]:
    rows = conn.execute(
        sql.SQL("SELECT package, status, version, definition FROM {}").format(
            sql.Identifier(tenant.schema, "meta_packages")
        )
    ).fetchall()
    return {r[0]: _Installed(r[1], r[2], r[3]) for r in rows}


def _package_states(conn: TenantConnection, tenant: Tenant) -> dict[str, str]:
    return {name: row.status for name, row in _package_rows(conn, tenant).items()}


def upgrade_operations(
    name: str, installed: _Installed, snapshot: SchemaSnapshot
) -> tuple[list[dict[str, Any]], list[str]]:
    package = get_package(name)
    ops, notes = compute_upgrade(name, installed.definition, package.operations, snapshot)
    marker = {
        "op": "upgrade_package",
        "package": name,
        "from_version": installed.version,
        "to_version": package.version,
    }
    return [marker, *ops], notes


def verify_package_operations(
    conn: TenantConnection, snapshot: SchemaSnapshot, operations: list[Any]
) -> None:
    """Check a package change set against the registry and the installed packages."""
    rows = _package_rows(conn, snapshot.tenant)
    states = {name: row.status for name, row in rows.items()}
    installed = {name for name, status in states.items() if status == "installed"}
    expected: list[dict[str, Any]] = []
    for op in operations:
        if op.op == "upgrade_package":
            package = get_package(op.package)
            row = rows.get(op.package)
            if row is None or row.status != "installed":
                raise SchemaChangeError(
                    f"Package {op.package!r} is not installed.", code="PACKAGE_NOT_INSTALLED"
                )
            if op.from_version != row.version or op.to_version != package.version:
                raise SchemaChangeError(
                    f"Package {op.package!r} upgrade {op.from_version}->{op.to_version} does "
                    f"not match installed {row.version} / available {package.version}.",
                    code="PACKAGE_MISMATCH",
                )
            expected.extend(upgrade_operations(op.package, row, snapshot)[0])
            continue
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
    return _resolve_in(REGISTRY, names, installed)


def _resolve_in(
    registry: dict[str, Package], names: list[str], installed: set[str]
) -> list[Package]:
    """Packages to install, dependencies first (depth-first topological order)."""
    order: list[Package] = []
    seen: set[str] = set()

    def visit(name: str, chain: tuple[str, ...]) -> None:
        if name in chain:
            raise SchemaChangeError(f"Package dependency cycle: {[*chain, name]}.")
        if name in seen or name in installed:
            return
        package = registry.get(name) or get_package(name)
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
        rows = _package_rows(self._session.conn, self._session.tenant)
        states = {name: row.status for name, row in rows.items()}
        return [
            {
                "package": p.name,
                "label": p.label,
                "description": p.description,
                "version": p.version,
                "depends": list(p.depends),
                "objects": p.object_names,
                "system": p.system,
                "removable": p.removable,
                "status": states.get(p.name, "available"),
                "installed_version": rows[p.name].version if p.name in rows else None,
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

    def propose_upgrade(self, name: str) -> Proposal | None:
        """Propose upgrading an installed package to the registry version.

        Returns None when the package is already current. Raises
        ``PACKAGE_UPGRADE_UNSAFE`` when the new version contains non-additive changes.
        """
        package = get_package(name)
        row = _package_rows(self._session.conn, self._session.tenant).get(name)
        if row is None or row.status != "installed":
            raise SchemaChangeError(
                f"Package {name!r} is not installed.", code="PACKAGE_NOT_INSTALLED"
            )
        if row.version == package.version:
            return None
        if row.version > package.version:
            raise SchemaChangeError(
                f"Package {name!r} is at version {row.version}, newer than the available "
                f"version {package.version}; downgrades are not supported.",
                code="PACKAGE_DOWNGRADE_NOT_SUPPORTED",
            )
        operations, _notes = upgrade_operations(name, row, self._session.snapshot())
        return self._session.schema.propose(operations, source="package")

    def upgrade_notes(self, name: str) -> list[str]:
        """Changes in the new version that an upgrade will not apply (and why)."""
        row = _package_rows(self._session.conn, self._session.tenant)[name]
        return upgrade_operations(name, row, self._session.snapshot())[1]

    def ensure_current(self) -> list[ApplyResult]:
        """Bring the tenant up to date, applied by the platform without human approval:
        install any missing system packages, then upgrade every installed package whose
        new version is purely additive. Unsafe upgrades raise and change nothing."""
        platform = PackageService(self._session.as_principal(Principal.system("upgrade")))
        results = []
        states = platform.states()
        missing = [n for n in system_package_names() if states.get(n) != "installed"]
        if missing:
            proposal = platform.propose_install(missing)
            results.append(platform.apply(proposal.id, diff_hash=proposal.diff_hash))
        for name, status in platform.states().items():
            if status != "installed" or name not in REGISTRY:
                continue
            proposal = platform.propose_upgrade(name)
            if proposal is not None:
                results.append(platform.apply(proposal.id, diff_hash=proposal.diff_hash))
        return results

    def preview_install(self, names: list[str]) -> dict[str, Any]:
        """What installing would do, without storing anything (for confirmations)."""
        installed = {n for n, s in self.states().items() if s == "installed"}
        order = _resolve(list(names), installed)
        if not order:
            raise SchemaChangeError(
                f"{sorted(names)} already installed.", code="PACKAGE_ALREADY_INSTALLED"
            )
        operations = [op for p in order for op in p.install_operations()]
        return self._session.schema.preview(operations, source="package")

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
            if op.get("op") in ("register_package", "upgrade_package"):
                self.seed(op["package"])
        return result

    def seed(self, name: str) -> None:
        """Create the package's seed records. Safe to run repeatedly."""
        system = self._session.as_principal(Principal.system(source="package"))
        for object_name, key, values in get_package(name).seeds:
            try:
                system.records.create(object_name, values, idempotency_key=f"package:{name}:{key}")
            except IntegrityError as exc:
                # Seeded before with different values (seed changed in a newer version,
                # or the tenant edited the record): keep what the tenant has.
                if exc.code != "IDEMPOTENCY_KEY_REUSED":
                    raise


validate_registry(REGISTRY)
