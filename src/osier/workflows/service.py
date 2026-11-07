"""Workflows: definitions (metadata), and runs executed by LangGraph.

- Definitions are tenant metadata, versioned like skills. Adding or changing one
  needs ``workflows.manage`` and a person's confirmation.
- A run executes with the permissions of whoever started it (a person, or an agent
  acting for a person). A workflow never grants more access than its starter has;
  every record step goes through ``RecordService`` as usual.
- Approval steps pause the run (LangGraph ``interrupt``). Only a person may decide,
  never an agent; the approver needs one of the step's roles, or
  ``workflows.manage`` when the step names none.
- Runs resume from checkpoints stored in the tenant schema
  (``TenantCheckpointSaver``), always with the definition version they started on.
"""

from __future__ import annotations

import dataclasses
import json
import uuid
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pydantic import TypeAdapter
from pydantic import ValidationError as PydanticValidationError

from ..context import ActorType, Principal
from ..errors import ApprovalError, NotFoundError, OsierError, RecordValidationError
from .checkpoint import TenantCheckpointSaver
from .spec import (
    END as END_TARGET,
)
from .spec import (
    AgentStep,
    Approval,
    Condition,
    CreateRecord,
    Definition,
    Finish,
    GetRecord,
    QueryRecords,
    ScheduleTrigger,
    UpdateRecord,
    WorkflowError,
    check,
    next_run,
    parse_definition,
    resolve,
    validate_definition,
)

_INPUT_TYPES: dict[str, Any] = {
    "string": str,
    "number": float,
    "integer": int,
    "boolean": bool,
    "object": dict,
    "array": list,
}
FINISHED = ("succeeded", "rejected", "failed", "cancelled")


def _merge(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    return {**a, **b}


class RunState(TypedDict):
    inputs: dict[str, Any]
    steps: Annotated[dict[str, Any], _merge]


class _StepFailed(Exception):
    def __init__(self, step_id: str, error: OsierError) -> None:
        super().__init__(error.message)
        self.step_id, self.error = step_id, error


def _plain(value: Any) -> Any:
    """Results are kept as plain JSON so checkpoints and run records stay simple."""
    return json.loads(json.dumps(value, default=str))


class WorkflowService:
    def __init__(self, session: Any) -> None:
        self._session = session
        self._conn = session.conn
        self._schema = session.tenant.schema
        self._principal = session.principal
        self._authz = session.authz

    def _t(self, name: str) -> sql.Identifier:
        return sql.Identifier(self._schema, name)

    def _person(self) -> uuid.UUID | None:
        p = self._principal
        return p.on_behalf_of if p.actor_type is ActorType.AGENT else p.user_id

    def _is_manager(self) -> bool:
        return self._principal.actor_type is ActorType.SYSTEM or self._authz.has("workflows.manage")

    def _require_manager(self, doing: str) -> None:
        self._authz.require("workflows.manage", doing)
        if not (self._principal.is_human or self._principal.actor_type is ActorType.SYSTEM):
            raise ApprovalError(
                f"{doing.capitalize()} must be confirmed by a person.",
                code="APPROVAL_REQUIRES_HUMAN",
                hint="Use the chat tools, which ask the user to confirm.",
            )

    # ---- definitions ------------------------------------------------------------------

    def _row(self, name: str) -> dict[str, Any]:
        with self._conn.cursor(row_factory=dict_row) as cur:
            row = cur.execute(
                sql.SQL("SELECT * FROM {} WHERE name = %s").format(self._t("meta_workflows")),
                (name,),
            ).fetchone()
        if row is None or row["status"] == "retired":
            raise NotFoundError(
                f"No workflow named {name!r}.",
                code="WORKFLOW_NOT_FOUND",
                expected=[w["name"] for w in self.list()],
            )
        return row

    @staticmethod
    def _summary(row: dict[str, Any]) -> dict[str, Any]:
        d = row["definition"]
        return {
            "name": row["name"],
            "description": row["description"],
            "version": row["version"],
            "status": row["status"],
            "inputs": d.get("inputs", {}),
            "triggers": d.get("triggers", []),
            "steps": [{"id": s["id"], "type": s["type"]} for s in d["steps"]],
        }

    def list(self) -> list[dict[str, Any]]:
        with self._conn.cursor(row_factory=dict_row) as cur:
            rows = cur.execute(
                sql.SQL("SELECT * FROM {} WHERE status <> 'retired' ORDER BY name").format(
                    self._t("meta_workflows")
                )
            ).fetchall()
        manager = self._is_manager()
        return [self._summary(r) for r in rows if manager or r["status"] == "active"]

    def get(self, name: str) -> dict[str, Any]:
        row = self._row(name)
        if row["status"] != "active" and not self._is_manager():
            raise NotFoundError(f"Workflow {name!r} is disabled.", code="WORKFLOW_DISABLED")
        return {**self._summary(row), "definition": row["definition"]}

    def versions(self, name: str) -> list[dict[str, Any]]:
        row = self._row(name)
        with self._conn.cursor(row_factory=dict_row) as cur:
            rows = cur.execute(
                sql.SQL(
                    "SELECT version, change, changed_by, changed_by_type, changed_at FROM {} "
                    "WHERE workflow_id = %s ORDER BY version"
                ).format(self._t("meta_workflow_versions")),
                (row["id"],),
            ).fetchall()
        return _plain(rows)

    def preview(self, definition: Any) -> dict[str, Any]:
        """Validate a definition without saving it (used for confirmations)."""
        self._authz.require("workflows.manage", "adding or changing workflows")
        parsed = parse_definition(definition)
        objects = validate_definition(parsed, self._session.snapshot())
        self._check_roles(parsed)
        self._check_profiles(parsed)
        with self._conn.cursor(row_factory=dict_row) as cur:
            existing = cur.execute(
                sql.SQL("SELECT version FROM {} WHERE name = %s").format(self._t("meta_workflows")),
                (parsed.name,),
            ).fetchone()
        return {
            "name": parsed.name,
            "description": parsed.description,
            "action": "update" if existing else "create",
            "current_version": existing["version"] if existing else None,
            "objects": objects,
            "steps": [describe_step(s) for s in parsed.steps],
            "triggers": [describe_trigger(t) for t in parsed.triggers],
            "definition": parsed.dump(),
        }

    def _check_profiles(self, definition: Definition) -> None:
        wanted = {s.model for s in definition.steps if isinstance(s, AgentStep)}
        if not wanted:
            return
        from ..ai import AIService

        ai = AIService(self._session)
        known = {p["name"] for p in ai.profiles()}
        if None in wanted and ai.default_profile() is None:
            raise WorkflowError(
                "This workflow has AI steps but no default AI model profile is set.",
                code="AI_NOT_CONFIGURED",
                hint="Set one with `osier ai profile <name> ... --default`, or name a profile "
                "in the step's 'model'.",
            )
        if unknown := sorted(w for w in wanted if w is not None and w not in known):
            raise WorkflowError(
                f"Unknown AI model profiles {unknown}.",
                field="model",
                code="AI_PROFILE_NOT_FOUND",
                expected=sorted(known),
            )

    def _check_roles(self, definition: Definition) -> None:
        wanted = {r for s in definition.steps if isinstance(s, Approval) for r in s.approver_roles}
        if not wanted:
            return
        known = {r["key"] for r in self._session.records.list("role", limit=200)}
        if unknown := sorted(wanted - known):
            raise WorkflowError(
                f"Unknown approver roles {unknown}.",
                field="approver_roles",
                expected=sorted(known),
            )

    def save(self, definition: Any) -> dict[str, Any]:
        """Create a workflow, or save a new version of an existing one."""
        self._require_manager("adding or changing workflows")
        preview = self.preview(definition)
        stored = preview["definition"]
        actor = self._principal.user_id
        with self._conn.transaction():
            with self._conn.cursor(row_factory=dict_row) as cur:
                existing = cur.execute(
                    sql.SQL("SELECT * FROM {} WHERE name = %s FOR UPDATE").format(
                        self._t("meta_workflows")
                    ),
                    (preview["name"],),
                ).fetchone()
            if existing is None:
                workflow_id, version, change = uuid.uuid4(), 1, "created"
                self._conn.execute(
                    sql.SQL(
                        "INSERT INTO {} (id, name, description, definition, created_by, "
                        "updated_by) VALUES (%s, %s, %s, %s, %s, %s)"
                    ).format(self._t("meta_workflows")),
                    (
                        workflow_id,
                        preview["name"],
                        preview["description"],
                        Jsonb(stored),
                        actor,
                        actor,
                    ),
                )
            else:
                workflow_id, version, change = existing["id"], existing["version"] + 1, "updated"
                self._conn.execute(
                    sql.SQL(
                        "UPDATE {} SET description = %s, definition = %s, version = %s, "
                        "status = 'active', updated_by = %s, updated_at = now() WHERE id = %s"
                    ).format(self._t("meta_workflows")),
                    (preview["description"], Jsonb(stored), version, actor, workflow_id),
                )
            self._conn.execute(
                sql.SQL(
                    "INSERT INTO {} (workflow_id, version, definition, changed_by, "
                    "changed_by_type, change) VALUES (%s, %s, %s, %s, %s, %s)"
                ).format(self._t("meta_workflow_versions")),
                (
                    workflow_id,
                    version,
                    Jsonb(stored),
                    actor,
                    self._principal.actor_type.value,
                    change,
                ),
            )
        self._sync_schedules(workflow_id, parse_definition(stored), active=True)
        return self._summary(self._row(preview["name"]))

    def set_status(self, name: str, status: str) -> dict[str, Any]:
        self._require_manager("enabling, disabling, or retiring workflows")
        if status not in ("active", "disabled", "retired"):
            raise RecordValidationError(
                f"Invalid status {status!r}.",
                field="status",
                expected=["active", "disabled", "retired"],
            )
        row = self._row(name)
        self._conn.execute(
            sql.SQL(
                "UPDATE {} SET status = %s, updated_by = %s, updated_at = now() WHERE id = %s"
            ).format(self._t("meta_workflows")),
            (status, self._principal.user_id, row["id"]),
        )
        self._sync_schedules(
            row["id"], parse_definition(row["definition"]), active=status == "active"
        )
        return {"name": name, "status": status}

    def _sync_schedules(
        self, workflow_id: uuid.UUID, definition: Definition, *, active: bool
    ) -> None:
        """Schedules run as the person who last saved or enabled the workflow."""
        import datetime as dt

        self._conn.execute(
            sql.SQL("DELETE FROM {} WHERE workflow_id = %s").format(self._t("workflow_schedules")),
            (workflow_id,),
        )
        if not active:
            return
        owner = self._person() or self._principal.user_id
        now = dt.datetime.now(dt.UTC)
        for index, trigger in enumerate(definition.triggers):
            if isinstance(trigger, ScheduleTrigger):
                self._conn.execute(
                    sql.SQL(
                        "INSERT INTO {} (workflow_id, trigger_index, cron, timezone, "
                        "run_as_user, next_run_at) VALUES (%s, %s, %s, %s, %s, %s)"
                    ).format(self._t("workflow_schedules")),
                    (
                        workflow_id,
                        index,
                        trigger.cron,
                        trigger.timezone,
                        owner,
                        next_run(trigger.cron, trigger.timezone, now),
                    ),
                )

    # ---- runs -------------------------------------------------------------------------

    def _definition(self, workflow_id: uuid.UUID, version: int) -> Definition:
        row = self._conn.execute(
            sql.SQL("SELECT definition FROM {} WHERE workflow_id = %s AND version = %s").format(
                self._t("meta_workflow_versions")
            ),
            (workflow_id, version),
        ).fetchone()
        return parse_definition(row[0])

    def _check_inputs(self, definition: Definition, inputs: dict[str, Any]) -> dict[str, Any]:
        unknown = sorted(set(inputs) - set(definition.inputs))
        if unknown:
            raise RecordValidationError(
                f"Unknown inputs {unknown}.",
                code="WORKFLOW_INPUT_INVALID",
                expected=sorted(definition.inputs),
            )
        checked: dict[str, Any] = {}
        for name, spec in definition.inputs.items():
            if inputs.get(name) is None:
                if spec.required:
                    raise RecordValidationError(
                        f"Input {name!r} is required.",
                        code="WORKFLOW_INPUT_INVALID",
                        field=name,
                        expected=spec.description,
                    )
                continue
            try:
                checked[name] = TypeAdapter(_INPUT_TYPES[spec.type]).validate_python(inputs[name])
            except PydanticValidationError:
                raise RecordValidationError(
                    f"Input {name!r} must be a {spec.type}.",
                    code="WORKFLOW_INPUT_INVALID",
                    field=name,
                    expected=spec.type,
                ) from None
        return _plain(checked)

    def start(
        self, name: str, inputs: dict[str, Any] | None = None, *, background: bool = False
    ) -> dict[str, Any]:
        """Start a run. It runs until it finishes or reaches an approval step, here or,
        with ``background``, in a worker (``osier worker``)."""
        self._authz.require("records.use", "running workflows")
        row = self._row(name)
        if row["status"] != "active":
            raise NotFoundError(f"Workflow {name!r} is disabled.", code="WORKFLOW_DISABLED")
        definition = self._definition(row["id"], row["version"])
        checked = self._check_inputs(definition, inputs or {})
        background = background or has_ai_steps(definition)  # AI steps never block a request
        run_id = self.create_run(row, checked, self._principal, queued=background)
        if background:
            return self.get_run(run_id)
        return self._execute(run_id, {"inputs": checked, "steps": {}})

    def create_run(
        self,
        workflow: dict[str, Any],
        inputs: dict[str, Any],
        principal: Principal,
        *,
        queued: bool,
        trigger: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
    ) -> uuid.UUID:
        """Insert a run (and, when queued, its job). Used by start and by triggers."""
        run_id = uuid.uuid4()
        status = "failed" if error else ("queued" if queued else "running")
        self._conn.execute(
            sql.SQL(
                "INSERT INTO {} (id, workflow_id, workflow_name, version, status, inputs, "
                "run_as_user, run_as_type, on_behalf_of, agent_id, trigger, error, finished_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
                "CASE WHEN %s THEN now() END)"
            ).format(self._t("workflow_runs")),
            (
                run_id,
                workflow["id"],
                workflow["name"],
                workflow["version"],
                status,
                Jsonb(_plain(inputs)),
                principal.user_id,
                principal.actor_type.value,
                principal.on_behalf_of,
                principal.agent_id,
                Jsonb(_plain(trigger)) if trigger else None,
                Jsonb(error) if error else None,
                bool(error),
            ),
        )
        self._event(
            run_id,
            None,
            "started" if not error else "failed",
            {
                "inputs": inputs,
                "version": workflow["version"],
                "trigger": trigger,
                **({"error": error} if error else {}),
            },
        )
        # A queued run is handed to the tenant's job queue by the worker (osier.jobs).
        return run_id

    def execute_job(self, run_id: Any) -> dict[str, Any]:
        """Run or resume a run in a worker. Safe to repeat after a crash: it continues
        from the last checkpoint, with a decision made in the background if any."""
        run = self._run_row(run_id)
        if run["status"] in FINISHED or run["status"] == "waiting":
            return self._run_out(run)
        self._conn.execute(
            sql.SQL(
                "UPDATE {} SET status = 'running', updated_at = now() WHERE id = %s "
                "AND status IN ('queued', 'running')"
            ).format(self._t("workflow_runs")),
            (run["id"],),
        )
        result = self._execute(run["id"], None, definition_of=run, decision=run["resume_with"])
        if run["resume_with"] is not None:
            self._conn.execute(
                sql.SQL("UPDATE {} SET resume_with = NULL WHERE id = %s").format(
                    self._t("workflow_runs")
                ),
                (run["id"],),
            )
        return result

    def run_principal(self, run_id: Any) -> Principal:
        """Who a run acts as (for the worker)."""
        return self._run_as(self._run_row(run_id))

    def mark_failed(self, run_id: Any, error: dict[str, Any]) -> None:
        run = self._run_row(run_id)
        if run["status"] not in FINISHED:
            self._finish(run["id"], "failed", error=error)
            self._event(run["id"], None, "failed", error)

    def _run_row(self, run_id: Any, *, lock: bool = False) -> dict[str, Any]:
        try:
            run_uuid = uuid.UUID(str(run_id))
        except ValueError:
            run_uuid = None
        row = None
        if run_uuid:
            with self._conn.cursor(row_factory=dict_row) as cur:
                row = cur.execute(
                    sql.SQL("SELECT * FROM {} WHERE id = %s").format(self._t("workflow_runs")),
                    (run_uuid,),
                ).fetchone()
        if row is None or not self._can_see(row):
            raise NotFoundError(f"No workflow run {run_id}.", code="WORKFLOW_RUN_NOT_FOUND")
        return row

    def _can_see(self, run: dict[str, Any]) -> bool:
        if self._is_manager():
            return True
        me = self._person()
        if me in (run["run_as_user"], run["on_behalf_of"]):
            return True
        roles = (run["waiting"] or {}).get("approver_roles") or []
        return bool(set(roles) & set(self._authz.roles()))

    def _run_out(self, row: dict[str, Any]) -> dict[str, Any]:
        return _plain(
            {
                "run_id": row["id"],
                "workflow": row["workflow_name"],
                "version": row["version"],
                "status": row["status"],
                "inputs": row["inputs"],
                "steps": row["steps"],
                "output": row["output"],
                "waiting_for": row["waiting"],
                "error": row["error"],
                "started_by": row["on_behalf_of"] or row["run_as_user"],
                "started_by_agent": row["run_as_type"] == ActorType.AGENT.value,
                "trigger": row.get("trigger"),
                "started_at": row["started_at"],
                "finished_at": row["finished_at"],
            }
        )

    def get_run(self, run_id: Any) -> dict[str, Any]:
        return self._run_out(self._run_row(run_id))

    def runs(self, *, status: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        statement = sql.SQL("SELECT * FROM {}").format(self._t("workflow_runs"))
        params: list[Any] = []
        if status:
            statement += sql.SQL(" WHERE status = %s")
            params.append(status)
        statement += sql.SQL(" ORDER BY started_at DESC LIMIT 500")
        with self._conn.cursor(row_factory=dict_row) as cur:
            rows = cur.execute(statement, params).fetchall()
        return [self._run_out(r) for r in rows if self._can_see(r)][: max(1, min(limit, 100))]

    def events(self, run_id: Any) -> list[dict[str, Any]]:
        row = self._run_row(run_id)
        with self._conn.cursor(row_factory=dict_row) as cur:
            rows = cur.execute(
                sql.SQL(
                    "SELECT step_id, event, detail, actor, actor_type, at FROM {} "
                    "WHERE run_id = %s ORDER BY id"
                ).format(self._t("workflow_run_events")),
                (row["id"],),
            ).fetchall()
        return _plain(rows)

    def _event(
        self, run_id: uuid.UUID, step_id: str | None, event: str, detail: dict[str, Any]
    ) -> None:
        self._conn.execute(
            sql.SQL(
                "INSERT INTO {} (run_id, step_id, event, detail, actor, actor_type) "
                "VALUES (%s, %s, %s, %s, %s, %s)"
            ).format(self._t("workflow_run_events")),
            (
                run_id,
                step_id,
                event,
                Jsonb(_plain(detail)),
                self._principal.user_id,
                self._principal.actor_type.value,
            ),
        )

    def _may_decide(self, run: dict[str, Any]) -> bool:
        roles = (run["waiting"] or {}).get("approver_roles") or []
        if roles:
            return bool(set(roles) & set(self._authz.roles()))
        return self._authz.has("workflows.manage")

    def awaiting_decision(self) -> list[dict[str, Any]]:
        """Waiting runs that the person behind this session may decide."""
        with self._conn.cursor(row_factory=dict_row) as cur:
            rows = cur.execute(
                sql.SQL("SELECT * FROM {} WHERE status = 'waiting' ORDER BY started_at").format(
                    self._t("workflow_runs")
                )
            ).fetchall()
        return [self._run_out(r) for r in rows if self._may_decide(r)]

    def decide(
        self, run_id: Any, *, approve: bool, comment: str | None = None, background: bool = False
    ) -> dict[str, Any]:
        """Approve or reject the step a run is waiting on, then continue the run."""
        run = self._run_row(run_id)
        waiting = run["waiting"] or {}
        if run["status"] != "waiting":
            raise ApprovalError(
                f"Run {run['id']} is {run['status']}, not waiting for a decision.",
                code="WORKFLOW_RUN_NOT_WAITING",
            )
        if not self._principal.is_human:
            raise ApprovalError(
                "Workflow approvals must be decided by a person.",
                code="APPROVAL_REQUIRES_HUMAN",
                hint="Use the chat tools, which ask the user to confirm.",
            )
        roles = waiting.get("approver_roles") or []
        if roles:
            if not self._may_decide(run):
                from ..authz import PermissionDenied

                raise PermissionDenied(
                    f"Deciding this approval needs one of the roles {roles}.",
                    expected=roles,
                )
        else:
            self._authz.require("workflows.manage", "deciding workflow approvals")
        decision = {
            "approved": approve,
            "decided_by": str(self._principal.user_id),
            "comment": comment,
        }
        background = background or has_ai_steps(
            self._definition(run["workflow_id"], run["version"])
        )
        claimed = self._conn.execute(
            sql.SQL(
                "UPDATE {} SET status = %s, waiting = NULL, resume_with = %s, "
                "deferred_at = NULL, updated_at = now() "
                "WHERE id = %s AND status = 'waiting' RETURNING id"
            ).format(self._t("workflow_runs")),
            (
                "queued" if background else "running",
                Jsonb(decision) if background else None,
                run["id"],
            ),
        ).fetchone()
        if claimed is None:
            raise ApprovalError("Someone else just decided this.", code="WORKFLOW_RUN_NOT_WAITING")
        self._event(run["id"], waiting.get("step"), "approved" if approve else "rejected", decision)
        run_as = self._run_as(run)
        if background:
            return self.get_run(run["id"])
        runner = self._session.as_principal(run_as)
        return WorkflowService(runner)._execute(
            run["id"], Command(resume=decision), definition_of=run
        )

    def cancel(self, run_id: Any) -> dict[str, Any]:
        run = self._run_row(run_id)
        if run["status"] in FINISHED:
            raise ApprovalError(
                f"Run {run['id']} already {run['status']}.", code="WORKFLOW_RUN_FINISHED"
            )
        if self._person() not in (run["run_as_user"], run["on_behalf_of"]):
            self._authz.require("workflows.manage", "cancelling other people's runs")
        self._finish(run["id"], "cancelled")
        self._event(run["id"], None, "cancelled", {})
        return self.get_run(run["id"])

    @staticmethod
    def _run_as(run: dict[str, Any]) -> Principal:
        return Principal(
            user_id=run["run_as_user"],
            actor_type=ActorType(run["run_as_type"]),
            on_behalf_of=run["on_behalf_of"],
            agent_id=run["agent_id"],
            source="workflow",
        )

    def _finish(self, run_id: uuid.UUID, status: str, **fields: Any) -> None:
        self._conn.execute(
            sql.SQL(
                "UPDATE {} SET status = %s, steps = COALESCE(%s, steps), output = %s, "
                "error = %s, waiting = NULL, updated_at = now(), finished_at = now() "
                "WHERE id = %s"
            ).format(self._t("workflow_runs")),
            (
                status,
                Jsonb(fields["steps"]) if "steps" in fields else None,
                Jsonb(fields.get("output")) if fields.get("output") is not None else None,
                Jsonb(fields.get("error")) if fields.get("error") else None,
                run_id,
            ),
        )

    # ---- execution --------------------------------------------------------------------

    def _execute(
        self,
        run_id: uuid.UUID,
        payload: Any,
        *,
        definition_of: dict[str, Any] | None = None,
        decision: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        run = definition_of or self._run_row(run_id)
        definition = self._definition(run["workflow_id"], run["version"])
        graph = self._compile(definition, run_id)
        config = {"configurable": {"thread_id": str(run_id)}}
        if payload is None:  # a worker: start, resume with a decision, or continue
            current = graph.get_state(config)
            if not current.values:
                payload = {"inputs": run["inputs"], "steps": {}}
            elif decision is not None and current.interrupts:
                payload = Command(resume=decision)
        try:
            graph.invoke(payload, config)
        except _StepFailed as failed:
            state = graph.get_state(config).values
            error = {"step": failed.step_id, **failed.error.to_dict()}
            self._finish(run_id, "failed", steps=state.get("steps", {}), error=error)
            self._event(run_id, failed.step_id, "failed", error)
            return self.get_run(run_id)
        snapshot = graph.get_state(config)
        steps = snapshot.values.get("steps", {})
        if snapshot.interrupts:
            waiting = snapshot.interrupts[0].value
            self._conn.execute(
                sql.SQL(
                    "UPDATE {} SET status = 'waiting', steps = %s, waiting = %s, "
                    "updated_at = now() WHERE id = %s AND status = 'running'"
                ).format(self._t("workflow_runs")),
                (Jsonb(steps), Jsonb(waiting), run_id),
            )
            self._event(run_id, waiting["step"], "waiting", waiting)
            return self.get_run(run_id)
        rejected = any(
            isinstance(s, Approval)
            and s.on_reject is None
            and steps.get(s.id, {}).get("approved") is False
            for s in definition.steps
        )
        output = next(
            (
                steps[s.id]["output"]
                for s in reversed(definition.steps)
                if isinstance(s, Finish) and s.id in steps
            ),
            None,
        )
        status = "rejected" if rejected else "succeeded"
        self._finish(run_id, status, steps=steps, output=output)
        self._event(run_id, None, status, {})
        return self.get_run(run_id)

    def _compile(self, definition: Definition, run_id: uuid.UUID) -> Any:
        graph = StateGraph(RunState)
        steps = definition.steps

        def target(name: str | None, index: int) -> str:
            if name == END_TARGET:
                return END
            if name:
                return name
            return steps[index + 1].id if index + 1 < len(steps) else END

        for i, step in enumerate(steps):
            graph.add_node(step.id, self._node(step, run_id))
            if isinstance(step, Condition):
                then, other = target(step.then, i), target(step.else_, i)
                graph.add_conditional_edges(
                    step.id,
                    lambda state, sid=step.id, a=then, b=other: (
                        a if state["steps"][sid]["result"] else b
                    ),
                    sorted({then, other}),
                )
            elif isinstance(step, Approval):
                ok = target(step.next, i)
                no = target(step.on_reject, i) if step.on_reject else END
                graph.add_conditional_edges(
                    step.id,
                    lambda state, sid=step.id, a=ok, b=no: (
                        a if state["steps"][sid]["approved"] else b
                    ),
                    sorted({ok, no}),
                )
            elif isinstance(step, Finish):
                graph.add_edge(step.id, END)
            else:
                graph.add_edge(step.id, target(step.next, i))
        graph.add_edge(START, steps[0].id)
        return graph.compile(checkpointer=TenantCheckpointSaver(self._conn))

    def _node(self, step: Any, run_id: uuid.UUID) -> Any:
        # Changes made by workflow steps are marked as such and never fire triggers.
        step_session = self._session.as_principal(
            dataclasses.replace(self._principal, source="workflow")
        )
        records = step_session.records
        provenance = {"source_type": "system", "source_ref": f"workflow run {run_id}"}

        def run(state: RunState) -> dict[str, Any]:
            context = {"inputs": state["inputs"], "steps": state["steps"]}
            if isinstance(step, Approval):
                decision = interrupt(
                    {
                        "step": step.id,
                        "message": resolve(step.message, context),
                        "approver_roles": step.approver_roles,
                    }
                )
                return {"steps": {step.id: _plain(decision)}}
            try:
                if isinstance(step, AgentStep):
                    from .agent_step import run_agent_step

                    result = run_agent_step(step, context, step_session, run_id)
                else:
                    result = self._run_step(step, context, records, provenance, run_id)
            except OsierError as exc:
                raise _StepFailed(step.id, exc) from None
            return {"steps": {step.id: _plain(result)}}

        return run

    @staticmethod
    def _run_step(
        step: Any,
        context: dict[str, Any],
        records: Any,
        provenance: dict[str, Any],
        run_id: uuid.UUID,
    ) -> dict[str, Any]:
        if isinstance(step, GetRecord):
            return {"record": records.get(step.object, resolve(step.record_id, context))}
        if isinstance(step, QueryRecords):
            found = records.query(resolve(step.query, context))
            return {
                "records": found["records"],
                "count": len(found["records"]),
                "has_more": found.get("has_more", False),
            }
        if isinstance(step, CreateRecord):
            return {
                "record": records.create(
                    step.object,
                    resolve(step.values, context),
                    idempotency_key=f"workflow:{run_id}:{step.id}",
                    provenance=provenance,
                )
            }
        if isinstance(step, UpdateRecord):
            record_id = resolve(step.record_id, context)
            current = records.get(step.object, record_id)
            return {
                "record": records.update(
                    step.object,
                    record_id,
                    resolve(step.values, context),
                    expected_row_version=current["row_version"],
                    provenance=provenance,
                )
            }
        if isinstance(step, Condition):
            return {"result": all(check(c, context) for c in step.when)}
        if isinstance(step, Finish):
            return {"output": resolve(step.output, context)}
        raise WorkflowError(f"Unsupported step type {step.type!r}.")


def has_ai_steps(definition: Definition) -> bool:
    return any(isinstance(s, AgentStep) for s in definition.steps)


def describe_trigger(trigger: Any) -> str:
    if isinstance(trigger, ScheduleTrigger):
        return (
            f"runs on schedule '{trigger.cron}' ({trigger.timezone}) with the permissions "
            "of the person saving this workflow"
        )
    what = "created" if trigger.type == "record_created" else "updated"
    fields = f" ({', '.join(trigger.fields)} changed)" if trigger.fields else ""
    cond = " when its conditions hold" if trigger.when else ""
    return (
        f"runs when a {trigger.object} record is {what}{fields}{cond}, with the "
        "permissions of whoever made the change"
    )


def describe_step(step: Any) -> str:
    """One line per step, for confirmation prompts."""
    if isinstance(step, AgentStep):
        tools = (
            f"may {', '.join(step.tools)} on {', '.join(step.objects)}"
            if step.tools
            else "no tools"
        )
        skill = f", following skill {step.skill!r}" if step.skill else ""
        skill += f", model profile {step.model!r}" if step.model else ", default model profile"
        return (
            f"{step.id}: AI step ({tools}{skill}) returning {', '.join(step.output)}; "
            "uses this organization's AI tokens"
        )
    if isinstance(step, GetRecord):
        return f"{step.id}: read a {step.object} record"
    if isinstance(step, QueryRecords):
        return f"{step.id}: find {step.query.get('object')} records"
    if isinstance(step, CreateRecord):
        return f"{step.id}: create a {step.object} record ({', '.join(step.values)})"
    if isinstance(step, UpdateRecord):
        return f"{step.id}: update a {step.object} record ({', '.join(step.values)})"
    if isinstance(step, Condition):
        return f"{step.id}: if conditions hold go to {step.then}, else {step.else_}"
    if isinstance(step, Approval):
        who = ", ".join(step.approver_roles) or "someone with workflows.manage"
        return f"{step.id}: wait for approval by {who}"
    return f"{step.id}: finish"
