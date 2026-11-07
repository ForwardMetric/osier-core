"""``JobBackend`` on Procrastinate (MIT), with one isolated queue per tenant.

Each tenant's queue lives in its own schema ``j_<hex>`` next to its data schema
``t_<hex>``. Procrastinate's SQL is unqualified, so its connection uses
``search_path = j_<hex>`` only, and every Procrastinate function is pinned to that
``search_path`` at install. That connection cannot see tenant data.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

import procrastinate
import psycopg
from procrastinate import exceptions as pexc
from procrastinate.schema import SchemaManager
from psycopg import sql

from ..context import Tenant
from ..identifiers import validate_tenant_schema
from .runner import Connect, execute_run
from .settings import JobSettings

SCHEMA_MARK = "procrastinate-schema-3.10"
STALLED_AFTER_SECONDS = 60


def jobs_schema(tenant: Tenant) -> str:
    """The schema holding a tenant's queue: ``t_<hex>`` -> ``j_<hex>``."""
    return "j_" + validate_tenant_schema(tenant.schema)[2:]


def retry_strategy(settings: JobSettings) -> procrastinate.RetryStrategy:
    wait = settings.retry_wait_seconds
    return procrastinate.RetryStrategy(
        max_attempts=settings.max_attempts - 1,  # Procrastinate counts retries
        wait=wait if settings.retry_backoff == "fixed" else 0,
        linear_wait=wait if settings.retry_backoff == "linear" else 0,
    )


class TenantQueue:
    """Procrastinate for one tenant: its app and task, built from its settings."""

    def __init__(self, conninfo: str, tenant: Tenant, settings: JobSettings, connect: Connect):
        self.tenant, self.settings = tenant, settings
        self.app = procrastinate.App(
            connector=procrastinate.PsycopgConnector(
                conninfo=conninfo,
                kwargs={"options": f"-c search_path={jobs_schema(tenant)}"},
                min_size=1,
                max_size=settings.concurrency + 2,
            )
        )

        @self.app.task(name="workflow.execute", pass_context=True, retry=retry_strategy(settings))
        def workflow_execute(context: Any, run_id: str) -> None:
            execute_run(
                connect,
                tenant,
                run_id,
                attempt=context.job.attempts + 1,
                max_attempts=settings.max_attempts,
            )

        self.workflow_execute = workflow_execute

    async def defer_run(self, run_id: str) -> None:
        try:
            await self.workflow_execute.configure(
                lock=f"run:{run_id}", queueing_lock=f"run:{run_id}"
            ).defer_async(run_id=run_id)
        except pexc.AlreadyEnqueued:
            pass  # handed over before; its job is still waiting

    async def recover_stalled(self) -> int:
        """Procrastinate detects jobs of workers whose heartbeat stopped but does not
        retry them itself."""
        manager = self.app.job_manager
        stalled = list(
            await manager.get_stalled_jobs(seconds_since_heartbeat=STALLED_AFTER_SECONDS)
        )
        for job in stalled:
            await manager.retry_job_by_id_async(
                job.id, retry_at=dt.datetime.now(dt.UTC), lock=job.lock
            )
        return len(stalled)

    async def finished_count(self) -> int:
        jobs = await self.app.job_manager.list_jobs_async()
        return sum(1 for j in jobs if j.status in ("succeeded", "failed"))


@dataclass
class ProcrastinateBackend:
    conninfo: str
    connect: Connect
    name: str = "procrastinate"

    def __post_init__(self) -> None:
        self._queues: dict[Any, TenantQueue] = {}

    def _queue(self, tenant: Tenant, settings: JobSettings) -> TenantQueue:
        key = tenant.tenant_id
        queue = self._queues.get(key)
        if queue is None or queue.settings != settings:
            queue = TenantQueue(self.conninfo, tenant, settings, self.connect)
            self._queues[key] = queue
        return queue

    # -- provisioning --

    @staticmethod
    def install(conn: psycopg.Connection, tenant: Tenant) -> None:
        schema = jobs_schema(tenant)
        mark_table = sql.Identifier(tenant.schema, "meta_engine_migrations")
        done = conn.execute(
            sql.SQL("SELECT 1 FROM {} WHERE name = %s").format(mark_table), (SCHEMA_MARK,)
        ).fetchone()
        if done:
            return
        with conn.transaction():
            conn.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(schema)))
            conn.execute(sql.SQL("SET LOCAL search_path TO {}").format(sql.Identifier(schema)))
            conn.execute(SchemaManager.get_schema())
            # Procrastinate's functions resolve names through search_path. Pin it per
            # function so they only ever see this tenant's queue, whoever calls them.
            conn.execute(
                "DO $$ DECLARE f regprocedure; BEGIN "
                "FOR f IN SELECT p.oid FROM pg_proc p JOIN pg_namespace n "
                "ON n.oid = p.pronamespace WHERE n.nspname = current_schema() LOOP "
                "EXECUTE format('ALTER FUNCTION %s SET search_path = %I', f, current_schema()); "
                "END LOOP; END $$"
            )
            conn.execute(
                sql.SQL("INSERT INTO {} (name) VALUES (%s)").format(mark_table), (SCHEMA_MARK,)
            )

    @staticmethod
    def drop(conn: psycopg.Connection, tenant: Tenant) -> None:
        conn.execute(
            sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(jobs_schema(tenant)))
        )

    # -- queueing and running --

    async def hand_over(self, tenant: Tenant, settings: JobSettings, run_ids: list[str]) -> None:
        queue = self._queue(tenant, settings)
        async with queue.app.open_async():
            for run_id in run_ids:
                await queue.defer_run(run_id)

    async def run_until_idle(self, tenant: Tenant, settings: JobSettings) -> int:
        queue = self._queue(tenant, settings)
        async with queue.app.open_async():
            before = await queue.finished_count()
            await queue.app.run_worker_async(
                wait=False,
                concurrency=settings.concurrency,
                install_signal_handlers=False,
                listen_notify=False,
                name=f"osier-{tenant.tenant_id.hex[:8]}",
            )
            return await queue.finished_count() - before

    async def serve(self, tenant: Tenant, settings: JobSettings) -> None:  # pragma: no cover
        queue = self._queue(tenant, settings)
        async with queue.app.open_async():
            await queue.app.run_worker_async(
                concurrency=settings.concurrency,
                install_signal_handlers=False,
                name=f"osier-{tenant.tenant_id.hex[:8]}",
            )

    async def maintain(self, tenant: Tenant, settings: JobSettings) -> None:
        queue = self._queue(tenant, settings)
        async with queue.app.open_async():
            await queue.recover_stalled()
            await queue.app.job_manager.delete_old_jobs(
                nb_hours=settings.keep_finished_days * 24,
                include_failed=True,
                include_cancelled=True,
                include_aborted=True,
            )

    async def list_jobs(
        self, tenant: Tenant, settings: JobSettings, *, status: str | None, limit: int
    ) -> list[dict[str, Any]]:
        queue = self._queue(tenant, settings)
        async with queue.app.open_async():
            jobs = await queue.app.job_manager.list_jobs_async(status=status)
        out = [
            {
                "id": j.id,
                "task": j.task_name,
                "status": j.status,
                "attempts": j.attempts,
                "args": j.task_kwargs,
                "scheduled_at": j.scheduled_at,
            }
            for j in jobs
        ]
        return sorted(out, key=lambda j: j["id"], reverse=True)[:limit]


__all__ = ["ProcrastinateBackend", "TenantQueue", "jobs_schema"]
