"""Local development configuration (written by ``osier dev init``)."""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import psycopg

from .connectors import CredentialBroker
from .context import ActorType, Principal, Tenant
from .errors import OsierError
from .session import Session


def default_config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "osier"


def config_path() -> Path:
    override = os.environ.get("OSIER_CONFIG")
    return Path(override) if override else default_config_dir() / "dev.json"


@dataclass
class DevConfig:
    database_url: str
    tenant_id: str
    tenant_schema: str
    organization_id: str
    admin_user_id: str
    agent_user_id: str
    key_file: str
    allow_private_network: bool = False
    # Local development only: lets MCP connectors start local server processes (stdio).
    allow_local_processes: bool = True
    oauth_callback_port: int = 8765

    @property
    def tenant(self) -> Tenant:
        return Tenant(tenant_id=uuid.UUID(self.tenant_id), schema=self.tenant_schema)

    def admin(self, source: str = "cli") -> Principal:
        return Principal(user_id=uuid.UUID(self.admin_user_id), source=source)

    def agent(self, agent_id: str = "mcp") -> Principal:
        return Principal(
            user_id=uuid.UUID(self.agent_user_id),
            actor_type=ActorType.AGENT,
            on_behalf_of=uuid.UUID(self.admin_user_id),
            agent_id=agent_id,
            source="chat",
        )

    def connect(self) -> psycopg.Connection:
        return psycopg.connect(self.database_url, autocommit=True)

    def session(self, principal: Principal, conn: psycopg.Connection | None = None) -> Session:
        return Session(conn or self.connect(), self.tenant, principal)

    def ensure_current(self) -> None:
        """Bring this tenant up to the installed osier version: engine tables, system
        packages, and additive package upgrades. Cheap when already current."""
        from .packages import PackageService
        from .tenant import upgrade_engine_tables

        with self.connect() as conn:
            upgrade_engine_tables(conn, self.tenant)
            PackageService(self.session(Principal.system("upgrade"), conn)).ensure_current()

    def connectors(self, principal: Principal, conn: psycopg.Connection | None = None) -> Any:
        from .connectors import ConnectorService

        return ConnectorService(
            self.session(principal, conn),
            self.broker(),
            allow_private_network=self.allow_private_network,
            allow_local_processes=self.allow_local_processes,
        )

    def complete_oauth(self, state: str, code: str | None, error: str | None) -> tuple[bool, str]:
        """OAuth callback handler: finish the token exchange for a pending sign-in."""
        from .errors import OsierError

        with self.connect() as conn:
            try:
                done = self.connectors(Principal.system("oauth-callback"), conn).complete_mcp_oauth(
                    state, code=code, error=error
                )
            except OsierError as exc:
                return False, exc.message
        return True, f"{done['name']} is signed in. Return to Claude (or the terminal) to finish."

    def broker(self) -> CredentialBroker:
        return CredentialBroker.from_environment(Path(self.key_file))

    def save(self, path: Path | None = None) -> Path:
        path = path or config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2) + "\n")
        path.chmod(0o600)
        return path

    @classmethod
    def load(cls, path: Path | None = None) -> DevConfig:
        path = path or config_path()
        if not path.exists():
            raise OsierError(
                f"No osier configuration at {path}.",
                code="NOT_CONFIGURED",
                hint="Run 'osier dev init' first, or set OSIER_CONFIG.",
            )
        return cls(**json.loads(path.read_text()))


def write_key_file(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_bytes(CredentialBroker.generate_key())
    path.chmod(0o600)
