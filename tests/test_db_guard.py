"""The central tenant guard (osier.db.TenantConnection)."""

import pytest
from psycopg import sql

from osier import Session
from osier.db import TenantConnection, TenantIsolationError


@pytest.fixture
def guard(crm) -> TenantConnection:
    return crm.conn


def test_sessions_only_expose_the_guarded_connection(crm):
    assert isinstance(crm.conn, TenantConnection)
    for service in (crm.metadata, crm.schema, crm.records):
        assert isinstance(service._conn, TenantConnection)


def test_own_schema_is_allowed(guard):
    row = guard.execute(
        sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(guard.tenant.schema, "account"))
    ).fetchone()
    assert row[0] == 0


def test_plain_string_sql_is_rejected(guard):
    with pytest.raises(TenantIsolationError):
        guard.execute(f'SELECT * FROM "{guard.tenant.schema}".account')  # noqa: S608


def test_other_tenant_schema_is_rejected(guard, conn):
    other = sql.Identifier("t_" + "0" * 32, "account")
    with pytest.raises(TenantIsolationError) as exc:
        guard.execute(sql.SQL("SELECT * FROM {}").format(other))
    assert exc.value.code == "TENANT_ISOLATION_VIOLATION"


def test_other_schema_is_rejected_in_cursors_and_executemany(guard):
    other = sql.SQL("SELECT * FROM {}").format(sql.Identifier("public", "x"))
    with guard.cursor() as cur:
        with pytest.raises(TenantIsolationError):
            cur.execute(other)
        with pytest.raises(TenantIsolationError):
            cur.executemany(other, [()])


def test_three_part_identifiers_are_rejected(guard):
    with pytest.raises(TenantIsolationError):
        guard.execute(
            sql.SQL("SELECT * FROM {}").format(sql.Identifier("db", guard.tenant.schema, "account"))
        )


@pytest.mark.parametrize(
    "fragment",
    [
        'SELECT * FROM "t_other".account',
        "SELECT * FROM t_other.account",
        "SELECT * FROM pg_catalog.pg_class",
        "SELECT * FROM t_other . account",
    ],
)
def test_qualification_cannot_be_smuggled_in_literal_sql(guard, fragment):
    with pytest.raises(TenantIsolationError):
        guard.execute(sql.SQL(fragment))  # noqa: S608


def test_guard_inspects_nested_compositions(guard):
    other = sql.Identifier("t_" + "1" * 32, "account")
    nested = sql.SQL("SELECT 1 WHERE EXISTS ({})").format(
        sql.SQL("SELECT 1 FROM {} WHERE {} = %s").format(other, sql.Identifier("id"))
    ) + sql.SQL(" LIMIT 1")
    with pytest.raises(TenantIsolationError):
        guard.execute(nested, (None,))


def test_session_rejects_guard_for_another_tenant(crm, conn, boot):
    from osier import bootstrap_tenant, drop_tenant

    other = bootstrap_tenant(
        conn, organization_name="Other", admin_email="o@x.test", admin_display_name="O"
    )
    try:
        with pytest.raises(ValueError):
            Session(crm.conn, other.tenant, other.admin)
    finally:
        drop_tenant(conn, other.tenant)


def test_psycopg_internals_relied_on_by_the_guard_still_exist():
    composed = sql.SQL("SELECT {}").format(sql.Identifier("a", "b"))
    assert isinstance(composed._obj, list)
    assert sql.Identifier("a", "b")._obj == ("a", "b")
    assert sql.SQL("x")._obj == "x"
