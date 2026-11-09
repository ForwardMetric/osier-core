"""Portal: AI model connections and profiles."""

import json

import pytest
from portal_helpers import csrf_of, post, sign_in
from psycopg import sql

from osier.ai import AIService

SECRET = "sk-portal-secret-1234567890"


@pytest.fixture(autouse=True)
def no_dns(monkeypatch):
    import osier.connectors.netguard as netguard

    monkeypatch.setattr(netguard, "check_destination", lambda *a, **k: None)


def add_connection(client, **overrides):
    data = {
        "name": "main",
        "provider": "openrouter",
        "models": "vendor/big\nvendor/small",
        "api_key": SECRET,
        **overrides,
    }
    return post(client, "/ai/connections", **data)


def test_members_cannot_open_ai_pages(client, make_user):
    sign_in(client, make_user("rep@acme.test", "member").user_id)
    assert client.get("/ai").status_code == 403
    token = csrf_of(client.get("/").text)
    response = client.post(
        "/ai/connections",
        data={"csrf": token, "name": "x", "provider": "openai", "models": "m", "api_key": "k"},
    )
    assert response.status_code == 403


def test_the_nav_hides_ai_from_people_without_permission(client, admin_client, make_user):
    assert "/ai" in admin_client.get("/").text
    other = client.__class__(client.app, base_url=str(client.base_url), follow_redirects=False)
    sign_in(other, make_user("rep@acme.test", "member").user_id)
    assert 'href="/ai"' not in other.get("/").text


def test_connecting_a_model_never_shows_the_key(admin_client):
    response = add_connection(admin_client)
    assert response.status_code == 303 and response.headers["location"] == "/ai"
    page = admin_client.get("/ai")
    assert "Connected main." in page.text and "Key saved" in page.text
    assert SECRET not in page.text and SECRET not in response.text
    assert "vendor/big" in page.text


def test_the_key_is_encrypted_at_rest(admin_client, conn, config):
    add_connection(admin_client)
    ciphertext, settings = conn.execute(
        sql.SQL("SELECT secret_ciphertext, settings::text FROM {}").format(
            sql.Identifier(config.tenant_schema, "ai_connections")
        )
    ).fetchone()
    assert SECRET.encode() not in bytes(ciphertext) and SECRET not in settings
    status = AIService(config.session(config.admin("test"))).status()
    assert SECRET not in json.dumps(status, default=str)


def test_profiles_make_a_model_usable_and_default(admin_client):
    add_connection(admin_client)
    response = post(
        admin_client,
        "/ai/profiles",
        name="careful",
        connection="main",
        model="main: vendor/big",
        effort="high",
        description="Best answers",
        default="1",
    )
    assert response.status_code == 303
    page = admin_client.get("/ai").text
    assert "careful" in page and "Default" in page and "main / vendor/big" in page


def test_a_profile_cannot_use_a_model_the_connection_does_not_allow(admin_client):
    add_connection(admin_client)
    response = post(admin_client, "/ai/profiles", name="x", connection="main", model="other/model")
    assert response.status_code == 400 and "does not allow model" in response.text


def test_bad_input_shows_a_clear_error(admin_client):
    response = add_connection(admin_client, provider="compatible")  # no address
    assert response.status_code == 400 and "needs an API base URL" in response.text
    response = add_connection(admin_client, name="Bad Name")
    assert response.status_code == 400
    response = add_connection(admin_client, monthly_budget_usd="10", provider="openai")
    assert response.status_code == 400 and "needs prices" in response.text
    assert SECRET not in response.text  # errors never echo the key


def test_updating_limits_and_removing_a_connection(admin_client):
    add_connection(admin_client)
    response = post(admin_client, "/ai/connections/main/update", monthly_token_limit="50000")
    assert response.status_code == 303
    assert "50000" in admin_client.get("/ai").text
    confirm = admin_client.get("/ai/connections/main/disconnect")
    assert confirm.status_code == 200 and "Remove main?" in confirm.text
    assert admin_client.get("/ai/connections/nope/disconnect").status_code == 404
    done = post(admin_client, "/ai/connections/main/disconnect")
    assert done.status_code == 303
    assert "No AI connections yet" in admin_client.get("/ai").text


def test_a_connection_in_use_cannot_be_removed(admin_client):
    add_connection(admin_client)
    post(admin_client, "/ai/profiles", name="p", connection="main", model="vendor/big")
    response = post(admin_client, "/ai/connections/main/disconnect")
    assert response.status_code == 400 and "use this connection" in response.text
    post(admin_client, "/ai/profiles/p/remove")
    assert post(admin_client, "/ai/connections/main/disconnect").status_code == 303


def test_forms_without_the_csrf_token_do_nothing(admin_client):
    response = admin_client.post(
        "/ai/connections",
        data={"name": "main", "provider": "openai", "models": "m", "api_key": SECRET},
    )
    assert response.status_code == 403
    assert "No AI connections yet" in admin_client.get("/ai").text


def test_a_developer_without_ai_manage_is_refused(client, make_user):
    sign_in(client, make_user("dev@acme.test", "developer").user_id)
    assert client.get("/ai").status_code == 403  # ai.manage is admin-only by default
