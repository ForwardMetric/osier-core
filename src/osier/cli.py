"""osier command line: local setup, the MCP server, approvals, and connections.

Everything here runs as the configured *person* (not as an agent): approvals and
credentials are entered here, never in the chat.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import shlex
import shutil
import sys
import uuid
from typing import Any

from .bootstrap import bootstrap_tenant
from .connectors import ConnectorService
from .devconfig import DevConfig, config_path, default_config_dir, write_key_file
from .errors import OsierError
from .session import Session

DEFAULT_DB = "postgresql://osier:osier@localhost:5432/osier_test"


def _print(data: Any) -> None:
    print(json.dumps(data, indent=2, default=str))


def _admin(config: DevConfig) -> Session:
    return config.session(config.admin())


def _confirm(prompt: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    return input(f"{prompt} [y/N] ").strip().lower() in ("y", "yes")


# ---- dev init -----------------------------------------------------------------------


def cmd_dev_init(args: argparse.Namespace) -> None:
    path = config_path()
    if path.exists() and not args.force:
        raise OsierError(
            f"{path} already exists.",
            code="ALREADY_CONFIGURED",
            hint="Use --force to create a new tenant and overwrite the configuration.",
        )
    import psycopg

    with psycopg.connect(args.database_url, autocommit=True) as conn:
        boot = bootstrap_tenant(
            conn,
            organization_name=args.org,
            admin_email=args.email,
            admin_display_name=args.name,
        )
        key_file = default_config_dir() / "secret.key"
        write_key_file(key_file)
        config = DevConfig(
            database_url=args.database_url,
            tenant_id=str(boot.tenant.tenant_id),
            tenant_schema=boot.tenant.schema,
            organization_id=str(boot.organization_id),
            admin_user_id=str(boot.admin_user_id),
            agent_user_id=str(uuid.uuid5(boot.admin_user_id, "mcp-agent")),
            key_file=str(key_file),
            allow_private_network=args.allow_private_network,
        )
        session = config.session(config.admin(), conn)
        packages = [p for p in (args.packages or "").split(",") if p]
        if packages:
            proposal = session.packages.propose_install(packages)
            session.packages.apply(proposal.id, diff_hash=proposal.diff_hash)
        saved = config.save()
    print(f"Created tenant {boot.tenant.schema} for {args.org}.")
    print(f"Installed packages: {', '.join(['core', 'integrations', *packages])}")
    print(f"Configuration: {saved}")
    print(f"Secret key:    {key_file}  (keep it; credentials are encrypted with it)")
    print()
    _print_client_setup()


def _print_client_setup() -> None:
    exe = shutil.which("osier") or f"{sys.executable} -m osier"
    command, *rest = exe.split()
    args = [*rest, "mcp"]
    env = {"OSIER_CONFIG": str(config_path())}
    print('Claude Desktop: add this to claude_desktop_config.json under "mcpServers":')
    _print({"osier": {"command": command, "args": args, "env": env}})
    print()
    print("Claude Code:")
    print(f"  claude mcp add osier --env OSIER_CONFIG={config_path()} -- {exe} mcp")


# ---- status / mcp -----------------------------------------------------------------------


def cmd_status(args: argparse.Namespace) -> None:
    config = DevConfig.load()
    session = _admin(config)
    _print(
        {
            "config": str(config_path()),
            "tenant_schema": config.tenant_schema,
            "packages": {p["package"]: p["status"] for p in session.packages.available()},
            "objects": [o["api_name"] for o in session.describe_schema()["objects"]],
            "pending_approvals": len(session.schema.pending())
            + len(ConnectorService(session, config.broker()).pending_calls()),
        }
    )


def cmd_mcp(args: argparse.Namespace) -> None:
    from .mcp_server import run

    config = DevConfig.load()
    run(config, config.find_person(args.as_user) if args.as_user else None)


def cmd_dev_add_user(args: argparse.Namespace) -> None:
    """Add a person with roles (for testing permissions locally)."""
    config = DevConfig.load()
    session = _admin(config)
    user = session.records.create(
        "app_user",
        {
            "display_name": args.name,
            "email": args.email,
            "status": "active",
            "organization": config.organization_id,
        },
    )
    for key in args.role or []:
        matches = session.records.list("role", where={"key": key}, limit=1)
        if not matches:
            raise OsierError(f"No role {key!r}.", code="ROLE_NOT_FOUND")
        session.records.create("user_role", {"app_user": user["id"], "role": matches[0]["id"]})
    print(f"Added {args.name} <{args.email}> with roles: {', '.join(args.role or []) or 'none'}")
    print("Run Claude's osier server as this person with:")
    print(f"  osier mcp --as {args.email}")


def cmd_dev_oauth_redirect(args: argparse.Namespace) -> None:
    config = DevConfig.load()
    value = args.value
    if value not in ("127.0.0.1", "localhost") and not value.startswith("https://"):
        raise OsierError("Use 127.0.0.1, localhost, or an https tunnel URL.")
    config.oauth_redirect = value
    config.save()
    print(f"OAuth callback address: {config.redirect_uri()}")
    if value.startswith("https://"):
        print(f"Point the tunnel at http://127.0.0.1:{config.oauth_callback_port}")


def cmd_connect_finish(args: argparse.Namespace) -> None:
    config = DevConfig.load()
    svc = _connectors(config)
    review = svc.review_mcp_oauth(args.oauth_id)
    _print_tools(review["tools"], "default_policy")
    if _confirm("Connect with these tools and policies?", args.yes):
        _print(svc.finish_mcp_oauth(args.oauth_id, expected_tools_hash=review["tools_hash"]))


def cmd_client_setup(args: argparse.Namespace) -> None:
    _print_client_setup()


# ---- approvals ---------------------------------------------------------------------------


def cmd_approvals_list(args: argparse.Namespace) -> None:
    config = DevConfig.load()
    session = _admin(config)
    changes = session.schema.pending()
    calls = ConnectorService(session, config.broker()).pending_calls()
    if not changes and not calls:
        print("Nothing is waiting for approval.")
        return
    for c in changes:
        print(
            f"[schema] {c['change_set_id']}  ({c['source']}, proposed by {c['proposed_by_type']})"
        )
        for line in c["summary"]:
            print(f"    {line}")
        for b in c["blockers"]:
            print(f"    BLOCKER: {b['message']}")
    for c in calls:
        print(
            f"[action] {c['call_id']}  ({c['capability'] or c['connector_type']}, "
            f"requested by {c['requested_by_type']})"
        )
        print(f"    {c['summary']}")


def _find(session: Session, config: DevConfig, item_id: str) -> tuple[str, dict[str, Any]]:
    try:
        return "schema", session.schema.get(item_id)
    except (OsierError, ValueError):
        pass
    try:
        connectors = ConnectorService(session, config.broker())
        call = connectors.get_call(item_id)
        call["arguments"] = next(
            (c["arguments"] for c in connectors.pending_calls() if c["call_id"] == call["call_id"]),
            None,
        )
        return "action", call
    except OsierError:
        raise OsierError(f"Nothing to approve with id {item_id}.", code="NOT_FOUND") from None


def cmd_approvals_show(args: argparse.Namespace) -> None:
    config = DevConfig.load()
    kind, item = _find(_admin(config), config, args.id)
    item.pop("diff_hash", None)
    _print({"kind": kind, **item})


def cmd_approvals_approve(args: argparse.Namespace) -> None:
    config = DevConfig.load()
    session = _admin(config)
    kind, item = _find(session, config, args.id)
    if kind == "schema":
        print("\n".join(item["summary"]))
        for ddl in item["ddl"]:
            print(f"  SQL: {ddl}")
        if item["blockers"]:
            _print(item["blockers"])
            raise OsierError(
                "This change set has blockers and cannot be applied.", code="CHANGE_SET_BLOCKED"
            )
        if not _confirm("Apply this change?", args.yes):
            print("Not applied.")
            return
        if item["source"] == "package":
            result = session.packages.apply(uuid.UUID(args.id), diff_hash=item["diff_hash"])
        else:
            result = session.schema.apply(uuid.UUID(args.id), diff_hash=item["diff_hash"])
        _print(result.to_dict())
    else:
        print(item["summary"])
        arguments = item.get("arguments") or {}
        if "body_text" in arguments:
            print(f"  Body:\n{arguments['body_text']}")
        elif arguments:
            print("  Arguments:\n" + json.dumps(arguments, indent=2, default=str))
        if not _confirm("Run this action now?", args.yes):
            print("Not run.")
            return
        _print(_connectors(config).approve(args.id))


def cmd_approvals_reject(args: argparse.Namespace) -> None:
    config = DevConfig.load()
    session = _admin(config)
    kind, _ = _find(session, config, args.id)
    if kind == "schema":
        _print(session.schema.reject(args.id))
    else:
        _print(ConnectorService(session, config.broker()).reject(args.id))


# ---- connections --------------------------------------------------------------------------


def _connectors(config: DevConfig) -> ConnectorService:
    return ConnectorService(
        _admin(config),
        config.broker(),
        allow_private_network=config.allow_private_network,
        allow_local_processes=config.allow_local_processes,
    )


def cmd_connect_smtp(args: argparse.Namespace) -> None:
    config = DevConfig.load()
    password = os.environ.get("OSIER_SMTP_PASSWORD") or getpass.getpass(
        f"SMTP password for {args.username or args.from_address}: "
    )
    connection = _connectors(config).create_connection(
        "smtp",
        args.name,
        purpose=args.purpose,
        scope=args.scope,
        is_default=args.default,
        approval="not_required" if args.auto_approve else "required",
        config={
            "host": args.host,
            "port": args.port,
            "security": args.security,
            "username": args.username or args.from_address,
            "from_address": args.from_address,
            "from_name": args.from_name,
        },
        secrets={"password": password},
    )
    print("Connected and verified.")
    _print(connection)


def cmd_connect_outbox(args: argparse.Namespace) -> None:
    config = DevConfig.load()
    _print(
        _connectors(config).create_connection(
            "dev_outbox",
            args.name,
            purpose="transactional",
            is_default=args.default,
            approval="not_required" if args.auto_approve else "required",
            config={"from_address": args.from_address},
        )
    )


def _ask_secret(name: str) -> str:
    env_name = "OSIER_SECRET_" + "".join(c if c.isalnum() else "_" for c in name.upper())
    return os.environ.get(env_name) or getpass.getpass(f"Value for {name}: ")


def _print_tools(tools: list[dict[str, Any]], policy_key: str) -> None:
    for t in tools:
        hints = ", ".join(k for k, v in t.get("annotations", {}).items() if v is True) or "-"
        print(f"  {t['name']:<32} {t[policy_key]:<9} hints: {hints}")
        print(f"      {t['description'][:200]}")


def _connect_mcp_oauth(args: argparse.Namespace, config: DevConfig) -> None:
    import threading
    import webbrowser

    from .connectors.oauth_callback import CallbackServer

    svc = _connectors(config)
    done = threading.Event()
    outcome: dict[str, Any] = {}

    def on_callback(state: str, code: str | None, error: str | None) -> tuple[bool, str]:
        ok, message = config.complete_oauth(state, code, error)
        outcome.update(ok=ok, message=message)
        done.set()
        return ok, message

    server = CallbackServer(config.oauth_callback_port, on_callback)
    server.start()
    try:
        started = svc.start_mcp_oauth(
            args.name,
            url=args.url,
            description=args.description,
            redirect_uri=config.redirect_uri(),
            visibility=args.visibility,
            visible_roles=args.role,
            client_id=args.client_id,
            client_secret=_ask_secret("client secret") if args.client_secret else None,
        )
        print(f"Sign in to {started['provider']} in your browser:")
        print(f"  {started['authorization_url']}")
        webbrowser.open(started["authorization_url"])
        if not done.wait(timeout=600):
            raise OsierError("Timed out waiting for the sign-in to finish.", code="OAUTH_TIMEOUT")
    finally:
        server.stop()
    if not outcome.get("ok"):
        raise OsierError(outcome.get("message", "Sign-in failed."), code="OAUTH_FAILED")
    review = svc.review_mcp_oauth(started["oauth_id"])
    print(
        f"{len(review['tools'])} tools offered (auto = runs immediately, approval = you approve):"
    )
    _print_tools(review["tools"], "default_policy")
    if not _confirm("Connect with these tools and policies?", args.yes):
        print("Not connected.")
        return
    connection = svc.finish_mcp_oauth(started["oauth_id"], expected_tools_hash=review["tools_hash"])
    print(f"Connected {connection['name']} (visible to: {connection['visibility']}).")
    if connection.get("warning"):
        print(f"Note: {connection['warning']}")


def cmd_connect_mcp(args: argparse.Namespace) -> None:
    config = DevConfig.load()
    if args.oauth:
        if not args.url:
            raise OsierError("--oauth needs --url (the server's MCP endpoint).")
        _connect_mcp_oauth(args, config)
        return
    if bool(args.url) == bool(args.command):
        raise OsierError("Give either --url (remote server) or --command (local server).")
    transport: dict[str, Any] = (
        {"type": "http", "url": args.url, "secret_headers": args.secret_header or []}
        if args.url
        else {
            "type": "stdio",
            "command": args.command,
            "args": args.arg or [],
            "secret_env": args.secret_env or [],
        }
    )
    secrets = {
        name: _ask_secret(name) for name in [*(args.secret_header or []), *(args.secret_env or [])]
    }
    svc = _connectors(config)
    inspected = svc.inspect_mcp_server(transport, secrets)
    print(
        f"{len(inspected['tools'])} tools offered by this server "
        "(auto = runs immediately, approval = you approve each call):"
    )
    _print_tools(inspected["tools"], "default_policy")
    print("Tool descriptions come from the server; read them before trusting it.")
    if not _confirm("Connect this server with these tools and policies?", args.yes):
        print("Not connected.")
        return
    connection = svc.register_mcp_server(
        args.name,
        description=args.description,
        transport=transport,
        secrets=secrets,
        expected_tools_hash=inspected["tools_hash"],
        visibility=args.visibility,
        visible_roles=args.role,
    )
    print(f"Connected {connection['name']} ({connection['connector_type']}).")
    print(
        f"Change a tool's policy with: osier connections policy {shlex.quote(args.name)} "
        "<tool> auto|approval|blocked"
    )


def cmd_connections_tools(args: argparse.Namespace) -> None:
    listed = _connectors(DevConfig.load()).list_tools(args.name)
    print(f"{listed['server']} ({listed['status']}):")
    _print_tools(listed["tools"], "policy")


def cmd_connections_policy(args: argparse.Namespace) -> None:
    _connectors(DevConfig.load()).set_tool_policy(args.name, args.tool, args.policy)
    print(f"{args.tool}: {args.policy}")


def cmd_connections_review(args: argparse.Namespace) -> None:
    svc = _connectors(DevConfig.load())
    review = svc.review_mcp_tools(args.name)
    if not (review["added"] or review["removed"] or review["changed"]):
        print("No changes: the server's tools match what you approved.")
        return
    for key in ("added", "removed", "changed"):
        if review[key]:
            print(f"{key}: {', '.join(review[key])}")
    current = {t["name"]: t for t in review["tools"]}
    for name in review["added"] + review["changed"]:
        print(f"  {name}: {current[name]['description'][:300]}")
    if _confirm("Accept the server's current tools?", args.yes):
        svc.accept_mcp_tools(args.name, review["tools_hash"])
        print("Accepted. Changed tools went back to their default policy.")


def _visibility_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--visibility",
        choices=["private", "roles", "organization"],
        help="who may have agents use it (default: organization, or private for personal accounts)",
    )
    parser.add_argument("--role", action="append", help="role key, with --visibility roles")


def cmd_connections_share(args: argparse.Namespace) -> None:
    _print(_connectors(DevConfig.load()).set_visibility(args.name, args.visibility, args.role))


def cmd_connections_list(args: argparse.Namespace) -> None:
    _print(_connectors(DevConfig.load()).list_connections())


def cmd_outbox(args: argparse.Namespace) -> None:
    """Show recent connector calls, including messages captured by the dev outbox."""
    config = DevConfig.load()
    from psycopg import sql
    from psycopg.rows import dict_row

    session = _admin(config)
    with session.conn.cursor(row_factory=dict_row) as cur:
        rows = cur.execute(
            sql.SQL(
                "SELECT id, status, summary, arguments, result, created_at FROM {} "
                "ORDER BY created_at DESC LIMIT %s"
            ).format(sql.Identifier(config.tenant_schema, "connector_calls")),
            (args.limit,),
        ).fetchall()
    for r in rows:
        print(f"{r['created_at']:%Y-%m-%d %H:%M}  {r['status']:<18} {r['summary']}  ({r['id']})")
        if args.verbose:
            print(f"    {r['arguments'].get('body_text', '')[:500]}")


# ---- parser ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="osier", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    dev = sub.add_parser("dev", help="local development setup").add_subparsers(
        dest="dev_command", required=True
    )
    init = dev.add_parser("init", help="create a local tenant and configuration")
    init.add_argument("--database-url", default=os.environ.get("OSIER_DATABASE_URL", DEFAULT_DB))
    init.add_argument("--org", required=True, help="your organization's name")
    init.add_argument("--email", required=True, help="your email (the first administrator)")
    init.add_argument("--name", required=True, help="your display name")
    init.add_argument("--packages", default="crm", help="comma-separated, e.g. crm,case_management")
    init.add_argument(
        "--allow-private-network",
        action="store_true",
        help="allow connectors to reach local/private hosts (local testing only)",
    )
    init.add_argument("--force", action="store_true")
    init.set_defaults(func=cmd_dev_init)
    add_user = dev.add_parser("add-user", help="add a person with roles (local testing)")
    add_user.add_argument("--email", required=True)
    add_user.add_argument("--name", required=True)
    add_user.add_argument("--role", action="append", help="role key, e.g. developer, member")
    add_user.set_defaults(func=cmd_dev_add_user)
    redirect = dev.add_parser(
        "oauth-redirect", help="how the OAuth callback address is written (provider quirks)"
    )
    redirect.add_argument("value", help="127.0.0.1 (default), localhost, or https://<tunnel>")
    redirect.set_defaults(func=cmd_dev_oauth_redirect)

    sub.add_parser("status", help="show tenant, packages, and pending approvals").set_defaults(
        func=cmd_status
    )
    mcp_cmd = sub.add_parser("mcp", help="run the MCP server (stdio)")
    mcp_cmd.add_argument(
        "--as",
        dest="as_user",
        metavar="EMAIL",
        help="act for this person (default: the administrator)",
    )
    mcp_cmd.set_defaults(func=cmd_mcp)
    sub.add_parser("client-setup", help="print Claude Desktop / Claude Code setup").set_defaults(
        func=cmd_client_setup
    )

    approvals = sub.add_parser("approvals", help="review and approve agent proposals")
    asub = approvals.add_subparsers(dest="approvals_command", required=True)
    asub.add_parser("list").set_defaults(func=cmd_approvals_list)
    for name, func in (
        ("show", cmd_approvals_show),
        ("approve", cmd_approvals_approve),
        ("reject", cmd_approvals_reject),
    ):
        p = asub.add_parser(name)
        p.add_argument("id")
        if name == "approve":
            p.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation")
        p.set_defaults(func=func)

    connect = sub.add_parser("connect", help="connect an external account").add_subparsers(
        dest="connect_command", required=True
    )
    smtp = connect.add_parser("smtp", help="email through an SMTP server")
    smtp.add_argument("--name", required=True)
    smtp.add_argument("--host", required=True, help="e.g. smtp.gmail.com")
    smtp.add_argument("--port", type=int, default=587)
    smtp.add_argument("--security", choices=["starttls", "ssl", "none"], default="starttls")
    smtp.add_argument("--username")
    smtp.add_argument("--from-address", required=True)
    smtp.add_argument("--from-name")
    smtp.add_argument("--purpose", choices=["transactional", "marketing"], default="transactional")
    smtp.add_argument("--scope", choices=["organization", "user"], default="organization")
    smtp.add_argument("--default", action="store_true")
    smtp.add_argument(
        "--auto-approve",
        action="store_true",
        help="send without asking for approval (use with care)",
    )
    smtp.set_defaults(func=cmd_connect_smtp)
    outbox = connect.add_parser("dev-outbox", help="fake email connection that sends nothing")
    outbox.add_argument("--name", default="Dev outbox")
    outbox.add_argument("--from-address", default="agent@example.test")
    outbox.add_argument("--default", action="store_true")
    outbox.add_argument("--auto-approve", action="store_true")
    outbox.set_defaults(func=cmd_connect_outbox)
    finish_p = connect.add_parser("finish", help="finish an app sign-in started in chat")
    finish_p.add_argument("oauth_id")
    finish_p.add_argument("-y", "--yes", action="store_true")
    finish_p.set_defaults(func=cmd_connect_finish)
    mcp_p = connect.add_parser("mcp", help="connect an app through its MCP server")
    mcp_p.add_argument("--name", required=True, help="e.g. Notion")
    mcp_p.add_argument(
        "--description", required=True, help="what the app is used for (at least 4 words)"
    )
    mcp_p.add_argument("--url", help="remote MCP endpoint (https)")
    mcp_p.add_argument(
        "--secret-header", action="append", help="header carrying a secret, e.g. Authorization"
    )
    mcp_p.add_argument("--command", help="local MCP server command, e.g. npx")
    mcp_p.add_argument("--arg", action="append", help="argument for --command (repeatable)")
    mcp_p.add_argument(
        "--secret-env", action="append", help="environment variable with a secret value"
    )
    mcp_p.add_argument(
        "--oauth", action="store_true", help="sign in with OAuth in your browser (e.g. Notion)"
    )
    mcp_p.add_argument(
        "--client-id", help="OAuth client id, if the server has no automatic registration"
    )
    mcp_p.add_argument(
        "--client-secret", action="store_true", help="ask for an OAuth client secret"
    )
    _visibility_args(mcp_p)
    mcp_p.add_argument("-y", "--yes", action="store_true")
    mcp_p.set_defaults(func=cmd_connect_mcp)

    conns = sub.add_parser("connections", help="list connections").add_subparsers(
        dest="connections_command", required=True
    )
    conns.add_parser("list").set_defaults(func=cmd_connections_list)
    share_p = conns.add_parser("share", help="change who may use a connection")
    share_p.add_argument("name")
    share_p.add_argument("visibility", choices=["private", "roles", "organization"])
    share_p.add_argument("--role", action="append")
    share_p.set_defaults(func=cmd_connections_share)
    tools_p = conns.add_parser("tools", help="tools of an MCP server connection")
    tools_p.add_argument("name")
    tools_p.set_defaults(func=cmd_connections_tools)
    policy_p = conns.add_parser("policy", help="set a tool's policy")
    policy_p.add_argument("name")
    policy_p.add_argument("tool")
    policy_p.add_argument("policy", choices=["auto", "approval", "blocked"])
    policy_p.set_defaults(func=cmd_connections_policy)
    review_p = conns.add_parser("review", help="review changed tools of an MCP server")
    review_p.add_argument("name")
    review_p.add_argument("-y", "--yes", action="store_true")
    review_p.set_defaults(func=cmd_connections_review)

    out = sub.add_parser("outbox", help="recent emails and connector actions")
    out.add_argument("--limit", type=int, default=20)
    out.add_argument("-v", "--verbose", action="store_true")
    out.set_defaults(func=cmd_outbox)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.func is not cmd_dev_init and config_path().exists():
            DevConfig.load().ensure_current()
        args.func(args)
    except OsierError as exc:
        print(f"error: {exc.message}", file=sys.stderr)
        if exc.hint:
            print(f"hint: {exc.hint}", file=sys.stderr)
        if exc.details:
            print(json.dumps(exc.details, indent=2, default=str), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
