"""The osier workflow definition: a declarative, versioned JSON document.

A definition never contains code. Steps run in order unless a step names its
``next`` step; ``condition`` steps branch with ``then`` / ``else``; ``approval``
steps pause the run until a person decides. Jumps may only go forward, so every
workflow ends.

Values may refer to the run's inputs and to earlier steps' results with
``{{inputs.lead_id}}`` or ``{{steps.find.records.0.id}}``. A string that is only a
reference keeps the referenced value's type; references inside longer text are
inserted as text. There are no expressions.

    {"name": "qualify-lead",
     "description": "Ask for approval, then mark a lead as qualified.",
     "inputs": {"lead_id": {"type": "string", "description": "The lead to qualify."}},
     "steps": [
       {"id": "lead", "type": "get_record", "object": "lead",
        "record_id": "{{inputs.lead_id}}"},
       {"id": "approve", "type": "approval",
        "message": "Qualify {{steps.lead.record.company_name}}?"},
       {"id": "mark", "type": "update_record", "object": "lead",
        "record_id": "{{inputs.lead_id}}", "values": {"status": "qualified"}}]}
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic import ValidationError as PydanticValidationError

from ..errors import OsierError
from ..metadata import SchemaSnapshot

NAME = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
STEP_ID = r"^[a-z][a-z0-9_]{0,39}$"
END = "end"
MAX_STEPS = 50
_REF = re.compile(r"\{\{\s*([A-Za-z0-9_.\-]+)\s*\}\}")


class WorkflowError(OsierError):
    default_code = "WORKFLOW_INVALID"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class InputDef(_Model):
    type: Literal["string", "number", "integer", "boolean", "object", "array"] = "string"
    description: str = Field(min_length=1, max_length=500)
    required: bool = True


class Check(_Model):
    value: Any
    op: Literal[
        "eq",
        "ne",
        "gt",
        "gte",
        "lt",
        "lte",
        "in",
        "not_in",
        "contains",
        "is_empty",
        "is_not_empty",
    ] = "eq"
    compare_to: Any = None


class _Step(_Model):
    id: str = Field(pattern=STEP_ID)
    description: str | None = Field(default=None, max_length=500)
    next: str | None = None


class GetRecord(_Step):
    type: Literal["get_record"]
    object: str
    record_id: Any


class QueryRecords(_Step):
    type: Literal["query_records"]
    query: dict[str, Any]


class CreateRecord(_Step):
    type: Literal["create_record"]
    object: str
    values: dict[str, Any]


class UpdateRecord(_Step):
    type: Literal["update_record"]
    object: str
    record_id: Any
    values: dict[str, Any]


class Condition(_Step):
    type: Literal["condition"]
    when: list[Check] = Field(min_length=1, max_length=10)  # all must hold
    then: str
    else_: str = Field(default=END, alias="else")


class Approval(_Step):
    type: Literal["approval"]
    message: str = Field(min_length=1, max_length=2000)
    approver_roles: list[str] = Field(default_factory=list, max_length=10)
    on_reject: str | None = None  # default: the run ends as rejected


class Finish(_Step):
    type: Literal["finish"]
    output: dict[str, Any] = Field(default_factory=dict)


class ConnectorStep(_Step):
    """Use a connected app: a capability such as ``email.send`` (routed to a connection by
    purpose, or the named ``connection``), or a tool of a connected MCP server
    (``connection`` + ``tool``). Runs with the run's permissions through the connector
    service, so tool policies apply: an action that needs approval pauses the run until
    a person approves it in the approval queue. Rejected actions end the run as
    rejected; failed ones fail the step unless ``on_failure`` is ``continue``."""

    type: Literal["connector"]
    capability: str | None = Field(default=None, max_length=100)
    connection: str | None = Field(default=None, max_length=200)
    tool: str | None = Field(default=None, max_length=200)
    arguments: dict[str, Any] = Field(default_factory=dict)
    on_failure: Literal["fail", "continue"] = "fail"


AGENT_TOOLS = ("get_record", "query_records", "create_record", "update_record")


class OutputField(_Model):
    type: Literal["string", "number", "integer", "boolean", "string_list"] = "string"
    description: str = Field(min_length=1, max_length=500)
    enum: list[str] | None = Field(default=None, max_length=50)


class AgentStep(_Step):
    """An AI step: a model from one of the tenant's AI connections (chosen through a
    model profile) works on ``instructions``
    (and an optional skill), may use the listed record tools on the listed objects
    with the run's permissions, and must return ``output`` (checked against the
    declared fields). Always runs in the background worker."""

    type: Literal["agent"]
    instructions: str = Field(min_length=1, max_length=20_000)
    skill: str | None = Field(default=None, max_length=64)
    data: dict[str, Any] = Field(default_factory=dict)
    tools: list[Literal["get_record", "query_records", "create_record", "update_record"]] = Field(
        default_factory=list, max_length=4
    )
    objects: list[str] = Field(default_factory=list, max_length=10)
    output: dict[str, OutputField] = Field(min_length=1, max_length=30)
    model: str | None = Field(
        default=None,
        max_length=40,
        description="AI model profile to use (osier ai profiles); default profile if empty.",
    )
    max_turns: int = Field(default=6, ge=1, le=20)
    effort: Literal["low", "medium", "high"] | None = None


Step = Annotated[
    GetRecord
    | QueryRecords
    | CreateRecord
    | UpdateRecord
    | Condition
    | Approval
    | Finish
    | AgentStep
    | ConnectorStep,
    Field(discriminator="type"),
]
STEP_TYPES = [
    "get_record",
    "query_records",
    "create_record",
    "update_record",
    "condition",
    "approval",
    "finish",
    "agent",
    "connector",
]


class RecordTrigger(_Model):
    """Start a run when a record is created or updated. ``fields`` (updates only)
    limits it to changes of those fields; ``when`` checks the record. Inputs may use
    ``{{record.<field>}}`` and ``{{event.changed_fields}}``. The run uses the
    permissions of whoever made the change; changes made by workflows never fire
    triggers."""

    type: Literal["record_created", "record_updated"]
    object: str
    fields: list[str] = Field(default_factory=list, max_length=20)
    when: list[Check] = Field(default_factory=list, max_length=10)
    inputs: dict[str, Any] = Field(default_factory=dict)


class ScheduleTrigger(_Model):
    """Start a run on a cron schedule (at most every 5 minutes) in a time zone. The
    run uses the permissions of the person who last saved the workflow. Inputs may
    use ``{{schedule.time}}``."""

    type: Literal["schedule"]
    cron: str = Field(min_length=9, max_length=100)
    timezone: str = "UTC"
    inputs: dict[str, Any] = Field(default_factory=dict)


Trigger = Annotated[RecordTrigger | ScheduleTrigger, Field(discriminator="type")]
MIN_SCHEDULE_SECONDS = 300


class Definition(_Model):
    name: str = Field(min_length=1, max_length=64)
    description: str = Field(min_length=1, max_length=1024)
    inputs: dict[str, InputDef] = Field(default_factory=dict, max_length=30)
    triggers: list[Trigger] = Field(default_factory=list, max_length=5)
    steps: list[Step] = Field(min_length=1, max_length=MAX_STEPS)

    def dump(self) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=True, exclude_none=True)


# ---- references -----------------------------------------------------------------------


def references(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield from _REF.findall(value)
    elif isinstance(value, dict):
        for v in value.values():
            yield from references(v)
    elif isinstance(value, list):
        for v in value:
            yield from references(v)


def lookup(path: str, context: dict[str, Any]) -> Any:
    current: Any = context
    for part in path.split("."):
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            return None
    return current


def resolve(value: Any, context: dict[str, Any]) -> Any:
    """Replace references with values from the run (inputs and earlier step results)."""
    if isinstance(value, str):
        whole = _REF.fullmatch(value.strip())
        if whole:
            return lookup(whole.group(1), context)

        def text(m: re.Match[str]) -> str:
            found = lookup(m.group(1), context)
            return "" if found is None else str(found)

        return _REF.sub(text, value)
    if isinstance(value, dict):
        return {k: resolve(v, context) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve(v, context) for v in value]
    return value


def check(c: Check, context: dict[str, Any]) -> bool:
    value, other = resolve(c.value, context), resolve(c.compare_to, context)
    if c.op == "is_empty":
        return value in (None, "", [], {})
    if c.op == "is_not_empty":
        return value not in (None, "", [], {})
    if c.op == "eq":
        return value == other
    if c.op == "ne":
        return value != other
    if c.op in ("in", "not_in"):
        found = isinstance(other, list) and value in other
        return found if c.op == "in" else not found
    if c.op == "contains":
        return isinstance(value, str | list) and other is not None and other in value
    try:
        a, b = _comparable(value), _comparable(other)
        return {"gt": a > b, "gte": a >= b, "lt": a < b, "lte": a <= b}[c.op]
    except (TypeError, ValueError):
        return False  # missing or incomparable values never match


def _comparable(v: Any) -> Any:
    if isinstance(v, str):
        try:
            return float(v)  # decimals are stored as text in results
        except ValueError:
            return v
    if v is None:
        raise TypeError
    return v


# ---- validation -----------------------------------------------------------------------


def parse_definition(raw: Any) -> Definition:
    try:
        definition = Definition.model_validate(raw)
    except PydanticValidationError as exc:
        raise WorkflowError(
            "The workflow definition is not valid.",
            details={
                "errors": [
                    {"location": ".".join(str(p) for p in e["loc"]), "message": e["msg"]}
                    for e in exc.errors(include_url=False)
                ]
            },
            hint=f"Step types: {', '.join(STEP_TYPES)}.",
        ) from None
    if not NAME.fullmatch(definition.name):
        raise WorkflowError(
            f"Invalid workflow name {definition.name!r}.",
            field="name",
            expected="lowercase letters, digits, and single hyphens, e.g. 'qualify-lead'",
        )
    return definition


def validate_definition(definition: Definition, snapshot: SchemaSnapshot) -> list[str]:
    """Check the definition against itself and the tenant's metadata. Returns the
    objects it uses."""
    ids = [s.id for s in definition.steps]
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    if duplicates:
        raise WorkflowError(f"Step ids must be unique: {duplicates}.", field="steps")
    if END in ids:
        raise WorkflowError(f"{END!r} is reserved; use another step id.", field="steps")
    position = {step_id: i for i, step_id in enumerate(ids)}

    objects: set[str] = set()
    for i, step in enumerate(definition.steps):
        where = f"steps.{step.id}"
        targets = [step.next]
        if isinstance(step, Condition):
            targets += [step.then, step.else_]
        if isinstance(step, Approval):
            targets.append(step.on_reject)
        for target in filter(None, targets):
            if target != END and target not in position:
                raise WorkflowError(
                    f"{where} goes to unknown step {target!r}.",
                    field=where,
                    expected=[*ids, END],
                )
            if target != END and position[target] <= i:
                raise WorkflowError(
                    f"{where} goes back to {target!r}; steps may only go forward.",
                    field=where,
                    hint="Loops are not supported yet.",
                )

        for ref in references(step.model_dump(by_alias=True)):
            root, _, rest = ref.partition(".")
            name = rest.split(".")[0]
            if root == "inputs" and name in definition.inputs:
                continue
            if root == "steps" and name in position and position[name] < i:
                continue
            raise WorkflowError(
                f"{where} refers to {{{{{ref}}}}}, which is not an input or an earlier step.",
                field=where,
                expected=[f"inputs.{n}" for n in definition.inputs]
                + [f"steps.{s}" for s in ids[:i]],
            )

        if isinstance(step, AgentStep):
            objects |= _validate_agent(step, where, snapshot)
            continue
        if isinstance(step, ConnectorStep):
            if bool(step.capability) == bool(step.tool):
                raise WorkflowError(
                    f"{where}: give either 'capability' (e.g. email.send) or 'connection' "
                    "and 'tool'.",
                    field=where,
                )
            if step.tool and not step.connection:
                raise WorkflowError(f"{where}: 'tool' needs 'connection'.", field=where)
            continue
        object_name = getattr(step, "object", None)
        if isinstance(step, QueryRecords):
            object_name = step.query.get("object")
            if not isinstance(object_name, str):
                raise WorkflowError(f"{where}: the query needs an object.", field=where)
        if object_name:
            obj = snapshot.get_object(object_name)  # raises with the known objects
            objects.add(obj.api_name)
            for field in getattr(step, "values", {}) or {}:
                if field not in obj.active_fields:
                    raise WorkflowError(
                        f"{where}: {obj.api_name!r} has no field {field!r}.",
                        field=where,
                        expected=sorted(obj.active_fields),
                    )
    for i, trigger in enumerate(definition.triggers):
        objects |= _validate_trigger(definition, trigger, f"triggers.{i}", snapshot)
    return sorted(objects)


def _validate_agent(step: AgentStep, where: str, snapshot: SchemaSnapshot) -> set[str]:
    if step.tools and not step.objects:
        raise WorkflowError(
            f"{where}: list the objects the AI step's tools may use.",
            field=where,
            hint='e.g. "objects": ["lead"]',
        )
    if step.objects and not step.tools:
        raise WorkflowError(f"{where}: 'objects' needs 'tools'.", field=where)
    if step.skill is not None and not NAME.fullmatch(step.skill):
        raise WorkflowError(f"{where}: invalid skill name {step.skill!r}.", field=where)
    for name in step.output:
        if not re.fullmatch(r"^[a-z][a-z0-9_]{0,39}$", name):
            raise WorkflowError(
                f"{where}: output field {name!r} must be lowercase_with_underscores.",
                field=where,
            )
    return {snapshot.get_object(o).api_name for o in step.objects}


def output_schema(step: AgentStep) -> dict[str, Any]:
    """The JSON schema an AI step's result must match."""
    props: dict[str, Any] = {}
    for name, f in step.output.items():
        if f.type == "string_list":
            prop: dict[str, Any] = {"type": "array", "items": {"type": "string"}}
        else:
            prop = {"type": f.type}
        if f.enum:
            prop = {**prop, "enum": f.enum} if f.type == "string" else prop
        props[name] = {**prop, "description": f.description}
    return {
        "type": "object",
        "properties": props,
        "required": sorted(step.output),
        "additionalProperties": False,
    }


def _validate_trigger(
    definition: Definition, trigger: Any, where: str, snapshot: SchemaSnapshot
) -> set[str]:
    unknown = sorted(set(trigger.inputs) - set(definition.inputs))
    if unknown:
        raise WorkflowError(
            f"{where} sets unknown inputs {unknown}.",
            field=where,
            expected=sorted(definition.inputs),
        )
    missing = sorted(
        n for n, spec in definition.inputs.items() if spec.required and n not in trigger.inputs
    )
    if missing:
        raise WorkflowError(
            f"{where} must set the required inputs {missing}.",
            field=where,
            hint='e.g. "inputs": {"lead_id": "{{record.id}}"}',
        )
    if isinstance(trigger, ScheduleTrigger):
        _check_schedule(trigger, where)
        for ref in references(trigger.inputs):
            if ref != "schedule.time":
                raise WorkflowError(
                    f"{where} refers to {{{{{ref}}}}}; schedules can use {{{{schedule.time}}}}.",
                    field=where,
                )
        return set()
    obj = snapshot.get_object(trigger.object)
    known = set(obj.active_fields) | {
        "id",
        "created_at",
        "updated_at",
        "created_by",
        "updated_by",
        "owner_id",
        "row_version",
    }
    if trigger.fields and trigger.type != "record_updated":
        raise WorkflowError(f"{where}: 'fields' only applies to record_updated.", field=where)
    for field in trigger.fields:
        if field not in obj.active_fields:
            raise WorkflowError(
                f"{where}: {obj.api_name!r} has no field {field!r}.",
                field=where,
                expected=sorted(obj.active_fields),
            )
    for ref in references([trigger.inputs, [c.model_dump() for c in trigger.when]]):
        root, _, rest = ref.partition(".")
        if (root == "record" and rest.split(".")[0] in known) or ref == "event.changed_fields":
            continue
        raise WorkflowError(
            f"{where} refers to {{{{{ref}}}}}, which is not a field of {obj.api_name!r}.",
            field=where,
            expected=sorted(f"record.{f}" for f in known) + ["event.changed_fields"],
        )
    return {obj.api_name}


def _check_schedule(trigger: ScheduleTrigger, where: str) -> None:
    import datetime as dt
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    from croniter import croniter

    try:
        zone = ZoneInfo(trigger.timezone)
    except (ZoneInfoNotFoundError, ValueError):
        raise WorkflowError(
            f"{where}: unknown time zone {trigger.timezone!r}.",
            field=where,
            expected="an IANA time zone, e.g. 'Europe/London' or 'UTC'",
        ) from None
    if len(trigger.cron.split()) != 5 or not croniter.is_valid(trigger.cron):
        raise WorkflowError(
            f"{where}: {trigger.cron!r} is not a 5-field cron expression.",
            field=where,
            expected="minute hour day-of-month month day-of-week, e.g. '0 9 * * 1-5'",
        )
    it = croniter(trigger.cron, dt.datetime.now(zone))
    times = [it.get_next(dt.datetime) for _ in range(6)]
    shortest = min((b - a).total_seconds() for a, b in zip(times, times[1:], strict=False))
    if shortest < MIN_SCHEDULE_SECONDS:
        raise WorkflowError(
            f"{where}: schedules may run at most every {MIN_SCHEDULE_SECONDS // 60} minutes.",
            field=where,
        )


def next_run(cron: str, timezone: str, after: Any) -> Any:
    """The next time a schedule fires after ``after`` (an aware datetime)."""
    import datetime as dt
    from zoneinfo import ZoneInfo

    from croniter import croniter

    local = after.astimezone(ZoneInfo(timezone))
    return croniter(cron, local).get_next(dt.datetime).astimezone(dt.UTC)
