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
- Standard objects: Account, Contact, Opportunity

Not yet (see roadmap in ARCHITECTURE.md): MCP server, permissions, confirmation
tokens, views, multi-database placement, reporting, jobs and workflows.

## Quick start

```bash
uv sync
docker compose up -d
uv run pytest -q
```

```python
import uuid, psycopg
from osier import Principal, Session, provision_tenant, standard

conn = psycopg.connect("postgresql://osier:osier@localhost:5432/osier_test", autocommit=True)
tenant = provision_tenant(conn)
admin = Session(conn, tenant, Principal(user_id=uuid.uuid4()))
standard.install(admin)

proposal = admin.schema.propose(
    [
        {
            "op": "add_field",
            "object": "account",
            "field": {
                "api_name": "c_region",
                "label": "Region",
                "type": "picklist",
                "options": {"values": ["emea", "amer", "apac"]},
            },
        },
    ]
)
print(proposal.summary, proposal.ddl, proposal.blockers)
admin.schema.apply(proposal.id, diff_hash=proposal.diff_hash)

acme = admin.records.create("account", {"name": "Acme", "c_region": "emea"})
```

## License

MIT
