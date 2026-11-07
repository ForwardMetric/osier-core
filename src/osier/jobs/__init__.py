"""Background jobs. The rest of osier uses only this module's public API; the queue
library sits behind ``JobBackend`` (``base.py``) and can be replaced.

- ``JobSettings`` / ``JobSettingsService``: per-tenant settings (backend-neutral).
- ``default_backend``: the configured backend (Procrastinate today).
- ``install_queue`` / ``drop_queue``: provisioning hooks used by ``osier.tenant``.
- ``Worker``: the worker loop; ``list_jobs``: reading jobs (``jobs.manage``).
- ``runner.execute_run``: what any backend calls to execute a run.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import psycopg

from ..context import Tenant
from .base import JobBackend
from .procrastinate_backend import ProcrastinateBackend
from .runner import Connect, execute_run
from .settings import JobSettings, JobSettingsService, describe_settings
from .worker import Worker

# The backend used for provisioning. Change here (or make configurable) to switch.
BACKEND_CLASS = ProcrastinateBackend


def default_backend(conninfo: str, connect: Connect | None = None) -> JobBackend:
    return BACKEND_CLASS(conninfo, connect or (lambda: psycopg.connect(conninfo, autocommit=True)))


def install_queue(conn: psycopg.Connection, tenant: Tenant) -> None:
    BACKEND_CLASS.install(conn, tenant)


def drop_queue(conn: psycopg.Connection, tenant: Tenant) -> None:
    BACKEND_CLASS.drop(conn, tenant)


def list_jobs(
    backend: JobBackend, session: Any, *, status: str | None = None, limit: int = 50
) -> list[dict[str, Any]]:
    session.authz.require("jobs.manage", "viewing background jobs")
    settings = JobSettingsService(session).get()
    jobs = asyncio.run(backend.list_jobs(session.tenant, settings, status=status, limit=limit))
    return json.loads(json.dumps(jobs, default=str))


__all__ = [
    "JobBackend",
    "JobSettings",
    "JobSettingsService",
    "Worker",
    "default_backend",
    "describe_settings",
    "drop_queue",
    "execute_run",
    "install_queue",
    "list_jobs",
]
