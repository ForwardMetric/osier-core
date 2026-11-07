"""The MCP server, exercised through a real in-memory MCP client."""

import asyncio
import json
import uuid

import pytest
from mcp.client import Client

from osier.connectors import ConnectorService
from osier.devconfig import DevConfig, write_key_file
from osier.mcp_server import build_server


@pytest.fixture
def config(boot, crm, tmp_path):
    key = tmp_path / "secret.key"
    write_key_file(key)
    return DevConfig(
        database_url="unused",
        tenant_id=str(boot.tenant.tenant_id),
        tenant_schema=boot.tenant.schema,
        organization_id=str(boot.organization_id),
        admin_user_id=str(boot.admin_user_id),
        agent_user_id=str(uuid.uuid5(boot.admin_user_id, "mcp-agent")),
        key_file=str(key),
    )


@pytest.fixture
def server(config, conn):
    class Sessions:
        def agent(self):
            return config.session(config.agent(), conn)

    return build_server(config, sessions=Sessions())


def call(server, tool, **arguments):
    async def go():
        async with Client(server) as client:
            result = await client.call_tool(tool, arguments)
            text = result.content[0].text
            if result.is_error:
                return {"error": json.loads(text[text.index("{") :])}
            return json.loads(text)

    return asyncio.run(go())


def tools(server):
    async def go():
        async with Client(server) as client:
            return {t.name: t for t in (await client.list_tools()).tools}

    return asyncio.run(go())


def test_tools_have_annotations(server):
    listed = tools(server)
    assert {
        "describe_schema",
        "query_records",
        "create_record",
        "send_email",
        "propose_schema_change",
    } <= set(listed)
    assert listed["query_records"].annotations.read_only_hint is True
    assert listed["send_email"].annotations.open_world_hint is True
    assert listed["retire_record"].annotations.destructive_hint is True


def test_agent_crm_workflow(server, conn, config):
    schema = call(server, "describe_schema", object="lead")
    assert any(f["api_name"] == "status" for f in schema["fields"])

    lead = call(
        server,
        "create_record",
        object="lead",
        values={
            "family_name": "Doe",
            "given_name": "Jane",
            "email": "jane@example.com",
            "company_name": "Acme",
            "lead_source": "web",
        },
    )
    assert lead["status"] == "new" and lead["created_by"] == config.agent_user_id

    found = call(
        server,
        "query_records",
        object="lead",
        filters=[{"field": "company_name", "op": "contains", "value": "acme"}],
    )
    assert found["count"] == 1

    updated = call(
        server,
        "update_record",
        object="lead",
        record_id=lead["id"],
        values={"status": "working"},
        expected_row_version=1,
    )
    assert updated["row_version"] == 2
    history = call(server, "get_record_history", object="lead", record_id=lead["id"])
    change = history["changes"][0]
    assert change["new_value"] == "working" and change["actor_type"] == "agent"


def test_errors_are_structured_json(server):
    result = call(server, "create_record", object="lead", values={"status": "bogus"})
    assert result["error"]["code"] == "VALIDATION_FAILED"
    assert result["error"]["retryable"] is False
    fields = {e["field"] for e in result["error"]["details"]["errors"]}
    assert {"family_name", "status"} <= fields


def test_schema_proposal_requires_human_approval(server, config, conn):
    proposal = call(
        server,
        "propose_schema_change",
        operations=[
            {
                "op": "add_field",
                "object": "lead",
                "field": {
                    "api_name": "c__budget",
                    "label": "Budget",
                    "type": "decimal",
                    "description": "Budget the lead says they have for this purchase.",
                },
            }
        ],
    )
    assert proposal["can_apply"] and "diff_hash" not in proposal
    assert "osier approvals approve" in proposal["next_step"]
    # the person approves (what `osier approvals approve` does)
    admin = config.session(config.admin(), conn)
    stored = admin.schema.get(proposal["change_set_id"])
    admin.schema.apply(uuid.UUID(stored["change_set_id"]), diff_hash=stored["diff_hash"])
    lead = call(server, "describe_schema", object="lead")
    assert any(f["api_name"] == "c__budget" for f in lead["fields"])


def test_send_email_waits_for_approval_and_logs_activity(server, config, conn):
    admin = config.session(config.admin(), conn)
    ConnectorService(admin, config.broker()).create_connection("dev_outbox", "Outbox")
    lead = call(server, "create_record", object="lead", values={"family_name": "Doe"})
    sent = call(
        server,
        "send_email",
        to=["jane@example.com"],
        subject="Hello",
        body_text="Thanks for reaching out.",
        related_object="lead",
        related_record_id=lead["id"],
    )
    assert sent["status"] == "awaiting_approval"
    ConnectorService(admin, config.broker()).approve(sent["call_id"])
    status = call(server, "get_connector_call", call_id=sent["call_id"])
    assert status["status"] == "succeeded"
    activities = call(
        server,
        "query_records",
        object="activity",
        filters=[{"field": "lead", "op": "eq", "value": lead["id"]}],
    )
    assert activities["records"][0]["subject"] == "Email: Hello"


def test_agent_cannot_change_protected_or_engine_managed_objects(server):
    result = call(server, "create_record", object="role", values={"name": "X", "key": "x"})
    assert result["error"]["code"] == "AGENT_WRITE_NOT_ALLOWED"
    result = call(
        server, "create_record", object="connection", values={"name": "x", "connector_type": "smtp"}
    )
    assert result["error"]["code"] == "ENGINE_MANAGED_OBJECT"


def test_empty_lists_still_return_an_object(server):
    assert call(server, "list_connections") == {"connections": [], "count": 0}
