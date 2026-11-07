"""Connecting an MCP server with OAuth sign-in, against a fake provider shaped like Notion's
hosted MCP server (protected-resource metadata, dynamic client registration, PKCE)."""

import base64
import hashlib
import json
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx2
import pytest
from conftest import agent_for

from osier.authz import PermissionDenied
from osier.connectors import ConnectorService, CredentialBroker
from osier.errors import ApprovalError, OsierError

KEY = CredentialBroker.generate_key()
MCP_URL = "https://mcp.notion.test/mcp"
REDIRECT = "http://localhost:8765/oauth/callback"
sys.path.insert(0, str(Path(__file__).parent / "fixtures"))


class FakeProvider:
    def __init__(self, *, registration=True, redirect=REDIRECT):
        self.registration = registration
        self.redirect = redirect
        self.challenges: dict[str, str] = {}
        self.token_requests: list[dict] = []
        self.issued = 0

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        url = str(request.url)
        if url == MCP_URL and request.method == "POST":
            return httpx2.Response(
                401,
                headers={
                    "WWW-Authenticate": 'Bearer resource_metadata="https://mcp.notion.test/'
                    '.well-known/oauth-protected-resource/mcp"'
                },
            )
        if url == "https://mcp.notion.test/.well-known/oauth-protected-resource/mcp":
            return httpx2.Response(
                200,
                json={"resource": MCP_URL, "authorization_servers": ["https://auth.notion.test"]},
            )
        if url == "https://auth.notion.test/.well-known/oauth-authorization-server":
            meta = {
                "issuer": "https://auth.notion.test",
                "authorization_endpoint": "https://auth.notion.test/authorize",
                "token_endpoint": "https://auth.notion.test/token",
                "code_challenge_methods_supported": ["S256"],
            }
            if self.registration:
                meta["registration_endpoint"] = "https://auth.notion.test/register"
            return httpx2.Response(200, json=meta)
        if url == "https://auth.notion.test/register":
            body = json.loads(request.content)
            assert body["redirect_uris"] == [self.redirect]
            assert body["token_endpoint_auth_method"] == "none"
            return httpx2.Response(201, json={"client_id": "dcr-client-1"})
        if url == "https://auth.notion.test/token":
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            self.token_requests.append(form)
            assert form["resource"] == MCP_URL
            if form["grant_type"] == "authorization_code":
                verifier = form["code_verifier"]
                challenge = (
                    base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
                    .decode()
                    .rstrip("=")
                )
                if self.challenges.get(form["code"]) != challenge:
                    return httpx2.Response(400, json={"error": "invalid_grant"})
            elif form["refresh_token"] != "rt-1":
                return httpx2.Response(400, json={"error": "invalid_grant"})
            self.issued += 1
            return httpx2.Response(
                200,
                json={
                    "access_token": f"at-{self.issued}",
                    "refresh_token": "rt-1",
                    "expires_in": 3600,
                    "token_type": "Bearer",
                },
            )
        return httpx2.Response(404)

    def authorize(self, authorization_url: str) -> tuple[str, str]:
        """What the person's browser does: sign in, approve, get redirected with a code."""
        params = {k: v[0] for k, v in parse_qs(urlparse(authorization_url).query).items()}
        assert params["code_challenge_method"] == "S256"
        assert params["resource"] == MCP_URL and params["redirect_uri"] == self.redirect
        code = f"code-{len(self.challenges)}"
        self.challenges[code] = params["code_challenge"]
        return params["state"], code


@pytest.fixture
def provider():
    return FakeProvider()


def service(session, provider):
    from mcp.client import Client
    from notes_mcp_server import server as notes  # type: ignore[import-not-found]

    return ConnectorService(
        session,
        CredentialBroker(KEY),
        mcp_client_factory=lambda: Client(notes),
        oauth_http_client=httpx2.Client(transport=httpx2.MockTransport(provider.handle)),
    )


def connect(svc, provider, **kw):
    started = svc.start_mcp_oauth(
        "Notion",
        url=MCP_URL,
        description="Company wiki and meeting notes in Notion.",
        redirect_uri=REDIRECT,
        **kw,
    )
    state, code = provider.authorize(started["authorization_url"])
    svc.complete_mcp_oauth(state, code=code)
    review = svc.review_mcp_oauth(started["oauth_id"])
    return svc.finish_mcp_oauth(started["oauth_id"], expected_tools_hash=review["tools_hash"])


def test_full_oauth_connect(crm, provider, monkeypatch):
    monkeypatch.setenv("NOTES_TOKEN", "secret-token")  # the in-process fixture server checks it
    svc = service(crm, provider)
    started = svc.start_mcp_oauth(
        "Notion",
        url=MCP_URL,
        description="Company wiki and meeting notes in Notion.",
        redirect_uri=REDIRECT,
    )
    params = parse_qs(urlparse(started["authorization_url"]).query)
    assert params["client_id"] == ["dcr-client-1"]  # registered automatically
    assert svc.oauth_status(started["oauth_id"])["status"] == "pending"

    state, code = provider.authorize(started["authorization_url"])
    assert svc.complete_mcp_oauth(state, code=code)["status"] == "authorized"
    review = svc.review_mcp_oauth(started["oauth_id"])
    assert {t["name"] for t in review["tools"]} == {
        "search_notes",
        "create_note",
        "delete_all_notes",
    }
    connection = svc.finish_mcp_oauth(started["oauth_id"], expected_tools_hash=review["tools_hash"])
    assert connection["runtime"] == "mcp" and connection["status"] == "active"
    assert svc.oauth_status(started["oauth_id"])["status"] == "completed"
    call = svc.call_tool("Notion", "search_notes", {"query": "acme"})
    assert call["status"] == "succeeded"


def test_tokens_are_encrypted_and_never_returned(crm, provider, conn):
    from psycopg import sql

    svc = service(crm, provider)
    connection = connect(svc, provider)
    assert "at-1" not in json.dumps(connection) and "at-1" not in json.dumps(svc.list_connections())
    row = conn.execute(
        sql.SQL("SELECT secret_ciphertext FROM {} WHERE connection_id = %s").format(
            sql.Identifier(crm.tenant.schema, "connector_credentials")
        ),
        (connection["id"],),
    ).fetchone()
    assert b"at-1" not in bytes(row[0])


def test_expired_access_token_is_refreshed(crm, provider):
    svc = service(crm, provider)
    connection = connect(svc, provider)
    _config, secrets = svc._secrets_for(connection["id"])
    assert secrets["oauth"]["tokens"]["access_token"] == "at-1"
    # force expiry
    secrets["oauth"]["tokens"]["expires_at"] = time.time() - 10
    from psycopg import sql

    svc._conn.execute(
        sql.SQL("UPDATE {} SET secret_ciphertext = %s WHERE connection_id = %s").format(
            svc._t("connector_credentials")
        ),
        (CredentialBroker(KEY).encrypt(secrets), connection["id"]),
    )
    _config, refreshed = svc._secrets_for(connection["id"])
    assert refreshed["oauth"]["tokens"]["access_token"] == "at-2"
    assert provider.token_requests[-1]["grant_type"] == "refresh_token"
    _config, again = svc._secrets_for(connection["id"])  # stored, no second refresh
    assert again["oauth"]["tokens"]["access_token"] == "at-2"


def test_state_is_single_use_and_unknown_states_fail(crm, provider):
    svc = service(crm, provider)
    started = svc.start_mcp_oauth(
        "Notion",
        url=MCP_URL,
        redirect_uri=REDIRECT,
        description="Company wiki and meeting notes in Notion.",
    )
    state, code = provider.authorize(started["authorization_url"])
    with pytest.raises(OsierError) as exc:
        svc.complete_mcp_oauth("forged-state", code=code)
    assert exc.value.code == "OAUTH_STATE_UNKNOWN"
    svc.complete_mcp_oauth(state, code=code)
    with pytest.raises(OsierError) as exc:
        svc.complete_mcp_oauth(state, code=code)
    assert exc.value.code == "OAUTH_STATE_USED"


def test_wrong_pkce_verifier_is_rejected(crm, provider):
    svc = service(crm, provider)
    started = svc.start_mcp_oauth(
        "Notion",
        url=MCP_URL,
        redirect_uri=REDIRECT,
        description="Company wiki and meeting notes in Notion.",
    )
    state, code = provider.authorize(started["authorization_url"])
    provider.challenges[code] = "tampered"
    with pytest.raises(OsierError) as exc:
        svc.complete_mcp_oauth(state, code=code)
    assert exc.value.code == "OAUTH_TOKEN_FAILED"


def test_provider_denial(crm, provider):
    svc = service(crm, provider)
    started = svc.start_mcp_oauth(
        "Notion",
        url=MCP_URL,
        redirect_uri=REDIRECT,
        description="Company wiki and meeting notes in Notion.",
    )
    state, _ = provider.authorize(started["authorization_url"])
    with pytest.raises(OsierError) as exc:
        svc.complete_mcp_oauth(state, error="access_denied")
    assert exc.value.code == "OAUTH_DENIED"


def test_servers_without_registration_need_a_client_id(crm):
    provider = FakeProvider(registration=False)
    svc = service(crm, provider)
    with pytest.raises(OsierError) as exc:
        svc.start_mcp_oauth(
            "Notion",
            url=MCP_URL,
            redirect_uri=REDIRECT,
            description="Company wiki and meeting notes in Notion.",
        )
    assert exc.value.code == "OAUTH_CLIENT_REQUIRED"
    connection = connect(svc, provider, client_id="manual-client", client_secret="s3cret")
    assert connection["status"] == "active"
    assert provider.token_requests[0]["client_id"] == "manual-client"


def test_only_people_who_manage_connections_can_connect(crm, provider, make_user):
    agent = service(crm.as_principal(agent_for(crm.principal.user_id)), provider)
    with pytest.raises(ApprovalError):
        agent.start_mcp_oauth(
            "Notion",
            url=MCP_URL,
            redirect_uri=REDIRECT,
            description="Company wiki and meeting notes in Notion.",
        )
    sales = service(crm.as_principal(make_user("sam@acme.test", "member")), provider)
    with pytest.raises(PermissionDenied):
        sales.start_mcp_oauth(
            "Notion",
            url=MCP_URL,
            redirect_uri=REDIRECT,
            description="Company wiki and meeting notes in Notion.",
        )


def test_shared_with_a_role(crm, provider, make_user):
    crm.records.create("role", {"name": "Sales Rep", "key": "sales_rep"})
    svc = service(crm, provider)
    connect(svc, provider, visibility="roles", visible_roles=["sales_rep"])
    rep = service(crm.as_principal(make_user("sam@acme.test", "member", "sales_rep")), provider)
    other = service(crm.as_principal(make_user("ola@acme.test", "member")), provider)
    assert [c["name"] for c in rep.list_connections()] == ["Notion"]
    assert other.list_connections() == []


def test_only_the_starter_can_finish(crm, provider, make_user):
    svc = service(crm, provider)
    started = svc.start_mcp_oauth(
        "Notion",
        url=MCP_URL,
        redirect_uri=REDIRECT,
        description="Company wiki and meeting notes in Notion.",
    )
    state, code = provider.authorize(started["authorization_url"])
    svc.complete_mcp_oauth(state, code=code)
    other_dev = service(crm.as_principal(make_user("dee@acme.test", "developer")), provider)
    with pytest.raises(ApprovalError) as exc:
        other_dev.review_mcp_oauth(started["oauth_id"])
    assert exc.value.code == "OAUTH_NOT_YOURS"


def test_oauth_transport_sends_bearer_token(monkeypatch):
    from osier.connectors import mcp_remote
    from osier.connectors.mcp_remote import McpTransport, RemoteMcpClient

    captured = {}

    class FakeAsyncClient:
        def __init__(self, headers=None, timeout=None):
            captured["headers"] = headers

    monkeypatch.setattr(mcp_remote, "check_destination", lambda *a, **k: None)
    import httpx2 as real

    monkeypatch.setattr(real, "AsyncClient", FakeAsyncClient)
    client = RemoteMcpClient(
        McpTransport(type="http", url=MCP_URL, auth="oauth"),
        {"oauth": {"tokens": {"access_token": "at-9"}}},
    )
    client._client()
    assert captured["headers"]["Authorization"] == "Bearer at-9"


def test_callback_server_passes_state_and_code_and_hides_internals():
    import socket
    import urllib.request

    from osier.connectors.oauth_callback import CallbackServer, redirect_uri

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    seen = []

    def handler(state, code, error):
        seen.append((state, code, error))
        if state == "boom":
            raise RuntimeError("secret internals")
        return True, "Notion is signed in."

    server = CallbackServer(port, handler)
    server.start()
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        page = opener.open(f"{redirect_uri(port)}?state=s1&code=c1").read().decode()
        assert "Notion is signed in." in page and seen[-1] == ("s1", "c1", None)
        try:
            opener.open(f"{redirect_uri(port)}?state=boom&code=c2")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode()
            assert exc.code == 400 and "secret internals" not in body
        try:
            opener.open(f"http://127.0.0.1:{port}/other")
        except urllib.error.HTTPError as exc:
            assert exc.code == 404
    finally:
        server.stop()
