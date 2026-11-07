"""Package upgrades: turn the difference between two package versions into operations.

The diff compares the definition stored at install time (old) with the registry's
current definition (new), and checks the tenant's current metadata so tenant
customizations are respected:

- **Applied automatically (additive):** new objects, new fields, new picklist
  values, and changes to labels, descriptions, semantic refs, and history tracking.
  A label or description the tenant has customized is left alone.
- **Refused (unsafe):** removed objects or fields, type changes, option changes
  other than added picklist values, required/unique/indexed/default changes,
  constraint or access changes. These need an explicit migration plan.
- **Not applied (noted):** picklist value label/description edits.
"""

from __future__ import annotations

from typing import Any

from ..errors import SchemaChangeError
from ..fieldtypes import normalize_picklist_values
from ..metadata import SchemaSnapshot

_FIELD_DEFAULTS: dict[str, Any] = {
    "description": None,
    "required": False,
    "unique": False,
    "indexed": False,
    "track_history": False,
    "default": None,
    "options": {},
    "semantic_ref": None,
}
_OBJECT_DEFAULTS: dict[str, Any] = {
    "plural_label": None,
    "description": None,
    "semantic_ref": None,
    "unique_together": [],
    "access": "open",
    "managed_by": None,
}
_DOC_ATTRS_OBJECT = ("label", "plural_label", "description", "semantic_ref")
_DOC_ATTRS_FIELD = ("label", "description", "semantic_ref", "track_history")
_UNSAFE_FIELD_ATTRS = ("type", "required", "unique", "indexed", "default")


def _norm(spec: dict[str, Any], defaults: dict[str, Any]) -> dict[str, Any]:
    return {**defaults, **spec}


def _picklist_values(options: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {v["value"]: v for v in normalize_picklist_values(options.get("values"))}


def compute_upgrade(
    package_name: str,
    old_operations: list[dict[str, Any]],
    new_operations: list[dict[str, Any]],
    snapshot: SchemaSnapshot,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Return (operations, notes). Raises SchemaChangeError for unsafe differences."""
    old_objects = {op["api_name"]: _norm(op, _OBJECT_DEFAULTS) for op in old_operations}
    new_objects = {op["api_name"]: _norm(op, _OBJECT_DEFAULTS) for op in new_operations}
    unsafe: list[str] = []
    notes: list[str] = []
    creates: list[dict[str, Any]] = []  # new objects first, so later lookups can target them
    ops: list[dict[str, Any]] = []

    for name in old_objects.keys() - new_objects.keys():
        unsafe.append(f"object {name!r} was removed")

    for name, new in new_objects.items():
        old = old_objects.get(name)
        if old is None:
            if name in snapshot.objects:
                unsafe.append(f"new object {name!r} collides with an existing object")
            else:
                creates.append(next(op for op in new_operations if op["api_name"] == name))
            continue

        current = snapshot.objects.get(name)
        if current is None or not current.active:
            unsafe.append(f"object {name!r} is missing or retired in this tenant")
            continue

        for attr in ("unique_together", "access", "managed_by"):
            if old[attr] != new[attr]:
                unsafe.append(f"{name}: {attr} changed")

        object_changes = {}
        for attr in _DOC_ATTRS_OBJECT:
            if old[attr] != new[attr] and new[attr] is not None:
                if getattr(current, attr) == old[attr]:
                    object_changes[attr] = new[attr]
                else:
                    notes.append(f"{name}.{attr} was customized by the tenant; kept as is")
        if object_changes:
            ops.append({"op": "update_object", "object": name, **object_changes})

        old_fields = {f["api_name"]: _norm(f, _FIELD_DEFAULTS) for f in old["fields"]}
        new_fields = {f["api_name"]: _norm(f, _FIELD_DEFAULTS) for f in new["fields"]}
        for fname in old_fields.keys() - new_fields.keys():
            unsafe.append(f"field {name}.{fname} was removed")

        for fname, nf in new_fields.items():
            of = old_fields.get(fname)
            if of is None:
                if fname in current.fields:
                    unsafe.append(f"new field {name}.{fname} collides with an existing field")
                else:
                    spec = next(f for f in new["fields"] if f["api_name"] == fname)
                    ops.append({"op": "add_field", "object": name, "field": spec})
                continue
            cf = current.fields.get(fname)
            if cf is None or not cf.active:
                unsafe.append(f"field {name}.{fname} is missing or retired in this tenant")
                continue
            for attr in _UNSAFE_FIELD_ATTRS:
                if of[attr] != nf[attr]:
                    unsafe.append(f"{name}.{fname}: {attr} changed")

            update: dict[str, Any] = {}
            if of["options"] != nf["options"]:
                if nf["type"] == "picklist" and of["type"] == "picklist":
                    old_values = _picklist_values(of["options"])
                    new_values = _picklist_values(nf["options"])
                    removed = old_values.keys() - new_values.keys()
                    if removed:
                        unsafe.append(f"{name}.{fname}: picklist values removed {sorted(removed)}")
                    tenant_values = {v["value"] for v in cf.options.get("values", [])}
                    added = [
                        {k: v[k] for k in ("value", "label", "description") if k in v}
                        for value, v in new_values.items()
                        if value not in old_values and value not in tenant_values
                    ]
                    if added:
                        update["add_picklist_values"] = added
                    relabelled = [
                        v for v in new_values if v in old_values and new_values[v] != old_values[v]
                    ]
                    if relabelled:
                        notes.append(
                            f"{name}.{fname}: label/description edits to picklist values "
                            f"{sorted(relabelled)} are not applied by upgrades"
                        )
                    other_keys = (set(of["options"]) | set(nf["options"])) - {"values"}
                    if any(of["options"].get(k) != nf["options"].get(k) for k in other_keys):
                        unsafe.append(f"{name}.{fname}: options changed")
                else:
                    unsafe.append(f"{name}.{fname}: options changed")

            for attr in _DOC_ATTRS_FIELD:
                if of[attr] != nf[attr]:
                    if getattr(cf, attr) == of[attr]:
                        update[attr] = nf[attr]
                    else:
                        notes.append(
                            f"{name}.{fname}.{attr} was customized by the tenant; kept as is"
                        )
            if update:
                ops.append({"op": "update_field", "object": name, "field": fname, **update})

    if unsafe:
        raise SchemaChangeError(
            f"Package {package_name!r} cannot be upgraded automatically.",
            code="PACKAGE_UPGRADE_UNSAFE",
            details={"unsafe_changes": sorted(unsafe)},
            hint="Unsafe changes need an explicit migration plan; only additive changes are "
            "applied automatically.",
        )
    return creates + ops, notes
