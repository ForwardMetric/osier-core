"""Safe expressions for workflow data steps and conditions, in CEL (Common Expression
Language), evaluated by ``common-expression-language`` (Apache-2.0, a Rust interpreter).

CEL cannot loop forever, has no side effects, and cannot read anything except the
variables given to it: ``inputs``, ``steps`` (earlier results), and, in a transform
step, ``values`` (values computed earlier in the same step). The CEL string, list, math,
and set extensions are available (e.g. ``split``, ``join``, ``upperAscii``,
``math.round``).

Decimal fields arrive as text (they are stored exactly); convert with ``double(...)``
for arithmetic.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

from ..errors import OsierError

MAX_LENGTH = 2000
MAX_RESULT_BYTES = 100_000
VARIABLES = {"inputs", "steps", "values"}
# Function namespaces of the CEL extensions (e.g. math.round), which the compiler reports
# like variables.
NAMESPACES = {"math", "sets", "lists", "strings", "base64"}


class ExpressionError(OsierError):
    default_code = "EXPRESSION_INVALID"


def _cel() -> Any:
    import cel

    return cel


def check(expression: str, where: str) -> list[str]:
    """Compile an expression (syntax check) and return the variables it uses."""
    if len(expression) > MAX_LENGTH:
        raise ExpressionError(
            f"{where}: expressions are limited to {MAX_LENGTH} characters.", field=where
        )
    try:
        program = _cel().compile(expression)
    except ValueError as exc:
        raise ExpressionError(
            f"{where}: {str(exc).splitlines()[0]}",
            field=where,
            hint="Expressions use CEL syntax, e.g. double(steps.a.record.amount) * 0.2",
        ) from None
    refs = program.references()
    used = list(refs.get("variables", []))
    unknown = sorted(set(used) - VARIABLES - NAMESPACES)
    if unknown:
        raise ExpressionError(
            f"{where}: unknown names {unknown}.", field=where, expected=sorted(VARIABLES)
        )
    return [u for u in used if u in VARIABLES]


def _jsonable(value: Any) -> Any:
    if isinstance(value, dt.datetime | dt.date):
        return value.isoformat()
    if isinstance(value, dt.timedelta):
        return value.total_seconds()
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(v) for v in value]
    return value


def evaluate(expression: str, variables: dict[str, Any], where: str) -> Any:
    cel = _cel()
    from cel import stdlib

    context = cel.Context()
    stdlib.add_stdlib_to_context(context)
    context.update({name: variables.get(name, {}) for name in VARIABLES})
    try:
        result = cel.evaluate(expression, context)
    except (KeyError, ValueError, TypeError, ZeroDivisionError, RuntimeError) as exc:
        message = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
        if isinstance(exc, KeyError):
            message = f"no value {message}"
        raise ExpressionError(
            f"{where}: {message}",
            code="EXPRESSION_FAILED",
            field=where,
            hint="Use has(...) to check for optional values.",
        ) from None
    result = _jsonable(result)
    try:
        encoded = json.dumps(result, default=str, allow_nan=False)
    except ValueError:
        raise ExpressionError(
            f"{where}: the result is not a finite number (for example, a division by zero).",
            code="EXPRESSION_FAILED",
            field=where,
        ) from None
    if len(encoded) > MAX_RESULT_BYTES:
        raise ExpressionError(
            f"{where}: the result is too large.", code="EXPRESSION_FAILED", field=where
        )
    return result
