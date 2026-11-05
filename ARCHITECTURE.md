# osier-core — Architecture

> Status: **Draft v0.1** — decisions agreed in planning; open questions in §14.
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
        control-plane database          one database per tenant
                                        (on shared Postgres clusters)
                                                  │
                         later: CDC ─► columnar analytics store
```

External services: an OIDC identity provider (§9), object storage for files,
and (later) an analytics store.

---

## 3. Tenancy model

**Decision: one PostgreSQL database per tenant**, hosted on shared Postgres
clusters, plus a separate control-plane database.

Rationale:

- Each tenant customizes its own schema, so tenants cannot share tables.
- **Schema-per-tenant was rejected**: all tenants' tables would share one
  system catalog (bloat at scale) and `search_path` handling with transaction
  pooling risks cross-tenant leaks.
- Database-per-tenant keeps each tenant's catalog small, limits the blast
  radius of DDL locks to one tenant, and makes backup, restore, export, and
  deletion of a single tenant straightforward.

Costs we accept (see risks in §15):

- Connection pooling is per database; pools must be small and created lazily.
- Every platform upgrade is applied to N databases (orchestrated, §11).
- Cost per tenant is higher than with shared tables; free/trial tiers need
  care (e.g. idle-tenant suspension).

The **control-plane database** holds tenants, plans, cluster placement, schema
versions per tenant, and upgrade state. It never holds tenant business data.

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
- **Namespacing:** custom objects and fields get a `c_` prefix
  (`c_net_worth_statement`, `c_region`). Platform upgrades can therefore add
  standard fields without colliding with tenant customizations.

### 4.3 Field types (initial set)

`text`, `long_text`, `integer`, `decimal(precision, scale)`, `currency`,
`boolean`, `date`, `datetime`, `email`, `phone`, `url`, `picklist`,
`multi_picklist`, `lookup` (foreign key), `json` (escape hatch, not
indexable/reportable by default).

Every type maps to a fixed PostgreSQL column type and a validator (§6.1).
Domain types such as `email` are validated in the engine *and* backed by a
`CHECK` constraint where practical.

### 4.4 Relationships and integrity

- A **lookup** is a real foreign key column. Delete behavior from metadata maps
  to the real FK action (`ON DELETE RESTRICT | CASCADE | SET NULL`).
- **Many-to-many** relationships compile to a junction table with two real FKs.
- **Uniqueness** compiles to a real unique index.
- **Required** compiles to `NOT NULL` (with the backfill rules in §5.3).

Because these are real constraints, integrity holds even if application code
has a bug.

### 4.5 Standard system columns (every object table)

`id` (UUID, primary key), `created_at`, `created_by`, `updated_at`,
`updated_by`, `owner_id` (for record-level access, §8), `row_version` (for
optimistic concurrency).

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
   `c_risk_score` from text to integer: 17 values will not convert."*
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
  constraints (§4.4) are the backstop.
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
  "fields": ["name", "amount", "account.name", "account.c_region"],
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

- **We do not build authentication.** An external OIDC provider (self-hosted
  Keycloak/Authentik or a managed service such as WorkOS/Auth0 — decision
  open, §14) handles sign-in, SSO, and MFA.
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
- Host support for MCP Apps varies and must be verified per host (§14).

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
- **Custom** objects and fields (`c_` prefix) are tenant-owned.
- Tenants may add custom fields to standard objects.

### 11.2 Standard-object packages

Standard objects ship as versioned packages (metadata + migrations). New
tenants are provisioned with the current package; existing tenants are moved
forward through change sets marked `source = upgrade`.

### 11.3 Upgrade process

1. **Pre-upgrade analysis** per tenant: name collisions, use of deprecated
   items, broken references in views and reports. Output like a compiler:
   warnings and blockers.
2. **Canary tenants** first, then rolling batches.
3. **Version policy:** no tenant more than one platform version behind.
4. Platform migrations (engine tables, standard objects) use Alembic. Tenant
   customizations never use hand-written migrations; they are change sets.

---

## 12. Technology stack

| Concern | Choice |
|---|---|
| Language | Python 3.12+ |
| Database | PostgreSQL 16+ |
| SQL layer | SQLAlchemy 2.x Core (not ORM for dynamic objects), psycopg 3 |
| Platform migrations | Alembic |
| Validation | Pydantic v2 (models generated from metadata) |
| HTTP API | FastAPI |
| MCP | Official MCP Python SDK |
| Auth | External OIDC provider (open, §14) |
| Background jobs | Open (e.g. a Postgres-backed queue); needed for index builds and backfills |
| Tests | pytest against real PostgreSQL (testcontainers or a CI service) |
| License | MIT |

### Intellectual property rule

osier-core is MIT-licensed. **No code may be copied or adapted from projects
under incompatible licenses** (e.g. Directus MSCL, Twenty AGPL, Baserow
premium/enterprise). Studying public documentation and APIs for ideas is fine;
the implementation is written from first principles.

---

## 13. Roadmap

| Milestone | Scope |
|---|---|
| **M1 — Engine core** | Metadata tables; DDL compiler with naming/quoting rules; change sets (propose / validate / impact / apply); standard package with Account, Contact, Opportunity; record create/read/update/retire with generated validators; field history and record events; tests against real Postgres. Single tenant database. |
| **M2 — MCP server** | Read, write, query tools; propose/apply schema changes; structured errors; tested in a real MCP host. |
| **M3 — Permissions** | Roles; object, field, and owner-based record access; two-phase permission changes with confirmation tokens. |
| **M4 — Views** | Declarative view specs; trusted renderer as MCP Apps (table and form first); validation and migration on schema change. |
| **M5 — Multi-tenancy** | Control plane; provisioning a database per tenant; routing; upgrade orchestration with pre-upgrade analysis. |
| **M6 — Reporting & chat client** | Aggregation reports; benchmarks at 100k / 1M / 10M rows; own sign-in + chat page. Analytics store if benchmarks require it. |

Tenant isolation is built in M5 but **designed for from M1**: the engine
always receives a principal with a tenant ID and never assumes a single
tenant.

---

## 14. Open questions

| # | Question | Why it matters |
|---|---|---|
| 1 | Expected number and size of tenants in years 1 and 3 | Validates database-per-tenant cost and connection-pooling design. |
| 2 | Identity provider: self-hosted vs managed | Cost, compliance, operational load. |
| 3 | Which MCP hosts fully support MCP Apps today | Determines whether views work in Claude/ChatGPT or require our own client sooner. |
| 4 | Reporting freshness requirement (real-time vs minutes) | Determines when the analytics store is needed. |
| 5 | Direct BI tool access (Power BI, Tableau) needed? | May require read replicas or a warehouse export. |
| 6 | Retention requirements for history and retired data | Partition policy, purge process. |
| 7 | Background job system | Needed in M1 for index builds and backfills. |
| 8 | Target users for early validation of the chat-only experience | The biggest product risk (§15). |

---

## 15. Risks and data gaps

| Risk | Status / mitigation |
|---|---|
| **Users may not accept a chat-only interface** for repetitive work. | No data yet. Mitigated by AI-built views (M4); validate with target users early. |
| **Cost per tenant** with database-per-tenant (connections, memory, operations). | No benchmark yet. Small lazy pools, idle-tenant handling; revisit if tenant counts reach thousands of small tenants. |
| **Upgrade orchestration across N databases.** | Change-set model, ownership rules, pre-upgrade analysis, canaries. |
| **Reporting performance** at large row counts. | No benchmark yet. M6 benchmarks; analytics store as planned fallback. |
| **DDL lock contention** on busy tenants. | `lock_timeout`, retries, background index builds, expand/backfill pattern. |
| **Prompt injection** through record data or documents. | Two-phase confirmations with unforgeable tokens; AI limited to the user's permissions; no raw SQL. |
| **SQL injection through identifiers.** | Engine-generated physical names, strict pattern, always quoted; dedicated tests. |
| **MCP Apps support varies by host.** | Verify per host; own chat client as fallback. |
| **AI-written code without expert review.** | Security-critical areas (permissions, tenant isolation, DDL compiler, auth integration) require human review before production data. |
