# osier-core — Architecture

> Status: **Draft v0.1** — decisions agreed in planning; open questions in §15.
> This document is the source of truth for design decisions. Changes to a
> decision should update this file in the same pull request as the code.

---

## 1. What we are building

A lightweight, **headless, agent-native** business platform in the spirit of
Salesforce: it ships with **standard objects** (Account, Contact, Opportunity, …)
and lets each customer **create new objects and modify existing ones** to fit
their business.

There is no traditional application UI. People interact through **AI chat**:

- Administrators ask the AI to create objects, add fields, and change permissions.
- Users ask the AI to read, write, and join data across objects.
- Users ask the AI to build the screens they want (tables, forms, boards, charts).
  These are delivered as **MCP Apps** rendered inside the chat.

Everything is exposed through a **Model Context Protocol (MCP) server**. Any
MCP-capable client (Claude, ChatGPT, others) can connect; our own lightweight
login + chat page is just one more client.

### Design principles

1. **The engine is the safety boundary.** The AI is never trusted to enforce
   rules. Every validation, permission check, and integrity constraint lives in
   the engine and the database.
2. **Customization is data, never code.** Tenant customizations are metadata.
   Nobody patches platform code for a single tenant. This is what keeps
   upgrades possible (see §11).
3. **Real tables, real constraints.** Objects compile to real PostgreSQL tables
   and columns, so integrity, joins, and reporting use the database as designed.
4. **High-impact changes are two-phase.** Schema changes, permission changes,
   and bulk destructive operations are *proposed*, then *confirmed by a human*
   through a mechanism the model cannot forge.
5. **Everything is attributable.** Every change records who made it, on whose
   behalf, through which agent, and from what source.
6. **Use mature components for undifferentiated work.** We do not build
   authentication, a query planner, or a chat model.

---

## 2. High-level architecture

```
  Claude / ChatGPT / other MCP hosts        osier chat page (later)
                 │                                   │
                 └──────────────┬────────────────────┘
                                │  MCP over HTTPS, OAuth 2.1
  ┌─────────────────────────────▼──────────────────────────────────┐
  │ MCP SERVER                                                      │
  │  tools · two-phase confirmations · MCP App UI resources         │
  │  trusted view renderer (table / form / kanban / chart / detail) │
  ├─────────────────────────────────────────────────────────────────┤
  │ ENGINE  (Python library; the safety boundary)                   │
  │  metadata registry ─► DDL compiler ─► change-set executor       │
  │  record service (validation, history, provenance)               │
  │  permission service (object / field / record level)             │
  │  query & report compiler · view-spec validator                  │
  ├─────────────────────────────────────────────────────────────────┤
  │ CONTROL PLANE                                                   │
  │  tenants · provisioning · routing · upgrades · backups          │
  └───────────────┬───────────────────────────────┬─────────────────┘
                  │                               │
        control-plane database          tenant schemas: many per shared
                                        database, or one per dedicated DB
                                                  │
                         later: CDC ─► columnar analytics store
```

External services: an OIDC identity provider (§9), object storage for files,
and (later) an analytics store.

---

## 3. Tenancy model

**Decision: every tenant lives in its own PostgreSQL schema. A database holds
one or more tenant schemas.** The control plane records where each tenant is
and routes requests there.

| Placement | Database holds | Typical use |
|---|---|---|
| **Shared** | Many tenant schemas (up to a configured cap per database) | Free and small tiers |
| **Dedicated** | Exactly one tenant schema | Paid tiers needing isolation, enterprise |

Because a tenant is *always* a schema, both placements run **the same code**.
Moving a tenant from shared to dedicated is relocating one schema to a new
database (dump/restore of that schema, or logical replication for minimal
downtime), followed by a routing switch in the control plane.

A tenant's location is `(cluster, database, schema)`. The engine receives it
with every request and never assumes a fixed location.

### 3.1 Safety rules for shared databases

Schema-per-tenant has two known failure modes. Both are designed out:

1. **Cross-tenant leaks through `search_path`.** With transaction-mode
   connection pooling, a session-level `SET search_path` can carry over to the
   next tenant's transaction on the same server connection. **Rule: the engine
   never relies on `search_path`.** All SQL is generated with fully qualified
   names (`"t_7f3a…"."c__loan"`). Hand-written SQL that uses unqualified names
   is rejected in review and by a test.
2. **Defense in depth with database roles.** Each tenant schema has its own
   database role with privileges only on that schema. Each transaction runs
   `SET LOCAL ROLE <tenant_role>` (transaction-scoped, so safe with pooling).
   A bug that targets the wrong schema then fails with a permission error
   instead of reading another tenant's data.

### 3.2 Scaling shared databases

- All tenants' tables in one database share its system catalog. Very large
  catalogs slow down backups, planning metadata lookups, and per-connection
  memory. **There is no documented hard limit**; problems at hundreds of
  thousands of tables are widely reported, but we have no reliable threshold.
- Therefore each shared database has a **cap on tenant schemas** (initial
  value to be set from benchmarks in M5). When a database reaches the cap,
  the control plane places new tenants in a new shared database.
- Monitor catalog size, `pg_dump` duration, and connection memory per shared
  database.

### 3.3 Why tenant count is not a blocker now

Starting with one shared database is enough for early growth. Because
placement is a control-plane decision, scaling means adding databases, not
changing the design. The open question is only the per-database cap, which
benchmarks will answer.

### 3.4 Control plane

The **control-plane database** holds organizations, users-to-tenant mapping,
plans, tenant placement `(cluster, database, schema)`, schema versions per
tenant, and upgrade state. It never holds tenant business data.

### 3.5 Managed PostgreSQL

We use a managed PostgreSQL provider rather than running servers ourselves.
Requirements for any provider:

- Full DDL rights, including `CREATE SCHEMA`, `CREATE ROLE`, and
  `CREATE INDEX CONCURRENTLY`.
- Logical replication (tenant moves now, analytics later).
- A connection pooler that supports transaction mode.
- An API to create databases (for dedicated tenants).

Candidates: **Supabase** (managed Postgres with pooling; we would use it as
plain Postgres, not its schema-centric API features), **Neon** (serverless,
scales idle databases to zero — attractive for many dedicated tenants),
AWS RDS/Aurora, Google Cloud SQL. The engine uses only standard PostgreSQL so
the provider can be changed. Provider choice for dedicated tenants depends on
cost per database, which must be checked against current pricing.

---

## 4. Storage model: metadata compiled to real tables

**Decision: every object is a real table; every field is a real column.**
Metadata describes all objects; the DDL compiler turns metadata changes into
DDL.

Alternatives considered and rejected:

| Option | Why not |
|---|---|
| Universal JSONB "heap" table | Weak integrity, no planner statistics per field, GIN does not serve range/sort, reporting is slow; its main advantage (instant self-service schema changes across thousands of tenants in shared tables) does not outweigh this for us. |
| Flex columns (Salesforce-style) | Everything stored as text, needs pivot tables for indexes; clunky. |
| EAV (entity-attribute-value) | Reading a record means pivoting many rows; painful to query and report on. |

Evidence that this works: Frappe, Odoo, Twenty, Directus, Baserow, and
EspoCRM all compile metadata to real tables.

### 4.1 Metadata tables (per tenant database)

| Table | Purpose |
|---|---|
| `meta_objects` | Object definitions: stable `id`, `api_name`, label, owner (`standard` / `custom`), status (`active` / `retired`). |
| `meta_fields` | Field definitions: stable `id`, object, `api_name`, type, required, default, unique, indexed, `track_history`, owner, status, type-specific options (picklist values, precision, …). |
| `meta_relationships` | Lookup (many-to-one) and many-to-many definitions, delete behavior (`restrict` / `cascade` / `set_null`). |
| `meta_schema_versions` | Monotonic schema version per tenant; bumped by every applied change set. |
| `meta_change_sets` | Every proposed and applied schema change (§5). Append-only. |

### 4.2 Naming and identifiers

- Every object and field has a **stable internal ID** (UUID). History, views,
  permissions, and reports reference IDs, never names, so renames are safe.
- **Physical names are generated by the engine**, never taken verbatim from user
  or AI input. They must match `^[a-z][a-z0-9_]{0,47}$`, must not be reserved
  words, and are always quoted by the SQL layer. User-supplied strings are never
  interpolated into SQL.
- **Namespacing:** custom objects and fields get a `c__` prefix
  (`c__net_worth_statement`, `c__region`). Platform upgrades can therefore add
  standard fields without colliding with tenant customizations.
- **Platform-owned names never look custom:** standard objects, standard
  fields, system columns, and engine tables may not start with `c_` and may
  never contain `__`. A test checks every system column, engine table, and
  package definition against this rule.

### 4.3 Field limit

Each object may have at most **500 fields** (configurable later). Retired
fields count towards the limit because their columns and data are kept.
PostgreSQL's hard limit is 1,600 columns per table. A permanent purge of
retired columns (respecting retention) is future work.

### 4.4 Field types

Implemented: `text`, `long_text`, `integer`, `decimal(precision, scale)`,
`boolean`, `date`, `datetime` (time zone required), `email`, `url`,
`picklist`, `lookup` (foreign key).

Options:

- `text` may declare a built-in **format**: `currency_code` (ISO 4217),
  `country_code` (ISO 3166-1 alpha-2), `phone`, `language_tag` (BCP 47),
  `time_zone` (IANA). Tenants cannot supply their own regular expressions
  (this avoids patterns with catastrophic backtracking).
- `integer` and `decimal` may declare `min` and `max`.
- Picklist values may carry a `description` explaining what each value means.

Planned: `multi_picklist`, `json` (escape hatch, not indexable/reportable by
default).

Every type maps to a fixed PostgreSQL column type and a validator (§6.1).
Domain rules (formats, picklist values, bounds) are currently enforced by the
engine only, not by database `CHECK` constraints, so adding a picklist value
needs no DDL. Direct SQL writes bypass them; only the application role may
write object tables.

### 4.5 Relationships and integrity

- A **lookup** is a real foreign key column. Delete behavior from metadata maps
  to the real FK action (`ON DELETE RESTRICT | CASCADE | SET NULL`).
- **Many-to-many** relationships compile to a junction table with two real FKs.
- **Uniqueness** compiles to a real unique index. Objects may also declare
  `unique_together` (e.g. one assignment per user and role), which compiles
  to a multi-column unique index.
- **Required** compiles to `NOT NULL` (with the backfill rules in §5.3).

Because these are real constraints, integrity holds even if application code
has a bug.

### 4.6 Standard system columns (every object table)

`id` (UUID, primary key), `created_at`, `created_by`, `updated_at`,
`updated_by`, `owner_id` (for record-level access, §8), `row_version` (for
optimistic concurrency).

---

### 4.7 Design rules: documentation and ontology

So that the schema stays understandable to AI agents and people, every change
is checked against deterministic design rules (`src/osier/design.py`), for
platform packages and tenants alike:

- **Every object and field needs a description** of at least 4 words and 20
  characters. Placeholders ("TBD", "test") and descriptions that only repeat
  the label or name are rejected (`DOCUMENTATION_REQUIRED`,
  `DOCUMENTATION_TOO_THIN`). Descriptions cannot be cleared later.
- **Names must say what they hold.** Names made only of generic words and
  numbers (`c__field1`, `c__new_data`, `c__misc`) are rejected (`POOR_NAME`).
- **Ontology links.** Objects and fields may carry a `semantic_ref`: the URI
  of an ontology term, normally schema.org (e.g.
  `https://schema.org/givenName`). Platform packages use schema.org wherever
  a matching term exists.
- **Advice.** Tenant proposals also get non-blocking `advice` entries, e.g. a
  missing `semantic_ref`, booleans not named `is_…`/`has_…`, picklist values
  without descriptions.
- `describe_schema` returns descriptions, semantic refs, picklist value
  descriptions, and constraints, so agents always see the documented model.

Limits: deterministic checks reject the obviously bad but cannot guarantee
good design. The future `create_object` MCP tool should add an AI design
review (non-blocking, shown to the approver) on top of these rules.

---

## 5. Schema changes: propose → validate → confirm → apply

All schema changes, whether initiated by an admin through AI or by a platform
upgrade, go through **change sets**.

### 5.1 Lifecycle

1. **Propose.** The AI (or the platform) submits a structured change set, e.g.
   *create object*, *add field*, *add relationship*, *change label*, *add
   picklist value*, *make required*, *retire field*. Never raw SQL.
2. **Validate.** The engine checks naming, reserved words, type compatibility,
   collisions, standard-object protections (§11.1), and dependencies.
3. **Impact analysis.** The engine runs the change against existing data and
   reports concrete effects, e.g. *"Making `employer_name` required: 1,243
   records have no value — provide a default or backfill"*, or *"Changing
   `c__risk_score` from text to integer: 17 values will not convert."*
4. **Confirm.** A human with the right permission approves the exact diff,
   generated DDL, and impact report (§10.2). Optionally, a second approver for
   production tenants.
5. **Apply.** The executor runs the DDL with `lock_timeout` and bounded
   retries, bumps `meta_schema_versions`, and invalidates caches. The MCP
   tools see the new schema immediately because they read metadata.
6. **Record.** The change set, proposer, approver, impact report, and outcome
   are stored permanently in `meta_change_sets`.

### 5.2 Safe DDL rules

- Prefer operations that only touch the catalog (e.g. `ADD COLUMN` with a
  constant default is metadata-only since PostgreSQL 11).
- Indexes are built with `CREATE INDEX CONCURRENTLY` as **background jobs**
  with a field status (`pending` → `building` → `active` / `failed`). This
  cannot run in a transaction, so it is never part of the synchronous apply.
- Table-rewriting changes (e.g. type changes on large tables) use
  **expand / backfill / switch / retire**: add a new column, backfill in
  batches, switch reads and writes, retire the old column.

### 5.3 Destructive changes

- **Fields and objects are never dropped by tenants.** They are *retired*:
  hidden from tools and views, data retained. Physical purge is a separate,
  delayed, audited operation that respects retention policies.
- Making a field required or unique when existing data violates it is
  **blocked** until the conflicts are resolved; the impact report lists them.

---

## 6. Records

### 6.1 Validation

- For each object and schema version, the engine generates a **Pydantic v2
  model** from metadata and caches it keyed by `(tenant, object, schema_version)`.
- Validation happens in the engine before SQL is executed. The database
  constraints (§4.5) are the backstop.
- Writes use **optimistic concurrency** (`row_version`) and accept an
  **idempotency key** so agent retries do not create duplicates.

### 6.2 Field history (opt-in per field)

Fields with `track_history = true` have every change recorded:

```sql
CREATE TABLE field_history (
  id             bigint GENERATED ALWAYS AS IDENTITY,
  object_id      uuid        NOT NULL,   -- meta_objects.id
  record_id      uuid        NOT NULL,
  field_id       uuid        NOT NULL,   -- meta_fields.id (stable across renames)
  old_value      jsonb,                  -- JSON so type changes don't break history
  new_value      jsonb,
  actor_id       uuid        NOT NULL,   -- who performed the change
  actor_type     text        NOT NULL,   -- 'human' | 'agent' | 'system'
  on_behalf_of   uuid,                   -- the human an agent acted for
  agent_id       text,                   -- client / agent identity
  source         text,                   -- 'chat' | 'api' | 'import' | 'upgrade' | ...
  request_id     uuid,                   -- groups the fields changed in one request
  changed_at     timestamptz NOT NULL DEFAULT now()
) PARTITION BY RANGE (changed_at);
```

- Written **in the same transaction** as the record change.
- Monthly partitions so retention becomes partition drops.
- The diff is computed in the engine's write path. Only the application's
  database role may write object tables, so writes cannot bypass history.
  A database trigger may be added later if a stronger guarantee is required.
- Record create and delete events go to an `record_events` table with the same
  actor columns.

### 6.3 Provenance

Values written by agents carry provenance: source type (`agent_extraction`,
`user_entered`, `integration`, …), source reference (e.g. document ID and
location), confidence, and verification status. Provenance is stored with the
history entry and is queryable. This lets later business rules require, for
example, human-verified values.

---

## 7. Querying and reporting

### 7.1 Agent and API queries

Agents do **not** write SQL. They use a constrained, structured query
language that the engine compiles to SQL:

```json
{
  "object": "opportunity",
  "fields": ["name", "amount", "account.name", "account.c__region"],
  "filters": [{"field": "close_date", "op": ">=", "value": "2026-01-01"}],
  "order_by": [{"field": "amount", "dir": "desc"}],
  "limit": 100
}
```

- Joins follow **declared relationships only** (dot paths such as
  `account.name`).
- The compiler enforces permissions (§8), maximum join depth, row limits, and
  a query timeout.
- Errors are structured (§10.3).

### 7.2 Reporting

- **Phase 1:** the same language with `group_by` and aggregates (`count`,
  `sum`, `avg`, `min`, `max`), compiled to SQL on the tenant database. Real
  columns make this efficient at small and medium scale.
- **Phase 2 (when benchmarks require it):** change data capture from tenant
  databases into a columnar analytics store; the report language is compiled
  to that backend instead. The interface does not change.
- Roll-up summary fields (e.g. total opportunity amount on an account) may be
  added to make common metrics instant.

---

## 8. Permissions

Permissions are stored in their own tables, **not** inside field definitions,
so permission changes do not bump the schema version.

| Level | v1 model | Enforcement |
|---|---|---|
| Object | Per role: create / read / update / delete | Engine |
| Field | Per role: read / update | Engine (query compiler strips or rejects; writes rejected) |
| Record | Owner-based: own records, team records, all records; per role | Engine query compiler; Postgres row-level security as a later defense-in-depth layer |

- The AI always acts **with the signed-in user's permissions, never more**.
- Reports and aggregates obey field permissions. A user who cannot read a
  field cannot aggregate it.
- Permission changes are two-phase (§10.2).
- Sharing rules, territories, and field-level encryption are out of scope for
  v1 but the model must not preclude them.

---

## 9. Identity and authentication

- **We do not build authentication.** An external OIDC provider handles
  sign-in, SSO, and MFA.
- **Start simple:** email + social sign-in from one managed provider. Add
  enterprise SSO (SAML / OIDC, per customer) later through the same provider.
- **Hard requirement:** the provider must support the **MCP authorization
  flow**, so MCP clients such as Claude can sign users in. That means OAuth
  2.1 with dynamic client registration or client ID metadata documents.
  Not every identity provider supports this. Verify before choosing.
  Candidates to check: WorkOS AuthKit, Auth0, Keycloak, Supabase Auth.
- The engine depends only on standard OIDC claims, so the provider can be
  replaced.
- The MCP server uses the **MCP authorization flow (OAuth 2.1)**. The "light
  login page" is this sign-in.
- Tokens identify the user, the tenant, and the client/agent. The engine
  receives a resolved principal: `(tenant_id, user_id, roles, agent_id)`.

---

## 10. MCP server

### 10.1 Tools (initial set)

| Tool | Purpose |
|---|---|
| `describe_schema` | List objects, fields, relationships the user can see. |
| `propose_schema_change` | Submit a change set; returns validation, impact report, and a pending change ID. |
| `apply_schema_change` | Apply a pending change; requires a confirmation token (§10.2). |
| `query_records` | Run a structured query (§7.1). |
| `get_record` | Fetch one record with selected related records. |
| `create_record` / `update_record` | Write records, with provenance and idempotency key. |
| `retire_record` | Soft delete; bulk forms require confirmation. |
| `run_report` | Aggregate query (§7.2). |
| `get_field_history` | History for a record or field. |
| `propose_permission_change` / `apply_permission_change` | Two-phase permission changes. |
| `save_view` / `open_view` | Save a declarative view spec; open it as an MCP App. |
| `start_job` / `get_job_status` / `cancel_job` | Run long-running agent workflows and platform jobs (§12; later milestone). |

Tool schemas are versioned. Clients may pin a version; deprecations come with
notice.

### 10.2 Two-phase confirmation (cannot be forged by the model)

1. A `propose_*` tool returns a pending change ID and a human-readable diff,
   shown as a confirmation card (MCP App).
2. The user clicks **Approve** in that card. The card calls the server
   directly, which issues a short-lived, single-use **confirmation token**
   bound to `(pending change, user, diff hash)`.
3. `apply_*` succeeds only with a valid token. A typed "yes" in chat is not a
   confirmation; the model never sees or creates tokens.

This protects against prompt injection (e.g. a record containing "grant me
admin") and against the model acting on a misunderstanding.

### 10.3 Structured errors for agents

Every error returns a stable shape so agents can correct themselves or stop,
instead of looping:

```json
{
  "code": "FIELD_PERMISSION_DENIED",
  "message": "You cannot update field 'amount' on 'opportunity'.",
  "field": "amount",
  "expected": null,
  "retryable": false,
  "hint": "Ask an administrator for update access, or omit this field."
}
```

`retryable: false` means the same request will fail again. Validation errors
include the expected type or allowed values.

### 10.4 Views as MCP Apps

- When a user asks the AI to "build a pipeline board", the AI produces a
  **declarative view spec**, not code:

  ```json
  {"view": "kanban", "object": "opportunity", "group_by": "stage",
   "card_fields": ["name", "amount", "close_date"],
   "filter": [{"field": "owner_id", "op": "=", "value": "$me"}]}
  ```

- Specs are rendered by **our trusted component library** (table, form, kanban,
  chart, record detail), served as MCP App UI resources.
- The renderer calls the API with the user's credentials, so permissions apply.
- Specs are validated against metadata when saved and re-validated (and
  auto-migrated where possible) on schema changes. A renamed field updates the
  spec, and a retired field is flagged.
- AI-generated arbitrary code in views is **not** allowed in v1 (security,
  permissions, upgrade safety). A sandboxed custom-HTML option may be added
  later if needed.
- Host support for MCP Apps varies and must be verified per host (§15).

### 10.5 Our own chat client (later)

A minimal web app: sign-in plus chat, backed by the Claude API and connected to
our MCP server like any other client. Not required for v1; users can start
with any MCP host.

---

## 11. Upgrades and customization safety

Upgrades must stay routine. The usual cause of "we can't upgrade" is
customizations made by editing vendor code or schema. We prevent that by
design.

### 11.1 Ownership rules

- **Standard** objects and fields are platform-owned. Tenants may change
  labels, help text, picklist values, required (subject to §5.3), and history
  tracking. They may **not** change types, physical names, or retire standard
  fields.
- **Custom** objects and fields (`c__` prefix) are tenant-owned.
- Tenants may add custom fields to standard objects.

### 11.2 Packages (standard objects as plugins)

Standard objects ship as **packages** that tenants choose to install
(`src/osier/packages/`). A package is metadata only — objects, fields, seed
records; later views, workflows, permission templates — **never code**.

| Package | Depends on | Objects | Notes |
|---|---|---|---|
| `core` | — | organization, app_user (User), role, user_role (Role Assignment) | Installed in every tenant at bootstrap; cannot be removed. User, Role, and Role Assignment are **protected**: agents cannot write them. |
| `crm` | core | account, contact, lead, opportunity, activity | schema.org Organization, Person, PostalAddress, Action where they fit. |
| `case_management` | crm | support_case (Case), case_comment | schema.org Comment for comments. |

schema.org has no types for Lead, Opportunity, or Case; those are documented
in their own words with no `semantic_ref` rather than an invented one.

Rules:

- **Installing** runs an ordinary change set with `source = package`, shown
  to and **approved by a human** (agents may propose). Missing dependencies
  are added automatically, dependencies first. The engine records installs in
  `meta_packages` and creates seed records (e.g. the `admin` and `member`
  roles) idempotently.
- **Tamper-proof.** The engine rebuilds the expected operations from the
  package registry and rejects any package change set that differs
  (`PACKAGE_MISMATCH`), so the package path cannot create arbitrary standard
  objects.
- **Dependencies are required.** A package cannot be installed before its
  dependencies, nor uninstalled while an installed package depends on it.
- **Uninstalling retires** the package's objects (hidden, data kept). It is
  blocked by tenant lookups that still point at those objects. Reinstalling a
  previously uninstalled package is not supported yet.
- **Platform packages only.** Third-party packages are out of scope for now.
- Package objects are standard-owned; tenants may extend them with `c__` fields.
- **Upgrades** (a new package version) will run as change sets with
  `source = upgrade` (§11.3).

New tenants are created with `bootstrap_tenant`, which installs `core` and
creates the tenant's organization, the first administrator user, and their
admin role assignment.

### 11.3 Upgrade process

1. **Pre-upgrade analysis** per tenant: name collisions, use of deprecated
   items, broken references in views and reports. Output like a compiler:
   warnings and blockers.
2. **Canary tenants** first, then rolling batches.
3. **Version policy:** no tenant more than one platform version behind.
4. Platform migrations (engine tables) use Alembic. Standard objects are
   upgraded through package versions (§11.4), and tenant customizations through
   change sets; neither uses hand-written migrations.

### 11.4 Package catalog in the database (planned for M5)

Goal: roll out new or changed **standard** objects with a database update
instead of a code deployment. Decided:

- **Catalog as data.** Each package version's full definition is stored as
  JSON in a platform catalog (`catalog_packages`: name, version, status
  draft → published → deprecated, definition, checksum, published_by).
  Published versions are immutable.
- **Authored in git, published to the database.** Package files in
  `src/osier/packages/` stay the reviewed source of truth; a publish command
  loads a reviewed version into the catalog. Publishing needs no deployment.
- **Materialized by the engine, not by a database trigger.** A trigger would
  bypass approval and the change-set record, duplicate the validation rules
  in PL/pgSQL, cannot run `CREATE INDEX CONCURRENTLY`, and cannot reach tenant
  databases from the control-plane database. Instead, publishing marks a
  version pending and a materializer (woken by `LISTEN/NOTIFY` or the job
  queue) creates upgrade change sets for every tenant with that package.
- **Upgrades are computed diffs** between the installed and the new version.
  **Additive changes apply automatically** (new objects, new fields, new
  picklist values, label and description changes), recorded with
  `source = upgrade`. Unsafe differences (type changes, removals) are refused
  and need a separate, explicit migration plan.
- The package tamper check (§11.2) compares against the catalog instead of the
  in-code registry.
- **Tenant-defined objects are not part of this.** They are created only
  through tenant change sets with human approval (§5).
- **Limit:** "no deployment" holds only while new definitions use existing
  engine capabilities. New field types or behaviours still need a release.

Timing: built with the control plane in M5, because the catalog belongs in the
control-plane database and there are no production tenants to roll out to yet.

---

## 12. Background jobs and workflows

Two kinds of long-running work run outside the request/response path:

| Kind | Examples | Needs |
|---|---|---|
| **Platform jobs** | Index builds, backfills, type-change migrations, tenant moves, imports | Retries, scheduling, status visible to the user |
| **Agent workflows** | Multi-step agent tasks: research, bulk data enrichment, document extraction, report generation | Everything above, plus **durable execution** (resume after a crash without repeating completed steps or LLM calls), **pausing for days** for human approval (§10.2), progress reporting, cancellation, cost/time budgets |

### 12.1 Decisions

- **Postgres-backed, no extra broker.** Celery was considered and rejected:
  its database broker transport is not production-grade (to be verified)
  and it does not provide durable, step-checkpointed workflows.
- **Our own interface in the engine** (e.g. `enqueue_job(...)`,
  `start_workflow(...)`, `get_status(...)`, `cancel(...)`). The underlying
  tool can be replaced without touching callers.
- **Every job carries the tenant location and the principal**
  (`tenant, user, roles, agent_id`). Workers execute with the tenant's
  database role (§3.1); jobs obey the same permissions as interactive
  requests, and their writes go through the record service (history,
  provenance).
- **One central queue in the control-plane database**, served by a shared
  worker pool, with **per-tenant concurrency limits and quotas** so one
  tenant cannot starve others.
- **Idempotent steps.** Every step that writes data uses an idempotency key,
  so retries and resumes are safe.
- **Visible to agents.** Jobs are exposed through MCP tools (start, status,
  cancel). If the MCP "tasks" mechanism for long-running operations is
  supported by hosts, use it (to be verified).

### 12.2 Jobs are metadata-driven

Like objects and fields, jobs are described by metadata, not hard-coded per
tenant:

- **Job types** are metadata: name, input schema (validated against object and
  field metadata), who may run them, retry and timeout policy, schedule, and
  per-tenant limits. Their logic is either a platform-provided handler or an
  **agent step** (prompt plus allowed tools).
- **Workflows are metadata too (decided):** triggers (e.g. a record is
  created or a field changes, a schedule), conditions, and actions (create or
  update records, run an agent step, wait for approval, start a job),
  assembled by admins through chat, similar to Salesforce Flow. Workflow
  definitions reference objects and fields by stable ID, so they are
  validated against metadata and migrated on schema changes like views (§10.4).
- **Rule: tenant-defined jobs never run arbitrary code.** Steps are limited to
  built-in actions and agent steps, and run with the permissions of the user
  who defined or started them.
- Job definitions follow the same propose → validate → confirm → apply
  lifecycle as schema changes (§5) and are versioned.

### 12.3 Tool choice (deferred)

| Candidate | Type | Notes |
|---|---|---|
| DBOS | Durable workflow library on Postgres | Step checkpointing, waiting for events, queues. Strong fit for agent workflows. |
| Procrastinate | Task queue on Postgres | Good fit for platform jobs; no durable multi-step workflows. |
| Hatchet | Workflow/task orchestration on Postgres | Strong features; runs as a separate service. |
| Temporal | Durable workflow engine | Most mature, but a separate cluster; likely too heavy for now. |

Selection criteria: durable steps, long waits for events, cancellation,
per-tenant fairness, operational weight, Python async support, and a license
compatible with MIT. Prefer **one tool for both kinds** of work. **Deferred:** the full job system is built in M7. Until then, M1 uses a
minimal implementation behind the interface (enough for index builds and
backfills).

---

## 13. Technology stack

| Concern | Choice |
|---|---|
| Language | Python 3.12+ |
| Database | PostgreSQL 16+ |
| SQL layer | psycopg 3 with `psycopg.sql` composition. (SQLAlchemy Core was planned; M1 uses `psycopg.sql` directly because dynamic identifiers are simpler and safer to compose there. Revisit if a need appears.) |
| Platform migrations | Alembic |
| Validation | Pydantic v2 (models generated from metadata) |
| HTTP API | FastAPI |
| MCP | Official MCP Python SDK |
| Auth | External OIDC provider supporting MCP authorization (§9) |
| Background jobs | Postgres-backed; behind our own interface; tool chosen before M2 (§12) |
| Tests | pytest against real PostgreSQL (testcontainers or a CI service) |
| License | MIT |

### Intellectual property rule

osier-core is MIT-licensed. **No code may be copied or adapted from projects
under incompatible licenses** (e.g. Directus MSCL, Twenty AGPL, Baserow
premium/enterprise). Studying public documentation and APIs for ideas is fine;
the implementation is written from first principles.

---

## 14. Roadmap

| Milestone | Scope |
|---|---|
| **M1 — Engine core** | Metadata tables; DDL compiler with naming/quoting rules; change sets (propose / validate / impact / apply); standard package with Account, Contact, Opportunity; record create/read/update/retire with generated validators; field history and record events; tests against real Postgres. One tenant schema, always addressed by fully qualified names. |
| **M2 — MCP server** | Read, write, query tools; propose/apply schema changes; structured errors; tested in a real MCP host. |
| **M3 — Permissions** | Roles; object, field, and owner-based record access; two-phase permission changes with confirmation tokens. |
| **M4 — Views** | Declarative view specs; trusted renderer as MCP Apps (table and form first); validation and migration on schema change. |
| **M5 — Multi-tenancy** | Control plane; tenant placement in shared or dedicated databases; per-tenant roles; schema relocation (shared → dedicated); routing; upgrade orchestration with pre-upgrade analysis; package catalog in the control-plane database with publish, diff-based upgrades, and automatic rollout of additive changes (§11.4). |
| **M6 — Reporting & chat client** | Aggregation reports; benchmarks at 100k / 1M / 10M rows; own sign-in + chat page. Analytics store if benchmarks require it. |
| **M7 — Jobs & agent workflows** | Metadata-driven job types; durable agent workflows; tool trial (§12.3); MCP job tools; workflows as metadata (§12.2). |

Tenant isolation is built in M5 but **designed for from M1**: the engine
always receives a principal with a tenant ID and never assumes a single
tenant.

---

## 15. Open questions

| # | Question | Why it matters |
|---|---|---|
| # | Question | Status |
|---|---|---|
| 1 | Expected number and size of tenants | Unknown. Not a blocker: shared/dedicated placement scales by adding databases (§3.3). |
| 2 | Identity provider | Start simple with one managed provider that supports MCP authorization; enterprise SSO later (§9). Provider not yet chosen. |
| 3 | Which MCP hosts fully support MCP Apps today | Later. |
| 4 | Reporting freshness | Minutes of lag acceptable; not a priority now. |
| 5 | Direct BI tool access (Power BI, Tableau) | Later. |
| 6 | How long field history and retired fields/objects are kept | **Proposed default:** keep indefinitely; per-tenant retention settings later. Personal-data erasure requests handled by redacting values in history, not deleting history rows. |
| 7 | Background job system | **Decided:** Postgres-backed, metadata-driven, behind our own interface, also used for agent workflows. **Deferred** to M7; tool chosen by trial then (§12). |
| 8 | Are workflows metadata-driven too? | **Decided:** yes — triggers, conditions, and actions as metadata (§12.2); built in M7. |
| 9 | Reinstalling an uninstalled package: reactivate its retired objects, or start fresh? | Open; blocked with `PACKAGE_REINSTALL_NOT_SUPPORTED` for now. |
| 10 | How do engine principals map to `app_user` records once authentication exists? | Bootstrap uses the admin's `app_user` id as the principal id; M3 formalizes this. |

---

## 16. Risks and data gaps

| Risk | Status / mitigation |
|---|---|
| **Users may not accept a chat-only interface** for repetitive work. | No data yet. Mitigated by AI-built views (M4); validate with target users early. |
| **Too many tenant schemas in one shared database** (catalog size, backups, memory). | No reliable threshold known. Per-database cap set from M5 benchmarks; new shared databases added automatically. |
| **Cross-tenant access in shared databases.** | Fully qualified names only (no `search_path`), per-tenant database roles with `SET LOCAL ROLE`, isolation tests. |
| **Cost of dedicated databases.** | Depends on provider pricing (unverified). Offered on paid tiers; providers that scale idle databases to zero reduce cost. |
| **Upgrade orchestration across many tenant schemas and databases.** | Change-set model, ownership rules, pre-upgrade analysis, canaries. |
| **Reporting performance** at large row counts. | No benchmark yet. M6 benchmarks; analytics store as planned fallback. |
| **DDL lock contention** on busy tenants. | `lock_timeout`, retries, background index builds, expand/backfill pattern. |
| **Prompt injection** through record data or documents. | Two-phase confirmations with unforgeable tokens; AI limited to the user's permissions; no raw SQL. |
| **SQL injection through identifiers.** | Engine-generated physical names, strict pattern, always quoted; dedicated tests. |
| **MCP Apps support varies by host.** | Verify per host; own chat client as fallback. |
| **AI-written code without expert review.** | Security-critical areas (permissions, tenant isolation, DDL compiler, auth integration) require human review before production data. |
