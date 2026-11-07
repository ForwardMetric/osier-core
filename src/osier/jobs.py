"""Background jobs on Procrastinate, with one isolated queue per tenant.

- **Isolation.** Each tenant's queue lives in its own Postgres schema, ``j_<hex>``
  next to the tenant's data schema ``t_<hex>``. Procrastinate connects with
  ``search_path`` set to that schema only, so one tenant's jobs never share a table
  with another's, and the queue connection cannot reach tenant data.
- **Platform tasks only.** Tasks are registered here (``workflow.execute``). Tenants
  never supply job code; they cause jobs (workflow runs from triggers or chat).
- **Runs are the source of truth.** Engine code marks a workflow run ``queued`` in
  its own transaction. The worker hands queued runs to Procrastinate ("defers"
  them) and records that; a crash in between only means the run is handed over
  again, and per-run locks keep it from executing twice at once.
- **Per-tenant settings** (``JobSettings``) within platform limits: concurrency,
  retries, pause, and how long finished jobs are kept. Changing them needs
  ``jobs.manage`` and a person.
- Each job runs through a ``Session`` for the run's principal, so permissions apply.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, Literal

import procrastinate
import psycopg
from procrastinate import exceptions as pexc
from procrastinate.schema import SchemaManager
from psycopg import sql
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field
from pydantic import ValidationError as PydanticValidationError

from .context import Principal, Tenant
from .errors import ApprovalError, OsierError, RecordValidationError
from .identifiers import validate_tenant_schema

log = logging.getLogger("osier.jobs")

PROCRASTINATE_SCHEMA_MARK = "procrastinate-schema-3.10"
STALLED_AFTER_SECONDS = 60
SETTINGS_RELOAD_SECONDS = 10


# ---- per-tenant settings ---------------------------------------------------------------


class JobSettings(BaseModel):
    """A tenant's job settings. The bounds are platform limits that protect shared
    workers; plans may raise them later."""

    model_config = ConfigDict(extra="forbid")

    concurrency: int = Field(default=2, ge=1, le=10, description="Jobs run at the same time.")
    max_attempts: int = Field(default=3, ge=1, le=5, description="Tries per job.")
    retry_wait_seconds: int = Field(default=10, ge=1, le=3600, description="Wait before retry.")
    retry_backoff: Literal["fixed", "linear"] = Field(
        default="linear", description="fixed: same wait each time; linear: wait x attempt."
    )
    paused: bool = Field(default=False, description="Queue jobs but do not run them.")
    keep_finished_days: int = Field(
        default=14, ge=1, le=90, description="Days finished jobs are kept."
    )

    def retry_strategy(self) -> procrastinate.RetryStrategy:
        wait = self.retry_wait_seconds
        return procrastinate.RetryStrategy(
            max_attempts=self.max_attempts - 1,  # Procrastinate counts retries
            wait=wait if self.retry_backoff == "fixed" else 0,
            linear_wait=wait if self.retry_backoff == "linear" else 0,
        )


def describe_settings() -> dict[str, Any]:
    """Each setting with its default, limits, and meaning."""
    keep = ("default", "minimum", "maximum", "enum", "description")
    props = JobSettings.model_json_schema()["properties"]
    return {name: {k: v for k, v in p.items() if k in keep} for name, p in props.items()}


class JobSettingsService:
    def __init__(self, session: Any) -> None:
        self._session = session
        self._conn = session.conn
        self._table = sql.Identifier(session.tenant.schema, "meta_job_settings")

    def get(self) -> JobSettings:
        row = self._conn.execute(
            sql.SQL("SELECT settings FROM {} WHERE id = 1").format(self._table)
        ).fetchone()
        return JobSettings.model_validate(row[0] if row else {})

    def preview(self, changes: dict[str, Any]) -> dict[str, Any]:
        self._session.authz.require("jobs.manage", "changing background job settings")
        current = self.get()
        try:
            new = JobSettings.model_validate({**current.model_dump(), **changes})
        except PydanticValidationError as exc:
            raise RecordValidationError(
                "Invalid job settings.",
                code="JOB_SETTINGS_INVALID",
                details={
                    "errors": [
                        {"field": ".".join(map(str, e["loc"])), "message": e["msg"]}
                        for e in exc.errors(include_url=False)
                    ]
                },
                expected=describe_settings(),
            ) from None
        diff = {
            k: {"from": getattr(current, k), "to": v}
            for k, v in new.model_dump().items()
            if getattr(current, k) != v
        }
        return {"settings": new.model_dump(), "changes": diff}

    def update(self, changes: dict[str, Any]) -> dict[str, Any]:
        preview = self.preview(changes)
        if not self._session.principal.is_human:
            raise ApprovalError(
                "Changing job settings must be confirmed by a person.",
                code="APPROVAL_REQUIRES_HUMAN",
            )
        self._conn.execute(
            sql.SQL(
                "INSERT INTO {} (id, settings, updated_by) VALUES (1, %s, %s) "
                "ON CONFLICT (id) DO UPDATE SET settings = %s, updated_by = %s, "
                "updated_at = now()"
            ).format(self._table),
            (
                Jsonb(preview["settings"]),
                self._session.principal.user_id,
                Jsonb(preview["settings"]),
                self._session.principal.user_id,
            ),
        )
        return preview


# ---- queue placement -------------------------------------------------------------------


def jobs_schema(tenant: Tenant) -> str:
    """The schema holding a tenant's queue: ``t_<hex>`` -> ``j_<hex>``."""
    return "j_" + validate_tenant_schema(tenant.schema)[2:]


def install_queue(conn: psycopg.Connection, tenant: Tenant) -> None:
    """Create the tenant's queue schema with Procrastinate's tables (platform code,
    run at provisioning and upgrade). Safe to call repeatedly."""
    schema = jobs_schema(tenant)
    mark_table = sql.Identifier(tenant.schema, "meta_engine_migrations")
    done = conn.execute(
        sql.SQL("SELECT 1 FROM {} WHERE name = %s").format(mark_table),
        (PROCRASTINATE_SCHEMA_MARK,),
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
            "FOR f IN SELECT p.oid FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
            "WHERE n.nspname = current_schema() LOOP "
            "EXECUTE format('ALTER FUNCTION %s SET search_path = %I', f, current_schema()); "
            "END LOOP; END $$"
        )
        conn.execute(
            sql.SQL("INSERT INTO {} (name) VALUES (%s)").format(mark_table),
            (PROCRASTINATE_SCHEMA_MARK,),
        )


def drop_queue(conn: psycopg.Connection, tenant: Tenant) -> None:
    conn.execute(
        sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(jobs_schema(tenant)))
    )


# ---- the tenant's Procrastinate app ------------------------------------------------------

Connect = Callable[[], psycopg.Connection]


def _run_workflow(queue: TenantQueue, context: Any, run_id: str) -> None:
    from .session import Session

    attempts_so_far = context.job.attempts
    with queue.connect() as conn:
        system = Session(conn, queue.tenant, Principal.system("worker"))
        service = system.workflows
        run = service.get_run(run_id)
        runner = Session(conn, queue.tenant, service.run_principal(run_id))
        try:
            runner.workflows.execute_job(run_id)
        except Exception as exc:
            if attempts_so_far + 1 >= queue.settings.max_attempts:
                service.mark_failed(
                    run["run_id"],
                    {
                        "code": "JOB_FAILED",
                        "message": f"{type(exc).__name__}: {exc}",
                        "attempts": attempts_so_far + 1,
                    },
                )
            raise


class TenantQueue:
    """Procrastinate for one tenant: its app, tasks, and settings."""

    def __init__(
        self, conninfo: str, tenant: Tenant, settings: JobSettings, connect: Connect
    ) -> None:
        self.tenant, self.settings, self.connect = tenant, settings, connect
        self.schema = jobs_schema(tenant)
        self.app = procrastinate.App(
            connector=procrastinate.PsycopgConnector(
                conninfo=conninfo,
                kwargs={"options": f"-c search_path={self.schema}"},
                min_size=1,
                max_size=settings.concurrency + 2,
            )
        )

        @self.app.task(name="workflow.execute", pass_context=True, retry=settings.retry_strategy())
        def workflow_execute(context: Any, run_id: str) -> None:
            _run_workflow(self, context, run_id)

        self.workflow_execute = workflow_execute

    async def defer_run(self, run_id: str) -> None:
        try:
            await self.workflow_execute.configure(
                lock=f"run:{run_id}", queueing_lock=f"run:{run_id}"
            ).defer_async(run_id=run_id)
        except pexc.AlreadyEnqueued:
            pass  # handed over before; its job is still waiting

    async def recover_stalled(self) -> int:
        stalled = list(
            await self.app.job_manager.get_stalled_jobs(
                seconds_since_heartbeat=STALLED_AFTER_SECONDS
            )
        )
        for job in stalled:
            await self.app.job_manager.retry_job_by_id_async(
                job.id, retry_at=dt.datetime.now(dt.UTC), lock=job.lock
            )
        return len(stalled)

    async def cleanup(self) -> None:
        await self.app.job_manager.delete_old_jobs(
            nb_hours=self.settings.keep_finished_days * 24,
            include_failed=True,
            include_cancelled=True,
            include_aborted=True,
        )

    async def list_jobs(
        self, *, status: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        jobs = await self.app.job_manager.list_jobs_async(status=status)
        out = [
            {
                "id": j.id,
                "task": j.task_name,
                "status": j.status,
                "attempts": j.attempts,
                "args": j.task_kwargs,
                "lock": j.lock,
                "scheduled_at": j.scheduled_at,
            }
            for j in jobs
        ]
        return sorted(out, key=lambda j: j["id"], reverse=True)[:limit]


# ---- handing queued runs over ------------------------------------------------------------


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


def mark_deferred(session: Any, run_ids: Iterable[str]) -> None:
    session.conn.execute(
        sql.SQL("UPDATE {} SET deferred_at = now() WHERE id = ANY(%s)").format(
            sql.Identifier(session.tenant.schema, "workflow_runs")
        ),
        ([uuid.UUID(r) for r in run_ids],),
    )


# ---- the worker --------------------------------------------------------------------------


@dataclass
class _Runtime:
    queue: TenantQueue
    task: asyncio.Task[None] | None = None
    last_maintenance: float = 0.0
    counts: dict[str, int] = field(default_factory=dict)


class Worker:
    """Runs every tenant's queue in one process: dispatches triggers, hands queued runs
    to Procrastinate, and runs each tenant's Procrastinate worker with its settings."""

    def __init__(
        self, conninfo: str, tenants: Callable[[], Iterable[Tenant]], connect: Connect | None = None
    ) -> None:
        self.conninfo = conninfo
        self.tenants = tenants
        self.connect = connect or (lambda: psycopg.connect(conninfo, autocommit=True))
        self._runtimes: dict[uuid.UUID, _Runtime] = {}

    def _system(self, conn: psycopg.Connection, tenant: Tenant) -> Any:
        from .session import Session

        return Session(conn, tenant, Principal.system("worker"))

    def _dispatch(self, tenant: Tenant) -> tuple[dict[str, int], list[str], JobSettings]:
        from .workflows.triggers import dispatch_record_events, dispatch_schedules

        with self.connect() as conn:
            system = self._system(conn, tenant)
            counts = {
                "events": dispatch_record_events(system),
                "scheduled": dispatch_schedules(system),
            }
            return counts, queued_runs(system), JobSettingsService(system).get()

    def _mark(self, tenant: Tenant, run_ids: list[str]) -> None:
        with self.connect() as conn:
            mark_deferred(self._system(conn, tenant), run_ids)

    async def _prepare(self, tenant: Tenant) -> tuple[_Runtime, dict[str, int]]:
        counts, run_ids, settings = await asyncio.to_thread(self._dispatch, tenant)
        runtime = self._runtimes.get(tenant.tenant_id)
        if runtime is None or runtime.queue.settings != settings:
            if runtime and runtime.task:
                runtime.task.cancel()
            runtime = _Runtime(TenantQueue(self.conninfo, tenant, settings, self.connect))
            self._runtimes[tenant.tenant_id] = runtime
        if run_ids:
            async with runtime.queue.app.open_async():
                for run_id in run_ids:
                    await runtime.queue.defer_run(run_id)
            await asyncio.to_thread(self._mark, tenant, run_ids)
        counts["handed_over"] = len(run_ids)
        return runtime, counts

    async def run_until_idle(self, *, max_rounds: int = 20) -> dict[str, int]:
        """Dispatch and run jobs until nothing is left (tests, ``osier worker --once``)."""
        total: dict[str, int] = {"events": 0, "scheduled": 0, "handed_over": 0, "jobs": 0}
        for _ in range(max_rounds):
            busy = False
            for tenant in list(self.tenants()):
                runtime, counts = await self._prepare(tenant)
                for k, v in counts.items():
                    total[k] += v
                busy |= any(counts.values())
                if runtime.queue.settings.paused:
                    continue
                async with runtime.queue.app.open_async():
                    before = await self._finished(runtime)
                    await runtime.queue.app.run_worker_async(
                        wait=False,
                        concurrency=runtime.queue.settings.concurrency,
                        install_signal_handlers=False,
                        listen_notify=False,
                        name=f"osier-{tenant.tenant_id.hex[:8]}",
                    )
                    ran = await self._finished(runtime) - before
                total["jobs"] += ran
                busy |= ran > 0
            if not busy:
                break
        return total

    @staticmethod
    async def _finished(runtime: _Runtime) -> int:
        jobs = await runtime.queue.app.job_manager.list_jobs_async()
        return sum(1 for j in jobs if j.status in ("succeeded", "failed"))

    async def run_forever(self, *, poll_seconds: float = 2.0) -> None:  # pragma: no cover
        loop = asyncio.get_running_loop()
        while True:
            for tenant in list(self.tenants()):
                try:
                    runtime, _ = await self._prepare(tenant)
                    if runtime.queue.settings.paused:
                        if runtime.task:
                            runtime.task.cancel()
                            runtime.task = None
                        continue
                    if runtime.task is None or runtime.task.done():
                        runtime.task = asyncio.create_task(self._serve(runtime))
                    if loop.time() - runtime.last_maintenance > STALLED_AFTER_SECONDS:
                        runtime.last_maintenance = loop.time()
                        async with runtime.queue.app.open_async():
                            await runtime.queue.recover_stalled()
                            await runtime.queue.cleanup()
                except Exception:  # noqa: BLE001 - one tenant must not stop the others
                    log.exception("worker tick failed for tenant %s", tenant.tenant_id)
            await asyncio.sleep(poll_seconds)

    @staticmethod
    async def _serve(runtime: _Runtime) -> None:  # pragma: no cover
        queue = runtime.queue
        async with queue.app.open_async():
            await queue.app.run_worker_async(
                concurrency=queue.settings.concurrency,
                install_signal_handlers=False,
                name=f"osier-{queue.tenant.tenant_id.hex[:8]}",
            )


def list_jobs(
    conninfo: str, session: Any, *, status: str | None = None, limit: int = 50
) -> list[dict[str, Any]]:
    session.authz.require("jobs.manage", "viewing background jobs")
    settings = JobSettingsService(session).get()
    queue = TenantQueue(conninfo, session.tenant, settings, lambda: None)  # type: ignore[arg-type,return-value]

    async def go() -> list[dict[str, Any]]:
        async with queue.app.open_async():
            return await queue.list_jobs(status=status, limit=limit)

    import json

    return json.loads(json.dumps(asyncio.run(go()), default=str))


__all__ = [
    "JobSettings",
    "JobSettingsService",
    "OsierError",
    "TenantQueue",
    "Worker",
    "install_queue",
    "jobs_schema",
    "list_jobs",
]
