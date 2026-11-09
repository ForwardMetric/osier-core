"""Connected apps: email (SMTP) and MCP servers, who can use them, and what agents may do.

Credentials typed here (SMTP password, an MCP server's token) go straight into the
credential broker. For MCP servers, the server's tools are fetched and shown for review
first; the typed token waits in server memory (never in a page) until the person
confirms, and the tool list is pinned so later changes need another review.
"""

from __future__ import annotations

import secrets
from typing import Any

from starlette.responses import Response

from ...connectors.mcp_service import TOOL_POLICIES
from ...errors import NotFoundError, RecordValidationError
from ..core import Ctx, NavItem

MAX_PENDING = 5


def kind_of(connector_type: str) -> str:
    if connector_type == "smtp":
        return "Email (SMTP)"
    if connector_type == "dev_outbox":
        return "Email (development outbox)"
    return "MCP server" if connector_type.startswith("c__") else connector_type


def _visibility(c: Ctx) -> dict[str, Any]:
    f = c.form
    assert f is not None
    return {"visibility": f.get("visibility") or None, "visible_roles": f.getlist("visible_roles")}


def _roles(engine: Any) -> list[str]:
    return sorted(r["key"] for r in engine.records.list("role", limit=100))


def _require(c: Ctx, engine: Any) -> Any:
    engine.authz.require("connections.manage", "managing connected apps")
    return c.connectors(engine)


def register(portal: Any) -> None:
    portal.nav.append(NavItem("Connected apps", "/apps", ("connections.manage",)))

    @portal.page("/apps")
    def overview(c: Ctx) -> Response:
        with c.engine() as engine:
            service = _require(c, engine)
            rows = [
                {**x, "kind": kind_of(x["connector_type"])}
                for x in service.list_connections(manage=True)
            ]
            return c.render("apps.html", connections=rows, roles=_roles(engine))

    @portal.page("/apps/new/email", methods=("POST",))
    def add_email(c: Ctx) -> Response:
        f = c.form
        assert f is not None
        try:
            port = int(f.get("port") or 587)
        except ValueError:
            raise RecordValidationError("The port must be a number.", field="port") from None
        with c.engine() as engine:
            service = _require(c, engine)
            result = service.create_connection(
                "smtp",
                f.get("name"),
                config={
                    "host": f.get("host"),
                    "port": port,
                    "security": f.get("security") or "starttls",
                    "username": f.get("username") or None,
                    "from_address": f.get("from_address"),
                    "from_name": f.get("from_name") or None,
                },
                secrets={"password": f.raw("password")},
                approval=f.get("approval") or "required",
                is_default=f.checked("is_default"),
                **_visibility(c),
            )
        return c.redirect("/apps", f"Connected {result['name']}.")

    @portal.page("/apps/new/outbox", methods=("POST",))
    def add_outbox(c: Ctx) -> Response:
        f = c.form
        assert f is not None
        with c.engine() as engine:
            result = _require(c, engine).create_connection(
                "dev_outbox",
                f.get("name"),
                approval=f.get("approval") or "required",
                **_visibility(c),
            )
        return c.redirect(
            "/apps", f"Added {result['name']}. Emails are written to a local log, not sent."
        )

    @portal.page("/apps/new/mcp", methods=("POST",))
    def inspect_mcp(c: Ctx) -> Response:
        f = c.form
        assert f is not None
        header, value = f.get("header_name") or "Authorization", f.raw("header_value").strip()
        transport = {
            "type": "http",
            "url": f.get("url"),
            "auth": "headers" if value else "none",
            "secret_headers": [header] if value else [],
        }
        secrets_in = {header: value} if value else {}
        with c.engine() as engine:
            service = _require(c, engine)
            inspected = service.inspect_mcp_server(transport, secrets_in)
            roles = _roles(engine)
        assert c.session is not None
        while len(c.session.pending) >= MAX_PENDING:  # keep a few reviews open at most
            c.session.pending.pop(next(iter(c.session.pending)))
        token = secrets.token_urlsafe(16)
        c.session.pending[token] = {
            "name": f.get("name"),
            "description": f.get("description"),
            "transport": transport,
            "secrets": secrets_in,
            "tools": inspected["tools"],
            "tools_hash": inspected["tools_hash"],
            **_visibility(c),
        }
        pending = c.session.pending[token]
        return c.render(
            "apps_review.html", token=token, pending=pending, roles=roles, policies=TOOL_POLICIES
        )

    @portal.page("/apps/new/mcp/register", methods=("POST",))
    def register_mcp(c: Ctx) -> Response:
        f = c.form
        assert f is not None and c.session is not None
        pending = c.session.pending.get(f.get("token"))
        if pending is None:
            raise RecordValidationError(
                "That review expired.",
                code="REVIEW_EXPIRED",
                hint="Start again from Connected apps; nothing was connected.",
            )
        policies = {}
        for index, tool in enumerate(pending["tools"]):
            choice = f.get(f"policy_{index}")
            if choice:
                policies[tool["name"]] = choice
        with c.engine() as engine:
            result = _require(c, engine).register_mcp_server(
                pending["name"],
                description=pending["description"],
                transport=pending["transport"],
                secrets=pending["secrets"],
                expected_tools_hash=pending["tools_hash"],
                tool_policies=policies,
                visibility=pending["visibility"],
                visible_roles=pending["visible_roles"],
            )
        c.session.pending.pop(f.get("token"), None)
        return c.redirect("/apps", f"Connected {result['name']}. Its tools are pinned as reviewed.")

    @portal.page("/apps/{name}")
    def detail(c: Ctx) -> Response:
        name = c.params["name"]
        with c.engine() as engine:
            service = _require(c, engine)
            matches = [x for x in service.list_connections(manage=True) if x["name"] == name]
            if not matches:
                raise NotFoundError(
                    f"No connected app named {name!r}.", code="CONNECTION_NOT_FOUND"
                )
            connection = {**matches[0], "kind": kind_of(matches[0]["connector_type"])}
            server = (
                service.describe_mcp_server(name)
                if connection["connector_type"].startswith("c__")
                else None
            )
            return c.render(
                "apps_detail.html",
                connection=connection,
                server=server,
                roles=_roles(engine),
                policies=TOOL_POLICIES,
            )

    @portal.page("/apps/{name}/policy", methods=("POST",))
    def policy(c: Ctx) -> Response:
        f = c.form
        assert f is not None
        name = c.params["name"]
        with c.engine() as engine:
            service = _require(c, engine)
            current = {t["name"]: t["policy"] for t in service.describe_mcp_server(name)["tools"]}
            index = 0
            while (tool := f.get(f"tool_{index}")) != "":
                chosen = f.get(f"policy_{index}")
                if chosen and chosen != current.get(tool):
                    service.set_tool_policy(name, tool, chosen)
                index += 1
        return c.redirect(f"/apps/{_q(name)}", "Tool policies saved.")

    @portal.page("/apps/{name}/visibility", methods=("POST",))
    def visibility(c: Ctx) -> Response:
        name = c.params["name"]
        v = _visibility(c)
        with c.engine() as engine:
            _require(c, engine).set_visibility(
                name, v["visibility"] or "organization", v["visible_roles"]
            )
        return c.redirect(f"/apps/{_q(name)}", "Sharing updated.")

    @portal.page("/apps/{name}/review", methods=("POST",))
    def review(c: Ctx) -> Response:
        name = c.params["name"]
        with c.engine() as engine:
            result = _require(c, engine).review_mcp_tools(name)
        return c.render("apps_changes.html", name=name, review=result)

    @portal.page("/apps/{name}/accept", methods=("POST",))
    def accept(c: Ctx) -> Response:
        f = c.form
        assert f is not None
        name = c.params["name"]
        with c.engine() as engine:
            _require(c, engine).accept_mcp_tools(name, f.get("tools_hash"))
        return c.redirect(f"/apps/{_q(name)}", "Accepted the server's current tools.")

    @portal.page("/apps/{name}/remove", methods=("GET", "POST"))
    def remove(c: Ctx) -> Response:
        name = c.params["name"]
        with c.engine() as engine:
            service = _require(c, engine)
            if c.form is None:
                if name not in {x["name"] for x in service.list_connections(manage=True)}:
                    raise NotFoundError(
                        f"No connected app named {name!r}.", code="CONNECTION_NOT_FOUND"
                    )
                return c.render("apps_remove.html", name=name)
            service.revoke_connection(name)
        return c.redirect("/apps", f"Removed {name} and its stored credentials.")


def _q(name: str) -> str:
    from urllib.parse import quote

    return quote(name, safe="")
