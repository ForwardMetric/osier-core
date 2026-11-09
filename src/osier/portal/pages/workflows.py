"""Workflows: what is defined, what ran, and trying one out.

Workflows are usually written in chat; this is where people see them, start or try them
with sample inputs (nothing is kept by a try), watch runs, and enable or disable them.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote

from starlette.responses import Response

from ...errors import RecordValidationError
from ..core import Ctx, NavItem


def _inputs(c: Ctx, spec: dict[str, Any]) -> dict[str, Any]:
    """Typed input values from the form, according to the workflow's declared inputs."""
    f = c.form
    assert f is not None
    values: dict[str, Any] = {}
    for name, definition in spec.items():
        raw = f.get(f"in_{name}")
        if raw == "":
            continue
        if definition.get("type") in ("object", "array"):
            try:
                values[name] = json.loads(raw)
            except json.JSONDecodeError:
                raise RecordValidationError(
                    f"Input {name!r} must be valid JSON.", field=name
                ) from None
        else:
            values[name] = raw
    return values


def register(portal: Any) -> None:
    portal.nav.append(NavItem("Workflows", "/workflows"))

    @portal.page("/workflows")
    def index(c: Ctx) -> Response:
        with c.engine() as engine:
            workflows = engine.workflows.list()
            runs = engine.workflows.runs(limit=10)
        return c.render(
            "workflows.html",
            workflows=workflows,
            runs=runs,
            can_manage="workflows.manage" in c.who().permissions,
        )

    # Fixed paths come before /workflows/{name}.
    @portal.page("/workflows/runs/{id}")
    def run_detail(c: Ctx) -> Response:
        with c.engine() as engine:
            run = engine.workflows.get_run(c.params["id"])
            events = engine.workflows.events(c.params["id"])
        return c.render(
            "workflow_run.html",
            run=run,
            events=events,
            steps=json.dumps(run["steps"], indent=2, default=str),
            finished=run["status"] in ("succeeded", "rejected", "failed", "cancelled"),
        )

    @portal.page("/workflows/runs/{id}/cancel", methods=("POST",))
    def cancel(c: Ctx) -> Response:
        with c.engine() as engine:
            engine.workflows.cancel(c.params["id"])
        return c.redirect(f"/workflows/runs/{c.params['id']}", "Run cancelled.")

    @portal.page("/workflows/{name}")
    def detail(c: Ctx) -> Response:
        name = c.params["name"]
        with c.engine() as engine:
            workflow = engine.workflows.get(name)
            versions = engine.workflows.versions(name)
            runs = [r for r in engine.workflows.runs(limit=100) if r["workflow"] == name][:10]
        return c.render(
            "workflow_detail.html",
            workflow=workflow,
            versions=versions,
            runs=runs,
            definition=json.dumps(workflow["definition"], indent=2),
            can_manage="workflows.manage" in c.who().permissions,
        )

    @portal.page("/workflows/{name}/start", methods=("POST",))
    def start(c: Ctx) -> Response:
        name = c.params["name"]
        with c.engine() as engine:
            spec = engine.workflows.get(name)["inputs"]
            run = engine.workflows.start(name, _inputs(c, spec))
        return c.redirect(f"/workflows/runs/{run['run_id']}", f"Started. Status: {run['status']}.")

    @portal.page("/workflows/{name}/test", methods=("POST",))
    def test(c: Ctx) -> Response:
        name = c.params["name"]
        with c.engine() as engine:
            spec = engine.workflows.get(name)["inputs"]
            result = engine.workflows.test(
                name, _inputs(c, spec), approve=c.form.get("approve") != "reject"
            )
        return c.render(
            "workflow_test.html",
            name=name,
            result=result,
            steps=json.dumps(result["steps"], indent=2, default=str),
        )

    @portal.page("/workflows/{name}/status", methods=("POST",))
    def status(c: Ctx) -> Response:
        name = c.params["name"]
        chosen = c.form.get("status") if c.form else ""
        with c.engine() as engine:
            engine.workflows.set_status(name, chosen)
        return c.redirect(f"/workflows/{quote(name, safe='')}", f"{name} is now {chosen}.")
