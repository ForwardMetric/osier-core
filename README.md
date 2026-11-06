# osier-core

Headless, agent-native, metadata-driven business platform engine: standard
objects plus tenant-defined objects and fields, compiled to real PostgreSQL
tables, designed to be operated through AI chat and the Model Context Protocol.

See [ARCHITECTURE.md](ARCHITECTURE.md) for the design.

## Status: Milestone 1 (engine core)

Implemented:

- Tenant schema provisioning (one schema per tenant, fully qualified SQL, empty `search_path`)
- Metadata: objects, fields, lookups, schema versions, cached snapshots
- Schema change sets: propose → validate → impact analysis → human approval → apply
  - create / update / retire object; add / update / retire field
  - picklist values, defaults, required, unique and indexed fields, history tracking
  - blockers for data conflicts (e.g. required field on populated object)
  - concurrent index builds after commit for existing tables
  - standard items protected from tenant changes; `c_` prefix for custom items
- Records: create, get, list (equality filters), update, retire
  - validation generated from metadata, structured errors for agents
  - optimistic concurrency (`row_version`), idempotency keys
  - lookup integrity; retire with `restrict` / `cascade` / `set_null`
  - field history (opt-in per field) with actor, agent, request id, provenance
- Design rules: every object and field must be documented; generic names rejected;
  schema.org `semantic_ref` links; advice for tenant proposals
- Packages (standard objects as plugins), installed by human-approved change sets:
  - `core` (always installed): Organization, User, Role, Role Assignment
  - `crm`: Account, Contact, Lead, Opportunity, Activity
  - `case_management`: Case, Case Comment
- `bootstrap_tenant`: new tenant with core package, organization, and first administrator

Not yet (see roadmap in ARCHITECTURE.md): MCP server, permissions, confirmation
tokens, views, multi-database placement, reporting, jobs and workflows.

## Quick start

```bash
uv sync
docker compose up -d
uv run pytest -q
```

```python
import psycopg
from osier import Session, bootstrap_tenant

conn = psycopg.connect("postgresql://osier:osier@localhost:5432/osier_test", autocommit=True)
boot = bootstrap_tenant(conn, organization_name="Acme Lending",
                        admin_email="ada@acme.test", admin_display_name="Ada Lovelace")
admin = Session(conn, boot.tenant, boot.admin)

install = admin.packages.propose_install(["case_management"])  # adds crm automatically
admin.packages.apply(install.id, diff_hash=install.diff_hash)

proposal = admin.schema.propose([
    {"op": "add_field", "object": "account",
     "field": {"api_name": "c_region", "label": "Sales Region", "type": "picklist",
               "description": "Sales region responsible for the account, used for territory reporting.",
               "options": {"values": ["emea", "amer", "apac"]}}},
])
print(proposal.summary, proposal.ddl, proposal.blockers, proposal.impact)
admin.schema.apply(proposal.id, diff_hash=proposal.diff_hash)

acme = admin.records.create("account", {"name": "Acme", "c_region": "emea"})
```
## License

MIT
