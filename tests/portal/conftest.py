"""Fixtures for the portal tests."""

import uuid

import pytest
from portal_helpers import BASE, DATABASE_URL, KEY, sign_in
from starlette.testclient import TestClient

from osier.devconfig import DevConfig, write_key_file
from osier.portal import create_app


@pytest.fixture
def config(boot, crm, tmp_path):
    key = tmp_path / "secret.key"
    write_key_file(key)
    return DevConfig(
        database_url=DATABASE_URL,
        tenant_id=str(boot.tenant.tenant_id),
        tenant_schema=boot.tenant.schema,
        organization_id=str(boot.organization_id),
        admin_user_id=str(boot.admin_user_id),
        agent_user_id=str(uuid.uuid5(boot.admin_user_id, "mcp-agent")),
        key_file=str(key),
    )


@pytest.fixture
def made(config):
    return create_app(config, launch_key=KEY)


@pytest.fixture
def client(made):
    return TestClient(made[0], base_url=BASE, follow_redirects=False)


@pytest.fixture
def admin_client(client, boot):
    sign_in(client, boot.admin_user_id)
    return client
