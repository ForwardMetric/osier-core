"""Connector framework: MCP-shaped actions and connector types.

A connector action has exactly the shape of an MCP tool (name, description, JSON
Schema input, tool annotations), so native connectors and remote MCP servers are
interchangeable (ARCHITECTURE.md §13.2).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel

SideEffect = Literal["read", "write", "external_send"]


@dataclass(frozen=True)
class Action:
    name: str
    description: str
    input_model: type[BaseModel]
    side_effect: SideEffect
    capability: str | None = None
    destructive: bool = False

    def input_schema(self) -> dict[str, Any]:
        return self.input_model.model_json_schema()

    def annotations(self) -> dict[str, Any]:
        """MCP tool annotations derived from the side-effect level."""
        return {
            "readOnlyHint": self.side_effect == "read",
            "destructiveHint": self.destructive,
            "openWorldHint": self.side_effect == "external_send",
        }

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "capability": self.capability,
            "side_effect": self.side_effect,
            "inputSchema": self.input_schema(),
            "annotations": self.annotations(),
        }


@dataclass
class ExecutionContext:
    """What a connector receives when an action runs. Secrets never leave this object."""

    config: BaseModel
    secrets: BaseModel | None
    connection: dict[str, Any]
    allow_private_network: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


class ConnectorType:
    """Base class for native connector types (platform code, git-reviewed)."""

    key: str
    version: int
    label: str
    description: str
    runtime: Literal["native", "http", "mcp"] = "native"
    config_model: type[BaseModel]
    secret_model: type[BaseModel] | None = None
    actions: tuple[Action, ...] = ()

    @property
    def capabilities(self) -> list[str]:
        return sorted({a.capability for a in self.actions if a.capability})

    def action(self, name: str) -> Action | None:
        return next((a for a in self.actions if a.name == name), None)

    def action_for(self, capability: str) -> Action | None:
        return next((a for a in self.actions if a.capability == capability), None)

    def verify(self, ctx: ExecutionContext) -> None:
        """Check that the connection works (e.g. log in). Raise ConnectorError if not."""

    def execute(self, action: Action, arguments: BaseModel, ctx: ExecutionContext) -> dict:
        raise NotImplementedError

    def describe(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "version": self.version,
            "label": self.label,
            "description": self.description,
            "runtime": self.runtime,
            "capabilities": self.capabilities,
            "actions": [a.describe() for a in self.actions],
        }
