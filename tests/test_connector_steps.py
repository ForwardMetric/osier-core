"""Workflow steps that use connected apps: email capability and MCP tools."""

import asyncio
import sys
from pathlib import Path

import psycopg
import pytest
from conftest import DATABASE_URL

from osier import runtime
from osier.connectors import ConnectorService, CredentialBroker
from osier.errors import OsierError
from osier.jobs import Worker, default_backend

KEY = CredentialBroker.generate_key()
SERVER = str(Path(__file__).parent / "fixtures" / "notes_mcp_server.py")


@pytest.fixture(autouse=True)
def configured():
    runtime.configure(broker=CredentialBroker(KEY), allow_local_processes=True)
    yield
    runtime.configure(broker=None, allow_local_processes=False)


def connectors(session):
    return ConnectorService(session, CredentialBroker(KEY), allow_local_processes=True)


def run_worker(crm):
    def connect():
        return psycopg.connect(DATABASE_URL, autocommit=True)

    return asyncio.run(
        Worker(
            default_backend(DATABASE_URL, connect), lambda: [crm.tenant], connect
        ).run_until_idle()
    )


FOLLOW_UP = {
    "name": "follow-up",
    "description": "Email a lead a follow-up, then mark it as being worked.",
    "inputs": {"lead_id": {"type": "string", "description": "The lead."}},
    "steps": [
        {"id": "lead", "type": "get_record", "object": "lead", "record_id": "{{inputs.lead_id}}"},
        {
            "id": "email",
            "type": "connector",
            "capability": "email.send",
            "arguments": {
                "to": ["{{steps.lead.record.email}}"],
                "subject": "Following up",
                "body_text": "Hi {{steps.lead.record.given_name}}, thanks for your time.",
            },
        },
        {
            "id": "mark",
            "type": "update_record",
            "object": "lead",
            "record_id": "{{inputs.lead_id}}",
            "values": {"status": "working"},
        },
    ],
}


def lead(crm):
    return crm.records.create(
        "lead", {"given_name": "Jane", "family_name": "Doe", "email": "jane@example.com"}
    )


def test_email_step_runs_when_no_approval_is_needed(crm):
    connectors(crm).create_connection("dev_outbox", "Outbox", approval="not_required")
    crm.workflows.save(FOLLOW_UP)
    the_lead = lead(crm)
    run = crm.workflows.start("follow-up", {"lead_id": the_lead["id"]})
    assert run["status"] == "succeeded", run["error"]
    assert run["steps"]["email"]["status"] == "succeeded"
    call = connectors(crm).get_call(run["steps"]["email"]["call_id"])
    assert "jane@example.com" in call["summary"]
    assert crm.records.get("lead", the_lead["id"])["status"] == "working"


def test_email_step_waits_for_the_action_approval_then_continues(crm):
    connectors(crm).create_connection("dev_outbox", "Outbox")  # approval required
    crm.workflows.save(FOLLOW_UP)
    the_lead = lead(crm)
    run = crm.workflows.start("follow-up", {"lead_id": the_lead["id"]})
    assert run["status"] == "waiting" and run["waiting_for"]["kind"] == "connector_call"
    call_id = run["waiting_for"]["call_id"]
    # It is decided in the action queue, not as a workflow approval.
    assert crm.workflows.awaiting_decision() == []
    with pytest.raises(OsierError) as err:
        crm.workflows.decide(run["run_id"], approve=True)
    assert err.value.code == "WORKFLOW_WAITING_ON_ACTION"
    assert [c["call_id"] for c in connectors(crm).pending_calls()] == [call_id]
    connectors(crm).approve(call_id)
    assert crm.workflows.get_run(run["run_id"])["status"] == "queued"
    run_worker(crm)
    done = crm.workflows.get_run(run["run_id"])
    assert done["status"] == "succeeded", done["error"]
    assert done["steps"]["email"]["status"] == "succeeded"
    assert crm.records.get("lead", the_lead["id"])["status"] == "working"
    assert len(connectors(crm).pending_calls()) == 0  # the call ran once


def test_rejected_action_ends_the_run_as_rejected(crm):
    connectors(crm).create_connection("dev_outbox", "Outbox")
    crm.workflows.save(FOLLOW_UP)
    the_lead = lead(crm)
    run = crm.workflows.start("follow-up", {"lead_id": the_lead["id"]})
    connectors(crm).reject(run["waiting_for"]["call_id"])
    run_worker(crm)
    done = crm.workflows.get_run(run["run_id"])
    assert done["status"] == "rejected"
    assert crm.records.get("lead", the_lead["id"])["status"] == "new"


def test_connector_steps_use_the_run_permissions(crm, make_user):
    connectors(crm).create_connection("dev_outbox", "Outbox", approval="not_required")
    crm.workflows.save(FOLLOW_UP)
    viewer = make_user("viewer@example.com")  # no roles: no actions.request
    run = crm.as_principal(viewer)
    with pytest.raises(OsierError) as err:
        run.workflows.start("follow-up", {"lead_id": lead(crm)["id"]})
    assert err.value.code == "MISSING_PERMISSION"


@pytest.mark.parametrize(
    ("step", "message"),
    [
        ({"capability": "fax.send"}, "unknown capability"),
        ({"capability": "email.send", "tool": "x", "connection": "y"}, "either"),
        ({}, "either"),
        ({"tool": "x"}, "needs 'connection'"),
    ],
)
def test_invalid_connector_steps(crm, step, message):
    bad = {
        **FOLLOW_UP,
        "steps": [FOLLOW_UP["steps"][0], {"id": "email", "type": "connector", **step}],
    }
    with pytest.raises(OsierError, match=message):
        crm.workflows.preview(bad)


@pytest.fixture
def notes(crm, tmp_path):
    variant = tmp_path / "variant.txt"
    variant.write_text("")
    transport = {
        "type": "stdio",
        "command": sys.executable,
        "args": [SERVER, str(variant)],
        "secret_env": ["NOTES_TOKEN"],
    }
    service = connectors(crm)
    secrets = {"NOTES_TOKEN": "secret-token"}
    inspected = service.inspect_mcp_server(transport, secrets)
    return service.register_mcp_server(
        "Notes",
        description="Team notes app used for account research and meeting notes.",
        transport=transport,
        secrets=secrets,
        expected_tools_hash=inspected["tools_hash"],
    )


def test_mcp_tool_step(crm, notes):
    tools = connectors(crm).list_tools("Notes")["tools"]
    read_only = next(t for t in tools if t["policy"] == "auto")
    crm.workflows.save(
        {
            "name": "notes-lookup",
            "description": "Look up team notes for a company.",
            "inputs": {"company": {"type": "string", "description": "Company name."}},
            "steps": [
                {
                    "id": "search",
                    "type": "connector",
                    "connection": "Notes",
                    "tool": read_only["name"],
                    "arguments": {
                        k: "{{inputs.company}}"
                        for k in read_only["input_schema"].get("required", [])
                    },
                }
            ],
        }
    )
    run = crm.workflows.start("notes-lookup", {"company": "Acme"})
    assert run["status"] == "succeeded", run["error"]
    assert run["steps"]["search"]["status"] == "succeeded"


def test_unknown_mcp_tool_is_refused_on_save(crm, notes):
    with pytest.raises(OsierError, match="no usable tool"):
        crm.workflows.preview(
            {
                "name": "x",
                "description": "x",
                "steps": [{"id": "a", "type": "connector", "connection": "Notes", "tool": "nope"}],
            }
        )
