"""The job queue interface. Everything outside ``osier.jobs`` talks to this, never to
a queue library, so the backend can be replaced (another library, or a separate job
service) without touching the engine.

The contract between the engine and any backend is small:

- The engine marks workflow runs ``queued`` in the tenant's own schema; those rows are
  the source of truth (``osier.jobs.worker`` reads them).
- A backend receives run ids for a tenant (``hand_over``), runs them with that tenant's
  settings, and for each one calls ``osier.jobs.runner.execute_run``, the engine's
  single entry point for executing work. A separate job service would call the same
  entry point (or an API wrapping it).
- Job payloads carry identifiers only (tenant and run ids), never business data.
"""

from __future__ import annotations

from typing import Any, Protocol

import psycopg

from ..context import Tenant
from .settings import JobSettings


class JobBackend(Protocol):
    """A queue for tenants' background jobs."""

    name: str

    # Provisioning (platform code, raw connection): create or remove a tenant's queue.
    def install(self, conn: psycopg.Connection, tenant: Tenant) -> None: ...
    def drop(self, conn: psycopg.Connection, tenant: Tenant) -> None: ...

    # Queueing: hand queued runs over. Must tolerate the same run id twice.
    async def hand_over(
        self, tenant: Tenant, settings: JobSettings, run_ids: list[str]
    ) -> None: ...

    # Running: process the tenant's jobs until none are due (tests, ``--once``) and
    # return how many finished; or keep serving until cancelled.
    async def run_until_idle(self, tenant: Tenant, settings: JobSettings) -> int: ...
    async def serve(self, tenant: Tenant, settings: JobSettings) -> None: ...

    # Housekeeping: recover jobs of crashed workers, apply retention.
    async def maintain(self, tenant: Tenant, settings: JobSettings) -> None: ...

    # Reading, for people with jobs.manage.
    async def list_jobs(
        self, tenant: Tenant, settings: JobSettings, *, status: str | None, limit: int
    ) -> list[dict[str, Any]]: ...
