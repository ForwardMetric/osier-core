"""Background jobs, the worker, and workflow triggers (record events, schedules)."""

import asyncio
import datetime as dt

import psycopg
import pytest
from conftest import DATABASE_URL, agent_for
from psycopg import sql

from osier.errors import OsierError
from osier.jobs import JobSettingsService, Worker, jobs_schema, list_jobs
from osier.workflows import WorkflowError

ON_NEW_LEAD = {
    "name": "work-new-lead",
    "description": "When a lead is created from a referral, start working it.",
    "inputs": {"lead_id": {"type": "string", "description": "The new lead."}},
    "triggers": [
        {
            "type": "record_created",
            "object": "lead",
            "when": [{"value": "{{record.lead_source}}", "op": "eq", "compare_to": "referral"}],
            "inputs": {"lead_id": "{{record.id}}"},
        }
    ],
    "steps": [
        {
            "id": "mark",
            "type": "update_record",
            "object": "lead",
            "record_id": "{{inputs.lead_id}}",
            "values": {"status": "working"},
        }
    ],
}


class _SyncWorker:
    def __init__(self, tenant):
        self.worker = Worker(DATABASE_URL, lambda: [tenant])

    def run_until_idle(self):
        return asyncio.run(self.worker.run_until_idle())


@pytest.fixture
def worker(crm):
    return _SyncWorker(crm.tenant)


def jobs_table(crm):
    return sql.Identifier(jobs_schema(crm.tenant), "procrastinate_jobs")


def runs(session):
    return session.workflows.runs()


def test_record_created_trigger_runs_in_the_worker(crm, worker):
    crm.workflows.save(ON_NEW_LEAD)
    lead = crm.records.create("lead", {"family_name": "Doe", "lead_source": "referral"})
    other = crm.records.create("lead", {"family_name": "Roe", "lead_source": "web"})
    assert runs(crm) == []  # nothing happens until the worker runs
    counts = worker.run_until_idle()
    assert counts["jobs"] == 1
    [run] = runs(crm)
    assert run["status"] == "succeeded" and run["trigger"]["type"] == "record_created"
    assert crm.records.get("lead", lead["id"])["status"] == "working"
    assert crm.records.get("lead", other["id"])["status"] == "new"


def test_events_fire_once_and_workflow_changes_do_not_loop(crm, worker):
    looping = {
        **ON_NEW_LEAD,
        "name": "touch-lead",
        "triggers": [
            {"type": "record_updated", "object": "lead", "inputs": {"lead_id": "{{record.id}}"}}
        ],
        "steps": [{**ON_NEW_LEAD["steps"][0], "values": {"job_title": "touched"}}],
    }
    crm.workflows.save(looping)
    lead = crm.records.create("lead", {"family_name": "Doe"})
    crm.records.update("lead", lead["id"], {"status": "working"}, expected_row_version=1)
    worker.run_until_idle()
    worker.run_until_idle()
    assert len(runs(crm)) == 1  # the run's own update did not trigger another run
    assert crm.records.get("lead", lead["id"])["job_title"] == "touched"


def test_updated_trigger_with_fields(crm, worker):
    crm.workflows.save(
        {
            **ON_NEW_LEAD,
            "name": "on-status",
            "triggers": [
                {
                    "type": "record_updated",
                    "object": "lead",
                    "fields": ["status"],
                    "inputs": {"lead_id": "{{record.id}}"},
                }
            ],
            "steps": [{"id": "done", "type": "finish", "output": {"lead": "{{inputs.lead_id}}"}}],
        }
    )
    lead = crm.records.create("lead", {"family_name": "Doe"})
    crm.records.update("lead", lead["id"], {"job_title": "CEO"}, expected_row_version=1)
    worker.run_until_idle()
    assert runs(crm) == []
    crm.records.update("lead", lead["id"], {"status": "working"}, expected_row_version=2)
    worker.run_until_idle()
    assert [r["output"] for r in runs(crm)] == [{"lead": lead["id"]}]


def test_triggered_runs_use_the_permissions_of_whoever_made_the_change(crm, worker, make_user):
    crm.workflows.save(ON_NEW_LEAD)
    rep = make_user("rep@example.com", "member")
    agent = crm.as_principal(agent_for(rep.user_id))
    lead = agent.records.create("lead", {"family_name": "Doe", "lead_source": "referral"})
    worker.run_until_idle()
    [run] = runs(crm)
    assert run["started_by"] == str(rep.user_id) and run["started_by_agent"]
    history = crm.records.history("lead", lead["id"])
    assert history[-1]["on_behalf_of"] == str(rep.user_id)
    assert history[-1]["source"] == "workflow"


def test_old_events_do_not_fire_new_triggers(crm, worker):
    crm.records.create("lead", {"family_name": "Doe", "lead_source": "referral"})
    worker.run_until_idle()  # dispatched with no workflow listening
    crm.workflows.save(ON_NEW_LEAD)
    worker.run_until_idle()
    assert runs(crm) == []


def test_disabled_workflows_do_not_fire(crm, worker):
    crm.workflows.save(ON_NEW_LEAD)
    crm.workflows.set_status("work-new-lead", "disabled")
    crm.records.create("lead", {"family_name": "Doe", "lead_source": "referral"})
    worker.run_until_idle()
    assert runs(crm) == []


def test_trigger_with_bad_inputs_leaves_a_failed_run(crm, worker):
    crm.workflows.save(
        {**ON_NEW_LEAD, "inputs": {"lead_id": {"type": "integer", "description": "x"}}}
    )
    crm.records.create("lead", {"family_name": "Doe", "lead_source": "referral"})
    worker.run_until_idle()
    [run] = runs(crm)
    assert run["status"] == "failed" and run["error"]["code"] == "WORKFLOW_INPUT_INVALID"


def _make_due(crm):
    crm.conn.execute(
        sql.SQL("UPDATE {} SET next_run_at = now() - interval '1 minute'").format(
            sql.Identifier(crm.tenant.schema, "workflow_schedules")
        )
    )


SCHEDULED = {
    "name": "daily-summary",
    "description": "Every morning, count new leads.",
    "inputs": {"at": {"type": "string", "description": "When it ran."}},
    "triggers": [
        {
            "type": "schedule",
            "cron": "0 9 * * 1-5",
            "timezone": "Europe/London",
            "inputs": {"at": "{{schedule.time}}"},
        }
    ],
    "steps": [
        {
            "id": "new",
            "type": "query_records",
            "query": {"object": "lead", "filters": [{"field": "status", "value": "new"}]},
        },
        {"id": "out", "type": "finish", "output": {"new_leads": "{{steps.new.count}}"}},
    ],
}


def test_schedule_fires_once_per_due_time_as_the_saver(crm, worker, boot):
    crm.workflows.save(SCHEDULED)
    crm.records.create("lead", {"family_name": "Doe"})
    worker.run_until_idle()
    assert runs(crm) == []  # not due yet
    _make_due(crm)
    worker.run_until_idle()
    worker.run_until_idle()
    [run] = runs(crm)
    assert run["status"] == "succeeded" and run["output"] == {"new_leads": 1}
    assert run["started_by"] == str(boot.admin_user_id) and run["trigger"]["type"] == "schedule"
    next_at = crm.conn.execute(
        sql.SQL("SELECT next_run_at FROM {}").format(
            sql.Identifier(crm.tenant.schema, "workflow_schedules")
        )
    ).fetchone()[0]
    assert next_at > dt.datetime.now(dt.UTC)


def test_disabling_removes_the_schedule(crm, worker):
    crm.workflows.save(SCHEDULED)
    crm.workflows.set_status("daily-summary", "disabled")
    _make_due(crm)
    worker.run_until_idle()
    assert runs(crm) == []


@pytest.mark.parametrize(
    ("trigger", "message"),
    [
        ({"type": "schedule", "cron": "* * * * *"}, "at most every"),
        ({"type": "schedule", "cron": "0 9 * * *", "timezone": "Mars/Base"}, "time zone"),
        ({"type": "schedule", "cron": "not a cron"}, "cron"),
        (
            {"type": "record_created", "object": "lead", "inputs": {"lead_id": "{{record.nope}}"}},
            "not a field",
        ),
        ({"type": "record_created", "object": "lead"}, "required inputs"),
        (
            {
                "type": "record_created",
                "object": "lead",
                "fields": ["status"],
                "inputs": {"lead_id": "x"},
            },
            "only applies",
        ),
    ],
)
def test_invalid_triggers(crm, trigger, message):
    if trigger["type"] == "schedule":
        trigger = {**trigger, "inputs": {"lead_id": "x"}}
    with pytest.raises(OsierError, match=message):
        crm.workflows.preview({**ON_NEW_LEAD, "triggers": [trigger]})


def test_background_start_and_decision(crm, worker):
    from test_workflows import QUALIFY

    crm.workflows.save(QUALIFY)
    lead = crm.records.create("lead", {"family_name": "Doe"})
    run = crm.workflows.start("qualify-lead", {"lead_id": lead["id"]}, background=True)
    assert run["status"] == "queued"
    worker.run_until_idle()
    assert crm.workflows.get_run(run["run_id"])["status"] == "waiting"
    assert crm.workflows.decide(run["run_id"], approve=True, background=True)["status"] == (
        "queued"
    )
    worker.run_until_idle()
    assert crm.workflows.get_run(run["run_id"])["status"] == "succeeded"
    assert crm.records.get("lead", lead["id"])["status"] == "qualified"


def test_unexpected_errors_retry_then_fail_the_run(crm, conn, worker, monkeypatch):
    from test_workflows import QUALIFY

    from osier.workflows import WorkflowService

    crm.workflows.save(QUALIFY)
    JobSettingsService(crm).update({"max_attempts": 2, "retry_wait_seconds": 1})
    lead = crm.records.create("lead", {"family_name": "Doe"})
    run = crm.workflows.start("qualify-lead", {"lead_id": lead["id"]}, background=True)
    calls = []

    def boom(self, run_id):
        calls.append(run_id)
        raise RuntimeError("database went away")

    monkeypatch.setattr(WorkflowService, "execute_job", boom)
    worker.run_until_idle()
    conn.execute(sql.SQL("UPDATE {} SET scheduled_at = now()").format(jobs_table(crm)))
    worker.run_until_idle()
    assert len(calls) == 2
    [job] = list_jobs(DATABASE_URL, crm)
    assert job["status"] == "failed" and job["attempts"] == 2
    failed = crm.workflows.get_run(run["run_id"])
    assert failed["status"] == "failed" and "database went away" in failed["error"]["message"]


def test_handing_over_twice_does_not_run_twice(crm, worker):
    """A crash after handing a run to the queue but before recording it."""
    from test_workflows import QUALIFY

    crm.workflows.save(QUALIFY)
    lead = crm.records.create("lead", {"family_name": "Doe"})
    run = crm.workflows.start("qualify-lead", {"lead_id": lead["id"]}, background=True)
    rt = worker.worker

    async def hand_over_without_marking():
        _, counts = await rt._prepare(crm.tenant)
        return counts

    asyncio.run(hand_over_without_marking())
    crm.conn.execute(
        sql.SQL("UPDATE {} SET deferred_at = NULL").format(
            sql.Identifier(crm.tenant.schema, "workflow_runs")
        )
    )
    worker.run_until_idle()
    assert len(list_jobs(DATABASE_URL, crm)) == 1
    assert crm.workflows.get_run(run["run_id"])["status"] == "waiting"


def test_jobs_of_a_crashed_worker_are_retried(crm, conn, worker):
    from test_workflows import QUALIFY

    from osier.jobs import JobSettings, TenantQueue

    crm.workflows.save(QUALIFY)
    lead = crm.records.create("lead", {"family_name": "Doe"})
    run = crm.workflows.start("qualify-lead", {"lead_id": lead["id"]}, background=True)
    asyncio.run(worker.worker._prepare(crm.tenant))  # handed over, not run
    schema = jobs_schema(crm.tenant)
    worker_id = conn.execute(
        sql.SQL(
            "INSERT INTO {} (last_heartbeat) VALUES (now() - interval '1 hour') RETURNING id"
        ).format(sql.Identifier(schema, "procrastinate_workers"))
    ).fetchone()[0]
    conn.execute(  # the job was running on that worker when it died
        sql.SQL("UPDATE {} SET status = 'doing', worker_id = %s").format(jobs_table(crm)),
        (worker_id,),
    )
    queue = TenantQueue(DATABASE_URL, crm.tenant, JobSettings(), lambda: None)

    async def recover():
        async with queue.app.open_async():
            return await queue.recover_stalled()

    assert asyncio.run(recover()) == 1
    worker.run_until_idle()
    assert crm.workflows.get_run(run["run_id"])["status"] == "waiting"


def test_cancelled_queued_run_does_not_execute(crm, worker):
    from test_workflows import QUALIFY

    crm.workflows.save(QUALIFY)
    lead = crm.records.create("lead", {"family_name": "Doe"})
    run = crm.workflows.start("qualify-lead", {"lead_id": lead["id"]}, background=True)
    crm.workflows.cancel(run["run_id"])
    worker.run_until_idle()
    assert crm.workflows.get_run(run["run_id"])["status"] == "cancelled"


def test_job_settings_need_jobs_manage_and_a_person(crm, make_user, boot):
    member = crm.as_principal(make_user("rep@example.com", "member"))
    with pytest.raises(OsierError) as err:
        JobSettingsService(member).update({"concurrency": 3})
    assert err.value.code == "MISSING_PERMISSION"
    with pytest.raises(OsierError) as err:
        list_jobs(DATABASE_URL, member)
    assert err.value.code == "MISSING_PERMISSION"
    agent = crm.as_principal(agent_for(boot.admin_user_id))
    with pytest.raises(OsierError) as err:
        JobSettingsService(agent).update({"concurrency": 3})
    assert err.value.code == "APPROVAL_REQUIRES_HUMAN"


@pytest.mark.parametrize(
    "change", [{"concurrency": 50}, {"max_attempts": 0}, {"nope": 1}, {"retry_backoff": "random"}]
)
def test_job_settings_stay_within_platform_limits(crm, change):
    with pytest.raises(OsierError) as err:
        JobSettingsService(crm).update(change)
    assert err.value.code == "JOB_SETTINGS_INVALID"


def test_job_settings_are_per_tenant(crm, conn):
    from osier import bootstrap_tenant, drop_tenant

    other = bootstrap_tenant(
        conn, organization_name="Other", admin_email="o@o.test", admin_display_name="O"
    )
    try:
        JobSettingsService(crm).update({"concurrency": 5})
        from osier import Principal, Session

        theirs = Session(conn, other.tenant, Principal(user_id=other.admin_user_id))
        assert JobSettingsService(theirs).get().concurrency == 2
    finally:
        drop_tenant(conn, other.tenant)


def test_paused_tenant_queues_but_does_not_run(crm, worker):
    from test_workflows import QUALIFY

    crm.workflows.save(QUALIFY)
    JobSettingsService(crm).update({"paused": True})
    lead = crm.records.create("lead", {"family_name": "Doe"})
    run = crm.workflows.start("qualify-lead", {"lead_id": lead["id"]}, background=True)
    worker.run_until_idle()
    assert crm.workflows.get_run(run["run_id"])["status"] == "queued"
    assert [j["status"] for j in list_jobs(DATABASE_URL, crm)] == ["todo"]
    JobSettingsService(crm).update({"paused": False})
    worker.run_until_idle()
    assert crm.workflows.get_run(run["run_id"])["status"] == "waiting"


def test_tenant_queues_are_isolated(crm, conn, worker):
    """Each tenant's jobs live in its own schema; one tenant's worker never sees
    another tenant's jobs."""
    from test_workflows import QUALIFY

    from osier import bootstrap_tenant, drop_tenant

    other = bootstrap_tenant(
        conn, organization_name="Other", admin_email="o@o.test", admin_display_name="O"
    )
    try:
        crm.workflows.save(QUALIFY)
        lead = crm.records.create("lead", {"family_name": "Doe"})
        crm.workflows.start("qualify-lead", {"lead_id": lead["id"]}, background=True)
        worker.run_until_idle()
        counts = {}
        for t in (crm.tenant, other.tenant):
            counts[t.schema] = conn.execute(
                sql.SQL("SELECT count(*) FROM {}").format(
                    sql.Identifier(jobs_schema(t), "procrastinate_jobs")
                )
            ).fetchone()[0]
        assert counts == {crm.tenant.schema: 1, other.tenant.schema: 0}
        leaked = conn.execute(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name LIKE 'procrastinate%'"
        ).fetchone()[0]
        assert leaked == 0
    finally:
        drop_tenant(conn, other.tenant)
    gone = conn.execute(
        "SELECT count(*) FROM information_schema.schemata WHERE schema_name = %s",
        (jobs_schema(other.tenant),),
    ).fetchone()[0]
    assert gone == 0


def test_queue_connection_cannot_reach_tenant_data(crm):
    with psycopg.connect(
        DATABASE_URL, autocommit=True, options=f"-c search_path={jobs_schema(crm.tenant)}"
    ) as c:
        with pytest.raises(psycopg.errors.UndefinedTable):
            c.execute("SELECT * FROM lead")


def test_workflow_error_import_is_public():
    assert issubclass(WorkflowError, OsierError)
