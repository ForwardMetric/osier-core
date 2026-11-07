"""Connecting MCP servers with OAuth sign-in (e.g. Notion). Mixed into ConnectorService.

1. ``start_mcp_oauth``: a person with connections.manage asks to connect a server.
   osier discovers its OAuth settings, registers itself as a client (or uses one the
   person supplies), and returns an authorization URL. Nothing is connected yet.
2. The person signs in at the provider; the provider redirects to osier's callback,
   which calls ``complete_mcp_oauth`` with the state and code. osier exchanges the
   code for tokens and keeps them encrypted with the pending request.
3. ``review_mcp_oauth`` lists the server's tools with the new token, and
   ``finish_mcp_oauth`` registers the connection with exactly the tools the person
   reviewed. From then on tokens are refreshed automatically before they expire.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from ..errors import ApprovalError, NotFoundError
from .mcp_remote import McpTransport, tools_hash
from .mcp_service import default_tool_policy
from .oauth import OAuthClient, OAuthError, needs_refresh, new_state, pkce_pair, state_hash

FLOW_MINUTES = 15


class OAuthConnectMixin:
    _conn: Any
    _session: Any
    _principal: Any
    _broker: Any
    _allow_private_network: bool
    _oauth_http_client: Any

    def _oauth(self) -> OAuthClient:
        return OAuthClient(
            allow_private_network=self._allow_private_network,
            http_client=self._oauth_http_client,
        )

    def _oauth_row(self, *, oauth_id: Any = None, state: str | None = None) -> dict[str, Any]:
        if state is not None:
            where, value = sql.SQL("state_hash = %s"), state_hash(state)
        else:
            where, value = sql.SQL("id = %s"), uuid.UUID(str(oauth_id))
        with self._conn.cursor(row_factory=dict_row) as cur:
            row = cur.execute(
                sql.SQL("SELECT * FROM {} WHERE ").format(self._t("connector_oauth_states"))
                + where,
                (value,),
            ).fetchone()
        if row is None:
            raise NotFoundError("Unknown or expired sign-in request.", code="OAUTH_STATE_UNKNOWN")
        return row

    def _set_oauth(self, oauth_id: Any, **values: Any) -> None:
        assignments = [sql.SQL("{} = %s").format(sql.Identifier(k)) for k in values]
        self._conn.execute(
            sql.SQL("UPDATE {} SET {} WHERE id = %s").format(
                self._t("connector_oauth_states"), sql.SQL(", ").join(assignments)
            ),
            [*values.values(), oauth_id],
        )

    # ---- 1. start -----------------------------------------------------------------------

    def start_mcp_oauth(
        self,
        name: str,
        *,
        url: str,
        description: str,
        redirect_uri: str,
        visibility: str | None = None,
        visible_roles: list[str] | None = None,
        scope: str = "organization",
        client_id: str | None = None,
        client_secret: str | None = None,
        scopes: list[str] | None = None,
    ) -> dict[str, Any]:
        self._require_human("Connecting an app")
        visibility = self._check_visibility(visibility, visible_roles, scope)
        McpTransport.model_validate({"type": "http", "url": url, "auth": "oauth"})
        oauth = self._oauth()
        server = oauth.discover(url)
        client = (
            {"client_id": client_id, "client_secret": client_secret}
            if client_id
            else oauth.register(server, redirect_uri)
        )
        verifier, challenge = pkce_pair()
        state = new_state()
        oauth_id = uuid.uuid4()
        expires_at = dt.datetime.now(dt.UTC) + dt.timedelta(minutes=FLOW_MINUTES)
        request = {
            "name": name,
            "url": url,
            "description": description,
            "redirect_uri": redirect_uri,
            "visibility": visibility,
            "visible_roles": visible_roles or [],
            "scope": scope,
            "server": server,
            "client_id": client["client_id"],
            "scopes": scopes or [],
        }
        self._conn.execute(
            sql.SQL(
                "INSERT INTO {} (id, state_hash, user_id, request, secret_ciphertext, key_id, "
                "expires_at) VALUES (%s, %s, %s, %s, %s, %s, %s)"
            ).format(self._t("connector_oauth_states")),
            (
                oauth_id,
                state_hash(state),
                self._principal.user_id,
                Jsonb(request),
                self._broker.encrypt({"verifier": verifier, "client": client}),
                self._broker.key_id,
                expires_at,
            ),
        )
        authorization_url = OAuthClient.authorization_url(
            server,
            client_id=client["client_id"],
            redirect_uri=redirect_uri,
            state=state,
            code_challenge=challenge,
            scopes=scopes or [],
        )
        return {
            "oauth_id": str(oauth_id),
            "authorization_url": authorization_url,
            "expires_at": expires_at.isoformat(),
            "provider": server["issuer"],
        }

    # ---- 2. callback --------------------------------------------------------------------

    def complete_mcp_oauth(
        self, state: str, *, code: str | None = None, error: str | None = None
    ) -> dict[str, Any]:
        """Called by the OAuth callback endpoint. The state value proves which request
        (and which person) this sign-in belongs to."""
        row = self._oauth_row(state=state)
        if row["status"] != "pending":
            raise OAuthError("This sign-in was already used.", code="OAUTH_STATE_USED")
        if row["expires_at"] < dt.datetime.now(dt.UTC):
            self._set_oauth(row["id"], status="expired")
            raise OAuthError(
                "This sign-in request expired; start again.", code="OAUTH_STATE_EXPIRED"
            )
        if error or not code:
            self._set_oauth(row["id"], status="failed", error=Jsonb({"error": error or "no_code"}))
            raise OAuthError(
                f"The provider did not authorize the connection ({error}).", code="OAUTH_DENIED"
            )
        secret = self._broker.decrypt(row["secret_ciphertext"], row["key_id"])
        request = row["request"]
        try:
            tokens = self._oauth().exchange_code(
                request["server"],
                secret["client"],
                code=code,
                verifier=secret["verifier"],
                redirect_uri=request["redirect_uri"],
            )
        except OAuthError as exc:
            self._set_oauth(row["id"], status="failed", error=Jsonb(exc.to_dict()))
            raise
        self._set_oauth(
            row["id"],
            status="authorized",
            secret_ciphertext=self._broker.encrypt({**secret, "tokens": tokens}),
        )
        return {"oauth_id": str(row["id"]), "status": "authorized", "name": request["name"]}

    def oauth_status(self, oauth_id: Any) -> dict[str, Any]:
        row = self._oauth_row(oauth_id=oauth_id)
        return {
            "oauth_id": str(row["id"]),
            "status": row["status"],
            "name": row["request"]["name"],
            "error": row["error"],
        }

    # ---- 3. review and finish -----------------------------------------------------------

    def _authorized(self, oauth_id: Any) -> tuple[dict[str, Any], dict[str, Any], dict]:
        self._require_human("Connecting an app")
        row = self._oauth_row(oauth_id=oauth_id)
        if row["user_id"] != self._principal.user_id:
            raise ApprovalError(
                "Only the person who started this sign-in can finish it.", code="OAUTH_NOT_YOURS"
            )
        if row["status"] != "authorized":
            raise OAuthError(
                f"This sign-in is {row['status']}, not authorized yet.",
                code="OAUTH_NOT_AUTHORIZED",
                hint="Finish signing in at the provider first.",
            )
        request = row["request"]
        secret = self._broker.decrypt(row["secret_ciphertext"], row["key_id"])
        transport = {"type": "http", "url": request["url"], "auth": "oauth"}
        secrets = {
            "oauth": {
                "tokens": secret["tokens"],
                "client": secret["client"],
                "server": request["server"],
            }
        }
        return row, transport, secrets

    def review_mcp_oauth(self, oauth_id: Any) -> dict[str, Any]:
        _row, transport, secrets = self._authorized(oauth_id)
        tools = self._mcp_client(McpTransport.model_validate(transport), secrets).list_tools()
        return {
            "tools": [{**t, "default_policy": default_tool_policy(t)} for t in tools],
            "tools_hash": tools_hash(tools),
        }

    def finish_mcp_oauth(
        self,
        oauth_id: Any,
        *,
        expected_tools_hash: str,
        tool_policies: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        row, transport, secrets = self._authorized(oauth_id)
        request = row["request"]
        result = self.register_mcp_server(
            request["name"],
            description=request["description"],
            transport=transport,
            secrets=secrets,
            expected_tools_hash=expected_tools_hash,
            tool_policies=tool_policies,
            scope=request["scope"],
            visibility=request["visibility"],
            visible_roles=request["visible_roles"] or None,
        )
        self._set_oauth(row["id"], status="completed", completed_at=dt.datetime.now(dt.UTC))
        return result

    # ---- token refresh ------------------------------------------------------------------

    def _refresh_if_needed(self, connection_id: str, secrets: dict[str, Any]) -> dict[str, Any]:
        oauth = secrets.get("oauth")
        if not oauth or not needs_refresh(oauth["tokens"]):
            return secrets
        refresh_token = oauth["tokens"].get("refresh_token")
        if not refresh_token:
            raise OAuthError(
                "The app's sign-in expired and cannot be renewed automatically.",
                code="OAUTH_REAUTH_REQUIRED",
                hint="Someone with connections.manage needs to connect the app again.",
            )
        tokens = self._oauth().refresh(oauth["server"], oauth["client"], refresh_token)
        updated = {**secrets, "oauth": {**oauth, "tokens": tokens}}
        self._conn.execute(
            sql.SQL(
                "UPDATE {} SET secret_ciphertext = %s, key_id = %s, rotated_at = now() "
                "WHERE connection_id = %s"
            ).format(self._t("connector_credentials")),
            (self._broker.encrypt(updated), self._broker.key_id, connection_id),
        )
        return updated
