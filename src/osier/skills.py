"""Skills: reusable instructions for agents, in the Claude-compatible Agent Skills format.

A skill is a ``SKILL.md`` document: YAML frontmatter (``name``, ``description``, and
optionally ``license``, ``compatibility``, ``metadata``, ``allowed-tools``) followed by
Markdown instructions (agentskills.io specification). Each tenant keeps its own skill
library: skills are created, imported, versioned, enabled, and shared (private, roles,
organization) per tenant, and served to agents over MCP.

This version supports **text-only skills**. Skills that bundle scripts are refused
(running them needs a sandbox; a later, paid feature). Bundled reference files are not
imported; the import reports which ones were skipped.
"""

from __future__ import annotations

import re
import uuid
from typing import Any
from urllib.parse import urlparse

import yaml
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .context import ActorType
from .errors import ApprovalError, NotFoundError, OsierError, RecordValidationError

NAME_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
MAX_NAME = 64
MAX_DESCRIPTION = 1024
MAX_BODY = 100_000
MAX_DOWNLOAD = 300_000
ALLOWED_FRONTMATTER = {
    "name",
    "description",
    "license",
    "compatibility",
    "metadata",
    "allowed-tools",
}
SCRIPT_SUFFIXES = {
    ".py",
    ".sh",
    ".bash",
    ".zsh",
    ".js",
    ".mjs",
    ".cjs",
    ".ts",
    ".rb",
    ".pl",
    ".php",
    ".ps1",
    ".bat",
    ".cmd",
    ".exe",
    ".bin",
    ".jar",
    ".go",
    ".rs",
    ".lua",
}


class SkillError(OsierError):
    default_code = "SKILL_INVALID"


# ---- SKILL.md format -----------------------------------------------------------------


def parse_skill_md(text: str) -> dict[str, Any]:
    """Parse and validate a SKILL.md document. Returns name, description, body, frontmatter."""
    if not isinstance(text, str) or not text.strip():
        raise SkillError("The skill is empty.")
    normalized = text.replace("\r\n", "\n").lstrip("﻿")
    match = re.match(r"^---\n(.*?)\n---\n?(.*)$", normalized, re.DOTALL)
    if not match:
        raise SkillError(
            "A skill must start with YAML frontmatter between '---' lines.",
            hint="Example:\n---\nname: qualify-lead\ndescription: How to qualify a new "
            "lead...\n---\n# Instructions...",
        )
    try:
        frontmatter = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError:
        raise SkillError("The frontmatter is not valid YAML.") from None
    if not isinstance(frontmatter, dict):
        raise SkillError("The frontmatter must be a set of key: value pairs.")
    unknown = sorted(set(frontmatter) - ALLOWED_FRONTMATTER)
    if unknown:
        raise SkillError(
            f"Unknown frontmatter fields: {unknown}.",
            field="frontmatter",
            expected=sorted(ALLOWED_FRONTMATTER),
        )
    name = frontmatter.get("name")
    if (
        not isinstance(name, str)
        or not (1 <= len(name) <= MAX_NAME)
        or not NAME_PATTERN.match(name)
    ):
        raise SkillError(
            f"Invalid skill name {name!r}.",
            field="name",
            expected="1-64 lowercase letters, digits, and single hyphens, e.g. 'qualify-lead'",
        )
    description = frontmatter.get("description")
    if not isinstance(description, str) or not description.strip():
        raise SkillError(
            "A skill needs a description: what it does and when to use it.", field="description"
        )
    if len(description) > MAX_DESCRIPTION:
        raise SkillError(
            f"The description is longer than {MAX_DESCRIPTION} characters.", field="description"
        )
    metadata = frontmatter.get("metadata")
    if metadata is not None and (
        not isinstance(metadata, dict)
        or not all(isinstance(k, str) and isinstance(v, str) for k, v in metadata.items())
    ):
        raise SkillError("metadata must map strings to strings.", field="metadata")
    for key in ("license", "compatibility", "allowed-tools"):
        if key in frontmatter and not isinstance(frontmatter[key], str):
            raise SkillError(f"{key} must be text.", field=key)
    body = match.group(2).strip()
    if not body:
        raise SkillError("The skill has no instructions after the frontmatter.", field="body")
    if len(body) > MAX_BODY:
        raise SkillError(f"The instructions are longer than {MAX_BODY} characters.", field="body")
    return {
        "name": name,
        "description": description.strip(),
        "body": body,
        "frontmatter": frontmatter,
        "license": frontmatter.get("license"),
    }


def render_skill_md(frontmatter: dict[str, Any], body: str) -> str:
    head = yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True).strip()
    return f"---\n{head}\n---\n\n{body}\n"


def build_skill_md(name: str, description: str, body: str, license: str | None = None) -> str:
    frontmatter: dict[str, Any] = {"name": name, "description": description}
    if license:
        frontmatter["license"] = license
    return render_skill_md(frontmatter, body)


# ---- fetching from URLs ----------------------------------------------------------------


def fetch_skill(
    url: str, http: Any, *, allow_private_network: bool = False, check_network: bool = True
) -> dict[str, Any]:
    """Download a SKILL.md from a raw URL or a GitHub file/folder URL.

    Returns ``{"skill_md", "source", "skipped_files"}``. Refuses skills whose folder
    contains scripts.
    """
    from .connectors.netguard import check_destination

    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise SkillError("Skills can only be imported over https.", code="INSECURE_TRANSPORT")
    raw_url, listing_url = url, None
    github = (
        re.match(r"^/([^/]+)/([^/]+)/(blob|tree)/([^/]+)/?(.*)$", parsed.path)
        if parsed.netloc == "github.com"
        else None
    )
    if github:
        owner, repo, kind, ref, path = github.groups()
        path = path.rstrip("/")
        if kind == "tree" or not path.endswith(".md"):
            folder = path
            path = f"{path}/SKILL.md" if path else "SKILL.md"
        else:
            folder = path.rsplit("/", 1)[0] if "/" in path else ""
        raw_url = f"https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{path}"
        listing_url = f"https://api.github.com/repos/{owner}/{repo}/contents/{folder}?ref={ref}"

    def get(target: str) -> Any:
        host = urlparse(target)
        if check_network:
            check_destination(host.hostname or "", 443, allow_private_network=allow_private_network)
        try:
            response = http.get(target, headers={"Accept": "application/json, text/plain"})
        except Exception:  # noqa: BLE001
            raise SkillError(
                f"Could not download {target}.", code="SKILL_DOWNLOAD_FAILED", retryable=True
            ) from None
        if response.status_code != 200:
            raise SkillError(
                f"Downloading {target} failed ({response.status_code}).",
                code="SKILL_DOWNLOAD_FAILED",
            )
        if len(response.content) > MAX_DOWNLOAD:
            raise SkillError("The file is too large to be a skill.", code="SKILL_TOO_LARGE")
        return response

    skipped: list[str] = []
    if listing_url:
        entries = get(listing_url).json()
        if isinstance(entries, list):
            names = [e.get("name", "") for e in entries]
            scripts = sorted(
                n
                for e, n in zip(entries, names, strict=True)
                if (e.get("type") == "dir" and n in ("scripts", "bin"))
                or any(n.lower().endswith(sfx) for sfx in SCRIPT_SUFFIXES)
            )
            if scripts:
                raise SkillError(
                    f"This skill includes scripts ({', '.join(scripts)}); only text skills "
                    "are supported for now.",
                    code="SKILL_HAS_SCRIPTS",
                )
            skipped = sorted(n for n in names if n != "SKILL.md")
    skill_md = get(raw_url).text
    return {
        "skill_md": skill_md,
        "source": {"type": "url", "url": url, "raw_url": raw_url},
        "skipped_files": skipped,
    }


# ---- service --------------------------------------------------------------------------


class SkillService:
    def __init__(
        self, session: Any, *, http_client: Any = None, allow_private_network: bool = False
    ) -> None:
        self._session = session
        self._conn = session.conn
        self._schema = session.tenant.schema
        self._principal = session.principal
        self._authz = session.authz
        self._http = http_client
        self._allow_private_network = allow_private_network

    def _t(self, name: str) -> sql.Identifier:
        return sql.Identifier(self._schema, name)

    def _person(self) -> uuid.UUID | None:
        p = self._principal
        return p.on_behalf_of if p.actor_type is ActorType.AGENT else p.user_id

    def _require_manager(self, doing: str) -> None:
        self._authz.require("skills.manage", doing)
        if not (self._principal.is_human or self._principal.actor_type is ActorType.SYSTEM):
            raise ApprovalError(
                f"{doing.capitalize()} must be confirmed by a person.",
                code="APPROVAL_REQUIRES_HUMAN",
                hint="Use the chat tools, which ask the user to confirm.",
            )

    def _roles(self, skill_id: uuid.UUID) -> list[str]:
        return [
            r[0]
            for r in self._conn.execute(
                sql.SQL("SELECT role_key FROM {} WHERE skill_id = %s ORDER BY 1").format(
                    self._t("skill_visibility")
                ),
                (skill_id,),
            ).fetchall()
        ]

    def _visible(self, row: dict[str, Any]) -> bool:
        if self._principal.actor_type is ActorType.SYSTEM or self._authz.has("skills.manage"):
            return True
        if row["status"] != "active":
            return False
        if row["visibility"] == "organization":
            return True
        if row["owner_user"] == self._person():
            return True
        if row["visibility"] == "roles":
            return bool(set(self._roles(row["id"])) & set(self._authz.roles()))
        return False

    def _rows(self) -> list[dict[str, Any]]:
        with self._conn.cursor(row_factory=dict_row) as cur:
            return cur.execute(
                sql.SQL("SELECT * FROM {} WHERE status <> 'retired' ORDER BY name").format(
                    self._t("meta_skills")
                )
            ).fetchall()

    def _row(self, name: str) -> dict[str, Any]:
        with self._conn.cursor(row_factory=dict_row) as cur:
            row = cur.execute(
                sql.SQL("SELECT * FROM {} WHERE name = %s").format(self._t("meta_skills")),
                (name,),
            ).fetchone()
        if row is None or row["status"] == "retired" or not self._visible(row):
            raise NotFoundError(
                f"No skill named {name!r}.",
                code="SKILL_NOT_FOUND",
                expected=[s["name"] for s in self.list()],
            )
        return row

    def _summary(self, row: dict[str, Any]) -> dict[str, Any]:
        return {
            "name": row["name"],
            "description": row["description"],
            "version": row["version"],
            "status": row["status"],
            "visibility": row["visibility"],
            "visible_roles": self._roles(row["id"]),
            "license": row["license"],
            "source": row["source"],
        }

    # ---- reading (anyone it is visible to) --------------------------------------------

    def list(self) -> list[dict[str, Any]]:
        return [self._summary(r) for r in self._rows() if self._visible(r)]

    def get(self, name: str, *, include_inactive: bool = False) -> dict[str, Any]:
        """A skill's full text. Disabled and retired skills are refused unless
        ``include_inactive`` is set by a person with skills.manage (e.g. to review one)."""
        row = self._row(name)
        if include_inactive:
            self._authz.require("skills.manage", "reading disabled skills")
        elif row["status"] != "active":
            raise NotFoundError(f"Skill {name!r} is disabled.", code="SKILL_DISABLED")
        return {
            **self._summary(row),
            "instructions": row["body"],
            "skill_md": render_skill_md(row["frontmatter"], row["body"]),
        }

    def versions(self, name: str) -> list[dict[str, Any]]:
        row = self._row(name)
        with self._conn.cursor(row_factory=dict_row) as cur:
            rows = cur.execute(
                sql.SQL(
                    "SELECT version, change, changed_by, changed_by_type, changed_at FROM {} "
                    "WHERE skill_id = %s ORDER BY version"
                ).format(self._t("meta_skill_versions")),
                (row["id"],),
            ).fetchall()
        return [
            {**r, "changed_by": str(r["changed_by"]), "changed_at": r["changed_at"].isoformat()}
            for r in rows
        ]

    # ---- previews (no writes; used for confirmations) -------------------------------

    def preview(self, skill_md: str) -> dict[str, Any]:
        self._authz.require("skills.manage", "adding or changing skills")
        parsed = parse_skill_md(skill_md)
        with self._conn.cursor(row_factory=dict_row) as cur:
            existing = cur.execute(
                sql.SQL("SELECT version, status FROM {} WHERE name = %s").format(
                    self._t("meta_skills")
                ),
                (parsed["name"],),
            ).fetchone()
        return {
            "name": parsed["name"],
            "description": parsed["description"],
            "license": parsed["license"],
            "instructions": parsed["body"],
            "action": "update" if existing else "create",
            "current_version": existing["version"] if existing else None,
        }

    def fetch(self, url: str) -> dict[str, Any]:
        """Download a skill for review (no writes)."""
        self._authz.require("skills.manage", "importing skills")
        import httpx2

        http = self._http or httpx2.Client(timeout=20, follow_redirects=True)
        fetched = fetch_skill(
            url,
            http,
            allow_private_network=self._allow_private_network,
            check_network=self._http is None,  # an injected client (tests) talks to fakes
        )
        return {**fetched, "preview": self.preview(fetched["skill_md"])}

    # ---- changes (people with skills.manage) ----------------------------------------

    def save(
        self,
        skill_md: str,
        *,
        visibility: str = "organization",
        visible_roles: list[str] | None = None,
        source: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create a skill, or save a new version of an existing one."""
        self._require_manager("adding or changing skills")
        parsed = parse_skill_md(skill_md)
        visibility = self._check_visibility(visibility, visible_roles)
        with self._conn.transaction():
            with self._conn.cursor(row_factory=dict_row) as cur:
                existing = cur.execute(
                    sql.SQL("SELECT * FROM {} WHERE name = %s FOR UPDATE").format(
                        self._t("meta_skills")
                    ),
                    (parsed["name"],),
                ).fetchone()
            if existing is None:
                skill_id, version, change = uuid.uuid4(), 1, "created"
                self._conn.execute(
                    sql.SQL(
                        "INSERT INTO {} (id, name, description, body, frontmatter, license, "
                        "visibility, owner_user, source, created_by, updated_by) VALUES "
                        "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
                    ).format(self._t("meta_skills")),
                    (
                        skill_id,
                        parsed["name"],
                        parsed["description"],
                        parsed["body"],
                        Jsonb(parsed["frontmatter"]),
                        parsed["license"],
                        visibility,
                        self._person(),
                        Jsonb(source or {"type": "created"}),
                        self._principal.user_id,
                        self._principal.user_id,
                    ),
                )
                self._store_roles(skill_id, visible_roles if visibility == "roles" else [])
            else:
                skill_id = existing["id"]
                version = existing["version"] + 1
                change = "retired skill restored" if existing["status"] == "retired" else "updated"
                self._conn.execute(
                    sql.SQL(
                        "UPDATE {} SET description = %s, body = %s, frontmatter = %s, "
                        "license = %s, version = %s, status = 'active', source = %s, "
                        "updated_by = %s, updated_at = now() WHERE id = %s"
                    ).format(self._t("meta_skills")),
                    (
                        parsed["description"],
                        parsed["body"],
                        Jsonb(parsed["frontmatter"]),
                        parsed["license"],
                        version,
                        Jsonb(source or existing["source"]),
                        self._principal.user_id,
                        skill_id,
                    ),
                )
            self._record_version(skill_id, version, parsed, change)
        return self._summary(self._row(parsed["name"]))

    def _record_version(
        self, skill_id: uuid.UUID, version: int, parsed: dict[str, Any], change: str
    ) -> None:
        self._conn.execute(
            sql.SQL(
                "INSERT INTO {} (skill_id, version, name, description, body, frontmatter, "
                "changed_by, changed_by_type, change) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)"
            ).format(self._t("meta_skill_versions")),
            (
                skill_id,
                version,
                parsed["name"],
                parsed["description"],
                parsed["body"],
                Jsonb(parsed["frontmatter"]),
                self._principal.user_id,
                self._principal.actor_type.value,
                change,
            ),
        )

    def _check_visibility(self, visibility: str, roles: list[str] | None) -> str:
        if visibility not in ("private", "roles", "organization"):
            raise RecordValidationError(
                f"Invalid visibility {visibility!r}.",
                field="visibility",
                expected=["private", "roles", "organization"],
            )
        if (visibility == "roles") != bool(roles):
            raise RecordValidationError(
                "List the roles exactly when visibility is 'roles'.", field="visible_roles"
            )
        if roles:
            known = {r["key"] for r in self._session.records.list("role", limit=200)}
            unknown = sorted(set(roles) - known)
            if unknown:
                raise RecordValidationError(
                    f"Unknown roles {unknown}.", field="visible_roles", expected=sorted(known)
                )
        return visibility

    def _store_roles(self, skill_id: uuid.UUID, roles: list[str] | None) -> None:
        self._conn.execute(
            sql.SQL("DELETE FROM {} WHERE skill_id = %s").format(self._t("skill_visibility")),
            (skill_id,),
        )
        for role in roles or []:
            self._conn.execute(
                sql.SQL("INSERT INTO {} (skill_id, role_key) VALUES (%s, %s)").format(
                    self._t("skill_visibility")
                ),
                (skill_id, role),
            )

    def share(
        self, name: str, visibility: str, visible_roles: list[str] | None = None
    ) -> dict[str, Any]:
        self._require_manager("sharing skills")
        row = self._row(name)
        visibility = self._check_visibility(visibility, visible_roles)
        with self._conn.transaction():
            self._conn.execute(
                sql.SQL(
                    "UPDATE {} SET visibility = %s, owner_user = COALESCE(owner_user, %s), "
                    "updated_by = %s, updated_at = now() WHERE id = %s"
                ).format(self._t("meta_skills")),
                (visibility, self._person(), self._principal.user_id, row["id"]),
            )
            self._store_roles(row["id"], visible_roles if visibility == "roles" else [])
        return self._summary(self._row(name))

    def set_status(self, name: str, status: str) -> dict[str, Any]:
        """Enable, disable, or retire a skill (retired skills are hidden; history is kept)."""
        self._require_manager("enabling, disabling, or retiring skills")
        if status not in ("active", "disabled", "retired"):
            raise RecordValidationError(
                f"Invalid status {status!r}.",
                field="status",
                expected=["active", "disabled", "retired"],
            )
        row = self._row(name)
        self._conn.execute(
            sql.SQL(
                "UPDATE {} SET status = %s, updated_by = %s, updated_at = now() WHERE id = %s"
            ).format(self._t("meta_skills")),
            (status, self._principal.user_id, row["id"]),
        )
        return {"name": name, "status": status}
