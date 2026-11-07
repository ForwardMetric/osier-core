"""The engine's single entry point for executing a queued workflow run. Queue backends
(and, later, a separate job service) call this; it is the only thing they need from
the engine."""

from __future__ import annotations

from collections.abc import Callable

import psycopg

from ..context import Principal, Tenant

Connect = Callable[[], psycopg.Connection]


def execute_run(
    connect: Connect, tenant: Tenant, run_id: str, *, attempt: int, max_attempts: int
) -> None:
    """Execute (or resume) a run as the principal it belongs to.

    Raises on unexpected errors so the backend can retry; on the last attempt the run is
    marked failed first. Errors inside workflow steps are recorded on the run and do not
    raise. Safe to call again for the same run: it continues from the last checkpoint.
    """
    from ..session import Session

    with connect() as conn:
        system = Session(conn, tenant, Principal.system("worker")).workflows
        runner = Session(conn, tenant, system.run_principal(run_id))
        try:
            runner.workflows.execute_job(run_id)
        except Exception as exc:
            if attempt >= max_attempts:
                system.mark_failed(
                    run_id,
                    {
                        "code": "JOB_FAILED",
                        "message": f"{type(exc).__name__}: {exc}",
                        "attempts": attempt,
                    },
                )
            raise
