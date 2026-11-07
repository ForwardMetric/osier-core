"""Background jobs: a Postgres queue in each tenant schema, and the worker that runs it.

- A job is a platform handler (``HANDLERS``) plus a JSON payload. Tenants never
  supply code; they cause jobs (e.g. workflow runs started by triggers).
- Every job carries the principal it runs as and runs through a ``Session`` for
  that principal, so the usual permission checks apply.
- Workers claim jobs with ``FOR UPDATE SKIP LOCKED`` and a lease. A job whose worker
  disappears is claimed again after the lease expires. Unexpected errors retry with
  backoff up to ``max_attempts``; ``OsierError`` failures are final.
- Each worker tick also dispatches workflow triggers (record events, schedules).

The queue lives in the tenant schema for now. A central queue with cross-tenant
fairness comes with the control plane (ARCHITECTURE.md §12.1, §3.4).
"""

from __future__ import annotations

import logging
import socket
import time
import traceback
import uuid
from collections.abc import Callable, Iterable
from typing import Any

from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .context import ActorType, Principal
from .errors import NotFoundError, OsierError

log = logging.getLogger("osier.jobs")

LEASE_SECONDS = 300
MAX_RUNNING_PER_TENANT = 5
BACKOFF_SECONDS = (10, 60, 300)

Handler = Callable[[Any, dict[str, Any], dict[str, Any]], Any]


def _workflow_execute(session: Any, payload: dict[str, Any], job: dict[str, Any]) -> Any:
    return session.workflows.execute_job(payload["run_id"], payload.get("decision"))


def _workflow_abandoned(session: Any, payload: dict[str, Any], error: dict[str, Any]) -> None:
    session.workflows.mark_failed(payload["run_id"], error)


HANDLERS: dict[str, Handler] = {"workflow.execute": _workflow_execute}
# Called when a job fails for good, so the thing it was doing is marked failed too.
ON_GIVE_UP: dict[str, Callable[[Any, dict[str, Any], dict[str, Any]], None]] = {
    "workflow.execute": _workflow_abandoned
}


def principal_columns(p: Principal) -> tuple[Any, ...]:
    return (p.user_id, p.actor_type.value, p.on_behalf_of, p.agent_id)


def principal_of(row: dict[str, Any], source: str) -> Principal:
    return Principal(
        user_id=row["run_as_user"],
        actor_type=ActorType(row["run_as_type"]),
        on_behalf_of=row["on_behalf_of"],
        agent_id=row["agent_id"],
        source=source,
    )


class JobService:
    def __init__(self, session: Any) -> None:
        self._session = session
        self._conn = session.conn
        self._schema = session.tenant.schema

    def _t(self, name: str = "jobs") -> sql.Identifier:
        return sql.Identifier(self._schema, name)

    def enqueue(
        self,
        kind: str,
        payload: dict[str, Any],
        *,
        principal: Principal,
        source: str = "job",
        delay_seconds: float = 0,
        idempotency_key: str | None = None,
        max_attempts: int = 3,
    ) -> uuid.UUID:
        if kind not in HANDLERS:
            raise OsierError(f"Unknown job kind {kind!r}.", code="JOB_KIND_UNKNOWN")
        job_id = uuid.uuid4()
        row = self._conn.execute(
            sql.SQL(
                "INSERT INTO {} (id, kind, payload, run_after, idempotency_key, max_attempts, "
                "run_as_user, run_as_type, on_behalf_of, agent_id, source) "
                "VALUES (%s, %s, %s, now() + make_interval(secs => %s), %s, %s, %s, %s, %s, "
                "%s, %s) ON CONFLICT (idempotency_key) DO NOTHING RETURNING id"
            ).format(self._t()),
            (
                job_id,
                kind,
                Jsonb(payload),
                delay_seconds,
                idempotency_key,
                max_attempts,
                *principal_columns(principal),
                source,
            ),
        ).fetchone()
        if row is None:  # already queued under this key
            existing = self._conn.execute(
                sql.SQL("SELECT id FROM {} WHERE idempotency_key = %s").format(self._t()),
                (idempotency_key,),
            ).fetchone()
            return existing[0]
        return job_id

    def list(self, *, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        self._session.authz.require("workflows.manage", "viewing background jobs")
        statement = sql.SQL(
            "SELECT id, kind, payload, status, attempts, max_attempts, run_after, last_error, "
            "source, created_at, finished_at FROM {}"
        ).format(self._t())
        params: list[Any] = []
        if status:
            statement += sql.SQL(" WHERE status = %s")
            params.append(status)
        statement += sql.SQL(" ORDER BY created_at DESC LIMIT %s")
        params.append(max(1, min(limit, 200)))
        with self._conn.cursor(row_factory=dict_row) as cur:
            rows = cur.execute(statement, params).fetchall()
        import json

        return json.loads(json.dumps(rows, default=str))

    def get(self, job_id: Any) -> dict[str, Any]:
        with self._conn.cursor(row_factory=dict_row) as cur:
            row = cur.execute(
                sql.SQL("SELECT * FROM {} WHERE id = %s").format(self._t()), (job_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError(f"No job {job_id}.", code="JOB_NOT_FOUND")
        return row

    # ---- worker side ------------------------------------------------------------------

    def claim(self, worker_id: str) -> dict[str, Any] | None:
        with self._conn.cursor(row_factory=dict_row) as cur:
            return cur.execute(
                sql.SQL(
                    "UPDATE {jobs} SET status = 'running', attempts = attempts + 1, "
                    "locked_by = %s, locked_until = now() + make_interval(secs => %s), "
                    "started_at = COALESCE(started_at, now()) "
                    "WHERE id = (SELECT id FROM {jobs} WHERE "
                    "((status = 'queued' AND run_after <= now()) "
                    "OR (status = 'running' AND locked_until < now())) "
                    "AND (SELECT count(*) FROM {jobs} WHERE status = 'running' "
                    "AND locked_until >= now()) < %s "
                    "ORDER BY priority DESC, run_after LIMIT 1 FOR UPDATE SKIP LOCKED) "
                    "RETURNING *"
                ).format(jobs=self._t()),
                (worker_id, LEASE_SECONDS, MAX_RUNNING_PER_TENANT),
            ).fetchone()

    def _set(
        self,
        job_id: uuid.UUID,
        status: str,
        error: dict[str, Any] | None = None,
        retry_in: float | None = None,
    ) -> None:
        if retry_in is not None:
            self._conn.execute(
                sql.SQL(
                    "UPDATE {} SET status = 'queued', last_error = %s, locked_by = NULL, "
                    "locked_until = NULL, run_after = now() + make_interval(secs => %s) "
                    "WHERE id = %s"
                ).format(self._t()),
                (Jsonb(error), retry_in, job_id),
            )
            return
        self._conn.execute(
            sql.SQL(
                "UPDATE {} SET status = %s, last_error = %s, locked_by = NULL, "
                "locked_until = NULL, finished_at = now() WHERE id = %s"
            ).format(self._t()),
            (status, Jsonb(error) if error else None, job_id),
        )

    def run(self, job: dict[str, Any]) -> str:
        """Run a claimed job. Returns its new status."""
        handler = HANDLERS.get(job["kind"])
        session = self._session.as_principal(principal_of(job, job["source"]))
        if job["attempts"] > job["max_attempts"]:  # its worker was lost too often
            error = {"code": "JOB_ABANDONED", "message": "The job's worker stopped repeatedly."}
            self._give_up(job, session, error)
            return "failed"
        if handler is None:
            self._set(job["id"], "failed", {"code": "JOB_KIND_UNKNOWN", "message": job["kind"]})
            return "failed"
        try:
            handler(session, job["payload"], job)
        except OsierError as exc:
            self._give_up(job, session, exc.to_dict())
            return "failed"
        except Exception as exc:  # noqa: BLE001 - unexpected: retry with backoff
            log.exception("job %s (%s) failed", job["id"], job["kind"])
            error = {
                "code": "JOB_ERROR",
                "message": f"{type(exc).__name__}: {exc}",
                "trace": traceback.format_exc(limit=5),
            }
            if job["attempts"] < job["max_attempts"]:
                delay = BACKOFF_SECONDS[min(job["attempts"] - 1, len(BACKOFF_SECONDS) - 1)]
                self._set(job["id"], "queued", error, retry_in=delay)
                return "queued"
            self._give_up(job, session, error)
            return "failed"
        self._set(job["id"], "succeeded")
        return "succeeded"

    def _give_up(self, job: dict[str, Any], session: Any, error: dict[str, Any]) -> None:
        self._set(job["id"], "failed", error)
        on_give_up = ON_GIVE_UP.get(job["kind"])
        if on_give_up:
            try:
                on_give_up(session, job["payload"], error)
            except Exception:  # noqa: BLE001
                log.exception("could not record the failure of job %s", job["id"])


class Worker:
    """Runs jobs and dispatches triggers for one or more tenants.

    ``sessions`` yields a system ``Session`` per tenant on each tick."""

    def __init__(self, sessions: Callable[[], Iterable[Any]], worker_id: str | None = None):
        self._sessions = sessions
        self.worker_id = worker_id or f"{socket.gethostname()}:{uuid.uuid4().hex[:8]}"

    def tick(self, *, max_jobs: int = 50) -> dict[str, int]:
        """Dispatch triggers and run due jobs once. Returns counts."""
        from .workflows.triggers import dispatch_record_events, dispatch_schedules

        counts = {"events": 0, "scheduled": 0, "jobs": 0}
        for session in self._sessions():
            counts["events"] += dispatch_record_events(session)
            counts["scheduled"] += dispatch_schedules(session)
            jobs = JobService(session)
            for _ in range(max_jobs):
                job = jobs.claim(self.worker_id)
                if job is None:
                    break
                jobs.run(job)
                counts["jobs"] += 1
        return counts

    def run_until_idle(self, *, max_ticks: int = 100) -> dict[str, int]:
        total = {"events": 0, "scheduled": 0, "jobs": 0}
        for _ in range(max_ticks):
            counts = self.tick()
            for k, v in counts.items():
                total[k] += v
            if not any(counts.values()):
                break
        return total

    def run_forever(self, *, poll_seconds: float = 2.0) -> None:  # pragma: no cover
        log.info("worker %s started", self.worker_id)
        while True:
            try:
                counts = self.tick()
            except Exception:  # noqa: BLE001 - keep the worker alive
                log.exception("worker tick failed")
                counts = {}
            if not any(counts.values()):
                time.sleep(poll_seconds)
