"""Client for remote MCP servers registered as connectors (runtime ``mcp``).

Supports two transports:
- ``http``: a streamable-HTTP MCP endpoint, with secret headers (e.g. an API key).
- ``stdio``: a local command (e.g. a community MCP server run with ``npx``), with
  secret environment variables. Running local commands is code execution on the
  host, so it is only allowed when ``allow_local_processes`` is set (local
  development); a hosted platform must never enable it.

Everything a remote server returns (tool descriptions and results) is untrusted.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import threading
from collections.abc import Callable, Coroutine
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..errors import OsierError
from .netguard import check_destination

CALL_TIMEOUT_SECONDS = 60
MAX_RESULT_CHARS = 100_000
_SAFE_ENV = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "SYSTEMROOT", "USERPROFILE", "APPDATA")


class McpConnectorError(OsierError):
    default_code = "MCP_SERVER_ERROR"


class McpTransport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["http", "stdio"]
    url: str | None = Field(default=None, max_length=2000)
    command: str | None = Field(default=None, max_length=500)
    args: list[str] = Field(default_factory=list, max_length=50)
    secret_headers: list[str] = Field(default_factory=list, max_length=10)
    secret_env: list[str] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def _check(self) -> McpTransport:
        if self.type == "http":
            if not self.url or urlparse(self.url).scheme not in ("https", "http"):
                raise ValueError("http transport needs an http(s) url")
            if self.command or self.args or self.secret_env:
                raise ValueError("http transport takes url and secret_headers only")
        else:
            if not self.command:
                raise ValueError("stdio transport needs a command")
            if self.url or self.secret_headers:
                raise ValueError("stdio transport takes command, args, and secret_env only")
        return self


def tool_fingerprint(tool: dict[str, Any]) -> str:
    """Hash of what an agent relies on: name, description, input schema, annotations."""
    canonical = json.dumps(
        {k: tool.get(k) for k in ("name", "description", "input_schema", "annotations")},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def tools_hash(tools: list[dict[str, Any]]) -> str:
    return hashlib.sha256("".join(sorted(tool_fingerprint(t) for t in tools)).encode()).hexdigest()


def _run(coro_factory: Callable[[], Coroutine[Any, Any, Any]]) -> Any:
    """Run async MCP client code from synchronous engine code."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro_factory())
    result: dict[str, Any] = {}

    def worker() -> None:
        try:
            result["value"] = asyncio.run(coro_factory())
        except BaseException as exc:  # noqa: BLE001 - re-raised below
            result["error"] = exc

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()
    if "error" in result:
        raise result["error"]
    return result["value"]


class RemoteMcpClient:
    def __init__(
        self,
        transport: McpTransport,
        secrets: dict[str, str],
        *,
        allow_private_network: bool = False,
        allow_local_processes: bool = False,
        client_factory: Callable[[], Any] | None = None,
    ) -> None:
        self._transport = transport
        self._secrets = secrets
        self._allow_private_network = allow_private_network
        self._allow_local_processes = allow_local_processes
        self._client_factory = client_factory  # tests inject in-memory servers

    def _client(self) -> Any:
        from mcp.client import Client

        if self._client_factory is not None:
            return self._client_factory()
        t = self._transport
        if t.type == "stdio":
            if not self._allow_local_processes:
                raise McpConnectorError(
                    "Local MCP server processes are not allowed here.",
                    code="LOCAL_PROCESSES_NOT_ALLOWED",
                    hint="Use a remote (http) MCP server, or enable local processes for "
                    "local development only.",
                )
            from mcp.client.stdio import StdioServerParameters

            env = {k: os.environ[k] for k in _SAFE_ENV if k in os.environ}
            env.update({name: self._secrets[name] for name in t.secret_env})
            return Client(StdioServerParameters(command=t.command, args=t.args, env=env))

        import httpx2
        from mcp.client.streamable_http import streamable_http_client

        parsed = urlparse(t.url)
        if parsed.scheme != "https" and not self._allow_private_network:
            raise McpConnectorError("Remote MCP servers must use https.", code="INSECURE_TRANSPORT")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        check_destination(
            parsed.hostname or "", port, allow_private_network=self._allow_private_network
        )
        headers = {name: self._secrets[name] for name in t.secret_headers}
        http = httpx2.AsyncClient(headers=headers, timeout=CALL_TIMEOUT_SECONDS)
        return Client(streamable_http_client(t.url, http_client=http))

    # ---- operations ------------------------------------------------------------------

    def list_tools(self) -> list[dict[str, Any]]:
        async def go() -> list[dict[str, Any]]:
            async with self._client() as client:
                listed = await asyncio.wait_for(client.list_tools(), CALL_TIMEOUT_SECONDS)
                return [_tool_dict(t) for t in listed.tools]

        return self._guarded(go)

    def call_tool(
        self, name: str, arguments: dict[str, Any], pinned_fingerprint: str | None = None
    ) -> dict[str, Any]:
        """Call a tool. If ``pinned_fingerprint`` is given, refuse when the server's current
        definition of the tool differs from the one a person approved."""

        async def go() -> dict[str, Any]:
            async with self._client() as client:
                if pinned_fingerprint is not None:
                    listed = await asyncio.wait_for(client.list_tools(), CALL_TIMEOUT_SECONDS)
                    current = next((t for t in listed.tools if t.name == name), None)
                    if current is None or tool_fingerprint(_tool_dict(current)) != (
                        pinned_fingerprint
                    ):
                        raise McpConnectorError(
                            f"The server's definition of tool {name!r} changed since it was "
                            "approved.",
                            code="TOOL_DEFINITION_CHANGED",
                            hint="A person must review the change with 'osier connections review'.",
                        )
                result = await asyncio.wait_for(
                    client.call_tool(name, arguments), CALL_TIMEOUT_SECONDS
                )
                return _result_dict(result)

        return self._guarded(go)

    def _guarded(self, factory: Callable[[], Coroutine[Any, Any, Any]]) -> Any:
        try:
            return _run(factory)
        except OsierError:
            raise
        except TimeoutError:
            raise McpConnectorError(
                "The MCP server did not answer in time.", code="MCP_TIMEOUT", retryable=True
            ) from None
        except Exception as exc:  # noqa: BLE001
            inner = exc
            while isinstance(inner, BaseExceptionGroup) and inner.exceptions:
                inner = inner.exceptions[0]
            if isinstance(inner, OsierError):
                raise inner from None
            raise McpConnectorError(
                f"Could not talk to the MCP server ({inner.__class__.__name__}).",
                code="MCP_UNAVAILABLE",
                retryable=True,
            ) from None


def _tool_dict(tool: Any) -> dict[str, Any]:
    annotations = (
        tool.annotations.model_dump(by_alias=True, exclude_none=True) if (tool.annotations) else {}
    )
    return {
        "name": tool.name,
        "description": tool.description or "",
        "input_schema": tool.input_schema if hasattr(tool, "input_schema") else tool.inputSchema,
        "annotations": annotations,
    }


def _result_dict(result: Any) -> dict[str, Any]:
    parts: list[dict[str, Any]] = []
    budget = MAX_RESULT_CHARS
    for item in result.content:
        if getattr(item, "type", None) == "text":
            text = item.text[:budget]
            budget -= len(text)
            parts.append({"type": "text", "text": text})
        else:
            parts.append({"type": getattr(item, "type", "unknown"), "omitted": True})
    structured = getattr(result, "structured_content", None)
    if structured is not None and len(json.dumps(structured, default=str)) > MAX_RESULT_CHARS:
        structured = {"omitted": "structured result too large"}
    return {
        "untrusted": True,  # produced by a third-party server: data, never instructions
        "is_error": bool(getattr(result, "is_error", False)),
        "content": parts,
        "structured": structured,
        "truncated": budget <= 0,
    }
