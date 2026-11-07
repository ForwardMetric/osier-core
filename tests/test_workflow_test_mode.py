"""Test mode: try workflows with sample inputs without saving or sending anything."""

import pytest
from psycopg import sql
from test_connector_steps import FOLLOW_UP, configured, connectors, lead  # noqa: F401
from test_workflows import QUALIFY

from osier.errors import OsierError


def count(conn, schema, table):
    return conn.execute(
        sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(schema, table))
    ).fetchone()[0]


def test_unsaved_definition_runs_and_nothing_is_kept(crm, conn):
    connectors(crm).create_connection("dev_outbox", "Outbox")
    the_lead = lead(crm)
    before = {
        t: count(conn, crm.tenant.schema, t)
        for t in ("workflow_runs", "connector_calls", "record_events", "field_history")
    }
    result = crm.workflows.test(FOLLOW_UP, {"lead_id": the_lead["id"]})
    assert result["status"] == "succeeded" and result["test"] is True, result["error"]
    email = result["steps"]["email"]
    assert email["simulated"] and email["needs_approval"] is True
    assert "jane@example.com" in email["summary"]
    assert result["steps"]["mark"]["record"]["status"] == "working"  # really validated
    assert [t["step"] for t in result["trace"]] == ["lead", "email", "mark"]
    assert crm.records.get("lead", the_lead["id"])["status"] == "new"  # rolled back
    after = {t: count(conn, crm.tenant.schema, t) for t in before}
    assert after == before
    assert crm.workflows.list() == []  # the definition was not saved


def test_rejected_approvals_and_actions(crm):
    connectors(crm).create_connection("dev_outbox", "Outbox")
    result = crm.workflows.test(FOLLOW_UP, {"lead_id": lead(crm)["id"]}, approve=False)
    assert result["status"] == "rejected"
    assert "mark" not in result["steps"]
    crm.workflows.save(QUALIFY)
    result = crm.workflows.test("qualify-lead", {"lead_id": lead(crm)["id"]}, approve=False)
    assert result["status"] == "rejected" and result["steps"]["approve"]["simulated"]


def test_validation_errors_show_up_like_in_a_real_run(crm):
    bad = {
        **FOLLOW_UP,
        "steps": [
            FOLLOW_UP["steps"][0],
            {
                "id": "mark",
                "type": "update_record",
                "object": "lead",
                "record_id": "{{inputs.lead_id}}",
                "values": {"status": "not-a-status"},
            },
        ],
    }
    result = crm.workflows.test(bad, {"lead_id": lead(crm)["id"]})
    assert result["status"] == "failed" and result["error"]["step"] == "mark"


def test_ai_steps_use_given_outputs_or_placeholders(crm, monkeypatch):
    from test_ai_steps import ASSESS

    import osier.connectors.netguard as netguard
    from osier.ai import AIService

    monkeypatch.setattr(netguard, "check_destination", lambda *a, **k: None)
    from osier.connectors.vault import CredentialBroker

    AIService(crm, CredentialBroker(CredentialBroker.generate_key())).connect(
        "main", provider="openai", models=["m"], api_key="k"
    )
    AIService(crm).set_profile(name="default", connection="main", model="m", default=True)
    the_lead = lead(crm)
    placeholder = crm.workflows.test(ASSESS, {"lead_id": the_lead["id"]})
    assess = placeholder["steps"]["assess"]
    assert assess["from"] == "placeholder" and assess["output"]["decision"] == "pursue"
    given = crm.workflows.test(
        ASSESS,
        {"lead_id": the_lead["id"]},
        ai_outputs={"assess": {"decision": "drop", "reason": "Test."}},
    )
    assert given["steps"]["approve"]["message"] == "AI says drop: Test."
    with pytest.raises(OsierError, match="ai_outputs"):
        crm.workflows.test(
            ASSESS, {"lead_id": the_lead["id"]}, ai_outputs={"assess": {"decision": "maybe"}}
        )
    assert count(crm.conn._conn, crm.tenant.schema, "ai_usage") == 0  # no model was called


def test_testing_unsaved_definitions_needs_workflows_manage(crm, make_user):
    member = crm.as_principal(make_user("rep@example.com", "member"))
    with pytest.raises(OsierError) as err:
        member.workflows.test(QUALIFY, {"lead_id": "x"})
    assert err.value.code == "MISSING_PERMISSION"
    crm.workflows.save(QUALIFY)
    result = member.workflows.test("qualify-lead", {"lead_id": lead(crm)["id"]})
    assert result["test"] and result["status"] in ("succeeded", "rejected")


def test_real_ai_in_test_mode_keeps_its_usage(crm, monkeypatch):
    from test_ai_steps import ASSESS, FakeClient

    import osier.connectors.netguard as netguard
    from osier import ai
    from osier.ai import AIService
    from osier.connectors.vault import CredentialBroker

    broker = CredentialBroker(CredentialBroker.generate_key())
    client = FakeClient([("submit_result", {"decision": "drop", "reason": "Real."})])
    ai.configure_runtime(broker=broker, client_factory=lambda key, url: client)
    monkeypatch.setattr(netguard, "check_destination", lambda *a, **k: None)
    try:
        AIService(crm, broker).connect("main", provider="openai", models=["m"], api_key="k")
        AIService(crm).set_profile(name="default", connection="main", model="m", default=True)
        result = crm.workflows.test(ASSESS, {"lead_id": lead(crm)["id"]}, real_ai=True)
    finally:
        ai.configure_runtime(broker=None, client_factory=None)
    assert result["steps"]["assess"]["output"] == {"decision": "drop", "reason": "Real."}
    assert count(crm.conn._conn, crm.tenant.schema, "ai_usage") == 1  # spend is not hidden
