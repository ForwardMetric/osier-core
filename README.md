# osier-core

Headless, agent-native, metadata-driven business platform engine: standard
objects plus tenant-defined objects and fields, compiled to real PostgreSQL
tables, operated through AI chat and the Model Context Protocol (MCP).

See [ARCHITECTURE.md](ARCHITECTURE.md) for the design.

## Run it locally with Claude

You need Python 3.12+, [uv](https://docs.astral.sh/uv/), and PostgreSQL 16
(`docker compose up -d` starts one with the expected credentials).

**1. Install and create your local tenant**

```bash
uv sync
```

```bash
docker compose up -d
```

```bash
uv run osier dev init --org "Acme Lending" --email you@example.com --name "Your Name" --packages crm,case_management
```

This creates a tenant in your local database (packages `core`, `integrations`,
`crm`, `case_management`), writes `~/.config/osier/dev.json` and an encryption
key `~/.config/osier/secret.key`, and prints the Claude setup below.

**2. Add an email connection** (credentials are typed here, never in the chat)

A safe fake that records emails instead of sending them:

```bash
uv run osier connect dev-outbox --name "Dev outbox"
```

Or real email through SMTP, e.g. Gmail with an
[app password](https://support.google.com/accounts/answer/185833) (requires
2-Step Verification on the Google account):

```bash
uv run osier connect smtp --name "Gmail" --host smtp.gmail.com --port 587 --from-address you@gmail.com --from-name "Your Name"
```

With more than one email connection, add `--default` to one of them, or tell
Claude which connection to use.

**Optional: connect other apps through their MCP servers.** Any MCP server can
become an osier connector. osier lists its tools for you to review, pins exactly
what you approved, and lets agents call them with approvals and an audit trail.

A remote server that accepts an API token (you are asked for the header value,
e.g. `Bearer <token>`):

```bash
uv run osier connect mcp --name "Notes" --description "Team notes app used for account research." --url https://example.com/mcp --secret-header Authorization
```

A local server started as a command, with a token in an environment variable:

```bash
uv run osier connect mcp --name "Notes" --description "Team notes app used for account research." --command npx --arg -y --arg <server-package> --secret-env API_TOKEN
```

Read-only tools run immediately; other tools wait for your approval. Change that
per tool with `osier connections policy <name> <tool> auto|approval|blocked`. If
the server later changes a tool's definition, calls stop until you review it with
`osier connections review <name>`. Servers that require an OAuth sign-in are not
supported yet.

**3. Connect Claude**

Claude Code (run from this directory):

```bash
claude mcp add osier --env OSIER_CONFIG=$HOME/.config/osier/dev.json -- $(pwd)/.venv/bin/osier mcp
```

Claude Desktop: add the block printed by `uv run osier client-setup` to
`claude_desktop_config.json` under `"mcpServers"`, then restart Claude Desktop.

**4. Work with it in Claude**, for example:

> Add a lead: Jane Doe from Globex, jane@globex.example, found through our website.
> Then send her a short follow-up email and log it on the lead.

> Show me all leads from the web that are still new, newest first.

> Add a Budget field to leads.

**5. Confirm sensitive actions in the chat**

Agents read and write records directly. Schema changes, package installs,
emails, app actions, and connecting apps ask **you** to confirm in a prompt shown
by your Claude app; nothing happens if you decline. If your app cannot show these
prompts, the action waits: approve it later in chat (`approve_pending`) or in the
terminal:

```bash
uv run osier approvals list
```

```bash
uv run osier approvals approve <id>
```

**6. Roles: who may do what**

Developers and administrators can change objects, fields, packages, and
connections; members work with records and have agents send email and use apps.
Anyone else asking for a schema change is told which role they need. To try it
locally, add a person and run Claude's osier server as them:

```bash
uv run osier dev add-user --email sam@example.com --name "Sam Sales" --role member
```

```bash
claude mcp add osier-sam --env OSIER_CONFIG=$HOME/.config/osier/dev.json -- $(pwd)/.venv/bin/osier mcp --as sam@example.com
```

**7. Connect apps with OAuth (e.g. Notion)**

Ask Claude: *"Connect our Notion workspace for the whole team"*. Claude starts
the sign-in and gives you a link (`connect_app`); you sign in at Notion, then
confirm the tools Notion offers. Or from the terminal:

```bash
uv run osier connect mcp --oauth --name "Notion" --description "Company wiki and meeting notes." --url https://mcp.notion.com/mcp
```

Share a connection with specific roles with `--visibility roles --role <role>`,
or later with `osier connections share`.

If the provider rejects the sign-in with an invalid redirect / callback error,
change how osier's callback address is written and try again:

```bash
uv run osier dev oauth-redirect localhost
```

If it still fails, the provider probably requires an `https` callback. Run a
tunnel to `http://127.0.0.1:8765` (for example Cloudflare Tunnel or ngrok) and
give osier its address:

```bash
uv run osier dev oauth-redirect https://<your-tunnel-host>
```

### The /osier menu

Every tenant starts with eight starter skills (add leads, follow up with a lead,
review the pipeline, log an activity, automate something, connect an app, design
your own objects, what is waiting). In a client that shows MCP prompts, type
`/osier` (Claude Code lists it as `/mcp__osier__osier`) to see the menu. In any
client, say "osier menu": the `osier_menu` tool returns the options. Starter
skills are ordinary skills: edit, share, or retire them. Prompt display in Claude
Desktop is not verified yet.

### Skills

Skills are instructions for recurring work, in the Agent Skills `SKILL.md`
format. Ask Claude to write one ("save a skill for qualifying leads"), or to
import a published one, then confirm the full text in the prompt. Only text
skills are supported; skills that include scripts are refused for now. From the
terminal:

```bash
uv run osier skills import https://github.com/<owner>/<repo>/tree/main/<skill-folder>
```

```bash
uv run osier skills add path/to/skill-folder
```

`osier skills list | show | versions | status | share` manage the library.

### Workflows

A workflow is a saved process: read and find records, create or update them,
branch on conditions, and pause for a person's approval. Ask Claude to build one
("when I give you a lead, ask me to approve and then mark it qualified"), confirm
the steps, then ask Claude to run it. Runs use your own permissions, and approvals
appear in `list_pending_approvals`. From the terminal, with a JSON or YAML
definition (format in `src/osier/workflows/spec.py`):

```bash
uv run osier workflows add qualify-lead.yaml
```

```bash
uv run osier workflows start qualify-lead --input lead_id=<id>
```

`osier workflows list | show | versions | runs | run | cancel | status`, and
`osier approvals list | approve | reject` for waiting runs.

Workflows can start themselves: when a record is created or updated (optionally
only when certain fields change or conditions hold), or on a schedule (cron with a
time zone, at most every 5 minutes). Triggered runs and `--background` runs are
executed by the worker; keep it running in a terminal:

```bash
uv run osier worker
```

Runs from record changes use the permissions of whoever made the change; scheduled
runs use the permissions of the person who saved the workflow. Changes made by
workflows never trigger other workflows. `osier jobs list` shows background jobs.

Each organization has its own job queue (on Procrastinate) and its own settings:
how many jobs run at once, retries and their wait, pausing, and how long finished
jobs are kept, within platform limits. Change them in chat ("run up to 4 jobs at
once") or from the terminal:

```bash
uv run osier jobs settings --set concurrency=4
```

### Trying a workflow first

Ask Claude to test a workflow (saved or still a draft) with sample inputs, or run
`uv run osier workflows test <name-or-file> --input lead_id=<id>`. Record changes are
rolled back, emails and app actions are only previewed, approvals are simulated
(`--reject` to try the rejection path), and AI steps return placeholders or outputs you
give (`--ai-outputs`); `--real-ai` calls the models and is charged.

### Connected apps in workflows

A workflow step can send email (`"type": "connector", "capability": "email.send"`) or
call a tool of a connected app (`"connection": "Notion", "tool": "search"`). It uses the
permissions of whoever started the run, and the connection's tool policies: an action
that needs approval pauses the run until someone approves it in the approval queue,
then the worker continues the run.

### Calculations in workflows

A `transform` step computes values with CEL expressions, for example a debt-to-income
ratio: `"dti": "double(steps.applicant.record.monthly_debt) / double(steps.applicant.record.monthly_income)"`.
Later steps use `{{steps.<step>.dti}}`, and conditions can branch on an expression such
as `steps.calc.dti > 0.43`. Expressions cannot run code or reach anything outside the
workflow's own data.

### AI steps

Workflows can include AI steps that use your organization's own model connections:
OpenAI, OpenRouter (one key for hundreds of models, including Claude), or any
OpenAI-compatible server (vLLM, Ollama, Groq, ...). An administrator adds connections
(the key prompt is hidden; keys are stored encrypted and never shown again) and names
model profiles that steps refer to:

```bash
uv run osier ai connect main --provider openrouter --model <model-a> --model <model-b>
```

```bash
uv run osier ai profile careful --connection main --model <model-a> --effort high --default
```

```bash
uv run osier ai profile fast --connection main --model <model-b>
```

For a self-hosted server use `--provider compatible --api-base https://...`.

An AI step names a profile (`"model": "fast"`) or uses the default. It gets
instructions, optional skill text, the data you pass it, and only the record tools and
objects you list; it must return the fields you declare. AI steps run in the worker and
count against each connection's monthly token and dollar limits (`osier ai status`).

## The web portal

Instead of the terminal, administrators can configure everything in a browser. Start it:

```bash
uv run osier portal
```

It prints a one-time sign-in link; open it and choose who to sign in as (this is the
local development sign-in). Pages:

- **AI models:** connect OpenAI, OpenRouter, or a compatible server. Keys are stored
  encrypted and never shown again. Create model profiles and set limits.
- **Apps:** a gallery with one tile per kind of app. Gmail and SendGrid need only an
  address and a key (server settings are filled in); Notion and Zapier sign in through
  their own site (OAuth) and you review the tools before anything is connected; "Custom
  app" takes any MCP server. Choose what agents may do with each tool and who can use
  each app. OAuth tiles return to the portal's `/oauth/callback`; some providers accept
  only https return addresses, so test them on a hosted portal.
- **Approvals:** approve or reject what agents propose: schema changes, emails and app
  actions, and workflow approvals.
- **Workflows, Skills, People:** see and run workflows (or try them with nothing kept),
  add skills (you read the full text first), add people and assign roles.

Run `uv run osier worker` alongside it so triggered workflows and background jobs run.
What you configure here is what Claude can use through the MCP server. Hosting it for
claude.ai (HTTPS sign-in and per-user authorization) is not built yet.

## MCP tools

| Tool | What it does |
|---|---|
| `describe_schema` | Objects, fields, types, allowed values, descriptions |
| `query_records` | Filters (`eq`, `ne`, `gt`…, `in`, `contains`, `starts_with`, `is_null`), sorting, paging, lookup expansion |
| `get_record`, `create_record`, `update_record`, `retire_record` | Record operations with validation and structured errors |
| `get_record_history` | Field history: old/new values, person or agent, provenance |
| `propose_schema_change`, `get_change_set` | New objects or fields; you confirm (Developer role) |
| `list_packages`, `install_packages` | Standard-object packages; you confirm |
| `list_connections`, `send_email`, `get_connector_call` | Email through your connections; sends wait for approval and are logged as activities |
| `list_connector_tools`, `call_connector_tool` | Use connected apps' MCP tools: pinned definitions, per-tool approval policy, untrusted results |
| `connect_app`, `finish_app_connection`, `share_connection` | Connect apps with OAuth sign-in and decide who may use them |
| `list_skills`, `get_skill` | Your organization's skills (instructions for recurring work); agents check them first |
| `save_skill`, `import_skill`, `update_skill_settings` | Add, update, import (GitHub or a URL), share, disable skills; text only; you confirm |
| `list_workflows`, `get_workflow`, `save_workflow`, `update_workflow_status` | Repeatable processes: record steps, conditions, approvals; defining one needs Developer or Admin; you confirm |
| `start_workflow`, `get_workflow_run`, `list_workflow_runs`, `decide_workflow_approval`, `cancel_workflow_run` | Run workflows with your own permissions; approvals pause the run until a person decides |
| `whoami`, `list_pending_approvals`, `approve_pending` | Your roles and permissions; review and confirm waiting items |

## What is implemented

- **Engine:** tenant schemas with a central isolation guard; metadata compiled to
  real tables; schema change sets with impact analysis, blockers, and human
  approval; record validation, optimistic concurrency, idempotency, lookups and
  retire rules, field history with provenance; enforced documentation and naming
  rules with schema.org links.
- **Packages:** system (`core`, `integrations`) and optional (`crm`,
  `case_management`); install, uninstall, additive upgrades; object access levels
  `open`, `protected`, `engine_managed`.
- **Queries:** structured, validated, parameterized.
- **Connectors:** MCP-shaped actions; SMTP and a development outbox; any MCP
  server (remote over HTTPS with token headers, or a local command) with pinned
  tools, per-tool policies, and change detection; encrypted credentials;
  capability + purpose routing; approval queue; audit; automatic CRM logging.
- **Skills:** text skills in the Agent Skills (`SKILL.md`) format per tenant, with
  versions, visibility, import from GitHub, and `osier skills` commands.
- **Workflows:** declarative definitions (no code) run on LangGraph, with record
  steps, conditions, and approvals that pause the run; checkpoints in the tenant's
  schema; record and schedule triggers; a job queue per organization on Procrastinate
  with configurable concurrency and retries; `osier worker`; `osier workflows` and `osier jobs` commands.
- **Web portal:** a local browser UI for AI models, connected apps, approvals, workflows,
  skills, and people (`osier portal`).
- **MCP server and CLI** for local use with Claude Desktop and Claude Code.

Not yet (see the roadmap in ARCHITECTURE.md): hosting osier itself (HTTPS, everyone
signing in to osier), object- and field-level permissions, views, marketing email,
multi-database placement, reporting, skills with scripts.

## Using the engine from Python

```python
import psycopg
from osier import Session, bootstrap_tenant

conn = psycopg.connect("postgresql://osier:osier@localhost:5432/osier_test", autocommit=True)
boot = bootstrap_tenant(
    conn,
    organization_name="Acme Lending",
    admin_email="ada@acme.test",
    admin_display_name="Ada Lovelace",
)
admin = Session(conn, boot.tenant, boot.admin)

install = admin.packages.propose_install(["case_management"])  # adds crm automatically
admin.packages.apply(install.id, diff_hash=install.diff_hash)

lead = admin.records.create("lead", {"family_name": "Doe", "company_name": "Globex"})
print(
    admin.records.query(
        {
            "object": "lead",
            "filters": [{"field": "company_name", "op": "contains", "value": "glob"}],
        }
    )
)
```

## Development

```bash
uv run pytest -q
```

```bash
uv run ruff check . && uv run ruff format --check .
```

Tests run against real PostgreSQL (`OSIER_TEST_DATABASE_URL`, default matches
`docker-compose.yml`) and fail if it is not reachable.

## License

MIT
