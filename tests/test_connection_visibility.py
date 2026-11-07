"""Who may have their agents use a connection: private, roles, organization."""

import pytest
from conftest import agent_for

from osier.connectors import ConnectorService, CredentialBroker
from osier.errors import NotFoundError, RecordValidationError

KEY = CredentialBroker.generate_key()


@pytest.fixture
def setup(crm, make_user):
    crm.records.create("role", {"name": "Marketing", "key": "marketing"})
    people = {
        "dev": make_user("dee@acme.test", "developer"),
        "marketer": make_user("mo@acme.test", "member", "marketing"),
        "sales": make_user("sam@acme.test", "member"),
    }
    return people


def svc(session, principal):
    return ConnectorService(session.as_principal(principal), CredentialBroker(KEY))


def names(service):
    return sorted(c["name"] for c in service.list_connections())


def test_visibility_controls_who_can_use(crm, setup):
    dev = svc(crm, setup["dev"])
    dev.create_connection("dev_outbox", "Everyone outbox", approval="not_required")
    dev.create_connection(
        "dev_outbox",
        "Marketing outbox",
        approval="not_required",
        visibility="roles",
        visible_roles=["marketing"],
    )
    dev.create_connection(
        "dev_outbox", "Dee private", approval="not_required", visibility="private"
    )

    assert names(svc(crm, setup["marketer"])) == ["Everyone outbox", "Marketing outbox"]
    assert names(svc(crm, setup["sales"])) == ["Everyone outbox"]
    assert names(dev) == ["Dee private", "Everyone outbox", "Marketing outbox"]

    sales_agent = svc(crm, agent_for(setup["sales"].user_id))
    with pytest.raises(NotFoundError):
        sales_agent.send_email(
            {"to": ["a@b.com"], "subject": "Hi", "body_text": "Hello."},
            connection="Marketing outbox",
        )
    marketer_agent = svc(crm, agent_for(setup["marketer"].user_id))
    sent = marketer_agent.send_email(
        {"to": ["a@b.com"], "subject": "Hi", "body_text": "Hello."}, connection="Marketing outbox"
    )
    assert sent["status"] == "succeeded"


def test_sharing_can_be_changed_by_a_manager(crm, setup):
    dev = svc(crm, setup["dev"])
    dev.create_connection("dev_outbox", "Outbox", visibility="private")
    assert names(svc(crm, setup["sales"])) == []
    shared = dev.set_visibility("Outbox", "organization")
    assert shared["visibility"] == "organization"
    assert names(svc(crm, setup["sales"])) == ["Outbox"]


def test_sharing_a_personal_account_warns(crm, setup):
    dev = svc(crm, setup["dev"])
    result = dev.create_connection(
        "dev_outbox", "Dee mailbox", scope="user", visibility="organization"
    )
    assert "act as that account" in result["warning"]
    assert "warning" not in dev.create_connection("dev_outbox", "Team box")


def test_personal_accounts_default_to_private(crm, setup):
    dev = svc(crm, setup["dev"])
    assert dev.create_connection("dev_outbox", "Mine", scope="user")["visibility"] == "private"


@pytest.mark.parametrize(
    ("visibility", "roles"),
    [("roles", None), ("roles", ["nope"]), ("organization", ["marketing"]), ("public", None)],
)
def test_invalid_visibility(crm, setup, visibility, roles):
    with pytest.raises(RecordValidationError):
        svc(crm, setup["dev"]).create_connection(
            "dev_outbox", "X", visibility=visibility, visible_roles=roles
        )
