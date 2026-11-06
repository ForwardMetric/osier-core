# osier-core — working notes for Claude and contributors

Read `ARCHITECTURE.md` first. It is the source of truth for design decisions;
if a change alters a decision, update it in the same pull request.

## Commands

```bash
uv sync                      # install dependencies
docker compose up -d         # local PostgreSQL 16 (or set OSIER_TEST_DATABASE_URL)
uv run pytest -q             # tests run against real PostgreSQL
uv run ruff check . && uv run ruff format --check .
```

## Layout

- `src/osier/identifiers.py` — naming rules (API names, tenant schemas, index names)
- `src/osier/fieldtypes.py` — field type registry (options, column type, validator)
- `src/osier/metadata.py` — versioned metadata snapshots and cache
- `src/osier/changesets.py` — schema change sets: propose / validate / impact / apply
- `src/osier/records.py` — record create / read / update / retire, history, provenance
- `src/osier/db.py` — `TenantConnection`: the central check every statement passes through
- `src/osier/session.py` — tenant + principal scoped entry point
- `src/osier/design.py` — design rules: required documentation, naming quality, ontology refs
- `src/osier/packages/` — installable standard-object packages (core, crm, case_management)
- `src/osier/tenant.py` — tenant schema provisioning
- `src/osier/bootstrap.py` — new tenant with core package, organization, first admin

## Rules

1. **No SQL built from strings.** Compose SQL with `psycopg.sql`. Identifiers go
   through `sql.Identifier`, values through parameters or `sql.Literal`. Never
   put user, AI, or record data into SQL text.
2. **Always qualify tables with the tenant schema** (`sql.Identifier(schema, table)`).
   Sessions set an empty `search_path`, so unqualified names fail by design.
   Engine services only ever use the session's `TenantConnection` (`db.py`), which
   checks every statement; never pass a raw psycopg connection into a service.
3. **Schema changes only through change sets.** No hand-written DDL for tenant
   objects. Platform changes use `source="platform"` and the system principal.
4. **Record writes only through `RecordService`**, so validation, history,
   events, and attribution cannot be skipped.
5. **Errors are `OsierError` subclasses with stable `code`s**, plus `field`,
   `expected`, `hint`, and `retryable` where useful. Agents rely on them; do not
   rename codes casually.
6. **Agents cannot approve** schema (and later permission) changes. Keep that
   check in the engine, never only in a client.
7. **Tests use real PostgreSQL**, one fresh tenant schema per test. Add tests for
   every new operation, error code, and security-relevant rule.
8. **License hygiene:** this repository is MIT. Do not copy or adapt code from
   projects under incompatible licenses (e.g. Directus, Twenty, Baserow
   premium/enterprise, NocoDB). Ideas and public documentation are fine.
9. Do not put AI model identifiers in commits, code, or docs.
10. **Document everything you add to a schema.** Objects and fields need real
    descriptions (the engine enforces it) and a schema.org `semantic_ref` where a
    matching term exists. Never invent ontology URIs.
11. **Packages are metadata only.** Standard objects are added or changed by
    editing a package in `src/osier/packages/` and bumping its version, never by
    hand-written change sets.
