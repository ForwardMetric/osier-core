from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import psycopg
import pytest

from osier import Principal, Session, drop_tenant, provision_tenant, standard
from osier.context import ActorType

DATABASE_URL = os.environ.get(
    "OSIER_TEST_DATABASE_URL", "postgresql://osier:osier@localhost:5432/osier_test"
)

ADMIN = Principal(user_id=uuid.UUID("00000000-0000-0000-0000-00000000a001"), source="chat")
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
        pytest.skip(f"PostgreSQL not available at {DATABASE_URL}: {exc}")
    with connection:
        yield connection


@pytest.fixture
def tenant(conn: psycopg.Connection):
    t = provision_tenant(conn)
    yield t
    drop_tenant(conn, t)


@pytest.fixture
def session(conn, tenant) -> Session:
    return Session(conn, tenant, ADMIN)


@pytest.fixture
def crm(session: Session) -> Session:
    """A tenant with the standard objects installed."""
    standard.install(session)
    return session


@pytest.fixture
def agent(crm: Session) -> Session:
    return crm.as_principal(AGENT)


def apply(session: Session, operations):
    proposal = session.schema.propose(operations)
    assert proposal.can_apply, proposal.blockers
    return session.schema.apply(proposal.id, diff_hash=proposal.diff_hash)
