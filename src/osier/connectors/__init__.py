"""Connectors: let agents act in external systems (ARCHITECTURE.md §13)."""

from .base import Action, ConnectorType, ExecutionContext
from .dev_outbox import DevOutboxConnector
from .service import ConnectorService
from .smtp import ConnectorError, SmtpConnector
from .vault import CredentialBroker

CONNECTOR_TYPES: dict[str, ConnectorType] = {
    c.key: c for c in (SmtpConnector(), DevOutboxConnector())
}

__all__ = [
    "CONNECTOR_TYPES",
    "Action",
    "ConnectorError",
    "ConnectorService",
    "ConnectorType",
    "CredentialBroker",
    "ExecutionContext",
]
