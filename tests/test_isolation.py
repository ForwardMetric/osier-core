import psycopg
import pytest
from conftest import ADMIN

from osier import Session, drop_tenant, provision_tenant, standard


def test_tenants_have_independent_schemas(conn, crm):
    other_tenant = provision_tenant(conn)
    try:
        other = Session(conn, other_tenant, ADMIN)
        standard.install(other)
        crm.records.create("account", {"name": "Tenant A only"})
        assert other.records.list("account") == []

        proposal = crm.schema.propose(
            [
                {
                    "op": "add_field",
                    "object": "account",
                    "field": {"api_name": "c_region", "label": "Region", "type": "text"},
                }
            ]
        )
        crm.schema.apply(proposal.id, diff_hash=proposal.diff_hash)
        assert "c_region" in crm.snapshot().get_object("account").active_fields
        assert "c_region" not in other.snapshot().get_object("account").fields
    finally:
        drop_tenant(conn, other_tenant)


def test_session_disables_search_path(crm):
    # Unqualified table names must fail rather than reach some schema by accident.
    with pytest.raises(psycopg.errors.UndefinedTable):
        crm.conn.execute("SELECT * FROM account")


def test_connection_must_be_autocommit(tenant):
    from conftest import DATABASE_URL

    with psycopg.connect(DATABASE_URL) as c, pytest.raises(ValueError):
        Session(c, tenant, ADMIN)
