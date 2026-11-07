"""The worker loop, independent of the queue backend: dispatch triggers, hand queued
runs to the backend, and let the backend run each tenant's jobs with its settings."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg import sql

from ..context import Principal, Tenant
from .base import JobBackend
from .runner import Connect
from .settings import JobSettings, JobSettingsService

log = logging.getLogger("osier.jobs")
MAINTENANCE_SECONDS = 60


def queued_runs(session: Any, limit: int = 200) -> list[str]:
    table = sql.Identifier(session.tenant.schema, "workflow_runs")
    rows = session.conn.execute(
        sql.SQL(
            "SELECT id FROM {} WHERE status = 'queued' AND deferred_at IS NULL "
            "ORDER BY started_at LIMIT %s"
        ).format(table),
        (limit,),
    ).fetchall()
    return [str(r[0]) for r in rows]


def mark_handed_over(session: Any, run_ids: Iterable[str]) -> None:
    session.conn.execute(
        sql.SQL("UPDATE {} SET deferred_at = now() WHERE id = ANY(%s)").format(
            sql.Identifier(session.tenant.schema, "workflow_runs")
        ),
        ([uuid.UUID(r) for r in run_ids],),
    )


@dataclass
class _Runtime:
    settings: JobSettings
    task: asyncio.Task[None] | None = None
    last_maintenance: float = 0.0


class Worker:
    def __init__(
        self, backend: JobBackend, tenants: Callable[[], Iterable[Tenant]], connect: Connect
    ) -> None:
        self.backend, self.tenants, self.connect = backend, tenants, connect
        self._runtimes: dict[uuid.UUID, _Runtime] = {}

    def _system(self, conn: psycopg.Connection, tenant: Tenant) -> Any:
        from ..session import Session

        return Session(conn, tenant, Principal.system("worker"))

    def _dispatch(self, tenant: Tenant) -> tuple[dict[str, int], list[str], JobSettings]:
        from ..workflows.triggers import dispatch_record_events, dispatch_schedules

        with self.connect() as conn:
            system = self._system(conn, tenant)
            counts = {
                "events": dispatch_record_events(system),
                "scheduled": dispatch_schedules(system),
            }
            return counts, queued_runs(system), JobSettingsService(system).get()

    def _mark(self, tenant: Tenant, run_ids: list[str]) -> None:
        with self.connect() as conn:
            mark_handed_over(self._system(conn, tenant), run_ids)

    async def _prepare(self, tenant: Tenant) -> tuple[_Runtime, dict[str, int]]:
        counts, run_ids, settings = await asyncio.to_thread(self._dispatch, tenant)
        runtime = self._runtimes.get(tenant.tenant_id)
        if runtime is None or runtime.settings != settings:
            if runtime and runtime.task:
                runtime.task.cancel()  # restart with the new settings
            runtime = _Runtime(settings)
            self._runtimes[tenant.tenant_id] = runtime
        if run_ids:
            await self.backend.hand_over(tenant, settings, run_ids)
            await asyncio.to_thread(self._mark, tenant, run_ids)
        counts["handed_over"] = len(run_ids)
        return runtime, counts

    async def run_until_idle(self, *, max_rounds: int = 20) -> dict[str, int]:
        """Dispatch and run jobs until nothing is left (tests, ``osier worker --once``)."""
        total = {"events": 0, "scheduled": 0, "handed_over": 0, "jobs": 0}
        for _ in range(max_rounds):
            busy = False
            for tenant in list(self.tenants()):
                runtime, counts = await self._prepare(tenant)
                for k, v in counts.items():
                    total[k] += v
                busy |= any(counts.values())
                if runtime.settings.paused:
                    continue
                ran = await self.backend.run_until_idle(tenant, runtime.settings)
                total["jobs"] += ran
                busy |= ran > 0
            if not busy:
                break
        return total

    async def run_forever(self, *, poll_seconds: float = 2.0) -> None:  # pragma: no cover
        loop = asyncio.get_running_loop()
        while True:
            for tenant in list(self.tenants()):
                try:
                    runtime, _ = await self._prepare(tenant)
                    if runtime.settings.paused:
                        if runtime.task:
                            runtime.task.cancel()
                            runtime.task = None
                        continue
                    if runtime.task is None or runtime.task.done():
                        runtime.task = asyncio.create_task(
                            self.backend.serve(tenant, runtime.settings)
                        )
                    if loop.time() - runtime.last_maintenance > MAINTENANCE_SECONDS:
                        runtime.last_maintenance = loop.time()
                        await self.backend.maintain(tenant, runtime.settings)
                except Exception:  # noqa: BLE001 - one tenant must not stop the others
                    log.exception("worker tick failed for tenant %s", tenant.tenant_id)
            await asyncio.sleep(poll_seconds)
