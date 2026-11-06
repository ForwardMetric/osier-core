"""Small helpers for writing package definitions readably."""

from __future__ import annotations

from typing import Any

SCHEMA_ORG = "https://schema.org/"


def field(
    api_name: str,
    label: str,
    type: str,
    description: str,
    *,
    sdo: str | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "api_name": api_name,
        "label": label,
        "type": type,
        "description": description,
        **kwargs,
    }
    if sdo:
        spec["semantic_ref"] = SCHEMA_ORG + sdo
    return spec


def picklist(*values: tuple[str, str, str]) -> dict[str, Any]:
    """values: (value, label, description)."""
    return {"values": [{"value": v, "label": label, "description": d} for v, label, d in values]}


def lookup(target: str, on_delete: str = "set_null") -> dict[str, Any]:
    return {"target": target, "on_delete": on_delete}


def create_object(
    api_name: str,
    label: str,
    plural_label: str,
    description: str,
    fields: list[dict[str, Any]],
    *,
    sdo: str | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    op: dict[str, Any] = {
        "op": "create_object",
        "api_name": api_name,
        "label": label,
        "plural_label": plural_label,
        "description": description,
        "fields": fields,
        **kwargs,
    }
    if sdo:
        op["semantic_ref"] = SCHEMA_ORG + sdo
    return op


def postal_address(prefix: str, label: str, whose: str) -> list[dict[str, Any]]:
    """Flattened schema.org PostalAddress fields."""
    return [
        field(
            f"{prefix}street_address",
            f"{label} Street",
            "text",
            f"Street address (number, street, unit) of the {whose}.",
            sdo="streetAddress",
        ),
        field(
            f"{prefix}address_locality",
            f"{label} City",
            "text",
            f"City or locality of the {whose}.",
            sdo="addressLocality",
        ),
        field(
            f"{prefix}address_region",
            f"{label} Region",
            "text",
            f"State, province, or region of the {whose}.",
            sdo="addressRegion",
        ),
        field(
            f"{prefix}postal_code",
            f"{label} Postal Code",
            "text",
            f"Postal or ZIP code of the {whose}.",
            sdo="postalCode",
            options={"max_length": 20},
        ),
        field(
            f"{prefix}address_country",
            f"{label} Country",
            "text",
            f"Country of the {whose} as an ISO 3166-1 alpha-2 code, e.g. US or GB.",
            sdo="addressCountry",
            options={"format": "country_code", "max_length": 2},
        ),
    ]
