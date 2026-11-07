# osier-core — Architecture

> Status: **Draft v0.1** — decisions agreed in planning; open questions in §16.
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

Schema-per-tenant has two known failure modes. Both are designed out, and a
central check in code enforces the rules on every statement:

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
3. **One central checkpoint in code.** Every engine service gets a
   `TenantConnection` (`src/osier/db.py`), never a raw connection. Before any
   statement runs it checks that the SQL is composed with `psycopg.sql` (no
   plain strings), that every schema-qualified identifier names the session's
   own tenant schema (three-part names rejected), and that literal SQL
   fragments contain no quoted or dotted names that could smuggle in another
   schema. A violation raises `TENANT_ISOLATION_VIOLATION`, which always
   indicates a bug. Tenant schemas keep their current layout (engine tables
   and object tables together); per-tenant database roles (point 2) are added
   in M5 as a second layer.

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

### 8.1 Built: role permissions (`src/osier/authz.py`)

Platform-defined permissions, granted to roles per tenant in the
`meta_role_permissions` engine table:

| Permission | Allows | Default roles |
|---|---|---|
| `schema.manage` | Create and change objects and fields | admin, developer |
| `packages.manage` | Install, upgrade, uninstall packages | admin, developer |
| `connections.manage` | Connect apps and accounts, share them, set tool policies | admin, developer |
| `users.manage` | Invite users, assign roles (writes to user, role, role assignment) | admin |
| `records.use` | Read and write business records | admin, developer, member |
| `actions.request` | Have agents send email and use connected apps | admin, developer, member |

- Users get roles through `user_role` records (with optional start and end
  dates). Core v2 ships the roles Administrator, Developer, and Member.
- An agent has exactly the permissions of the person it acts for.
- Checks are in the engine (schema, packages, connectors, records), so every
  client gets the same answers. A refusal is `MISSING_PERMISSION` with the
  permission, the person's roles, the roles that grant it, and who can grant it.
- Approving an agent's action requires being the person it acted for, or
  holding `connections.manage`.

### 8.2 Planned: object, field, and record permissions

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

### 10.2 Confirmation in the chat (cannot be answered by the model)

**Built (MCP elicitation):** sensitive tools (schema changes, package
installs, emails, app tool calls with policy `approval`, connecting and
sharing apps, approving pending items) ask the person to confirm. The osier
server sends an elicitation request; the Claude app shows the person the exact
summary (and SQL or email body) with Approve / Decline. Only the person's
answer lets the action through; the model never sees or answers the prompt.

- The confirmation is computed from a **side-effect-free preview** (the MCP SDK
  may run it more than once per call); previews are deterministic so the
  prompt is identical across protocol rounds. Nothing is written before the
  person confirms; a decline changes nothing.
- After confirmation the server performs the action **as the person**
  (principal source `chat-confirmation`), so the engine's human-only and
  permission checks apply to them.
- Permission checks run before any prompt: a person without the permission
  gets `MISSING_PERMISSION` and no prompt.
- **Fallback:** if the app cannot show prompts, the action waits in the
  approval queue; the person approves later with `approve_pending` (in an app
  that can prompt) or `osier approvals approve`.
- **Trust boundary:** the guarantee depends on the client showing prompts to
  the person. MCP allows clients to let a model answer elicitations; Claude's
  apps are designed to ask the person, but osier cannot enforce that. A
  hosted osier will add an approval page with sign-in as an alternative.

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
- Host support for MCP Apps varies and must be verified per host (§16).

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

Standard objects ship as **packages** (`src/osier/packages/`). A package is
metadata only — objects, fields, seed records; later views, workflows,
permission templates — **never code**. There are two kinds:

- **System packages** are installed in the initial install phase of every
  tenant (`bootstrap_tenant`), can never be uninstalled, may depend only on
  other system packages, and are kept current automatically
  (`PackageService.ensure_current`). Today: `core`; the connector foundation
  adds `integrations` (§13).
- **Optional packages** are chosen by the tenant and can be uninstalled:
  `crm`, `case_management`.

| Package | Depends on | Objects | Notes |
|---|---|---|---|
| `core` (system) | — | organization, app_user (User), role, user_role (Role Assignment) | User, Role, and Role Assignment are **protected**: agents cannot write them. |
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
- **Object access levels.** Package objects declare who may write their
  records through the records API:

  | Level | Writes allowed by | Examples |
  |---|---|---|
  | `open` | Anyone with permission | account, support_case |
  | `protected` | People and the platform, never agents (`AGENT_WRITE_NOT_ALLOWED`) | app_user, role, user_role |
  | `engine_managed` | Only the engine service named in `managed_by`, through `RecordService.for_engine(...)`; read-only otherwise (`ENGINE_MANAGED_OBJECT`) | connection (§13) |

  Only packages can create `protected` or `engine_managed` objects. Tenants
  cannot add custom fields to `engine_managed` objects, since nobody could
  write them.

New tenants are created with `bootstrap_tenant`, which installs every system
package (today `core`) and
creates the tenant's organization, the first administrator user, and their
admin role assignment.

### 11.3 Package upgrades

When a package version changes (edit in git, bump `VERSION`), installed
tenants are brought forward by an upgrade change set (`upgrade_package`
operation, `source = package`), proposed with `PackageService.propose_upgrade`
or applied automatically by `PackageService.ensure_current`:

- **Diff of versions, not of tenant state.** The definition is stored in
  `meta_packages` at install/upgrade time; the upgrade diffs the stored old
  definition against the new one (`src/osier/packages/upgrade.py`) and then
  checks the tenant's current metadata.
- **Applied automatically (additive):** new objects (created first, so new
  lookups can target them), new fields, new picklist values, and changes to
  labels, descriptions, semantic refs, and history tracking.
- **Tenant customizations win.** If the tenant relabelled a standard field or
  edited its description, the upgrade leaves it and reports a note
  (`upgrade_notes`). Tenant `c__` fields are never touched.
- **Refused (`PACKAGE_UPGRADE_UNSAFE`):** removed objects or fields, type
  changes, option changes other than added picklist values, required /
  unique / indexed / default changes, constraint or access changes. These need
  an explicit migration plan. Nothing is changed.
- **Normal safety still applies:** blockers (e.g. a new required field
  without a default on a populated object), tamper check (the expected upgrade
  operations are recomputed and must match exactly), and human approval when a
  tenant applies it; the platform applies it automatically with the system
  principal.
- **Not applied:** label/description edits to existing picklist values (noted).
- Downgrades are refused. Seed records added in a new version are created;
  seeds that already exist are left as the tenant has them.

### 11.4 Rollout across tenants

1. **Pre-upgrade analysis** per tenant: name collisions, use of deprecated
   items, broken references in views and reports. Output like a compiler:
   warnings and blockers.
2. **Canary tenants** first, then rolling batches.
3. **Version policy:** no tenant more than one platform version behind.
4. Platform migrations (engine tables) use Alembic. Standard objects are
   upgraded through package versions (§11.3), and tenant customizations through
   change sets; neither uses hand-written migrations.

### 11.5 Package catalog in the database (planned for M5)

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
- **Upgrades are computed diffs** between the installed and the new version
  (already implemented against the in-code registry, §11.3).
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

## 12. Background jobs, workflows, skills, and tools

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

### 12.3 Tool choice (superseded for workflows by §12.4)

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

### 12.4 Workflow engine: LangGraph under osier's own definition (decided; first slice built)

- **osier owns the workflow definition**, a declarative, versioned JSON document
  stored as tenant metadata (§12.2): triggers, steps, conditions, approvals.
  Admins build it in chat; it goes through propose → preview → confirm like
  schema changes. The definition never contains code.
- **LangGraph is the execution engine underneath.** At run time osier compiles a
  definition into a LangGraph `StateGraph`, uses its Postgres checkpointer for
  durable steps and resume, and its `interrupt()` / `Command(resume=...)` for
  human approvals. Callers only see osier's interface (§12.1), so the engine can
  be replaced later.
- **osier builds around it:** triggers (record events, schedules, manual), the
  job runner and per-tenant limits, permissions (`workflows.manage`), audit, and
  agent steps.
- **Agent steps use the tenant's own Claude API key** (bring your own key),
  stored through the credential broker like any other secret (rule 12).
- **Rejected: CrewAI** — telemetry is on by default and its model is
  role-playing agent crews rather than durable, checkpointed graphs.
- **Checked when building:** `langgraph` 1.2 and `langgraph-checkpoint` 4.3 are MIT,
  pinned to `<1.3`. LangGraph's own Postgres checkpointer
  (`langgraph-checkpoint-postgres`) builds SQL from strings with unqualified table
  names and relies on `search_path`, so it cannot pass the isolation guard. osier
  therefore has its own `TenantCheckpointSaver` on LangGraph's public
  `BaseCheckpointSaver` interface, storing checkpoints in each tenant's schema
  (`workflow_checkpoints`, `workflow_checkpoint_writes`) through `TenantConnection`.
  It stores each checkpoint whole (no de-duplication of channel values); fine for
  small workflow state, to revisit if states grow. Risk: the saver interface marks
  some parts beta (delta channels); we rely on the default implementations.

**Built in the first slice** (`src/osier/workflows/`):

- **Definition** (`spec.py`): name, description, typed inputs, and steps
  `get_record`, `query_records`, `create_record`, `update_record`, `condition`,
  `approval`, `finish`. Steps run in order unless they name `next`; jumps go only
  forward (every run ends); `{{inputs.x}}` / `{{steps.<id>...}}` references, no
  expressions, no code. Validated on save against the tenant's objects, fields,
  and roles. Versioned in `meta_workflows` / `meta_workflow_versions`; a run always
  finishes on the version it started with.
- **Runs** (`workflow_runs`, `workflow_run_events`): statuses running, waiting,
  succeeded, rejected, failed, cancelled. A run executes **with the permissions of
  whoever started it** (a person or an agent acting for them); a workflow never
  grants more access. Record writes go through `RecordService` with provenance
  `workflow run <id>`; `create_record` uses an idempotency key per run and step.
- **Approvals** pause the run (`interrupt`). Only a person decides, never an
  agent; the approver needs one of the step's `approver_roles`, or
  `workflows.manage` when none are named. The decision is claimed atomically, so
  a run cannot be decided twice. Approvals show in `list_pending_approvals` and
  `osier approvals`.
- **Execution:** a run started by hand runs inside the request until it ends or
  reaches an approval, or, with `background`, in the worker (§12.7). Triggered runs
  always run in the worker. **Not built yet:** retrying failed runs, loops, agent
  steps (tenant's Claude key), connector-call steps, running other workflows as
  steps.

### 12.7 Job queue, worker, and triggers (built)

- **Per-tenant queues (decided).** Each tenant's jobs live in its own schema,
  `j_<hex>`, next to its data schema `t_<hex>`; one tenant's jobs never share a
  table with another's. This replaces the central queue planned in §12.1: tenant
  isolation outweighs simpler cross-tenant scheduling. Dropping a tenant drops its
  queue.
- **Replaceable backend.** All queue code sits in `osier.jobs` behind the `JobBackend`
  interface (`jobs/base.py`): install/drop a tenant's queue, hand over run ids, run
  until idle or serve, maintain (crashed workers, retention), list jobs. The engine's
  only entry point for executing work is `jobs.runner.execute_run(tenant, run_id)`;
  workflow runs in the tenant schema are the source of truth; job payloads carry ids
  only. A test runs the full trigger → run → approval flow on a second, in-memory
  backend, and a test fails if anything outside `osier.jobs` imports the queue
  library.
- **Path to a separate job service (planned for scale).** The service would own the
  backend and the worker loop (`jobs/worker.py`), read queued runs per tenant, and
  call `execute_run` (in-process via the osier library, or through an internal API
  wrapping it). Nothing in the engine changes; tenant settings stay in the tenant
  schema. Open choices for that step: one shared, partitioned queue (e.g. DBOS
  partitioned queues or Hatchet round-robin concurrency) vs. keeping per-tenant
  queues; and per-tenant database roles for the service, which matter more for
  isolation than queue placement.
- **Engine: Procrastinate** (MIT, Postgres-only, psycopg 3; pinned `<3.11`) instead of
  our own queue. It provides workers, retries, locks, LISTEN/NOTIFY, heartbeats.
  Its SQL is unqualified, so its connection uses `search_path = j_<hex>` only, and
  every Procrastinate function is pinned to that `search_path` at install. That
  connection cannot see tenant data (tested). Engine code never touches queue
  tables through `TenantConnection`; the guard refuses it (tested).
- **Runs stay the source of truth.** Engine code marks a run `queued` in its own
  transaction (including trigger dispatch). The worker hands queued runs to the
  tenant's queue and records that (`deferred_at`). A crash in between means the run
  is handed over again; `queueing_lock` and `lock` per run keep it from being queued
  twice or executing twice at once (tested). Background approvals store the
  decision on the run (`resume_with`).
- **Per-tenant settings** (`meta_job_settings`, `jobs.manage`, changed by a person in
  chat or `osier jobs settings`): `concurrency` 1–10, `max_attempts` 1–5,
  `retry_wait_seconds` 1–3600 with `fixed` or `linear` backoff, `paused` (queue but
  do not run), `keep_finished_days` 1–90. The bounds are platform limits protecting
  shared workers; plans may raise them later. The worker applies changes within
  about 10 seconds by restarting that tenant's queue runner.
- **Worker** (`osier worker`): one process serves every tenant, with one
  Procrastinate worker per tenant using that tenant's settings. Every tick it
  dispatches triggers, hands over queued runs, recovers jobs of workers whose
  heartbeat stopped (Procrastinate detects them but does not retry them itself), and
  deletes finished jobs past retention. Errors in workflow steps fail the run
  without retrying; unexpected errors retry per the tenant's settings, then fail
  the run.
- **Scaling limits (data gap):** each active tenant holds its own small connection
  pool (up to concurrency + 2) in each worker process, so connections grow with
  the number of tenants. Not measured; at hundreds of tenants this needs workers
  that serve subsets of tenants, or starting a tenant's runner only when it has
  work.
- **Upgrading Procrastinate (to do):** a new version can need its SQL migrations
  applied to every tenant's queue schema. Today we only install the schema once;
  bumping the pin requires adding a per-tenant migration step.
- **Record triggers** (`record_created`, `record_updated` with optional `fields`
  and `when` checks). Record updates now write `updated` record events with the
  changed fields. The worker claims undispatched events with `SKIP LOCKED` and, in
  the same transaction, creates the runs and jobs and marks the events dispatched:
  each event fires once, none is skipped. Events written before this change, or
  before a workflow existed, never fire it.
- **Who triggered runs act as:** the person (or agent acting for a person) who made
  the change, so a trigger never grants more access than its cause had. Changes made
  by workflow steps are tagged `source = workflow` and **never fire triggers** (no
  loops; chaining workflows is deliberately unsupported for now). Platform changes
  (system principal) never fire triggers.
- **Schedules** (`schedule`: 5-field cron + IANA time zone, at most every 5
  minutes; `croniter`, MIT) run with the permissions of the **person who last saved
  or enabled the workflow**; the confirmation prompt says so. Each due time fires
  once; missed times are skipped, not replayed. Disabling or retiring a workflow
  removes its schedules.
- **Not built yet:** fair scheduling across tenants beyond per-tenant concurrency,
  job progress reporting, time and cost budgets, and alerting on failed runs. The
  dispatcher polls every 2 s; Procrastinate itself is woken by LISTEN/NOTIFY.

### 12.5 Skills (built: text only)

A **skill** is reusable instructions for recurring work, in the open Agent
Skills format (`SKILL.md` with `name` and `description` frontmatter; optional
`license`, `compatibility`, `metadata`, `allowed-tools`), so published skills,
including Claude's, can be imported.

- **Per tenant.** Stored in engine tables `meta_skills`, `meta_skill_versions`
  (every save is a new version; history is kept), and `skill_visibility`.
- **Text only for now.** Skills whose folder contains scripts are refused
  (`SKILL_HAS_SCRIPTS`); other extra files are skipped and reported. Running
  skill scripts needs a sandbox and is planned as a later, paid option.
- **Who.** Everyone may read and follow active skills visible to them
  (organization, their roles, or their own private ones). Adding, changing,
  importing, sharing, disabling, and retiring need `skills.manage` (admin and
  developer by default) and are done by a person: in chat after confirming the
  full text (and license for imports), or with `osier skills` in the CLI.
- **Imports** accept https URLs only: a raw `SKILL.md` or a GitHub file or
  folder. Downloads pass the private-network guard. The source is recorded.
- **Agents are told** (server instructions) to check `list_skills` at the start
  of a task and follow a matching skill. Skill text is instructions written by
  the tenant's people; it never grants permissions; every action it leads to is
  checked as usual.

### 12.5a AI steps in workflows (built)

- **Any model provider, through LiteLLM** (MIT; pinned `<1.109`). osier does not call
  provider SDKs directly. LiteLLM's bundled price list is used
  (`LITELLM_LOCAL_MODEL_COST_MAP=True`) so importing it does not download anything; it
  sends no telemetry by default. Cost of the choice: a heavy dependency (boto3,
  huggingface-hub, tokenizers, aiohttp, openai) and about 6 s import time, so it is
  imported only when an AI step runs.
- **AI connections**, several per tenant (`ai_connections`): a LiteLLM provider
  (openai, anthropic, openrouter, azure, groq, ollama, ...), an optional API base URL
  (https only, private addresses refused unless local development allows them), the
  models it may use, an encrypted key entered at a hidden CLI prompt
  (`osier ai connect`; never returned by any tool or log), optional prices per million
  tokens, and monthly limits: tokens, and dollars when the price is known (from the
  connection, or LiteLLM's price list).
- **Model profiles** (`ai_profiles`): named connection + model + effort, e.g.
  `careful`, `fast`, `private`. AI steps name a profile in `model` (or use the default
  profile), so different steps use different models and the model behind a profile can
  change without editing workflows. Profiles hold no secrets, so they can also be set
  in chat after confirmation (`set_ai_profile`). Saving a workflow checks that its
  profiles exist.
- Connections and profiles need `ai.manage` (admin only by default; it controls
  spending) and a person.
- **Step type `agent`**: `instructions` (with references), optional `skill` (its text
  is added as data), `data`, `tools` from `get_record`, `query_records`,
  `create_record`, `update_record` limited to the listed `objects`, a declared
  `output`, `model` (profile), `max_turns` (1–20, default 6), and `effort` (sent as
  `reasoning_effort`; dropped for providers without one).
- **Execution:** our own tool loop over LiteLLM's OpenAI-format `completion` (not a
  framework's agent loop), so every tool call goes through `RecordService` as the run's
  principal (source `workflow`, provenance `agent_inference`) with our object allowlist
  and limits. The step ends only through `submit_result` (`strict` where the provider
  supports it), validated with `jsonschema` against the declared output before later
  steps use it; invalid results are sent back for correction. Workflows with AI steps
  always run in the background worker.
- **Untrusted content:** record contents, tool results and skill text are data; the
  system prompt says so. A prompt injection can only do what the step's tools allow,
  with the starter's permissions. Recommended pattern: read-only AI step, then an
  approval, then a declarative update step.
- **Errors:** content filtering (`AI_REFUSED`), cut-off output, turn limit, budget,
  bad request or invalid key fail the step; rate limits, timeouts and server errors are
  retried by LiteLLM and then by the job.
- **Known limits / data gaps:** tool-calling reliability differs a lot between models
  and is untested here; weaker models will hit the output check and turn limit more
  often. Provider-specific features (e.g. server-side refusal fallbacks) are not used.
  Writes made by tools are not exactly-once if a worker crashes mid-step. Not yet run
  against a live provider.

### 12.5b Connected-app steps (built)

- Step type `connector`: a `capability` (e.g. `email.send`, routed by purpose or a
  named `connection`) or an MCP `connection` + `tool`, with `arguments` (references
  allowed) and `on_failure` (`fail` or `continue`). Saving checks the capability, the
  connection's visibility to the saver, and that the tool is pinned and not blocked.
- It runs through `ConnectorService` as the run's principal, so permissions
  (`actions.request`), connection visibility, per-tool policies, the approval queue and
  audit all apply unchanged. There is one approval system: an action that needs
  approval pauses the run (`waiting`, kind `connector_call`) and appears in the existing
  action queue; approving or rejecting it there re-queues the run for the worker. A
  rejected action ends the run as `rejected`.
- Repeat-safe: each call is keyed by run and step (`idempotency_key`), so re-running the
  node after a crash or on resume returns the same call instead of sending twice.
- A process-wide runtime (`osier.runtime`) holds the credential broker and network
  rules used by steps that run outside a request (worker, MCP server, CLI).
- Resuming after an approval needs the worker (`osier worker`).

### 12.5c Data steps with CEL (built)

- Step type `transform` (`values: {name: expression}`) and `condition.expression`, in
  **CEL** (Common Expression Language, used by Kubernetes and Google Cloud IAM):
  no loops, no side effects, no access beyond `inputs`, `steps`, and `values`.
- Library: **common-expression-language** 0.10 (Apache-2.0, Rust interpreter;
  pinned `<0.11`), chosen over cel-python after a comparison: about 7x faster in a
  probe (200 evaluations: 11 ms vs 77 ms), plain Python results, clearer errors, and it
  reports the variables an expression uses, which we check on save. Costs: pre-1.0,
  and it pulls CLI dependencies (typer, rich, prompt-toolkit). The CEL string, list,
  math, set and base64 extensions are enabled.
- Checked on save: syntax, length (2000 chars), names, and that `steps.<id>` refers to
  an earlier step. At run time: errors and non-finite numbers (e.g. division by zero)
  fail the step with `EXPRESSION_FAILED`; results are limited to 100 KB.
- Decimal fields arrive as text (stored exactly), so arithmetic needs `double(...)`;
  this loses exactness for money. A decimal-safe option is not built.

### 12.5d Test mode (built)

- `WorkflowService.test` (`test_workflow` tool, `osier workflows test`) runs a saved
  workflow or an **unsaved** definition with sample inputs. Record, data, and condition
  steps run for real inside a database transaction that is rolled back, so validation,
  lookups and uniqueness behave as in a real run while nothing is kept and no triggers
  fire. Checkpoints use an in-memory saver. Connected-app steps are previewed only
  (summary, whether approval would be needed). Approvals are simulated (`approve`).
  AI steps return `ai_outputs[step]` (validated against the declared output) or a
  placeholder; `real_ai` calls the models (the user confirms in chat), and their token
  usage is recorded after the rollback so test spend is not hidden.
- Testing an unsaved definition needs `workflows.manage` (it runs `preview`); testing a
  saved workflow needs only `records.use`.
- Limits: a test with real AI calls holds its transaction open while models answer;
  records created during the test are visible to the test only.

### 12.6 Tools per tenant (planned)

Tenants already add tools by connecting MCP servers (§13.2). Next: a tenant
tool catalogue (`tools.manage`) with declarative HTTP tools; custom code
functions come later, together with the skill script sandbox.

---

## 13. Connectors

Connectors let agents act in external systems (email, marketing platforms,
other SaaS tools) as part of their work.

### 13.1 Concepts

| Concept | What it is | Example |
|---|---|---|
| **Connector type** | Definition of an external system: auth method, non-secret config, actions, (later) triggers and data mappings | Gmail, Klaviyo, "Generic HTTP API" |
| **Connection** | A tenant's instance of a connector type, with credentials | "Acme newsletter (Klaviyo)", "Ada's mailbox (Gmail)" |
| **Action** | A callable operation, exposed to agents as a tool | `send_email`, `add_subscriber` |
| **Capability** | A provider-neutral contract several connector types implement | `email.send`, `email.read_inbox`, `marketing.audience`, `marketing.campaign` |
| **Purpose** | The tenant's label for what a connection is used for | `transactional`, `marketing` |

Agent instructions and workflows target **capabilities and purposes**, not
providers, so switching providers does not break them. Every action and
parameter needs a real description (same design rules as objects, §4.7), and
each action declares a **side-effect level**: `read`, `write`, or
`external_send`.

### 13.2 The connector interface is MCP-shaped

Claude's connectors, and the connectors many vendors publish, are MCP
servers. To use that ecosystem, osier's connector interface follows the MCP
tool model:

- A connector **action is an MCP tool**: `name`, `description`, JSON Schema
  `inputSchema` (and output schema where known), and MCP **tool annotations**.
  Side-effect levels map onto annotations: `read` → `readOnlyHint: true`;
  `write` → `readOnlyHint: false`; `external_send` → also
  `openWorldHint: true`; irreversible actions → `destructiveHint: true`.
- Native connectors expose exactly this shape, so a native connector and a
  registered remote MCP server are interchangeable from the engine's view.
- **Any MCP server can be registered as a connector type** (runtime `mcp`).
  Its tools become actions; a declarative mapping links provider tools to
  osier capabilities (e.g. a vendor's `send_message` → `email.send`).

**When osier relays a connector vs. when Claude uses it directly.** A user can
connect Claude to osier *and* to, say, a Gmail MCP server, and Claude can
already combine them in a chat with no osier work. osier relays a connector
when it must add something the client cannot: approvals and consent checks,
purpose routing, audit, automatic CRM logging (e.g. sent email → activity),
and use by **background agents and workflows** when no chat client is open.
Both modes are supported; the governed path goes through osier.

**Built (runtime `mcp`):** a person inspects a server (`osier connect mcp`), sees
its tools, and registers it. The engine stores a tenant connector type in
`meta_connector_types` (transport, pinned tool definitions, hash) and a
connection with encrypted secrets (HTTP headers or environment variables).
Each tool has a policy: `auto` (runs immediately; default for tools the server
marks read-only, confirmed by the person at registration because the hint is
untrusted), `approval` (each call waits in the approval queue; default for
everything else), or `blocked`. Arguments are validated against the pinned
input schema. Before every call the server's current definition of the tool is
compared with the pinned one; a difference fails the call and marks the
connector `needs_review` until a person accepts the new tools
(`osier connections review`), which resets changed tools to their default
policy. Results are returned marked `untrusted`, truncated to a size limit.
Transports: streamable HTTP (HTTPS only, network guard applies) and stdio local
commands, which are **code execution on the host** and therefore allowed only
when `allow_local_processes` is set (local development; never on a hosted
platform). Not yet: OAuth sign-in to remote servers, and mapping server tools
to capabilities (e.g. a vendor's send tool as `email.send`).

**OAuth sign-in (built):** `connect_app` (chat) or `osier connect mcp --oauth`
discovers the server's OAuth settings (protected-resource and
authorization-server metadata), registers osier as a client dynamically or
uses a supplied client, and returns a sign-in link with PKCE and a single-use,
expiring state (`connector_oauth_states`). The provider redirects to osier's
callback (locally `http://127.0.0.1:<port>/oauth/callback`); osier exchanges the
code, stores tokens encrypted, and lists the tools; the person reviews and
confirms them (`finish_app_connection`). Tokens refresh automatically. Only the
person who started a sign-in can finish it.

**Visibility (built):** each connection is `private`, shared with `roles`, or
`organization`-wide; managing it needs `connections.manage`. Sharing a
connection authorized by a person's own account warns that everyone it is
shared with acts as that account.

**Risks of third-party MCP servers:** their tool descriptions and outputs are
untrusted (prompt injection, "tool poisoning"). osier pins each registered
server's tool definitions (hash) and requires re-approval when they change;
outputs are marked untrusted. OAuth to remote servers follows the MCP
authorization spec; some servers do not support dynamic client registration,
so manual client registration must also be possible.

### 13.3 Runtimes: standard and custom connectors

| Runtime | Written by | How it runs | Use |
|---|---|---|---|
| **Native** | Platform team, git-reviewed (like packages) | Python adapter in the platform | Standard connectors with complex auth or logic |
| **Declarative HTTP** | Tenant admin, through chat | The engine calls the HTTP API described by the definition. **No tenant code runs.** | Custom connectors |
| **Remote MCP server** | Anyone | The engine relays calls to a registered MCP server | Reusing the MCP ecosystem (open question, §16) |

Standard connector types live in code (and in the database catalog from M5,
like packages). Custom connector types are registered through approved change
sets and stored in `meta_connector_types`.

### 13.4 Multiple connections per tenant

A tenant may have any number of connections, including several for the same
capability and several email platforms at once (e.g. Gmail for one-to-one
mail, SendGrid for system notifications, Klaviyo and Mailchimp for
marketing). The engine chooses a connection from **capability + purpose +
acting user**:

1. a connection the agent names explicitly, else
2. the tenant's default connection for that capability and purpose, else
3. a structured error listing the matching connections (never a silent guess).

Connections are **organization-wide** (one company account) or **per user**
(each person's own mailbox; the acting user's connection is used). Automatic
routing rules (by region, brand, failover) are deferred until needed.

### 13.5 Purpose is a safety rule

| | `transactional` | `marketing` |
|---|---|---|
| Recipients | One-to-one, tied to a record (case, deal, reply) | Lists and audiences |
| Consent | Not checked for genuine service messages | **Enforced by the engine** (`has_opted_out_of_email`, consent records) |
| Unsubscribe link | Not required | **Mandatory** |
| Approval | Per tenant policy | **Bulk sends always need human confirmation** |
| Recipients per call | Small limit | Large, rate-limited |

The engine cannot reliably tell whether a message *is* marketing; the
structural limits above (recipient caps, record ties, rate limits, audit)
make misuse of the transactional path hard and visible, not impossible.
Legal treatment (CAN-SPAM, GDPR, ePrivacy) must be confirmed with counsel.

### 13.6 Storage

**Engine tables** (tenant schema, written only by connector code):

| Table | Purpose |
|---|---|
| `meta_connector_types` | Custom connector definitions (key `c__…`, version, runtime, definition, status, checksum); changed only through approved change sets |
| `connector_credentials` | **Encrypted** OAuth tokens and API keys (connection, kind, ciphertext, key id, expiry, scopes, rotation/revocation) |
| `connector_oauth_states` | Short-lived OAuth sign-in state (hashed state token, user, encrypted PKCE verifier, expiry) |
| `connector_calls` | **Append-only** audit of every call (connection, action, capability, purpose, actor/agent/on-behalf-of, request id, idempotency key, redacted summary, recipient count, outcome, error, duration, external id); partitioned by month |
| `connector_policies` | Tenant rules: approval requirement, recipient and rate limits, which objects/fields may leave the platform; changes need human approval (may merge into M3 permission tables) |

Reused: `meta_idempotency` (namespaced) so retried sends do not send twice;
the shared approval/confirmation-token table planned for M3 (§10.2) for
external sends.

**Object:** `connection` (name, connector type, purpose, scope, owning user,
default flag, status, last error, last used, connected at) in a **system
package**, marked **engine-managed** (§13.7): visible and queryable like any
object, changed only by connector code.

**Credentials decision:** credentials are stored **in the tenant schema**,
encrypted with keys held **outside the database** (cloud key management
service, to be chosen), so a database dump alone reveals no secrets and a
tenant's credentials move with its schema. Access goes through a
credential-broker interface so a dedicated vault can replace this later
without changing connector code. Agents, logs, and `describe_schema` never
see secrets.

### 13.7 Foundations this needs (built)

1. **System packages vs optional packages** (§11.2). The connector foundation
   adds a system package `integrations` holding `connection`.
2. **Package upgrades** (§11.3), because engine releases will require newer
   system package versions in every tenant.
3. **The `engine_managed` access level** (§11.2): `connection` is
   `engine_managed` with `managed_by = connectors`.

### 13.8 Safety

- **Approval by side-effect level:** reads run freely; writes follow policy;
  external sends need confirmation by default, bulk sends always.
- **Outbound data obeys field permissions**, plus per-connector rules on which
  objects and fields may leave the platform.
- **External content is untrusted data:** email bodies and API responses are
  marked untrusted in tool results and can never approve anything.
- **Network protection for custom HTTP connectors:** block private and
  internal addresses and cloud metadata endpoints, per-connector domain
  allowlists, outbound proxy, response size and time limits (SSRF defense).
- **Audit every call;** rate limits, retries with idempotency keys,
  timeouts; long operations (syncs, campaign sends) use the job queue (M7).

### 13.9 Deferred

Inbound webhooks and triggers (M7), data sync and its state (M7), rate-limit
counters (when rate limiting is built), automatic routing rules.

---

## 14. Technology stack

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

## 15. Roadmap

| Milestone | Scope |
|---|---|
| **M1 — Engine core** | Metadata tables; DDL compiler with naming/quoting rules; change sets (propose / validate / impact / apply); standard package with Account, Contact, Opportunity; record create/read/update/retire with generated validators; field history and record events; tests against real Postgres. One tenant schema, always addressed by fully qualified names. |
| **M2 — MCP server** | **Local version built:** stdio MCP server and `osier` CLI for Claude Desktop / Claude Code, structured queries, approvals from the CLI. **Remaining:** remote HTTPS transport with OAuth (claude.ai connectors). Read, write, query tools; propose/apply schema changes; structured errors; tested in a real MCP host. |
| **M2.5 — Connector foundation** | **Partly built:** `integrations` package, engine tables, credential broker (Fernet, local key file), MCP-shaped actions, SMTP and dev outbox connectors, routing, approval queue, audit, CRM logging, MCP servers as connectors (§13.2). **Remaining:** OAuth sign-in to MCP servers, tool-to-capability mapping, declarative HTTP connectors, marketing email, connector policies via change sets. Engine tables (§13.6), credential broker with encryption, `connection` object, capability and purpose routing, action execution with policy and audit, one native connector, declarative HTTP runtime with network protections. Depends on system packages, package upgrades, and the `engine_managed` level (§13.7), which are built first. |
| **M3 — Permissions** | Roles; object, field, and owner-based record access; two-phase permission changes with confirmation tokens. |
| **M4 — Views** | Declarative view specs; trusted renderer as MCP Apps (table and form first); validation and migration on schema change. |
| **M5 — Multi-tenancy** | Control plane; tenant placement in shared or dedicated databases; per-tenant roles; schema relocation (shared → dedicated); routing; upgrade orchestration with pre-upgrade analysis; package catalog in the control-plane database with publish, diff-based upgrades, and automatic rollout of additive changes (§11.5). |
| **M6 — Reporting & chat client** | Aggregation reports; benchmarks at 100k / 1M / 10M rows; own sign-in + chat page. Analytics store if benchmarks require it. |
| **M7 — Skills, jobs & agent workflows** | **Built:** text skills per tenant (§12.5); workflows on LangGraph with record steps, conditions, and approvals (§12.4). job queue, worker, record and schedule triggers (§12.7). AI steps on the tenant's own model connections, any provider via LiteLLM (§12.5a). **Next:** metadata-driven job types; MCP job tools; tenant tool catalogue (§12.6). |

Tenant isolation is built in M5 but **designed for from M1**: the engine
always receives a principal with a tenant ID and never assumes a single
tenant.

---

## 16. Open questions

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
| 11 | First email connectors: which mailbox (Gmail / Microsoft 365), transactional (SendGrid / Postmark), and marketing (Klaviyo / Mailchimp) providers? | Open. |
| 12 | Custom connectors: declarative HTTP only, or also registering remote MCP servers? | Open. |
| 13 | Default approval policy for external sends: all sends, or only bulk sends? | Open; proposed default is confirm all external sends until there is a track record. |
| 14 | Which key management service holds credential encryption keys? | Open; decided with hosting. Credentials themselves live in the tenant schema (§13.6). |

---

## 17. Risks and data gaps

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
