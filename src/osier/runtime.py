"""Process-wide settings for code that runs outside a request: the credential broker and
network rules used by workflow steps that call connected apps or AI models.

The CLI (and, later, the hosted service) calls ``configure`` once at startup; tests set
it per test. Nothing here is tenant data."""

from __future__ import annotations

from typing import Any

_state: dict[str, Any] = {
    "broker": None,
    "allow_private_network": False,
    "allow_local_processes": False,
    "mcp_client_factory": None,
}


def configure(**settings: Any) -> None:
    unknown = set(settings) - set(_state)
    if unknown:
        raise ValueError(f"Unknown runtime settings {sorted(unknown)}")
    _state.update(settings)


def get(name: str) -> Any:
    return _state[name]


def connectors(session: Any) -> Any:
    """A ConnectorService for a session, with this process's broker and network rules."""
    from .connectors import ConnectorService
    from .errors import OsierError

    if _state["broker"] is None:
        raise OsierError(
            "No credential broker configured for this process.", code="CREDENTIAL_ERROR"
        )
    return ConnectorService(
        session,
        _state["broker"],
        allow_private_network=_state["allow_private_network"],
        allow_local_processes=_state["allow_local_processes"],
        mcp_client_factory=_state["mcp_client_factory"],
    )
