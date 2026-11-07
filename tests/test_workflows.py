"""Workflows: definitions, runs on LangGraph, approvals, permissions, durability."""

import uuid

import pytest
from conftest import agent_for

from osier.authz import PermissionDenied
from osier.context import Principal
from osier.errors import ApprovalError, NotFoundError, OsierError, RecordValidationError
from osier.session import Session
from osier.workflows import WorkflowError

QUALIFY = {
    "name": "qualify-lead",
    "description": "Ask for approval, then mark a lead as qualified.",
    "inputs": {"lead_id": {"type": "string", "description": "The lead to qualify."}},
    "steps": [
        {"id": "lead", "type": "get_record", "object": "lead", "record_id": "{{inputs.lead_id}}"},
        {
            "id": "is_new",
            "type": "condition",
            "when": [{"value": "{{steps.lead.record.status}}", "op": "eq", "compare_to": "new"}],
            "then": "approve",
            "else": "end",
        },
        {
            "id": "approve",
            "type": "approval",
            "message": "Qualify {{steps.lead.record.family_name}}?",
        },
        {
            "id": "mark",
            "type": "update_record",
            "object": "lead",
            "record_id": "{{inputs.lead_id}}",
            "values": {"status": "qualified"},
        },
        {
            "id": "done",
            "type": "finish",
            "output": {"lead": "{{inputs.lead_id}}", "status": "{{steps.mark.record.status}}"},
        },
    ],
}


@pytest.fixture
def lead(crm):
    return crm.records.create("lead", {"family_name": "Doe"})


def test_run_waits_for_approval_then_finishes(crm, lead):
    crm.workflows.save(QUALIFY)
    run = crm.workflows.start("qualify-lead", {"lead_id": lead["id"]})
    assert run["status"] == "waiting"
    assert run["waiting_for"]["message"] == "Qualify Doe?"
    assert crm.records.get("lead", lead["id"])["status"] == "new"

    done = crm.workflows.decide(run["run_id"], approve=True, comment="looks good")
    assert done["status"] == "succeeded"
    assert done["output"] == {"lead": lead["id"], "status": "qualified"}
    assert crm.records.get("lead", lead["id"])["status"] == "qualified"
    events = [e["event"] for e in crm.workflows.events(run["run_id"])]
    assert events == ["started", "waiting", "approved", "succeeded"]


def test_rejected_run_changes_nothing(crm, lead):
    crm.workflows.save(QUALIFY)
    run = crm.workflows.start("qualify-lead", {"lead_id": lead["id"]})
    done = crm.workflows.decide(run["run_id"], approve=False)
    assert done["status"] == "rejected"
    assert crm.records.get("lead", lead["id"])["status"] == "new"


def test_condition_skips_to_the_end(crm, lead):
    crm.workflows.save(QUALIFY)
    crm.records.update("lead", lead["id"], {"status": "working"}, expected_row_version=1)
    run = crm.workflows.start("qualify-lead", {"lead_id": lead["id"]})
    assert run["status"] == "succeeded" and "approve" not in run["steps"]


def test_resume_survives_a_new_connection(crm, lead, boot):
    """The waiting run lives in the tenant's checkpoint tables, not in memory."""
    import psycopg

    crm.workflows.save(QUALIFY)
    run = crm.workflows.start("qualify-lead", {"lead_id": lead["id"]})
    other = psycopg.connect(crm.conn._conn.info.dsn, autocommit=True, password="osier")
    try:
        fresh = Session(other, boot.tenant, Principal(user_id=boot.admin_user_id))
        assert fresh.workflows.decide(run["run_id"], approve=True)["status"] == "succeeded"
    finally:
        other.close()


def test_a_run_cannot_be_decided_twice(crm, lead):
    crm.workflows.save(QUALIFY)
    run = crm.workflows.start("qualify-lead", {"lead_id": lead["id"]})
    crm.workflows.decide(run["run_id"], approve=True)
    with pytest.raises(ApprovalError) as err:
        crm.workflows.decide(run["run_id"], approve=True)
    assert err.value.code == "WORKFLOW_RUN_NOT_WAITING"


def test_agents_can_start_runs_but_not_decide_or_define(crm, lead, boot):
    crm.workflows.save(QUALIFY)
    agent = crm.as_principal(agent_for(boot.admin_user_id))
    run = agent.workflows.start("qualify-lead", {"lead_id": lead["id"]})
    assert run["status"] == "waiting" and run["started_by_agent"]
    with pytest.raises(ApprovalError) as err:
        agent.workflows.decide(run["run_id"], approve=True)
    assert err.value.code == "APPROVAL_REQUIRES_HUMAN"
    with pytest.raises(ApprovalError):
        agent.workflows.save({**QUALIFY, "name": "other"})
    # The person decides; the rest of the run still runs as the agent that started it.
    assert crm.workflows.decide(run["run_id"], approve=True)["status"] == "succeeded"
    history = crm.records.history("lead", lead["id"])
    assert history[-1]["actor_type"] == "agent" and history[-1]["source"] == "workflow"


def test_members_run_with_their_own_permissions(crm, lead, make_user):
    crm.workflows.save(QUALIFY)
    member = crm.as_principal(make_user("rep@example.com", "member"))
    with pytest.raises(PermissionDenied):
        member.workflows.save({**QUALIFY, "name": "mine"})
    run = member.workflows.start("qualify-lead", {"lead_id": lead["id"]})
    with pytest.raises(PermissionDenied):  # no approver role named: needs workflows.manage
        member.workflows.decide(run["run_id"], approve=True)
    assert [r["run_id"] for r in member.workflows.runs()] == [run["run_id"]]


def test_approver_roles(crm, lead, make_user):
    crm.workflows.save(
        {
            **QUALIFY,
            "steps": [
                {**s, "approver_roles": ["member"]} if s["type"] == "approval" else s
                for s in QUALIFY["steps"]
            ],
        }
    )
    member = crm.as_principal(make_user("rep@example.com", "member"))
    run = crm.workflows.start("qualify-lead", {"lead_id": lead["id"]})
    assert member.workflows.get_run(run["run_id"])["status"] == "waiting"  # visible to approver
    assert member.workflows.decide(run["run_id"], approve=True)["status"] == "succeeded"


def test_unknown_approver_role_is_refused(crm):
    bad = {
        **QUALIFY,
        "steps": [
            {**s, "approver_roles": ["nobody"]} if s["type"] == "approval" else s
            for s in QUALIFY["steps"]
        ],
    }
    with pytest.raises(WorkflowError, match="nobody"):
        crm.workflows.save(bad)


def test_failed_step_marks_the_run_failed(crm):
    crm.workflows.save(QUALIFY)
    run = crm.workflows.start("qualify-lead", {"lead_id": str(uuid.uuid4())})
    assert run["status"] == "failed"
    assert run["error"]["step"] == "lead" and run["error"]["code"]


def test_create_step_is_idempotent_per_run(crm):
    crm.workflows.save(
        {
            "name": "new-lead",
            "description": "Create a lead from a name.",
            "inputs": {"name": {"type": "string", "description": "Family name."}},
            "steps": [
                {
                    "id": "create",
                    "type": "create_record",
                    "object": "lead",
                    "values": {"family_name": "{{inputs.name}}"},
                },
                {
                    "id": "found",
                    "type": "query_records",
                    "query": {
                        "object": "lead",
                        "filters": [{"field": "family_name", "value": "{{inputs.name}}"}],
                    },
                },
                {"id": "out", "type": "finish", "output": {"count": "{{steps.found.count}}"}},
            ],
        }
    )
    run = crm.workflows.start("new-lead", {"name": "Roe"})
    assert run["status"] == "succeeded" and run["output"] == {"count": 1}


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"name": "Bad Name"}, "Invalid workflow name"),
        ({"steps": [{"id": "x", "type": "shell", "command": "rm"}]}, "not valid"),
        ({"steps": [{"id": "a", "type": "finish", "next": "a"}]}, "forward"),
        (
            {
                "steps": [
                    {
                        "id": "a",
                        "type": "get_record",
                        "object": "lead",
                        "record_id": "{{inputs.nope}}",
                    }
                ]
            },
            "not an input",
        ),
        (
            {
                "steps": [
                    {"id": "a", "type": "create_record", "object": "lead", "values": {"nope": 1}}
                ]
            },
            "no field",
        ),
        ({"steps": [{"id": "a", "type": "finish"}, {"id": "a", "type": "finish"}]}, "unique"),
    ],
)
def test_invalid_definitions(crm, change, message):
    with pytest.raises(OsierError, match=message):
        crm.workflows.preview({**QUALIFY, **change})


def test_unknown_object_is_refused(crm):
    with pytest.raises(OsierError):
        crm.workflows.preview(
            {
                **QUALIFY,
                "steps": [{"id": "a", "type": "get_record", "object": "nope", "record_id": "x"}],
            }
        )


def test_inputs_are_checked(crm):
    crm.workflows.save(QUALIFY)
    with pytest.raises(RecordValidationError) as err:
        crm.workflows.start("qualify-lead", {})
    assert err.value.code == "WORKFLOW_INPUT_INVALID"
    with pytest.raises(RecordValidationError):
        crm.workflows.start("qualify-lead", {"lead_id": "x", "extra": 1})


def test_versions_and_runs_keep_their_version(crm, lead):
    crm.workflows.save(QUALIFY)
    run = crm.workflows.start("qualify-lead", {"lead_id": lead["id"]})
    v2 = {
        **QUALIFY,
        "steps": [*QUALIFY["steps"][:3], {**QUALIFY["steps"][3], "values": {"status": "working"}}],
    }
    assert crm.workflows.save(v2)["version"] == 2
    done = crm.workflows.decide(run["run_id"], approve=True)
    assert done["version"] == 1 and done["output"]["status"] == "qualified"
    assert [v["version"] for v in crm.workflows.versions("qualify-lead")] == [1, 2]


def test_disabled_workflows_cannot_start(crm, lead, make_user):
    crm.workflows.save(QUALIFY)
    crm.workflows.set_status("qualify-lead", "disabled")
    with pytest.raises(NotFoundError) as err:
        crm.workflows.start("qualify-lead", {"lead_id": lead["id"]})
    assert err.value.code == "WORKFLOW_DISABLED"
    member = crm.as_principal(make_user("rep@example.com", "member"))
    assert member.workflows.list() == []


def test_cancel(crm, lead):
    crm.workflows.save(QUALIFY)
    run = crm.workflows.start("qualify-lead", {"lead_id": lead["id"]})
    assert crm.workflows.cancel(run["run_id"])["status"] == "cancelled"
    with pytest.raises(ApprovalError):
        crm.workflows.decide(run["run_id"], approve=True)


def test_checkpoints_stay_in_the_tenant_schema(crm, lead, conn, boot):
    crm.workflows.save(QUALIFY)
    crm.workflows.start("qualify-lead", {"lead_id": lead["id"]})
    tables = {
        r[0]
        for r in conn.execute(
            "SELECT table_schema FROM information_schema.tables WHERE table_name LIKE 'checkpoint%'"
        ).fetchall()
    }
    assert tables == set()  # LangGraph's own tables are never created
    from psycopg import sql

    count = conn.execute(
        sql.SQL("SELECT count(*) FROM {}").format(
            sql.Identifier(boot.tenant.schema, "workflow_checkpoints")
        )
    ).fetchone()[0]
    assert count > 0
