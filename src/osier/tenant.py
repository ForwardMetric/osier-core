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
                               CHECK (status IN ('proposed', 'applied', 'failed', 'superseded')),
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
]


def provision_tenant(conn: psycopg.Connection, tenant_id: uuid.UUID | None = None) -> Tenant:
    """Create a new tenant schema with the engine tables. Returns its location."""
    tenant = Tenant(tenant_id=tenant_id or uuid.uuid4(), schema=new_tenant_schema_name())
    schema = sql.Identifier(tenant.schema)
    with conn.transaction():
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(schema))
        for statement in _ENGINE_DDL:
            conn.execute(sql.SQL(statement).format(s=schema))
    return tenant


def drop_tenant(conn: psycopg.Connection, tenant: Tenant) -> None:
    """Irreversibly delete a tenant schema. Intended for tests and tooling only."""
    validate_tenant_schema(tenant.schema)
    conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(tenant.schema)))
