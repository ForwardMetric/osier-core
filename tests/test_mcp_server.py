"""The MCP server, exercised through a real in-memory MCP client."""

import asyncio
import json
import uuid
from pathlib import Path

import pytest
from mcp.client import Client

from osier.connectors import ConnectorService
from osier.devconfig import DevConfig, write_key_file
from osier.mcp_server import build_server

prompts: list[str] = []


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


class Sessions:
    def __init__(self, config, conn, person_id=None):
        self.config, self.conn = config, conn
        self.person_id = person_id or uuid.UUID(config.admin_user_id)

    def agent(self):
        return self.config.session(self.config.agent(self.person_id), self.conn)

    def person(self):
        return self.config.session(
            self.config.person(self.person_id, "chat-confirmation"), self.conn
        )


@pytest.fixture
def server(config, conn):
    return build_server(config, sessions=Sessions(config, conn))


def call(server, tool_name, /, _answer=None, **arguments):
    """Call a tool. _answer simulates the person answering the app's confirmation prompt:
    None = the app cannot show prompts; "accept" / "decline" = the person's click."""
    import mcp.types as mt

    prompts.clear()

    async def answer(context, params):
        prompts.append(params.message)
        if _answer == "accept":
            return mt.ElicitResult(action="accept", content={"approve": True})
        return mt.ElicitResult(action="decline")

    async def go():
        kwargs = {"elicitation_callback": answer} if _answer else {}
        async with Client(server, **kwargs) as client:
            result = await client.call_tool(tool_name, arguments)
            text = result.content[0].text
            if result.is_error:
                if "{" in text:
                    return {"error": json.loads(text[text.index("{") :])}
                return {"error": {"message": text}}
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


def test_connector_tools_through_the_mcp_server(server, config, conn):
    import sys
    from pathlib import Path

    fixture = str(Path(__file__).parent / "fixtures" / "notes_mcp_server.py")
    transport = {
        "type": "stdio",
        "command": sys.executable,
        "args": [fixture],
        "secret_env": ["NOTES_TOKEN"],
    }
    admin = ConnectorService(
        config.session(config.admin(), conn), config.broker(), allow_local_processes=True
    )
    inspected = admin.inspect_mcp_server(transport, {"NOTES_TOKEN": "secret-token"})
    admin.register_mcp_server(
        "Notes",
        description="Team notes app used for research.",
        transport=transport,
        secrets={"NOTES_TOKEN": "secret-token"},
        expected_tools_hash=inspected["tools_hash"],
    )
    tools = call(server, "list_connector_tools", connection="Notes")
    assert {t["name"] for t in tools["tools"]} == {
        "search_notes",
        "create_note",
        "delete_all_notes",
    }
    found = call(
        server,
        "call_connector_tool",
        connection="Notes",
        tool="search_notes",
        arguments={"query": "acme"},
    )
    assert found["status"] == "succeeded" and found["result"]["untrusted"] is True
    pending = call(
        server,
        "call_connector_tool",
        connection="Notes",
        tool="create_note",
        arguments={"title": "x", "body": "y"},
    )
    assert pending["status"] == "awaiting_approval"


# --- confirmations in the chat ------------------------------------------------------------

BUDGET = [
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
]


def lead_fields(server):
    return {f["api_name"] for f in call(server, "describe_schema", object="lead")["fields"]}


def test_schema_change_confirmed_in_chat_is_applied(server):
    result = call(server, "propose_schema_change", _answer="accept", operations=BUDGET)
    assert result["status"] == "applied" and result["confirmed_by_user"] is True
    assert "c__budget" in prompts[0] and "ADD COLUMN" in prompts[0]
    assert "c__budget" in lead_fields(server)


def test_declined_schema_change_changes_nothing(server):
    result = call(server, "propose_schema_change", _answer="decline", operations=BUDGET)
    assert "error" in result and "decline" in json.dumps(result)
    assert "c__budget" not in lead_fields(server)
    assert call(server, "list_pending_approvals")["count"] == 0


def test_without_prompts_the_change_waits_and_can_be_approved_later(server):
    waiting = call(server, "propose_schema_change", operations=BUDGET)
    assert waiting["status"] == "proposed" and "approve_pending" in waiting["next_step"]
    pending = call(server, "list_pending_approvals")
    assert pending["count"] == 1
    approved = call(server, "approve_pending", _answer="accept", item_id=waiting["change_set_id"])
    assert approved["status"] == "applied"
    assert "c__budget" in lead_fields(server)


def test_approve_pending_needs_a_prompt(server):
    waiting = call(server, "propose_schema_change", operations=BUDGET)
    result = call(server, "approve_pending", item_id=waiting["change_set_id"])
    assert result["error"]["code"] == "CONFIRMATION_UNAVAILABLE"


def test_sales_person_is_told_which_role_they_need(config, conn, make_user):
    sam = make_user("sam@acme.test", "member")
    server = build_server(config, sessions=Sessions(config, conn, sam.user_id))
    result = call(server, "propose_schema_change", _answer="accept", operations=BUDGET)
    assert result["error"]["code"] == "MISSING_PERMISSION"
    assert "Developer" in result["error"]["message"]
    assert prompts == []  # no prompt is shown for something they may not do
    me = call(server, "whoami")
    assert me["roles"] == ["member"] and "schema.manage" not in me["permissions"]


def test_install_packages_with_confirmation(server):
    result = call(server, "install_packages", _answer="accept", packages=["case_management"])
    assert result["status"] == "applied"
    assert "Install package 'case_management'" in prompts[0]


def test_email_confirmed_in_chat_is_sent(server, config, conn):
    admin = config.session(config.admin(), conn)
    ConnectorService(admin, config.broker()).create_connection("dev_outbox", "Outbox")
    sent = call(
        server,
        "send_email",
        _answer="accept",
        to=["jane@example.com"],
        subject="Hello",
        body_text="Thanks for reaching out.",
    )
    assert sent["status"] == "succeeded" and sent["confirmed_by_user"] is True
    assert "Thanks for reaching out." in prompts[0]
    declined = call(
        server,
        "send_email",
        _answer="decline",
        to=["jane@example.com"],
        subject="Hello",
        body_text="Second message.",
    )
    assert "error" in declined
    assert call(server, "list_pending_approvals")["count"] == 0  # nothing queued or sent


def test_auto_approved_connections_do_not_prompt(server, config, conn):
    admin = config.session(config.admin(), conn)
    ConnectorService(admin, config.broker()).create_connection(
        "dev_outbox", "Outbox", approval="not_required"
    )
    sent = call(
        server,
        "send_email",
        _answer="accept",
        to=["jane@example.com"],
        subject="Hello",
        body_text="Thanks.",
    )
    assert sent["status"] == "succeeded" and prompts == []


def test_connect_app_in_chat_with_oauth(config, conn, monkeypatch):
    import httpx2

    sys_path = str(Path(__file__).parent / "fixtures")
    import sys

    sys.path.insert(0, sys_path)
    from notes_mcp_server import server as notes  # type: ignore[import-not-found]
    from test_oauth_connect import MCP_URL, FakeProvider

    from osier.connectors.oauth_callback import redirect_uri

    config.oauth_callback_port = _free_port()
    provider = FakeProvider(redirect=redirect_uri(config.oauth_callback_port))
    server = build_server(
        config,
        sessions=Sessions(config, conn),
        connector_options={
            "mcp_client_factory": lambda: Client(notes),
            "oauth_http_client": httpx2.Client(transport=httpx2.MockTransport(provider.handle)),
        },
    )
    started = call(
        server,
        "connect_app",
        name="Notion",
        url=MCP_URL,
        description="Company wiki and meeting notes in Notion.",
    )
    assert started["sign_in_url"].startswith("https://auth.notion.test/authorize")
    early = call(server, "finish_app_connection", _answer="accept", oauth_id=started["oauth_id"])
    assert early["status"] == "pending"

    # the person signs in; the provider redirects to osier's callback
    state, code = provider.authorize(started["sign_in_url"])
    admin = config.session(config.admin(), conn)
    ConnectorService(
        admin,
        config.broker(),
        oauth_http_client=httpx2.Client(transport=httpx2.MockTransport(provider.handle)),
    ).complete_mcp_oauth(state, code=code)

    done = call(server, "finish_app_connection", _answer="accept", oauth_id=started["oauth_id"])
    assert done["status"] == "active" and done["confirmed_by_user"] is True
    assert "search_notes (auto)" in prompts[0]
    assert call(server, "list_connections")["connections"][0]["name"] == "Notion"


def _free_port():
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


SKILL = {
    "name": "qualify-lead",
    "description": "Qualify a new lead. Use when a lead arrives without a status decision.",
    "instructions": "# Qualify a lead\n\n1. Check company size.\n2. Set status.",
    "license": "MIT",
}


def test_skill_saved_in_chat_after_confirmation(server):
    assert call(server, "list_skills") == {"skills": [], "count": 0}
    declined = call(server, "save_skill", _answer="decline", **SKILL)
    assert "decline" in json.dumps(declined)  # the MCP SDK reports the decline
    assert call(server, "list_skills")["count"] == 0

    saved = call(server, "save_skill", _answer="accept", **SKILL)
    assert saved["confirmed_by_user"] and saved["version"] == 1
    assert "Qualify a lead" in prompts[0] and "Create skill" in prompts[0]
    listed = call(server, "list_skills")["skills"]
    assert [s["name"] for s in listed] == ["qualify-lead"]
    assert "Check company size" in call(server, "get_skill", name="qualify-lead")["instructions"]

    again = call(server, "save_skill", _answer="accept", **{**SKILL, "instructions": "v2 text"})
    assert again["version"] == 2 and "Update skill" in prompts[0]


def test_skill_without_prompt_is_not_saved(server):
    result = call(server, "save_skill", **SKILL)
    assert result["error"]["code"] == "CONFIRMATION_UNAVAILABLE"
    assert call(server, "list_skills")["count"] == 0


def test_skill_settings_change_in_chat(server):
    call(server, "save_skill", _answer="accept", **SKILL)
    result = call(
        server, "update_skill_settings", _answer="accept", name="qualify-lead", status="disabled"
    )
    assert result["confirmed_by_user"]
    assert "disable it" in prompts[0]


def test_member_can_use_but_not_create_skills(server, config, conn, make_user):
    call(server, "save_skill", _answer="accept", **SKILL)
    member = make_user("rep@example.com", "member")
    member_server = build_server(config, sessions=Sessions(config, conn, member.user_id))
    assert call(member_server, "list_skills")["count"] == 1
    assert call(member_server, "get_skill", name="qualify-lead")["name"] == "qualify-lead"
    refused = call(
        member_server, "save_skill", _answer="accept", **{**SKILL, "name": "other-skill"}
    )
    assert refused["error"]["code"] == "MISSING_PERMISSION"
    assert prompts == []  # never asked to confirm something they cannot do
