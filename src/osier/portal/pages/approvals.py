"""The approvals inbox: schema changes, agent actions, and workflow approvals.

Only people decide here (the engine refuses agents). Schema approvals carry the hash of
the proposal that was on screen, so a proposal that changed in the meantime is refused
instead of being approved unseen.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from starlette.responses import Response

from ...errors import OsierError
from ..core import Ctx, NavItem


def collect(c: Ctx, engine: Any, permissions: set[str]) -> dict[str, list[dict[str, Any]]]:
    """Everything waiting for this person to decide."""
    schema = [
        item
        for item in engine.schema.pending()
        if ("packages.manage" if item["source"] == "package" else "schema.manage") in permissions
    ]
    actions = []
    for call in c.connectors(engine).decidable_calls():
        arguments = call.get("arguments") or {}
        text = arguments.get("body_text") or json.dumps(arguments, indent=2, default=str)
        actions.append({**call, "details": text[:4000]})
    return {
        "schema": schema,
        "actions": actions,
        "workflows": engine.workflows.awaiting_decision(),
    }


def count(c: Ctx) -> int | None:
    try:
        with c.engine() as engine:
            found = collect(c, engine, c.who().permissions)
        return sum(len(v) for v in found.values())
    except OsierError:
        return None


def register(portal: Any) -> None:
    portal.nav.append(NavItem("Approvals", "/approvals"))

    @portal.page("/approvals")
    def inbox(c: Ctx) -> Response:
        with c.engine() as engine:
            found = collect(c, engine, c.who().permissions)
        return c.render("approvals.html", **found)

    @portal.page("/approvals/schema/{id}/{decision}", methods=("POST",))
    def schema_decision(c: Ctx) -> Response:
        f = c.form
        assert f is not None
        change_id = c.params["id"]
        with c.engine() as engine:
            if c.params["decision"] == "reject":
                engine.schema.reject(change_id)
                return c.redirect("/approvals", "Rejected. Nothing was changed.")
            stored = engine.schema.get(change_id)
            service = engine.packages if stored["source"] == "package" else engine.schema
            service.apply(uuid.UUID(change_id), diff_hash=f.get("diff_hash"))
        return c.redirect("/approvals", "Approved and applied.")

    @portal.page("/approvals/action/{id}/{decision}", methods=("POST",))
    def action_decision(c: Ctx) -> Response:
        with c.engine() as engine:
            service = c.connectors(engine)
            if c.params["decision"] == "reject":
                service.reject(c.params["id"])
                return c.redirect("/approvals", "Rejected. Nothing was sent or run.")
            result = service.approve(c.params["id"])
        kind = "ok" if result["status"] == "succeeded" else "err"
        return c.redirect("/approvals", f"Approved. The action {result['status']}.", kind)

    @portal.page("/approvals/workflow/{id}/{decision}", methods=("POST",))
    def workflow_decision(c: Ctx) -> Response:
        f = c.form
        assert f is not None
        approve = c.params["decision"] == "approve"
        with c.engine() as engine:
            result = engine.workflows.decide(
                c.params["id"], approve=approve, comment=f.get("comment") or None
            )
        return c.redirect(
            "/approvals",
            f"{'Approved' if approve else 'Rejected'}. The workflow run is now {result['status']}.",
        )
