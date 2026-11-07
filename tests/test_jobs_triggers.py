"""Background jobs, the worker, and workflow triggers (record events, schedules)."""

import datetime as dt

import pytest
from conftest import agent_for
from psycopg import sql

from osier.context import Principal
from osier.errors import OsierError
from osier.jobs import HANDLERS, JobService, Worker
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


@pytest.fixture
def worker(crm):
    system = crm.as_principal(Principal.system("worker"))
    return Worker(lambda: [system], worker_id="test-worker")


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


def test_unexpected_errors_retry_with_backoff_then_fail(crm, worker, monkeypatch):
    from test_workflows import QUALIFY

    crm.workflows.save(QUALIFY)
    lead = crm.records.create("lead", {"family_name": "Doe"})
    run = crm.workflows.start("qualify-lead", {"lead_id": lead["id"]}, background=True)
    calls = []

    def boom(session, payload, job):
        calls.append(job["attempts"])
        raise RuntimeError("database went away")

    monkeypatch.setitem(HANDLERS, "workflow.execute", boom)
    jobs = JobService(crm.as_principal(Principal.system("worker")))
    table = sql.Identifier(crm.tenant.schema, "jobs")
    for _ in range(3):
        worker.run_until_idle()
        crm.conn.execute(sql.SQL("UPDATE {} SET run_after = now()").format(table))
    assert calls == [1, 2, 3]
    [job] = jobs.list()
    assert job["status"] == "failed" and "database went away" in job["last_error"]["message"]
    assert crm.workflows.get_run(run["run_id"])["status"] == "failed"


def test_a_lost_worker_lease_is_reclaimed(crm, worker):
    from test_workflows import QUALIFY

    crm.workflows.save(QUALIFY)
    lead = crm.records.create("lead", {"family_name": "Doe"})
    run = crm.workflows.start("qualify-lead", {"lead_id": lead["id"]}, background=True)
    jobs = JobService(crm.as_principal(Principal.system("worker")))
    assert jobs.claim("crashed-worker") is not None  # claimed, then the worker "dies"
    assert jobs.claim("other") is None  # still leased
    crm.conn.execute(
        sql.SQL("UPDATE {} SET locked_until = now() - interval '1 second'").format(
            sql.Identifier(crm.tenant.schema, "jobs")
        )
    )
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


def test_jobs_list_needs_workflows_manage(crm, make_user):
    member = crm.as_principal(make_user("rep@example.com", "member"))
    with pytest.raises(OsierError) as err:
        JobService(member).list()
    assert err.value.code == "MISSING_PERMISSION"


def test_unknown_job_kind_is_refused(crm):
    with pytest.raises(OsierError):
        JobService(crm).enqueue("shell.run", {}, principal=crm.principal)


def test_workflow_error_import_is_public():
    assert issubclass(WorkflowError, OsierError)
