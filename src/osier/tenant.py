"""Tenant schema provisioning.

Every tenant lives in its own PostgreSQL schema containing the engine tables
(metadata, change sets, history) and one table per object.
"""

from __future__ import annotations

import uuid

import psycopg
from psycopg import sql

from .context import Tenant
from .identifiers import new_tenant_schema_name, validate_tenant_schema

_ENGINE_DDL = [
    """
    CREATE TABLE {s}.meta_schema_version (
        singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
        version   bigint  NOT NULL
    )
    """,
    "INSERT INTO {s}.meta_schema_version (version) VALUES (0)",
    """
    CREATE TABLE {s}.meta_objects (
        id           uuid PRIMARY KEY,
        api_name     text NOT NULL UNIQUE,
        label        text NOT NULL,
        plural_label text NOT NULL,
        description  text NOT NULL,
        semantic_ref text,
        options      jsonb NOT NULL DEFAULT '{{}}',
        package      text,
        owner        text NOT NULL CHECK (owner IN ('standard', 'custom')),
        status       text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'retired')),
        created_at   timestamptz NOT NULL DEFAULT now(),
        updated_at   timestamptz NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE {s}.meta_fields (
        id            uuid PRIMARY KEY,
        object_id     uuid NOT NULL REFERENCES {s}.meta_objects (id),
        api_name      text NOT NULL,
        label         text NOT NULL,
        description   text NOT NULL,
        semantic_ref  text,
        type          text NOT NULL,
        required      boolean NOT NULL DEFAULT false,
        is_unique     boolean NOT NULL DEFAULT false,
        indexed       boolean NOT NULL DEFAULT false,
        track_history boolean NOT NULL DEFAULT false,
        default_value jsonb,
        options       jsonb NOT NULL DEFAULT '{{}}',
        owner         text NOT NULL CHECK (owner IN ('standard', 'custom')),
        status        text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'retired')),
        index_status  text CHECK (index_status IN ('building', 'active', 'failed')),
        position      integer NOT NULL,
        created_at    timestamptz NOT NULL DEFAULT now(),
        updated_at    timestamptz NOT NULL DEFAULT now(),
        UNIQUE (object_id, api_name)
    )
    """,
    """
    CREATE TABLE {s}.meta_change_sets (
        id                     uuid PRIMARY KEY,
        status                 text NOT NULL
                               CHECK (status IN
                               ('proposed', 'applied', 'failed', 'superseded', 'rejected')),
        source                 text NOT NULL
                               CHECK (source IN ('tenant', 'platform', 'upgrade', 'package')),
        operations             jsonb NOT NULL,
        summary                jsonb NOT NULL,
        ddl                    jsonb NOT NULL,
        impact                 jsonb NOT NULL,
        blockers               jsonb NOT NULL,
        diff_hash              text NOT NULL,
        base_schema_version    bigint NOT NULL,
        applied_schema_version bigint,
        proposed_by            uuid NOT NULL,
        proposed_by_type       text NOT NULL,
        proposed_by_agent      text,
        approved_by            uuid,
        approved_by_type       text,
        post_apply             jsonb,
        error                  jsonb,
        proposed_at            timestamptz NOT NULL DEFAULT now(),
        applied_at             timestamptz
    )
    """,
    """
    CREATE TABLE {s}.meta_packages (
        package                 text PRIMARY KEY,
        version                 integer NOT NULL,
        status                  text NOT NULL CHECK (status IN ('installed', 'uninstalled')),
        change_set_id           uuid NOT NULL,
        definition              jsonb NOT NULL,
        installed_at            timestamptz NOT NULL DEFAULT now(),
        upgraded_at             timestamptz,
        uninstall_change_set_id uuid,
        uninstalled_at          timestamptz
    )
    """,
    """
    CREATE TABLE {s}.meta_idempotency (
        key          text PRIMARY KEY,
        object_id    uuid NOT NULL,
        request_hash text NOT NULL,
        record_id    uuid,
        created_at   timestamptz NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE {s}.field_history (
        id           bigint GENERATED ALWAYS AS IDENTITY,
        object_id    uuid NOT NULL,
        record_id    uuid NOT NULL,
        field_id     uuid NOT NULL,
        old_value    jsonb,
        new_value    jsonb,
        actor_id     uuid NOT NULL,
        actor_type   text NOT NULL,
        on_behalf_of uuid,
        agent_id     text,
        source       text NOT NULL,
        request_id   uuid,
        provenance   jsonb,
        changed_at   timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (id, changed_at)
    ) PARTITION BY RANGE (changed_at)
    """,
    "CREATE TABLE {s}.field_history_default PARTITION OF {s}.field_history DEFAULT",
    "CREATE INDEX ON {s}.field_history (record_id, changed_at)",
    "CREATE INDEX ON {s}.field_history (field_id, changed_at)",
    """
    CREATE TABLE {s}.record_events (
        id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        object_id    uuid NOT NULL,
        record_id    uuid NOT NULL,
        event        text NOT NULL CHECK (event IN ('created', 'retired')),
        actor_id     uuid NOT NULL,
        actor_type   text NOT NULL,
        on_behalf_of uuid,
        agent_id     text,
        source       text NOT NULL,
        request_id   uuid,
        provenance   jsonb,
        occurred_at  timestamptz NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX ON {s}.record_events (record_id, occurred_at)",
    # --- connectors (ARCHITECTURE.md §13.6) ---------------------------------------------
    """
    CREATE TABLE {s}.connector_credentials (
        connection_id     uuid PRIMARY KEY,
        config            jsonb NOT NULL,
        secret_ciphertext bytea,
        key_id            text,
        created_at        timestamptz NOT NULL DEFAULT now(),
        rotated_at        timestamptz,
        revoked_at        timestamptz
    )
    """,
    """
    CREATE TABLE {s}.connector_policies (
        connection_id  uuid PRIMARY KEY,
        approval       text NOT NULL DEFAULT 'required'
                       CHECK (approval IN ('required', 'not_required')),
        max_recipients integer NOT NULL DEFAULT 10 CHECK (max_recipients BETWEEN 1 AND 50),
        tool_policies  jsonb NOT NULL DEFAULT '{{}}',
        updated_at     timestamptz NOT NULL DEFAULT now(),
        updated_by     uuid
    )
    """,
    """
    CREATE TABLE {s}.connector_calls (
        id               uuid PRIMARY KEY,
        connection_id    uuid NOT NULL,
        connector_type   text NOT NULL,
        action           text NOT NULL,
        capability       text,
        purpose          text,
        status           text NOT NULL CHECK (status IN
                         ('awaiting_approval', 'rejected', 'running', 'succeeded', 'failed')),
        summary          text NOT NULL,
        arguments        jsonb NOT NULL,
        result           jsonb,
        error            jsonb,
        recipients       integer,
        actor_id         uuid NOT NULL,
        actor_type       text NOT NULL,
        on_behalf_of     uuid,
        agent_id         text,
        source           text NOT NULL,
        request_id       uuid,
        idempotency_key  text UNIQUE,
        decided_by       uuid,
        created_at       timestamptz NOT NULL DEFAULT now(),
        decided_at       timestamptz,
        completed_at     timestamptz
    )
    """,
    """
    CREATE TABLE {s}.meta_connector_types (
        key          text PRIMARY KEY,
        label        text NOT NULL,
        description  text NOT NULL,
        runtime      text NOT NULL CHECK (runtime IN ('mcp', 'http')),
        transport    jsonb NOT NULL,
        tools        jsonb NOT NULL,
        tools_hash   text NOT NULL,
        status       text NOT NULL DEFAULT 'active'
                     CHECK (status IN ('active', 'needs_review', 'retired')),
        created_by   uuid NOT NULL,
        created_at   timestamptz NOT NULL DEFAULT now(),
        reviewed_by  uuid,
        reviewed_at  timestamptz
    )
    """,
    "CREATE INDEX ON {s}.connector_calls (connection_id, created_at)",
    "CREATE INDEX ON {s}.connector_calls (status) WHERE status = 'awaiting_approval'",
]


# Additive, idempotent changes to engine tables. They run for new tenants and, through
# upgrade_engine_tables, bring tenants created by earlier versions up to date. Never edit
# an entry; append new ones. (Alembic replaces this when the control plane exists.)
_ENGINE_MIGRATIONS = [
    "ALTER TABLE {s}.connector_policies "
    "ADD COLUMN IF NOT EXISTS tool_policies jsonb NOT NULL DEFAULT '{{}}'",
    """
    CREATE TABLE IF NOT EXISTS {s}.meta_connector_types (
        key          text PRIMARY KEY,
        label        text NOT NULL,
        description  text NOT NULL,
        runtime      text NOT NULL CHECK (runtime IN ('mcp', 'http')),
        transport    jsonb NOT NULL,
        tools        jsonb NOT NULL,
        tools_hash   text NOT NULL,
        status       text NOT NULL DEFAULT 'active'
                     CHECK (status IN ('active', 'needs_review', 'retired')),
        created_by   uuid NOT NULL,
        created_at   timestamptz NOT NULL DEFAULT now(),
        reviewed_by  uuid,
        reviewed_at  timestamptz
    )
    """,
    "ALTER TABLE {s}.meta_change_sets DROP CONSTRAINT IF EXISTS meta_change_sets_status_check, "
    "ADD CONSTRAINT meta_change_sets_status_check CHECK (status IN "
    "('proposed', 'applied', 'failed', 'superseded', 'rejected'))",
    """
    CREATE TABLE IF NOT EXISTS {s}.meta_role_permissions (
        role_key    text NOT NULL,
        permission  text NOT NULL,
        granted_at  timestamptz NOT NULL DEFAULT now(),
        granted_by  uuid,
        PRIMARY KEY (role_key, permission)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS {s}.connector_visibility (
        connection_id uuid NOT NULL,
        role_key      text NOT NULL,
        PRIMARY KEY (connection_id, role_key)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS {s}.connector_oauth_states (
        id                  uuid PRIMARY KEY,
        state_hash          text NOT NULL UNIQUE,
        user_id             uuid NOT NULL,
        request             jsonb NOT NULL,
        secret_ciphertext   bytea NOT NULL,
        key_id              text NOT NULL,
        status              text NOT NULL DEFAULT 'pending'
                            CHECK (status IN ('pending', 'authorized', 'completed', 'failed',
                                              'expired')),
        error               jsonb,
        created_at          timestamptz NOT NULL DEFAULT now(),
        expires_at          timestamptz NOT NULL,
        completed_at        timestamptz
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS {s}.meta_engine_migrations (
        name        text PRIMARY KEY,
        applied_at  timestamptz NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS {s}.meta_skills (
        id           uuid PRIMARY KEY,
        name         text NOT NULL UNIQUE,
        description  text NOT NULL,
        body         text NOT NULL,
        frontmatter  jsonb NOT NULL DEFAULT '{{}}',
        license      text,
        version      integer NOT NULL DEFAULT 1,
        status       text NOT NULL DEFAULT 'active'
                     CHECK (status IN ('active', 'disabled', 'retired')),
        visibility   text NOT NULL DEFAULT 'organization'
                     CHECK (visibility IN ('private', 'roles', 'organization')),
        owner_user   uuid,
        source       jsonb NOT NULL,
        created_by   uuid NOT NULL,
        updated_by   uuid NOT NULL,
        created_at   timestamptz NOT NULL DEFAULT now(),
        updated_at   timestamptz NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS {s}.meta_skill_versions (
        skill_id     uuid NOT NULL,
        version      integer NOT NULL,
        name         text NOT NULL,
        description  text NOT NULL,
        body         text NOT NULL,
        frontmatter  jsonb NOT NULL,
        changed_by   uuid NOT NULL,
        changed_by_type text NOT NULL,
        change       text NOT NULL,
        changed_at   timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (skill_id, version)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS {s}.skill_visibility (
        skill_id  uuid NOT NULL,
        role_key  text NOT NULL,
        PRIMARY KEY (skill_id, role_key)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS {s}.meta_workflows (
        id           uuid PRIMARY KEY,
        name         text NOT NULL UNIQUE,
        description  text NOT NULL,
        definition   jsonb NOT NULL,
        version      integer NOT NULL DEFAULT 1,
        status       text NOT NULL DEFAULT 'active'
                     CHECK (status IN ('active', 'disabled', 'retired')),
        created_by   uuid NOT NULL,
        updated_by   uuid NOT NULL,
        created_at   timestamptz NOT NULL DEFAULT now(),
        updated_at   timestamptz NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS {s}.meta_workflow_versions (
        workflow_id     uuid NOT NULL,
        version         integer NOT NULL,
        definition      jsonb NOT NULL,
        changed_by      uuid NOT NULL,
        changed_by_type text NOT NULL,
        change          text NOT NULL,
        changed_at      timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (workflow_id, version)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS {s}.workflow_runs (
        id             uuid PRIMARY KEY,
        workflow_id    uuid NOT NULL,
        workflow_name  text NOT NULL,
        version        integer NOT NULL,
        status         text NOT NULL CHECK (status IN
                       ('running', 'waiting', 'succeeded', 'rejected', 'failed', 'cancelled')),
        inputs         jsonb NOT NULL,
        steps          jsonb NOT NULL DEFAULT '{{}}',
        output         jsonb,
        waiting        jsonb,
        error          jsonb,
        run_as_user    uuid NOT NULL,
        run_as_type    text NOT NULL,
        on_behalf_of   uuid,
        agent_id       text,
        started_at     timestamptz NOT NULL DEFAULT now(),
        updated_at     timestamptz NOT NULL DEFAULT now(),
        finished_at    timestamptz
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS workflow_runs_status ON {s}.workflow_runs (status, started_at)
    """,
    """
    CREATE TABLE IF NOT EXISTS {s}.workflow_run_events (
        id          bigserial PRIMARY KEY,
        run_id      uuid NOT NULL,
        step_id     text,
        event       text NOT NULL,
        detail      jsonb NOT NULL DEFAULT '{{}}',
        actor       uuid,
        actor_type  text,
        at          timestamptz NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS {s}.workflow_checkpoints (
        thread_id             text NOT NULL,
        checkpoint_ns         text NOT NULL DEFAULT '',
        checkpoint_id         text NOT NULL,
        parent_checkpoint_id  text,
        type                  text NOT NULL,
        checkpoint            bytea NOT NULL,
        metadata_type         text NOT NULL,
        metadata              bytea NOT NULL,
        created_at            timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS {s}.meta_ai_settings (
        id          integer PRIMARY KEY CHECK (id = 1),
        settings    jsonb NOT NULL,
        updated_by  uuid NOT NULL,
        updated_at  timestamptz NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS {s}.ai_connections (
        id                 uuid PRIMARY KEY,
        name               text NOT NULL UNIQUE,
        provider           text NOT NULL,
        api_base           text,
        settings           jsonb NOT NULL,
        secret_ciphertext  bytea,
        key_id             text,
        status             text NOT NULL DEFAULT 'active'
                           CHECK (status IN ('active', 'disabled', 'removed')),
        created_by         uuid NOT NULL,
        updated_by         uuid NOT NULL,
        created_at         timestamptz NOT NULL DEFAULT now(),
        updated_at         timestamptz NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS {s}.ai_profiles (
        name           text PRIMARY KEY,
        connection_id  uuid NOT NULL,
        model          text NOT NULL,
        effort         text NOT NULL,
        description    text NOT NULL DEFAULT '',
        updated_by     uuid NOT NULL,
        updated_at     timestamptz NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS {s}.ai_usage (
        id             bigserial PRIMARY KEY,
        run_id         uuid,
        step_id        text,
        connection_id  uuid,
        profile        text,
        model          text NOT NULL,
        input_tokens   integer NOT NULL,
        output_tokens  integer NOT NULL,
        cost_usd       numeric(14, 6),
        at             timestamptz NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS ai_usage_at ON {s}.ai_usage (at)
    """,
    """
    CREATE TABLE IF NOT EXISTS {s}.meta_job_settings (
        id          integer PRIMARY KEY CHECK (id = 1),
        settings    jsonb NOT NULL,
        updated_by  uuid NOT NULL,
        updated_at  timestamptz NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS {s}.workflow_schedules (
        workflow_id    uuid NOT NULL,
        trigger_index  integer NOT NULL,
        cron           text NOT NULL,
        timezone       text NOT NULL,
        run_as_user    uuid NOT NULL,
        next_run_at    timestamptz NOT NULL,
        PRIMARY KEY (workflow_id, trigger_index)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS {s}.workflow_checkpoint_writes (
        thread_id      text NOT NULL,
        checkpoint_ns  text NOT NULL DEFAULT '',
        checkpoint_id  text NOT NULL,
        task_id        text NOT NULL,
        idx            integer NOT NULL,
        channel        text NOT NULL,
        type           text NOT NULL,
        blob           bytea NOT NULL,
        task_path      text NOT NULL DEFAULT '',
        PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id, task_id, idx)
    )
    """,
]


# One-time schema changes to existing engine tables, recorded in meta_engine_migrations.
# Append only; each runs once per tenant, in order.
_SCHEMA_ONCE: list[tuple[str, list[str]]] = [
    (
        "2026-11-record-events-updates-and-dispatch",
        [
            "ALTER TABLE {s}.record_events DROP CONSTRAINT IF EXISTS record_events_event_check",
            "ALTER TABLE {s}.record_events ADD CONSTRAINT record_events_event_check "
            "CHECK (event IN ('created', 'updated', 'retired'))",
            "ALTER TABLE {s}.record_events ADD COLUMN IF NOT EXISTS changed_fields text[]",
            # Existing events count as already dispatched, so old changes never fire
            # new triggers; new events start undispatched.
            "ALTER TABLE {s}.record_events ADD COLUMN IF NOT EXISTS dispatched_at timestamptz "
            "DEFAULT now()",
            "ALTER TABLE {s}.record_events ALTER COLUMN dispatched_at DROP DEFAULT",
            "CREATE INDEX IF NOT EXISTS record_events_undispatched ON {s}.record_events (id) "
            "WHERE dispatched_at IS NULL",
        ],
    ),
    (
        "2026-11-workflow-runs-queued",
        [
            "ALTER TABLE {s}.workflow_runs DROP CONSTRAINT IF EXISTS workflow_runs_status_check",
            "ALTER TABLE {s}.workflow_runs ADD CONSTRAINT workflow_runs_status_check CHECK "
            "(status IN ('queued', 'running', 'waiting', 'succeeded', 'rejected', 'failed', "
            "'cancelled'))",
            "ALTER TABLE {s}.workflow_runs ADD COLUMN IF NOT EXISTS trigger jsonb",
            "ALTER TABLE {s}.workflow_runs ADD COLUMN IF NOT EXISTS deferred_at timestamptz",
            "ALTER TABLE {s}.workflow_runs ADD COLUMN IF NOT EXISTS resume_with jsonb",
        ],
    ),
]


def _run_schema_once(conn: psycopg.Connection, tenant: Tenant) -> None:
    done_table = sql.Identifier(tenant.schema, "meta_engine_migrations")
    schema = sql.Identifier(tenant.schema)
    for name, statements in _SCHEMA_ONCE:
        claimed = conn.execute(
            sql.SQL(
                "INSERT INTO {} (name) VALUES (%s) ON CONFLICT DO NOTHING RETURNING name"
            ).format(done_table),
            (name,),
        ).fetchone()
        if claimed is None:
            continue
        for statement in statements:
            conn.execute(sql.SQL(statement).format(s=schema))


# One-time data migrations, recorded in meta_engine_migrations so a tenant's later
# changes (e.g. removing a granted permission) are never undone. Append only.
_DATA_MIGRATIONS: list[tuple[str, list[tuple[str, str]]]] = [
    (
        "2026-11-grant-skills-tools-workflows",
        [
            (role, permission)
            for role in ("admin", "developer")
            for permission in ("skills.manage", "tools.manage", "workflows.manage")
        ],
    ),
    (
        "2026-11-grant-jobs-manage",
        [(role, "jobs.manage") for role in ("admin", "developer")],
    ),
    ("2026-11-grant-ai-manage", [("admin", "ai.manage")]),
]


def _run_data_migrations(conn: psycopg.Connection, tenant: Tenant) -> None:
    done_table = sql.Identifier(tenant.schema, "meta_engine_migrations")
    grants = sql.Identifier(tenant.schema, "meta_role_permissions")
    for name, rows in _DATA_MIGRATIONS:
        claimed = conn.execute(
            sql.SQL(
                "INSERT INTO {} (name) VALUES (%s) ON CONFLICT DO NOTHING RETURNING name"
            ).format(done_table),
            (name,),
        ).fetchone()
        if claimed is None:
            continue
        for role, permission in rows:
            conn.execute(
                sql.SQL(
                    "INSERT INTO {} (role_key, permission) VALUES (%s, %s) ON CONFLICT DO NOTHING"
                ).format(grants),
                (role, permission),
            )


def upgrade_engine_tables(conn: psycopg.Connection, tenant: Tenant) -> None:
    """Apply engine table migrations to an existing tenant. Safe to run repeatedly.

    Default role permissions are only inserted when the tenant has none yet, so a
    tenant's own changes to role permissions are never overwritten."""
    schema = sql.Identifier(tenant.schema)
    with conn.transaction():
        for statement in _ENGINE_MIGRATIONS:
            conn.execute(sql.SQL(statement).format(s=schema))
        has_permissions = conn.execute(
            sql.SQL("SELECT EXISTS (SELECT 1 FROM {})").format(
                sql.Identifier(tenant.schema, "meta_role_permissions")
            )
        ).fetchone()
        if has_permissions and not has_permissions[0]:
            from .authz import DEFAULT_ROLE_PERMISSIONS

            for role, permissions in DEFAULT_ROLE_PERMISSIONS.items():
                for permission in permissions:
                    conn.execute(
                        sql.SQL(
                            "INSERT INTO {} (role_key, permission) VALUES (%s, %s) "
                            "ON CONFLICT DO NOTHING"
                        ).format(sql.Identifier(tenant.schema, "meta_role_permissions")),
                        (role, permission),
                    )
        _run_schema_once(conn, tenant)
        _run_data_migrations(conn, tenant)
    from .jobs import install_queue

    install_queue(conn, tenant)


def provision_tenant(conn: psycopg.Connection, tenant_id: uuid.UUID | None = None) -> Tenant:
    """Create a new tenant schema with the engine tables. Returns its location."""
    tenant = Tenant(tenant_id=tenant_id or uuid.uuid4(), schema=new_tenant_schema_name())
    schema = sql.Identifier(tenant.schema)
    with conn.transaction():
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(schema))
        for statement in _ENGINE_DDL:
            conn.execute(sql.SQL(statement).format(s=schema))
    upgrade_engine_tables(conn, tenant)
    return tenant


def drop_tenant(conn: psycopg.Connection, tenant: Tenant) -> None:
    """Irreversibly delete a tenant schema. Intended for tests and tooling only."""
    validate_tenant_schema(tenant.schema)
    from .jobs import drop_queue

    drop_queue(conn, tenant)
    conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(tenant.schema)))
