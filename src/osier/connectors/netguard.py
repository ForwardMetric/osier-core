"""Outbound network checks for connectors (SSRF defense, ARCHITECTURE.md §13.8)."""

from __future__ import annotations

import ipaddress
import socket

from ..errors import OsierError


class NetworkPolicyError(OsierError):
    default_code = "DESTINATION_NOT_ALLOWED"


def check_destination(host: str, port: int, *, allow_private_network: bool) -> None:
    """Refuse private, loopback, link-local (incl. cloud metadata) and reserved addresses
    unless explicitly allowed (local development)."""
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        raise NetworkPolicyError(
            f"Host {host!r} could not be resolved.", code="DESTINATION_UNRESOLVABLE", retryable=True
        ) from None
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if not allow_private_network and (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_reserved
            or address.is_multicast
            or address.is_unspecified
        ):
            raise NetworkPolicyError(
                f"Connections to {host!r} ({address}) are not allowed: it is a private or "
                "internal address.",
                hint="Use a public host, or enable private network access for local "
                "development only.",
            )
