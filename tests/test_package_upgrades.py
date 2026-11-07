"""System packages, package upgrades, and engine-managed objects."""

import copy
import dataclasses

import pytest
from conftest import add, apply

from osier.errors import ApprovalError, SchemaChangeError
from osier.packages import REGISTRY, Package, validate_registry
from osier.packages._dsl import create_object, field, picklist


def install(session, *names):
    proposal = session.packages.propose_install(list(names))
    session.packages.apply(proposal.id, diff_hash=proposal.diff_hash)


def release(monkeypatch, name, mutate):
    """Simulate a new platform release of package ``name`` (version + 1)."""
    old = REGISTRY[name]
    operations = copy.deepcopy(old.operations)
    mutate(operations)
    new = dataclasses.replace(old, version=old.version + 1, operations=operations)
    monkeypatch.setitem(REGISTRY, name, new)
    return new


def obj_op(operations, name):
    return next(op for op in operations if op["api_name"] == name)


def fld_spec(op, name):
    return next(f for f in op["fields"] if f["api_name"] == name)


def upgrade(session, name):
    proposal = session.packages.propose_upgrade(name)
    assert proposal is not None and proposal.can_apply, proposal and proposal.blockers
    session.packages.apply(proposal.id, diff_hash=proposal.diff_hash)
    return proposal


def installed_version(session, name):
    return next(p for p in session.packages.available() if p["package"] == name)[
        "installed_version"
    ]


# --- system packages ------------------------------------------------------------------


def test_core_is_a_system_package(session):
    core = next(p for p in session.packages.available() if p["package"] == "core")
    assert core["system"] is True and core["removable"] is False
    crm = next(p for p in session.packages.available() if p["package"] == "crm")
    assert crm["system"] is False and crm["status"] == "available"


def test_registry_rejects_system_package_depending_on_optional():
    bad = dict(REGISTRY)
    bad["zz_sys"] = Package("zz_sys", 1, "Z", "Z", ("crm",), [], system=True)
    with pytest.raises(ValueError):
        validate_registry(bad)


def test_ensure_current_installs_new_system_packages(session, monkeypatch):
    integrations = Package(
        name="integrations",
        version=1,
        label="Integrations",
        description="Connections to external systems used by agents.",
        depends=("core",),
        system=True,
        operations=[
            create_object(
                "connection",
                "Connection",
                "Connections",
                "A tenant's link to an external system such as an email provider.",
                access="engine_managed",
                managed_by="connectors",
                fields=[
                    field("name", "Name", "text", "Display name of the connection.", required=True),
                    field(
                        "status",
                        "Status",
                        "picklist",
                        "Whether the connection works.",
                        required=True,
                        default="pending",
                        options=picklist(
                            ("pending", "Pending", "Not connected yet."),
                            ("active", "Active", "Working."),
                        ),
                    ),
                ],
            )
        ],
    )
    monkeypatch.setitem(REGISTRY, "integrations", integrations)
    session.packages.ensure_current()
    assert session.packages.states()["integrations"] == "installed"
    with pytest.raises(SchemaChangeError) as exc:
        session.packages.propose_uninstall("integrations")
    assert exc.value.code == "PACKAGE_NOT_REMOVABLE"

    # engine_managed: read-only for everyone except the owning engine service
    with pytest.raises(ApprovalError) as exc:
        session.records.create("connection", {"name": "Mailbox"})
    assert exc.value.code == "ENGINE_MANAGED_OBJECT"
    with pytest.raises(ApprovalError):
        session.records.for_engine("billing").create("connection", {"name": "Mailbox"})
    conn = session.records.for_engine("connectors").create("connection", {"name": "Mailbox"})
    assert session.records.get("connection", conn["id"])["status"] == "pending"
    schema = {o["api_name"]: o for o in session.describe_schema()["objects"]}
    assert schema["connection"]["access"] == "engine_managed"
    assert schema["connection"]["managed_by"] == "connectors"

    # tenants cannot extend engine-managed objects (their records cannot be written)
    with pytest.raises(SchemaChangeError) as exc:
        session.schema.propose([add("connection", "c__cost_center", "text")])
    assert exc.value.code == "ENGINE_MANAGED_OBJECT"


# --- upgrades ---------------------------------------------------------------------------


def test_upgrade_is_none_when_current(crm):
    assert crm.packages.propose_upgrade("crm") is None


def test_additive_upgrade_adds_objects_fields_and_values(crm, monkeypatch):
    acme = crm.records.create("account", {"name": "Acme"})

    def mutate(ops):
        opp = obj_op(ops, "opportunity")
        opp["fields"].append(
            field(
                "forecast_category",
                "Forecast Category",
                "picklist",
                "How the deal counts in the sales forecast.",
                options=picklist(
                    ("pipeline", "Pipeline", "Open, not committed."),
                    ("commit", "Commit", "Expected to close."),
                ),
            )
        )
        fld_spec(opp, "stage")["options"]["values"].append(
            {"value": "on_hold", "label": "On Hold", "description": "Paused by the customer."}
        )
        fld_spec(obj_op(ops, "account"), "website")["description"] = (
            "Main public website of the organization, used for research."
        )
        ops.append(
            create_object(
                "competitor",
                "Competitor",
                "Competitors",
                "An organization you compete with for deals.",
                fields=[
                    field("name", "Name", "text", "Name of the competing company.", required=True)
                ],
            )
        )

    release(monkeypatch, "crm", mutate)
    proposal = upgrade(crm, "crm")
    assert proposal.summary[0] == "Upgrade package 'crm' from version 1 to 2."
    snapshot = crm.snapshot()
    opp = snapshot.get_object("opportunity")
    assert "forecast_category" in opp.active_fields
    assert "on_hold" in [v["value"] for v in opp.fields["stage"].options["values"]]
    assert snapshot.get_object("competitor").package == "crm"
    assert "research" in snapshot.get_object("account").fields["website"].description
    assert installed_version(crm, "crm") == 2
    assert crm.records.get("account", acme["id"])["name"] == "Acme"  # data untouched
    assert crm.packages.propose_upgrade("crm") is None


def test_upgrade_keeps_tenant_customizations(crm, monkeypatch):
    apply(crm, [{"op": "update_field", "object": "account", "field": "name", "label": "Company"}])
    apply(crm, [add("account", "c__region", "text")])

    def mutate(ops):
        fld_spec(obj_op(ops, "account"), "name")["label"] = "Organization Name"

    release(monkeypatch, "crm", mutate)
    notes = crm.packages.upgrade_notes("crm")
    assert any("account.name.label was customized" in n for n in notes)
    upgrade(crm, "crm")
    account = crm.snapshot().get_object("account")
    assert account.fields["name"].label == "Company"  # tenant's label wins
    assert "c__region" in account.active_fields  # custom fields untouched


@pytest.mark.parametrize(
    ("mutate", "fragment"),
    [
        (lambda ops: obj_op(ops, "account")["fields"].pop(), "was removed"),
        (
            lambda ops: fld_spec(obj_op(ops, "account"), "website").update(type="text"),
            "type changed",
        ),
        (
            lambda ops: fld_spec(obj_op(ops, "contact"), "family_name").update(required=False),
            "required changed",
        ),
        (
            lambda ops: fld_spec(obj_op(ops, "account"), "name").update(
                options={"max_length": 500}
            ),
            "options changed",
        ),
        (
            lambda ops: fld_spec(obj_op(ops, "opportunity"), "stage")["options"]["values"].pop(),
            "picklist values removed",
        ),
        (lambda ops: ops.remove(obj_op(ops, "lead")), "object 'lead' was removed"),
    ],
)
def test_unsafe_upgrades_are_refused(crm, monkeypatch, mutate, fragment):
    release(monkeypatch, "crm", mutate)
    with pytest.raises(SchemaChangeError) as exc:
        crm.packages.propose_upgrade("crm")
    assert exc.value.code == "PACKAGE_UPGRADE_UNSAFE"
    assert any(fragment in c for c in exc.value.details["unsafe_changes"])
    assert installed_version(crm, "crm") == 1


def test_upgrade_operations_cannot_be_tampered_with(crm, monkeypatch):
    release(monkeypatch, "crm", lambda ops: None)
    ops = [
        {"op": "upgrade_package", "package": "crm", "from_version": 1, "to_version": 2},
        add("account", "backdoor", "text"),
    ]
    with pytest.raises(SchemaChangeError) as exc:
        crm.schema.propose(ops, source="package")
    assert exc.value.code in ("PACKAGE_MISMATCH", "INVALID_NAME")
    ops[1] = {"op": "update_object", "object": "account", "label": "Hacked"}
    with pytest.raises(SchemaChangeError) as exc:
        crm.schema.propose(ops, source="package")
    assert exc.value.code == "PACKAGE_MISMATCH"


def test_agent_can_propose_upgrade_but_not_apply(agent, admin, monkeypatch):
    release(monkeypatch, "crm", lambda ops: None)
    proposal = agent.packages.propose_upgrade("crm")
    with pytest.raises(ApprovalError):
        agent.packages.apply(proposal.id, diff_hash=proposal.diff_hash)
    agent.as_principal(admin).packages.apply(proposal.id, diff_hash=proposal.diff_hash)
    assert installed_version(agent, "crm") == 2


def test_ensure_current_upgrades_installed_packages_automatically(crm, monkeypatch):
    def mutate(ops):
        obj_op(ops, "lead")["fields"].append(
            field(
                "rating",
                "Rating",
                "integer",
                "How promising the lead is, from 1 to 5.",
                options={"min": 1, "max": 5},
            )
        )

    release(monkeypatch, "crm", mutate)
    results = crm.packages.ensure_current()
    assert len(results) == 1
    assert "rating" in crm.snapshot().get_object("lead").active_fields
    assert installed_version(crm, "crm") == 2


def test_new_required_field_without_default_on_populated_object_is_blocked(crm, monkeypatch):
    crm.records.create("account", {"name": "Acme"})

    def mutate(ops):
        obj_op(ops, "account")["fields"].append(
            field("tier", "Tier", "text", "Service tier assigned to the account.", required=True)
        )

    release(monkeypatch, "crm", mutate)
    proposal = crm.packages.propose_upgrade("crm")
    assert not proposal.can_apply
    assert proposal.blockers[0]["records"] == 1


def test_downgrade_is_refused(crm, monkeypatch):
    old = REGISTRY["crm"]
    monkeypatch.setitem(REGISTRY, "crm", dataclasses.replace(old, version=0))
    with pytest.raises(SchemaChangeError) as exc:
        crm.packages.propose_upgrade("crm")
    assert exc.value.code == "PACKAGE_DOWNGRADE_NOT_SUPPORTED"
