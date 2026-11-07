"""Turn record events and due schedules into queued workflow runs.

Called by the worker on every tick with a system session for one tenant.

- **Record events** are claimed with ``FOR UPDATE SKIP LOCKED`` and marked dispatched
  in the same transaction that creates the runs and their jobs, so an event fires its
  triggers exactly once even with several workers or a crash. Rows only become
  visible once the transaction that wrote them commits, so no event is skipped.
- Runs from a record event use the permissions of whoever made the change. Events
  caused by workflow runs (source ``workflow``) and by the platform never fire
  triggers: no loops, and no run without a person behind it.
- **Schedules** fire once per due time and then move to the next time after *now*;
  missed times are skipped rather than replayed.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from psycopg import sql
from psycopg.rows import dict_row

from ..context import ActorType, Principal
from ..errors import OsierError
from .spec import RecordTrigger, ScheduleTrigger, check, next_run, parse_definition, resolve

BATCH = 200


def _active_workflows(session: Any) -> list[tuple[dict[str, Any], Any]]:
    with session.conn.cursor(row_factory=dict_row) as cur:
        rows = cur.execute(
            sql.SQL(
                "SELECT id, name, version, definition, updated_by FROM {} WHERE status = 'active'"
            ).format(sql.Identifier(session.tenant.schema, "meta_workflows"))
        ).fetchall()
    return [(row, parse_definition(row["definition"])) for row in rows]


def _start(
    session: Any,
    workflow: dict[str, Any],
    definition: Any,
    inputs: dict[str, Any],
    principal: Principal,
    trigger: dict[str, Any],
) -> None:
    service = session.as_principal(principal).workflows
    try:
        checked = service._check_inputs(definition, inputs)
    except OsierError as exc:  # visible as a failed run instead of silently dropped
        service.create_run(
            workflow, inputs, principal, queued=False, trigger=trigger, error=exc.to_dict()
        )
        return
    service.create_run(workflow, checked, principal, queued=True, trigger=trigger)


def dispatch_record_events(session: Any) -> int:
    schema = session.tenant.schema
    events_table = sql.Identifier(schema, "record_events")
    with session.conn.transaction():
        with session.conn.cursor(row_factory=dict_row) as cur:
            events = cur.execute(
                sql.SQL(
                    "SELECT * FROM {} WHERE dispatched_at IS NULL ORDER BY id LIMIT %s "
                    "FOR UPDATE SKIP LOCKED"
                ).format(events_table),
                (BATCH,),
            ).fetchall()
        if not events:
            return 0
        workflows = [
            (row, d, i, t)
            for row, d in _active_workflows(session)
            for i, t in enumerate(d.triggers)
            if isinstance(t, RecordTrigger)
        ]
        if workflows:
            snapshot = session.snapshot()
            for event in events:
                _fire(session, snapshot, event, workflows)
        session.conn.execute(
            sql.SQL("UPDATE {} SET dispatched_at = now() WHERE id = ANY(%s)").format(events_table),
            ([e["id"] for e in events],),
        )
    return len(events)


def _fire(session: Any, snapshot: Any, event: dict[str, Any], workflows: list[Any]) -> None:
    if event["source"] == "workflow" or event["actor_type"] == ActorType.SYSTEM.value:
        return
    kind = {"created": "record_created", "updated": "record_updated"}.get(event["event"])
    if kind is None:
        return
    obj = snapshot.objects_by_id.get(event["object_id"])
    if obj is None:
        return
    matching = [
        w
        for w in workflows
        if w[3].type == kind
        and w[3].object == obj.api_name
        and (not w[3].fields or set(w[3].fields) & set(event["changed_fields"] or []))
    ]
    if not matching:
        return
    record = session.records.get(obj.api_name, event["record_id"], include_retired=True)
    context = {
        "record": record,
        "event": {"changed_fields": event["changed_fields"] or [], "type": event["event"]},
    }
    principal = Principal(
        user_id=event["actor_id"],
        actor_type=ActorType(event["actor_type"]),
        on_behalf_of=event["on_behalf_of"],
        agent_id=event["agent_id"],
        source="trigger",
    )
    for row, definition, index, trigger in matching:
        if not all(check(c, context) for c in trigger.when):
            continue
        _start(
            session,
            row,
            definition,
            resolve(trigger.inputs, context),
            principal,
            {
                "type": trigger.type,
                "index": index,
                "event_id": event["id"],
                "record_id": str(event["record_id"]),
            },
        )


def dispatch_schedules(session: Any) -> int:
    schema = session.tenant.schema
    table = sql.Identifier(schema, "workflow_schedules")
    fired = 0
    with session.conn.transaction():
        with session.conn.cursor(row_factory=dict_row) as cur:
            due = cur.execute(
                sql.SQL(
                    "SELECT * FROM {} WHERE next_run_at <= now() ORDER BY next_run_at "
                    "LIMIT %s FOR UPDATE SKIP LOCKED"
                ).format(table),
                (BATCH,),
            ).fetchall()
        if not due:
            return 0
        workflows = {row["id"]: (row, d) for row, d in _active_workflows(session)}
        now = dt.datetime.now(dt.UTC)
        for schedule in due:
            found = workflows.get(schedule["workflow_id"])
            trigger = (
                found[1].triggers[schedule["trigger_index"]]
                if found and schedule["trigger_index"] < len(found[1].triggers)
                else None
            )
            if not isinstance(trigger, ScheduleTrigger):
                session.conn.execute(
                    sql.SQL("DELETE FROM {} WHERE workflow_id = %s AND trigger_index = %s").format(
                        table
                    ),
                    (schedule["workflow_id"], schedule["trigger_index"]),
                )
                continue
            row, definition = found  # type: ignore[misc]
            context = {"schedule": {"time": schedule["next_run_at"].isoformat()}}
            _start(
                session,
                row,
                definition,
                resolve(trigger.inputs, context),
                Principal(user_id=schedule["run_as_user"], source="schedule"),
                {
                    "type": "schedule",
                    "index": schedule["trigger_index"],
                    "scheduled_for": schedule["next_run_at"].isoformat(),
                },
            )
            fired += 1
            session.conn.execute(
                sql.SQL(
                    "UPDATE {} SET next_run_at = %s WHERE workflow_id = %s AND trigger_index = %s"
                ).format(table),
                (
                    next_run(trigger.cron, trigger.timezone, now),
                    schedule["workflow_id"],
                    schedule["trigger_index"],
                ),
            )
    return fired
