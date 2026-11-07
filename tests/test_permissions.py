"""Roles and permissions: what the person behind a request (or an agent's person) may do."""

import pytest
from conftest import add, agent_for

from osier.authz import PermissionDenied
from osier.connectors import ConnectorService, CredentialBroker
from osier.tenant import upgrade_engine_tables

KEY = CredentialBroker.generate_key()


@pytest.fixture
def people(crm, make_user):
    return {
        "dev": make_user("dee@acme.test", "developer"),
        "sales": make_user("sam@acme.test", "member"),
        "nobody": make_user("nora@acme.test"),
    }


def test_sales_cannot_create_custom_fields_and_gets_a_clear_message(crm, people):
    sales = crm.as_principal(people["sales"])
    with pytest.raises(PermissionDenied) as exc:
        sales.schema.propose([add("lead", "c__budget", "decimal")])
    err = exc.value
    assert err.code == "MISSING_PERMISSION"
    assert "Developer" in err.message and "Your roles: Member" in err.message
    assert err.details["permission"] == "schema.manage"
    assert set(err.details["roles_with_permission"]) == {"admin", "developer"}
    assert "Ask an administrator (Ada Admin)" in err.hint


def test_agent_acting_for_sales_is_refused_the_same_way(crm, people):
    agent = crm.as_principal(agent_for(people["sales"].user_id))
    with pytest.raises(PermissionDenied):
        agent.schema.propose([add("lead", "c__budget", "decimal")])


def test_developer_can_change_schema_and_packages(crm, people):
    dev = crm.as_principal(people["dev"])
    proposal = dev.schema.propose([add("lead", "c__budget", "decimal")])
    dev.schema.apply(proposal.id, diff_hash=proposal.diff_hash)
    assert "c__budget" in crm.snapshot().get_object("lead").active_fields
    install = dev.packages.propose_install(["case_management"])
    dev.packages.apply(install.id, diff_hash=install.diff_hash)


def test_approver_also_needs_the_permission(crm, people):
    dev = crm.as_principal(people["dev"])
    proposal = dev.schema.propose([add("lead", "c__budget", "decimal")])
    sales = crm.as_principal(people["sales"])
    with pytest.raises(PermissionDenied):
        sales.schema.apply(proposal.id, diff_hash=proposal.diff_hash)


def test_members_work_with_records(crm, people):
    sales = crm.as_principal(people["sales"])
    lead = sales.records.create("lead", {"family_name": "Doe"})
    assert sales.records.get("lead", lead["id"])["family_name"] == "Doe"


def test_users_without_roles_cannot_read_records(crm, people):
    nobody = crm.as_principal(people["nobody"])
    with pytest.raises(PermissionDenied) as exc:
        nobody.records.query({"object": "lead"})
    assert "Your roles: none" in exc.value.message


def test_only_user_managers_change_roles(crm, people):
    dev = crm.as_principal(people["dev"])
    with pytest.raises(PermissionDenied) as exc:
        dev.records.create("role", {"name": "Sales Rep", "key": "sales_rep"})
    assert exc.value.details["permission"] == "users.manage"
    crm.records.create("role", {"name": "Sales Rep", "key": "sales_rep"})  # admin can


def test_role_assignment_takes_effect(crm, people):
    sales_principal = people["sales"]
    developer = crm.records.list("role", where={"key": "developer"})[0]
    crm.records.create(
        "user_role", {"app_user": str(sales_principal.user_id), "role": developer["id"]}
    )
    crm.as_principal(sales_principal).schema.propose([add("lead", "c__budget", "decimal")])


def test_connections_need_connections_manage(crm, people):
    sales = ConnectorService(crm.as_principal(people["sales"]), CredentialBroker(KEY))
    with pytest.raises(PermissionDenied):
        sales.create_connection("dev_outbox", "Outbox")
    dev = ConnectorService(crm.as_principal(people["dev"]), CredentialBroker(KEY))
    dev.create_connection("dev_outbox", "Outbox", approval="not_required")
    # members may still have agents use it
    agent = ConnectorService(
        crm.as_principal(agent_for(people["sales"].user_id)), CredentialBroker(KEY)
    )
    sent = agent.send_email({"to": ["a@b.com"], "subject": "Hi", "body_text": "Hello there."})
    assert sent["status"] == "succeeded"


def test_only_the_requester_or_a_manager_decides_on_an_action(crm, people, make_user):
    dev = ConnectorService(crm.as_principal(people["dev"]), CredentialBroker(KEY))
    dev.create_connection("dev_outbox", "Outbox")
    agent = ConnectorService(
        crm.as_principal(agent_for(people["sales"].user_id)), CredentialBroker(KEY)
    )
    call = agent.send_email({"to": ["a@b.com"], "subject": "Hi", "body_text": "Hello there."})
    other_member = ConnectorService(
        crm.as_principal(make_user("ola@acme.test", "member")), CredentialBroker(KEY)
    )
    with pytest.raises(PermissionDenied):
        other_member.approve(call["call_id"])
    sales = ConnectorService(crm.as_principal(people["sales"]), CredentialBroker(KEY))
    assert sales.approve(call["call_id"])["status"] == "succeeded"


def test_permissions_summary(crm, people):
    assert crm.as_principal(people["sales"]).authz.permissions() == {
        "records.use",
        "actions.request",
    }
    assert "schema.manage" in crm.as_principal(people["dev"]).authz.permissions()


def test_engine_table_upgrade_is_idempotent_and_keeps_custom_permissions(crm, conn):
    from psycopg import sql

    table = sql.Identifier(crm.tenant.schema, "meta_role_permissions")
    conn.execute(
        sql.SQL("DELETE FROM {} WHERE role_key = 'member' AND permission = %s").format(table),
        ("actions.request",),
    )
    upgrade_engine_tables(conn, crm.tenant)
    upgrade_engine_tables(conn, crm.tenant)
    rows = conn.execute(
        sql.SQL("SELECT permission FROM {} WHERE role_key = 'member'").format(table)
    ).fetchall()
    assert [r[0] for r in rows] == ["records.use"]  # tenant's change kept
