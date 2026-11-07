"""A tiny third-party-style MCP server used by the connector tests (run over stdio).

argv[1]: a file whose contents are appended to search_notes' description, so tests can
simulate a server that changes its tool definitions. Requires NOTES_TOKEN=secret-token.
"""

import os
import sys
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

_arg = sys.argv[1] if __name__ == "__main__" and len(sys.argv) > 1 else None
variant = Path(_arg).read_text().strip() if _arg else ""
server = MCPServer(name="notes")
NOTES = [{"title": "Acme renewal", "body": "Renewal due in March."}]


def _auth() -> None:
    if os.environ.get("NOTES_TOKEN") != "secret-token":
        raise ToolError("unauthorized")


@server.tool(
    description=("Search notes by text." + (" " + variant if variant else "")),
    annotations=ToolAnnotations(readOnlyHint=True),
)
def search_notes(query: str) -> dict:
    _auth()
    hits = [n for n in NOTES if query.lower() in (n["title"] + n["body"]).lower()]
    return {"notes": hits, "count": len(hits)}


@server.tool(description="Create a note.", annotations=ToolAnnotations(readOnlyHint=False))
def create_note(title: str, body: str) -> dict:
    _auth()
    NOTES.append({"title": title, "body": body})
    return {"created": title}


@server.tool(
    description="Delete every note.",
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True),
)
def delete_all_notes() -> dict:
    _auth()
    NOTES.clear()
    return {"deleted": True}


if __name__ == "__main__":
    server.run("stdio")
