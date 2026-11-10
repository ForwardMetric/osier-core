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
    # How the OAuth callback address is written: "127.0.0.1", "localhost", or a full
    # https URL of a tunnel that forwards to the local callback port.
    oauth_redirect: str = "127.0.0.1"

    def redirect_uri(self) -> str:
        from .connectors.oauth_callback import CALLBACK_PATH, redirect_uri

        if self.oauth_redirect.startswith("https://"):
            base = self.oauth_redirect.rstrip("/")
            return base if base.endswith(CALLBACK_PATH) else base + CALLBACK_PATH
        return redirect_uri(self.oauth_callback_port, self.oauth_redirect)

    @property
    def tenant(self) -> Tenant:
        return Tenant(tenant_id=uuid.UUID(self.tenant_id), schema=self.tenant_schema)

    def admin(self, source: str = "cli") -> Principal:
        return Principal(user_id=uuid.UUID(self.admin_user_id), source=source)

    def person(self, user_id: uuid.UUID, source: str = "cli") -> Principal:
        return Principal(user_id=user_id, source=source)

    def agent(self, person_id: uuid.UUID | None = None, agent_id: str = "mcp") -> Principal:
        """The MCP agent acting for a person (the administrator by default)."""
        return Principal(
            user_id=uuid.UUID(self.agent_user_id),
            actor_type=ActorType.AGENT,
            on_behalf_of=person_id or uuid.UUID(self.admin_user_id),
            agent_id=agent_id,
            source="chat",
        )

    def find_person(self, email: str) -> uuid.UUID:
        from .errors import NotFoundError

        with self.connect() as conn:
            matches = self.session(Principal.system("cli"), conn).records.list(
                "app_user", where={"email": email}, limit=1
            )
        if not matches:
            raise NotFoundError(
                f"No user with email {email}.",
                code="USER_NOT_FOUND",
                hint="Add them with 'osier dev add-user'.",
            )
        return uuid.UUID(matches[0]["id"])

    def connect(self) -> psycopg.Connection:
        return psycopg.connect(self.database_url, autocommit=True)

    def session(self, principal: Principal, conn: psycopg.Connection | None = None) -> Session:
        return Session(conn or self.connect(), self.tenant, principal)

    def ensure_current(self) -> None:
        """Bring this tenant up to the installed osier version: engine tables, system
        packages, and additive package upgrades. Cheap when already current."""
        from .packages import PackageService
        from .starter_skills import install_starter_skills
        from .tenant import upgrade_engine_tables

        with self.connect() as conn:
            upgrade_engine_tables(conn, self.tenant)
            system = self.session(Principal.system("upgrade"), conn)
            PackageService(system).ensure_current()
            install_starter_skills(system)

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
