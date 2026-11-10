"""Portal: the app gallery, one-field presets, and OAuth sign-in from a tile."""

import re
import sys
from pathlib import Path
from urllib.parse import quote, urlparse

import httpx2
import pytest
from portal_helpers import BASE, KEY, PASSWORD, FakeSMTP, post, sign_in
from starlette.testclient import TestClient
from test_oauth_connect import MCP_URL, FakeProvider

from osier.connectors import smtp as smtp_module
from osier.portal import create_app
from osier.portal.catalog import BY_KEY, CATEGORIES, TILES

sys.path.insert(0, str(Path(__file__).parent.parent / "fixtures"))
CALLBACK = f"{BASE}/oauth/callback"


@pytest.fixture(autouse=True)
def fakes(monkeypatch):
    monkeypatch.setattr(smtp_module.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(smtp_module, "check_destination", lambda *a, **k: None)
    monkeypatch.setenv("NOTES_TOKEN", "secret-token")  # the in-process fixture server checks it


def test_every_tile_has_a_category_and_a_page(admin_client):
    assert {t.category for t in TILES} <= set(CATEGORIES)
    assert len({t.key for t in TILES}) == len(TILES)
    for tile in TILES:
        page = admin_client.get(f"/apps/add/{tile.key}")
        assert page.status_code == 200, tile.key
        assert tile.name in page.text


def test_browse_search_and_categories(admin_client):
    page = admin_client.get("/apps").text
    assert all(f"/apps/add/{t.key}" in page for t in TILES)
    found = admin_client.get("/apps?q=gmail").text
    assert "/apps/add/gmail" in found and "/apps/add/notion" not in found
    emails = admin_client.get("/apps?cat=Email").text
    assert "/apps/add/sendgrid" in emails and "/apps/add/zapier" not in emails
    unknown = admin_client.get("/apps?cat=Nope").text  # an unknown category shows everything
    assert "/apps/add/zapier" in unknown
    none = admin_client.get("/apps?q=zzzz").text
    assert "No app matches" in none and "/apps/add/mcp" in none


def test_search_text_is_escaped(admin_client):
    page = admin_client.get('/apps?q="><script>alert(1)</script>').text
    assert "<script>alert(1)</script>" not in page


def test_unknown_tile_is_404_and_members_are_refused(client, admin_client, make_user):
    assert admin_client.get("/apps/add/nope").status_code == 404
    member = TestClient(client.app, base_url=BASE, follow_redirects=False)
    sign_in(member, make_user("rep@acme.test", "member").user_id)
    assert member.get("/apps/add/gmail").status_code == 403
    assert post(member, "/apps/add/gmail", email="a@b.co", password="x").status_code == 403


def test_gmail_needs_only_an_address_and_a_password(admin_client, conn, config):
    response = post(admin_client, "/apps/add/gmail", email="sam@example.com", password=PASSWORD)
    assert response.status_code == 303 and "tab=connected" in response.headers["location"]
    page = admin_client.get("/apps?tab=connected").text
    assert "Gmail" in page and "Connected Gmail" in page and PASSWORD not in page
    assert "Connected" in admin_client.get("/apps").text  # the tile shows it
    detail = admin_client.get("/apps/Gmail")
    assert detail.status_code == 200 and PASSWORD not in detail.text
    from psycopg import sql

    config_json = conn.execute(
        sql.SQL("SELECT config FROM {}").format(
            sql.Identifier(config.tenant_schema, "connector_credentials")
        )
    ).fetchone()[0]
    assert config_json["host"] == "smtp.gmail.com" and config_json["port"] == 587
    assert config_json["username"] == config_json["from_address"] == "sam@example.com"


def test_sendgrid_uses_the_apikey_username(admin_client, conn, config):
    post(admin_client, "/apps/add/sendgrid", email="sam@example.com", password=PASSWORD)
    from psycopg import sql

    cfg = conn.execute(
        sql.SQL("SELECT config FROM {}").format(
            sql.Identifier(config.tenant_schema, "connector_credentials")
        )
    ).fetchone()[0]
    assert cfg["username"] == "apikey" and cfg["from_address"] == "sam@example.com"


def test_a_wrong_password_is_rejected_without_echoing_it(admin_client):
    response = post(admin_client, "/apps/add/gmail", email="sam@example.com", password="nope-xyz")
    assert response.status_code == 400 and "nope-xyz" not in response.text
    assert "Nothing connected yet" in admin_client.get("/apps?tab=connected").text


def test_tiles_with_their_own_form_refuse_the_generic_post(admin_client):
    assert post(admin_client, "/apps/add/mcp", name="x").status_code == 400
    assert post(admin_client, "/apps/add/outbox", name="x").status_code == 400


# ---- OAuth from a tile ---------------------------------------------------------------


@pytest.fixture
def oauth(config, monkeypatch):
    from mcp.client import Client
    from notes_mcp_server import server as notes  # type: ignore[import-not-found]

    provider = FakeProvider(redirect=CALLBACK)
    monkeypatch.setitem(BY_KEY["notion"].settings, "url", MCP_URL)
    app, _portal = create_app(
        config,
        launch_key=KEY,
        connector_options={
            "mcp_client_factory": lambda: Client(notes),
            "oauth_http_client": httpx2.Client(transport=httpx2.MockTransport(provider.handle)),
        },
    )
    yield TestClient(app, base_url=BASE, follow_redirects=False), provider


def start(client, provider):
    response = post(client, "/apps/add/notion", name="Notion", visibility="organization")
    assert response.status_code == 303
    location = response.headers["location"]
    assert location.startswith("https://auth.notion.test/authorize")
    return provider.authorize(location)


def test_notion_tile_signs_in_then_reviews_tools_then_connects(oauth, boot):
    client, provider = oauth
    sign_in(client, boot.admin_user_id)
    state, code = start(client, provider)
    callback = client.get(f"/oauth/callback?state={quote(state)}&code={quote(code)}")
    assert callback.status_code == 303 and callback.headers["location"].startswith("/apps/connect/")
    review = client.get(callback.headers["location"])
    assert review.status_code == 200
    assert "search_notes" in review.text and "delete_all_notes" in review.text
    assert "at-1" not in review.text and code not in review.text
    token = re.search(r'name="token" value="([^"]+)"', review.text).group(1)
    done = post(client, "/apps/connect/finish", token=token)
    assert done.status_code == 303
    page = client.get("/apps?tab=connected").text
    assert "Notion" in page and "Connected Notion" in page and "MCP server" in page
    assert post(client, "/apps/connect/finish", token=token).status_code == 400  # one use


def test_the_callback_works_without_a_session_but_only_once(oauth, boot):
    client, provider = oauth
    sign_in(client, boot.admin_user_id)
    state, code = start(client, provider)
    anonymous = TestClient(client.app, base_url=BASE, follow_redirects=False)
    first = anonymous.get(f"/oauth/callback?state={quote(state)}&code={quote(code)}")
    assert first.status_code == 303
    assert anonymous.get(first.headers["location"]).status_code == 303  # needs sign-in
    again = anonymous.get(f"/oauth/callback?state={quote(state)}&code={quote(code)}")
    assert again.status_code == 400 and "already used" in again.text


def test_a_denied_or_bogus_callback_connects_nothing(oauth, boot):
    client, provider = oauth
    sign_in(client, boot.admin_user_id)
    state, _code = start(client, provider)
    denied = client.get(f"/oauth/callback?state={quote(state)}&error=access_denied")
    assert denied.status_code == 400 and "access_denied" in denied.text
    assert client.get("/oauth/callback").status_code == 400
    assert client.get("/oauth/callback?state=bogus&code=x").status_code in (400, 404)
    assert "Nothing connected yet" in client.get("/apps?tab=connected").text


def test_only_the_person_who_started_can_finish(oauth, boot, make_user):
    client, provider = oauth
    sign_in(client, boot.admin_user_id)
    state, code = start(client, provider)
    done = client.get(f"/oauth/callback?state={quote(state)}&code={quote(code)}")
    path = urlparse(done.headers["location"]).path
    other = TestClient(client.app, base_url=BASE, follow_redirects=False)
    sign_in(other, make_user("dev@acme.test", "admin").user_id)
    refused = other.get(path)
    assert refused.status_code in (400, 403) and "Only the person who started" in refused.text
