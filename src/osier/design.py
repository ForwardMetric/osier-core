"""Design rules: every object and field must be documented and sensibly named.

The goal is a schema that an AI (or a new team member) can understand without
guessing. Rules are deterministic, so the same change always gets the same
verdict. Hard rules raise ``SchemaChangeError``; softer guidance is returned as
``advice`` entries in the proposal's impact list.

Deterministic checks cannot guarantee *good* documentation; they reject the
obviously bad (missing, empty, placeholder, label repeated as description). A
reviewer, human or AI, should still read proposals.
"""

from __future__ import annotations

import re
from typing import Any

from .errors import SchemaChangeError
from .identifiers import CUSTOM_PREFIX

MIN_DESCRIPTION_LENGTH = 20
MIN_DESCRIPTION_WORDS = 4
MAX_SEMANTIC_REF_LENGTH = 500

_GENERIC_WORDS = frozenset(
    """
    field fields column col data info information misc miscellaneous other others temp tmp
    test testing new value val values item items thing things stuff foo bar baz x y z
    object objects table tables record records entity entities custom attr attribute
    attributes property properties extra placeholder untitled unnamed sample example
    """.split()
)
_PLACEHOLDER_DESCRIPTIONS = re.compile(
    r"^(tbd|todo|n/?a|none|test|testing|description|placeholder|lorem ipsum.*|\.+|-+)$",
    re.IGNORECASE,
)
_SEMANTIC_REF = re.compile(r"^https?://[^\s]+$")


def _humanize(api_name: str) -> str:
    name = api_name.removeprefix(CUSTOM_PREFIX)
    return name.replace("_", " ").strip().lower()


def check_name_quality(api_name: str, *, kind: str, where: str) -> None:
    """Reject names made only of generic words and numbers (c__field1, c__new_data)."""
    core = api_name.removeprefix(CUSTOM_PREFIX)
    tokens = re.findall(r"[a-z]+|\d+", core)
    meaningful = [t for t in tokens if not t.isdigit() and t not in _GENERIC_WORDS]
    if not meaningful:
        raise SchemaChangeError(
            f"{where}: {api_name!r} does not say what the {kind} holds.",
            code="POOR_NAME",
            field="api_name",
            hint=f"Name the {kind} after its meaning, e.g. 'c__contract_end_date' rather than "
            "'c__field2'.",
        )


def check_description(
    description: str | None, *, label: str, api_name: str, kind: str, where: str
) -> None:
    text = (description or "").strip()
    if not text:
        raise SchemaChangeError(
            f"{where}: {kind} {api_name!r} needs a description.",
            code="DOCUMENTATION_REQUIRED",
            field="description",
            hint=f"Explain what the {kind} represents, what values mean, and how it is used.",
        )
    if (
        len(text) < MIN_DESCRIPTION_LENGTH
        or len(text.split()) < MIN_DESCRIPTION_WORDS
        or _PLACEHOLDER_DESCRIPTIONS.fullmatch(text)
        or text.lower().rstrip(".") in {label.strip().lower(), _humanize(api_name)}
    ):
        raise SchemaChangeError(
            f"{where}: the description of {kind} {api_name!r} is too short or only repeats "
            "its name.",
            code="DOCUMENTATION_TOO_THIN",
            field="description",
            expected=f"at least {MIN_DESCRIPTION_WORDS} words and {MIN_DESCRIPTION_LENGTH} "
            "characters that explain meaning and use",
        )


def check_semantic_ref(value: str | None, *, where: str) -> str | None:
    if value is None:
        return None
    if len(value) > MAX_SEMANTIC_REF_LENGTH or not _SEMANTIC_REF.fullmatch(value):
        raise SchemaChangeError(
            f"{where}: semantic_ref must be an http(s) URI of an ontology term.",
            code="INVALID_SEMANTIC_REF",
            field="semantic_ref",
            expected="e.g. https://schema.org/Organization",
        )
    return value


def field_advice(
    *,
    api_name: str,
    type_: str,
    semantic_ref: str | None,
    description: str,
    options: dict[str, Any],
) -> list[str]:
    advice = []
    if semantic_ref is None:
        advice.append(
            f"Field {api_name!r} has no semantic_ref. If a standard term exists (for example "
            "on schema.org), linking it helps AI and integrations interpret the field."
        )
    if type_ == "boolean" and not re.match(r"^(c__)?(is|has|can|should|allow|needs)_", api_name):
        advice.append(
            f"Boolean field {api_name!r}: names like 'is_…' or 'has_…' make true/false "
            "meaning unambiguous."
        )
    if type_ == "picklist":
        undocumented = [v["value"] for v in options.get("values", []) if not v.get("description")]
        if undocumented and len(undocumented) == len(options.get("values", [])):
            advice.append(f"Picklist {api_name!r}: consider describing what each value means.")
    return advice
