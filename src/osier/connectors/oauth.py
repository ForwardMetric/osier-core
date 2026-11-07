"""OAuth 2.1 client for MCP servers (the MCP authorization flow).

Steps: discover the server's protected-resource metadata and its authorization
server (RFC 9728, RFC 8414), register osier as a client dynamically when the
server supports it (RFC 7591) or use a client the person registered by hand,
send the person to the authorization URL with PKCE (S256) and a state value, then
exchange the code for tokens and refresh them before they expire. The ``resource``
parameter (RFC 8707) binds tokens to the MCP server.

Tokens never reach agents: they are stored encrypted by the connector service.
"""

from __future__ import annotations

import base64
import hashlib
import re
import secrets
import time
from typing import Any
from urllib.parse import urlencode, urlparse, urlunparse

import httpx2

from ..errors import OsierError
from .netguard import check_destination

TIMEOUT_SECONDS = 20
CLIENT_NAME = "osier"


class OAuthError(OsierError):
    default_code = "OAUTH_ERROR"


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode()
    return verifier, challenge.rstrip("=")


def new_state() -> str:
    return secrets.token_urlsafe(32)


def state_hash(state: str) -> str:
    return hashlib.sha256(state.encode()).hexdigest()


class OAuthClient:
    def __init__(
        self,
        *,
        allow_private_network: bool = False,
        http_client: httpx2.Client | None = None,
    ) -> None:
        self._allow_private_network = allow_private_network
        # An injected client (tests) talks to fake servers, so the network guard is skipped.
        self._injected = http_client is not None
        self._http = http_client or httpx2.Client(timeout=TIMEOUT_SECONDS, follow_redirects=False)

    # ---- HTTP helpers ---------------------------------------------------------------

    def _check_url(self, url: str) -> None:
        parsed = urlparse(url)
        if parsed.scheme != "https" and not (
            self._allow_private_network and parsed.scheme == "http"
        ):
            raise OAuthError(f"OAuth endpoints must use https: {url}", code="INSECURE_TRANSPORT")
        if not self._injected:
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            check_destination(
                parsed.hostname or "", port, allow_private_network=self._allow_private_network
            )

    def _get_json(self, url: str) -> dict[str, Any] | None:
        self._check_url(url)
        try:
            response = self._http.get(url, headers={"Accept": "application/json"})
        except httpx2.HTTPError:
            raise OAuthError(
                f"Could not reach {url}.", code="OAUTH_UNREACHABLE", retryable=True
            ) from None
        if response.status_code != 200:
            return None
        try:
            data = response.json()
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    def _post_form(self, url: str, data: dict[str, str], auth: tuple[str, str] | None) -> dict:
        self._check_url(url)
        try:
            response = self._http.post(
                url, data=data, auth=auth, headers={"Accept": "application/json"}
            )
        except httpx2.HTTPError:
            raise OAuthError(
                "The authorization server is unreachable.", code="OAUTH_UNREACHABLE", retryable=True
            ) from None
        try:
            body = response.json()
        except ValueError:
            body = {}
        if response.status_code != 200 or "access_token" not in body:
            raise OAuthError(
                "The authorization server refused the token request: "
                f"{body.get('error', response.status_code)}.",
                code="OAUTH_TOKEN_FAILED",
                details={
                    "error": body.get("error"),
                    "error_description": body.get("error_description"),
                },
            )
        return body

    # ---- discovery --------------------------------------------------------------------

    def discover(self, resource_url: str) -> dict[str, Any]:
        """Find the authorization server for an MCP server URL."""
        self._check_url(resource_url)
        parsed = urlparse(resource_url)
        origin = f"{parsed.scheme}://{parsed.netloc}"

        metadata_urls: list[str] = []
        try:
            probe = self._http.post(
                resource_url,
                json={"jsonrpc": "2.0", "id": 0, "method": "ping"},
                headers={"Accept": "application/json, text/event-stream"},
            )
            if probe.status_code == 401:
                match = re.search(
                    r'resource_metadata="([^"]+)"', probe.headers.get("www-authenticate", "")
                )
                if match:
                    metadata_urls.append(match.group(1))
        except httpx2.HTTPError:
            pass
        path = parsed.path.rstrip("/")
        if path:
            metadata_urls.append(f"{origin}/.well-known/oauth-protected-resource{path}")
        metadata_urls.append(f"{origin}/.well-known/oauth-protected-resource")

        resource_metadata = None
        for url in metadata_urls:
            resource_metadata = self._get_json(url)
            if resource_metadata:
                break
        if resource_metadata and resource_metadata.get("authorization_servers"):
            issuer = resource_metadata["authorization_servers"][0]
            scopes = resource_metadata.get("scopes_supported") or []
        else:
            issuer, scopes = origin, []  # older servers: the MCP server is its own auth server

        server = self._authorization_server(issuer)
        if "S256" not in server.get("code_challenge_methods_supported", ["S256"]):
            raise OAuthError(
                "The authorization server does not support PKCE (S256).", code="OAUTH_UNSUPPORTED"
            )
        return {
            "resource": (resource_metadata or {}).get("resource") or resource_url,
            "issuer": server.get("issuer", issuer),
            "authorization_endpoint": server["authorization_endpoint"],
            "token_endpoint": server["token_endpoint"],
            "registration_endpoint": server.get("registration_endpoint"),
            "scopes_supported": scopes or server.get("scopes_supported") or [],
        }

    def _authorization_server(self, issuer: str) -> dict[str, Any]:
        parsed = urlparse(issuer)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        path = parsed.path.rstrip("/")
        candidates = (
            [
                f"{origin}/.well-known/oauth-authorization-server{path}",
                f"{origin}/.well-known/openid-configuration{path}",
                f"{origin}{path}/.well-known/openid-configuration",
            ]
            if path
            else [
                f"{origin}/.well-known/oauth-authorization-server",
                f"{origin}/.well-known/openid-configuration",
            ]
        )
        for url in candidates:
            data = self._get_json(url)
            if data and data.get("authorization_endpoint") and data.get("token_endpoint"):
                return data
        raise OAuthError(
            f"Could not find OAuth settings for {issuer}.",
            code="OAUTH_DISCOVERY_FAILED",
            hint="The server may not support OAuth sign-in; try an API token instead.",
        )

    # ---- client registration ----------------------------------------------------------

    def register(self, server: dict[str, Any], redirect_uri: str) -> dict[str, Any]:
        endpoint = server.get("registration_endpoint")
        if not endpoint:
            raise OAuthError(
                "This server does not support automatic client registration.",
                code="OAUTH_CLIENT_REQUIRED",
                hint="Register an OAuth app with the provider and pass its client id "
                "(and secret, if it has one).",
            )
        self._check_url(endpoint)
        try:
            response = self._http.post(
                endpoint,
                json={
                    "client_name": CLIENT_NAME,
                    "redirect_uris": [redirect_uri],
                    "grant_types": ["authorization_code", "refresh_token"],
                    "response_types": ["code"],
                    "token_endpoint_auth_method": "none",
                },
                headers={"Accept": "application/json"},
            )
        except httpx2.HTTPError:
            raise OAuthError(
                "Client registration failed: server unreachable.",
                code="OAUTH_UNREACHABLE",
                retryable=True,
            ) from None
        body = response.json() if response.content else {}
        if response.status_code not in (200, 201) or "client_id" not in body:
            raise OAuthError(
                f"Client registration was refused ({response.status_code}).",
                code="OAUTH_REGISTRATION_FAILED",
                details={"error": body.get("error")},
            )
        return {"client_id": body["client_id"], "client_secret": body.get("client_secret")}

    # ---- authorization and tokens -----------------------------------------------------

    @staticmethod
    def authorization_url(
        server: dict[str, Any],
        *,
        client_id: str,
        redirect_uri: str,
        state: str,
        code_challenge: str,
        scopes: list[str],
    ) -> str:
        params = {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "resource": server["resource"],
        }
        if scopes:
            params["scope"] = " ".join(scopes)
        parsed = urlparse(server["authorization_endpoint"])
        query = (parsed.query + "&" if parsed.query else "") + urlencode(params)
        return urlunparse(parsed._replace(query=query))

    def exchange_code(
        self,
        server: dict[str, Any],
        client: dict[str, Any],
        *,
        code: str,
        verifier: str,
        redirect_uri: str,
    ) -> dict[str, Any]:
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": verifier,
            "client_id": client["client_id"],
            "resource": server["resource"],
        }
        return _tokens(self._post_form(server["token_endpoint"], data, _basic(client)))

    def refresh(self, server: dict[str, Any], client: dict[str, Any], refresh_token: str) -> dict:
        data = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client["client_id"],
            "resource": server["resource"],
        }
        tokens = _tokens(self._post_form(server["token_endpoint"], data, _basic(client)))
        tokens.setdefault("refresh_token", refresh_token)  # servers may not rotate it
        if tokens["refresh_token"] is None:
            tokens["refresh_token"] = refresh_token
        return tokens


def _basic(client: dict[str, Any]) -> tuple[str, str] | None:
    secret = client.get("client_secret")
    return (client["client_id"], secret) if secret else None


def _tokens(body: dict[str, Any]) -> dict[str, Any]:
    expires_in = body.get("expires_in")
    return {
        "access_token": body["access_token"],
        "refresh_token": body.get("refresh_token"),
        "token_type": body.get("token_type", "Bearer"),
        "scope": body.get("scope"),
        "expires_at": time.time() + float(expires_in) if expires_in else None,
    }


def needs_refresh(tokens: dict[str, Any], margin_seconds: int = 60) -> bool:
    expires_at = tokens.get("expires_at")
    return bool(expires_at) and time.time() > float(expires_at) - margin_seconds
