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


Step = Annotated[
    GetRecord | QueryRecords | CreateRecord | UpdateRecord | Condition | Approval | Finish,
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
]


class Definition(_Model):
    name: str = Field(min_length=1, max_length=64)
    description: str = Field(min_length=1, max_length=1024)
    inputs: dict[str, InputDef] = Field(default_factory=dict, max_length=30)
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
    return sorted(objects)
