"""Portal: the approvals inbox."""

import uuid

import pytest
from portal_helpers import FakeSMTP, agent_for, csrf_of, email_args, post, sign_in

from osier.connectors import ConnectorService, CredentialBroker
from osier.connectors import smtp as smtp_module

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


@pytest.fixture(autouse=True)
def fake_smtp(monkeypatch):
    FakeSMTP.sent, FakeSMTP.logins = [], []
    monkeypatch.setattr(smtp_module.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(smtp_module, "check_destination", lambda *a, **k: None)


def propose(crm, boot):
    agent = crm.as_principal(agent_for(boot.admin_user_id))
    return agent.schema.propose(BUDGET)


def test_an_empty_inbox(admin_client):
    page = admin_client.get("/approvals")
    assert page.status_code == 200 and "Nothing is waiting for you" in page.text


def test_a_schema_change_an_agent_proposed_can_be_approved(admin_client, crm, boot):
    proposal = propose(crm, boot)
    home = admin_client.get("/").text
    assert "1 item(s) need approval" in home
    page = admin_client.get("/approvals")
    assert "c__budget" in page.text and "proposed by agent" in page.text
    response = post(
        admin_client, f"/approvals/schema/{proposal.id}/approve", diff_hash=proposal.diff_hash
    )
    assert response.status_code == 303
    assert "c__budget" in crm.snapshot().get_object("lead").active_fields
    assert "Nothing is waiting for you" in admin_client.get("/approvals").text


def test_approving_a_proposal_that_changed_since_it_was_shown_is_refused(admin_client, crm, boot):
    proposal = propose(crm, boot)
    response = post(
        admin_client, f"/approvals/schema/{proposal.id}/approve", diff_hash="hash-of-something-else"
    )
    assert response.status_code == 400
    assert "c__budget" not in crm.snapshot().get_object("lead").active_fields


def test_a_schema_change_can_be_rejected(admin_client, crm, boot):
    proposal = propose(crm, boot)
    response = post(admin_client, f"/approvals/schema/{proposal.id}/reject")
    assert response.status_code == 303
    assert "c__budget" not in crm.snapshot().get_object("lead").active_fields
    assert "Nothing is waiting" in admin_client.get("/approvals").text


def test_a_member_cannot_see_or_decide_schema_changes(client, crm, boot, make_user):
    proposal = propose(crm, boot)
    sign_in(client, make_user("rep@acme.test", "member").user_id)
    assert "c__budget" not in client.get("/approvals").text
    response = post(
        client, f"/approvals/schema/{proposal.id}/approve", diff_hash=proposal.diff_hash
    )
    assert response.status_code == 403
    assert "c__budget" not in crm.snapshot().get_object("lead").active_fields


KEY = CredentialBroker.generate_key()


@pytest.fixture
def broker_key(config, monkeypatch):
    """Make the portal's broker the same as the one the test uses to set things up."""
    monkeypatch.setattr(type(config), "broker", lambda self: CredentialBroker(KEY))


def test_an_email_an_agent_wants_to_send_can_be_approved(admin_client, crm, boot, broker_key):
    admin = ConnectorService(crm, CredentialBroker(KEY))
    admin.create_connection(
        "smtp",
        "Sales SMTP",
        config={
            "host": "smtp.example.com",
            "username": "s@acme.test",
            "from_address": "s@acme.test",
        },
        secrets={"password": "app-password"},
    )
    agent = ConnectorService(crm.as_principal(agent_for(boot.admin_user_id)), CredentialBroker(KEY))
    call = agent.send_email(email_args())
    assert call["status"] == "awaiting_approval"
    page = admin_client.get("/approvals")
    assert "jane@example.com" in page.text and "Hi Jane, thanks for your time." in page.text
    assert FakeSMTP.sent == []  # nothing sent just by looking
    response = post(admin_client, f"/approvals/action/{call['call_id']}/approve")
    assert response.status_code == 303
    assert len(FakeSMTP.sent) == 1
    assert "Nothing is waiting" in admin_client.get("/approvals").text


def test_a_rejected_email_is_never_sent(admin_client, crm, boot, broker_key):
    admin = ConnectorService(crm, CredentialBroker(KEY))
    admin.create_connection(
        "smtp",
        "Sales SMTP",
        config={
            "host": "smtp.example.com",
            "username": "s@acme.test",
            "from_address": "s@acme.test",
        },
        secrets={"password": "app-password"},
    )
    agent = ConnectorService(crm.as_principal(agent_for(boot.admin_user_id)), CredentialBroker(KEY))
    call = agent.send_email(email_args())
    post(admin_client, f"/approvals/action/{call['call_id']}/reject")
    assert FakeSMTP.sent == []


def test_email_bodies_from_agents_are_escaped(admin_client, crm, boot, broker_key):
    admin = ConnectorService(crm, CredentialBroker(KEY))
    admin.create_connection(
        "smtp",
        "Sales SMTP",
        config={
            "host": "smtp.example.com",
            "username": "s@acme.test",
            "from_address": "s@acme.test",
        },
        secrets={"password": "app-password"},
    )
    agent = ConnectorService(crm.as_principal(agent_for(boot.admin_user_id)), CredentialBroker(KEY))
    agent.send_email(email_args(body_text="<script>alert('x')</script>"))
    page = admin_client.get("/approvals").text
    assert "<script>alert" not in page and "&lt;script&gt;" in page


WORKFLOW = {
    "name": "qualify-lead",
    "description": "Ask for approval, then mark a lead as qualified.",
    "inputs": {"lead_id": {"type": "string", "description": "The lead."}},
    "steps": [
        {"id": "approve", "type": "approval", "message": "Qualify this lead?"},
        {
            "id": "mark",
            "type": "update_record",
            "object": "lead",
            "record_id": "{{inputs.lead_id}}",
            "values": {"status": "qualified"},
        },
    ],
}


def test_a_workflow_approval_can_be_decided_in_the_portal(admin_client, crm):
    crm.workflows.save(WORKFLOW)
    lead = crm.records.create("lead", {"family_name": "Doe"})
    run = crm.workflows.start("qualify-lead", {"lead_id": lead["id"]})
    page = admin_client.get("/approvals")
    assert "Qualify this lead?" in page.text and "qualify-lead" in page.text
    response = post(admin_client, f"/approvals/workflow/{run['run_id']}/approve", comment="ok")
    assert response.status_code == 303
    assert crm.records.get("lead", lead["id"])["status"] == "qualified"
    assert crm.workflows.get_run(run["run_id"])["status"] == "succeeded"


def test_a_workflow_approval_can_be_rejected(admin_client, crm):
    crm.workflows.save(WORKFLOW)
    lead = crm.records.create("lead", {"family_name": "Doe"})
    run = crm.workflows.start("qualify-lead", {"lead_id": lead["id"]})
    post(admin_client, f"/approvals/workflow/{run['run_id']}/reject")
    assert crm.records.get("lead", lead["id"])["status"] == "new"
    assert crm.workflows.get_run(run["run_id"])["status"] == "rejected"


def test_unknown_items_are_errors_not_crashes(admin_client):
    assert post(admin_client, f"/approvals/workflow/{uuid.uuid4()}/approve").status_code == 404
    assert csrf_of(admin_client.get("/approvals").text)
