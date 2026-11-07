"""A LangGraph checkpointer that stores checkpoints in the tenant's own schema.

LangGraph's own Postgres checkpointer builds SQL from plain strings with
unqualified table names and relies on ``search_path``, so it cannot pass the
tenant isolation guard (``db.TenantConnection``). This saver implements LangGraph's
public ``BaseCheckpointSaver`` interface on top of ``TenantConnection`` instead.

Each checkpoint is stored whole (channel values included) in
``workflow_checkpoints``; pending writes go to ``workflow_checkpoint_writes``.
Workflow state is small, so the simplicity is worth more than de-duplicating
channel values across checkpoints as LangGraph's own savers do.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    WRITES_IDX_MAP,
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
    get_checkpoint_id,
    get_checkpoint_metadata,
)
from psycopg import sql
from psycopg.rows import dict_row

from ..db import TenantConnection


class TenantCheckpointSaver(BaseCheckpointSaver[str]):
    def __init__(self, conn: TenantConnection) -> None:
        super().__init__()
        self._conn = conn
        self._schema = conn.tenant.schema

    def _t(self, name: str) -> sql.Identifier:
        return sql.Identifier(self._schema, name)

    @staticmethod
    def _ids(config: RunnableConfig) -> tuple[str, str]:
        c = config["configurable"]
        return c["thread_id"], c.get("checkpoint_ns", "")

    def _tuple(self, row: dict[str, Any]) -> CheckpointTuple:
        thread_id, ns, cid = row["thread_id"], row["checkpoint_ns"], row["checkpoint_id"]
        with self._conn.cursor(row_factory=dict_row) as cur:
            writes = cur.execute(
                sql.SQL(
                    "SELECT task_id, channel, type, blob FROM {} WHERE thread_id = %s "
                    "AND checkpoint_ns = %s AND checkpoint_id = %s ORDER BY task_id, idx"
                ).format(self._t("workflow_checkpoint_writes")),
                (thread_id, ns, cid),
            ).fetchall()

        def cfg(checkpoint_id: str) -> RunnableConfig:
            return {
                "configurable": {
                    "thread_id": thread_id,
                    "checkpoint_ns": ns,
                    "checkpoint_id": checkpoint_id,
                }
            }

        return CheckpointTuple(
            config=cfg(cid),
            checkpoint=self.serde.loads_typed((row["type"], bytes(row["checkpoint"]))),
            metadata=self.serde.loads_typed((row["metadata_type"], bytes(row["metadata"]))),
            parent_config=cfg(row["parent_checkpoint_id"]) if row["parent_checkpoint_id"] else None,
            pending_writes=[
                (w["task_id"], w["channel"], self.serde.loads_typed((w["type"], bytes(w["blob"]))))
                for w in writes
            ],
        )

    def get_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        thread_id, ns = self._ids(config)
        checkpoint_id = get_checkpoint_id(config)
        where = sql.SQL("thread_id = %s AND checkpoint_ns = %s")
        params: list[Any] = [thread_id, ns]
        if checkpoint_id:
            where += sql.SQL(" AND checkpoint_id = %s")
            params.append(checkpoint_id)
        with self._conn.cursor(row_factory=dict_row) as cur:
            row = cur.execute(
                sql.SQL("SELECT * FROM {} WHERE {} ORDER BY checkpoint_id DESC LIMIT 1").format(
                    self._t("workflow_checkpoints"), where
                ),
                params,
            ).fetchone()
        return self._tuple(row) if row else None

    def list(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> Iterator[CheckpointTuple]:
        conditions: list[sql.Composable] = []
        params: list[Any] = []
        if config:
            thread_id, ns = self._ids(config)
            conditions.append(sql.SQL("thread_id = %s"))
            params.append(thread_id)
            if "checkpoint_ns" in config["configurable"]:
                conditions.append(sql.SQL("checkpoint_ns = %s"))
                params.append(ns)
            if checkpoint_id := get_checkpoint_id(config):
                conditions.append(sql.SQL("checkpoint_id = %s"))
                params.append(checkpoint_id)
        if before and (before_id := get_checkpoint_id(before)):
            conditions.append(sql.SQL("checkpoint_id < %s"))
            params.append(before_id)
        statement = sql.SQL("SELECT * FROM {}").format(self._t("workflow_checkpoints"))
        if conditions:
            statement += sql.SQL(" WHERE ") + sql.SQL(" AND ").join(conditions)
        statement += sql.SQL(" ORDER BY checkpoint_id DESC")
        with self._conn.cursor(row_factory=dict_row) as cur:
            rows = cur.execute(statement, params).fetchall()
        count = 0
        for row in rows:
            item = self._tuple(row)
            if filter and any(item.metadata.get(k) != v for k, v in filter.items()):
                continue
            if limit is not None and count >= limit:
                return
            count += 1
            yield item

    def put(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        thread_id, ns = self._ids(config)
        ctype, cblob = self.serde.dumps_typed(checkpoint)
        mtype, mblob = self.serde.dumps_typed(get_checkpoint_metadata(config, metadata))
        self._conn.execute(
            sql.SQL(
                "INSERT INTO {} (thread_id, checkpoint_ns, checkpoint_id, parent_checkpoint_id, "
                "type, checkpoint, metadata_type, metadata) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (thread_id, checkpoint_ns, checkpoint_id) DO UPDATE SET "
                "type = %s, checkpoint = %s, metadata_type = %s, metadata = %s"
            ).format(self._t("workflow_checkpoints")),
            (
                thread_id,
                ns,
                checkpoint["id"],
                config["configurable"].get("checkpoint_id"),
                ctype,
                cblob,
                mtype,
                mblob,
                ctype,
                cblob,
                mtype,
                mblob,
            ),
        )
        return {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": ns,
                "checkpoint_id": checkpoint["id"],
            }
        }

    def put_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        thread_id, ns = self._ids(config)
        checkpoint_id = config["configurable"]["checkpoint_id"]
        for idx, (channel, value) in enumerate(writes):
            index = WRITES_IDX_MAP.get(channel, idx)
            vtype, blob = self.serde.dumps_typed(value)
            # Special writes (errors, interrupts) replace; regular writes are kept once.
            if index < 0:
                conflict = sql.SQL("DO UPDATE SET channel = %s, type = %s, blob = %s")
                extra: tuple[Any, ...] = (channel, vtype, blob)
            else:
                conflict, extra = sql.SQL("DO NOTHING"), ()
            self._conn.execute(
                sql.SQL(
                    "INSERT INTO {} (thread_id, checkpoint_ns, checkpoint_id, task_id, idx, "
                    "channel, type, blob, task_path) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (thread_id, checkpoint_ns, checkpoint_id, task_id, idx) {}"
                ).format(self._t("workflow_checkpoint_writes"), conflict),
                (
                    thread_id,
                    ns,
                    checkpoint_id,
                    task_id,
                    index,
                    channel,
                    vtype,
                    blob,
                    task_path,
                    *extra,
                ),
            )

    def delete_thread(self, thread_id: str) -> None:
        for table in ("workflow_checkpoints", "workflow_checkpoint_writes"):
            self._conn.execute(
                sql.SQL("DELETE FROM {} WHERE thread_id = %s").format(self._t(table)),
                (thread_id,),
            )
