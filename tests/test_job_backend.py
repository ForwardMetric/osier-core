"""The job queue is replaceable: the worker and the engine work with any JobBackend.

``InlineBackend`` is a minimal in-memory backend. If it passes the same flows as the
Procrastinate backend, nothing outside ``osier.jobs`` depends on Procrastinate."""

import asyncio
import inspect

import psycopg
from conftest import DATABASE_URL

from osier.jobs import JobBackend, JobSettingsService, Worker, execute_run


class InlineBackend:
    name = "inline"

    def __init__(self, connect):
        self.connect = connect
        self.queues: dict = {}
        self.done: list[str] = []

    def install(self, conn, tenant):
        self.queues.setdefault(tenant.tenant_id, [])

    def drop(self, conn, tenant):
        self.queues.pop(tenant.tenant_id, None)

    async def hand_over(self, tenant, settings, run_ids):
        queue = self.queues.setdefault(tenant.tenant_id, [])
        queue.extend(r for r in run_ids if r not in queue)  # tolerate repeats

    async def run_until_idle(self, tenant, settings):
        queue = self.queues.setdefault(tenant.tenant_id, [])
        ran = 0
        while queue:
            run_id = queue.pop(0)
            await asyncio.to_thread(
                execute_run,
                self.connect,
                tenant,
                run_id,
                attempt=1,
                max_attempts=settings.max_attempts,
            )
            self.done.append(run_id)
            ran += 1
        return ran

    async def serve(self, tenant, settings):
        await self.run_until_idle(tenant, settings)

    async def maintain(self, tenant, settings):
        return None

    async def list_jobs(self, tenant, settings, *, status, limit):
        return [{"id": r, "status": "todo"} for r in self.queues.get(tenant.tenant_id, [])]


def test_inline_backend_matches_the_interface():
    for name, member in inspect.getmembers(JobBackend):
        if not name.startswith("_") and callable(member):
            assert hasattr(InlineBackend, name), name


def test_triggered_and_background_runs_work_with_another_backend(crm):
    from test_jobs_triggers import ON_NEW_LEAD
    from test_workflows import QUALIFY

    def connect():
        return psycopg.connect(DATABASE_URL, autocommit=True)

    backend = InlineBackend(connect)
    worker = Worker(backend, lambda: [crm.tenant], connect)
    crm.workflows.save(ON_NEW_LEAD)
    crm.workflows.save(QUALIFY)
    lead = crm.records.create("lead", {"family_name": "Doe", "lead_source": "referral"})
    run = crm.workflows.start("qualify-lead", {"lead_id": lead["id"]}, background=True)
    counts = asyncio.run(worker.run_until_idle())
    assert counts["jobs"] == 2
    assert crm.records.get("lead", lead["id"])["status"] == "working"
    assert crm.workflows.get_run(run["run_id"])["status"] == "waiting"
    crm.workflows.decide(run["run_id"], approve=True, background=True)
    asyncio.run(worker.run_until_idle())
    assert crm.workflows.get_run(run["run_id"])["status"] == "succeeded"
    assert len(backend.done) == 3


def test_paused_settings_apply_to_any_backend(crm):
    from test_workflows import QUALIFY

    def connect():
        return psycopg.connect(DATABASE_URL, autocommit=True)

    backend = InlineBackend(connect)
    worker = Worker(backend, lambda: [crm.tenant], connect)
    crm.workflows.save(QUALIFY)
    JobSettingsService(crm).update({"paused": True})
    lead = crm.records.create("lead", {"family_name": "Doe"})
    crm.workflows.start("qualify-lead", {"lead_id": lead["id"]}, background=True)
    asyncio.run(worker.run_until_idle())
    assert backend.done == [] and len(backend.queues[crm.tenant.tenant_id]) == 1


def test_nothing_outside_jobs_imports_the_queue_library():
    import pathlib

    root = pathlib.Path(__file__).parent.parent / "src" / "osier"
    offenders = [
        str(p.relative_to(root))
        for p in root.rglob("*.py")
        if "procrastinate" in p.read_text() and p.parent.name != "jobs"
    ]
    assert offenders == []
