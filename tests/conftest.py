from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from typing import Any

import psycopg
import pytest

from osier import Principal, Session, bootstrap_tenant, drop_tenant
from osier.context import ActorType

DATABASE_URL = os.environ.get(
    "OSIER_TEST_DATABASE_URL", "postgresql://osier:osier@localhost:5432/osier_test"
)

HUMAN_FOR_AGENT = uuid.UUID("00000000-0000-0000-0000-00000000a002")
AGENT = Principal(
    user_id=uuid.UUID("00000000-0000-0000-0000-00000000b001"),
    actor_type=ActorType.AGENT,
    on_behalf_of=HUMAN_FOR_AGENT,
    agent_id="test-agent",
    source="chat",
)


@pytest.fixture
def conn() -> Iterator[psycopg.Connection]:
    try:
        connection = psycopg.connect(DATABASE_URL, autocommit=True)
    except psycopg.OperationalError as exc:  # pragma: no cover - environment problem
        # Fail loudly: silently skipping database tests would report a green run that
        # tested almost nothing. Set OSIER_ALLOW_NO_DB=1 to skip them on purpose.
        if os.environ.get("OSIER_ALLOW_NO_DB") == "1":
            pytest.skip(f"PostgreSQL not available at {DATABASE_URL}: {exc}")
        pytest.fail(f"PostgreSQL not available at {DATABASE_URL}: {exc}", pytrace=False)
    with connection:
        yield connection


@pytest.fixture
def boot(conn):
    """A bootstrapped tenant: core package, organization, administrator."""
    b = bootstrap_tenant(
        conn,
        organization_name="Acme Lending",
        admin_email="ada@acme.test",
        admin_display_name="Ada Admin",
    )
    yield b
    drop_tenant(conn, b.tenant)


@pytest.fixture
def tenant(boot):
    return boot.tenant


@pytest.fixture
def admin(boot) -> Principal:
    return Principal(user_id=boot.admin_user_id, source="chat")


@pytest.fixture
def session(conn, boot, admin) -> Session:
    return Session(conn, boot.tenant, admin)


@pytest.fixture
def crm(session: Session) -> Session:
    """A tenant with the CRM package installed."""
    proposal = session.packages.propose_install(["crm"])
    session.packages.apply(proposal.id, diff_hash=proposal.diff_hash)
    return session


@pytest.fixture
def agent(crm: Session) -> Session:
    return crm.as_principal(AGENT)


def fld(api_name: str, type: str, **kwargs: Any) -> dict[str, Any]:
    """A documented field spec for tests."""
    label = api_name.removeprefix("c__").replace("_", " ").title()
    return {
        "api_name": api_name,
        "label": label,
        "type": type,
        "description": f"Test field holding the {label.lower()} used by this test case.",
        **kwargs,
    }


def obj(api_name: str, label: str, fields: list[dict[str, Any]] | None = None, **kw: Any):
    return {
        "op": "create_object",
        "api_name": api_name,
        "label": label,
        "description": f"Test object representing a {label.lower()} for this test case.",
        "fields": fields or [],
        **kw,
    }


def add(object_name: str, api_name: str, type: str, **kwargs: Any) -> dict[str, Any]:
    return {"op": "add_field", "object": object_name, "field": fld(api_name, type, **kwargs)}


def apply(session: Session, operations):
    proposal = session.schema.propose(operations)
    assert proposal.can_apply, proposal.blockers
    return session.schema.apply(proposal.id, diff_hash=proposal.diff_hash)
