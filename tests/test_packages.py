import pytest
from conftest import add, apply, obj

from osier.errors import ApprovalError, NotFoundError, SchemaChangeError
from osier.packages import REGISTRY


def install(session, *names):
    proposal = session.packages.propose_install(list(names))
    session.packages.apply(proposal.id, diff_hash=proposal.diff_hash)
    return proposal


def states(session):
    return {p["package"]: p["status"] for p in session.packages.available()}


def test_core_is_installed_by_default(session):
    assert states(session) == {
        "core": "installed",
        "integrations": "installed",
        "crm": "available",
        "case_management": "available",
    }


def test_install_resolves_dependencies(session):
    proposal = install(session, "case_management")
    assert proposal.summary[0] == "Install package 'crm' (version 1)."
    assert states(session)["crm"] == "installed"
    assert states(session)["case_management"] == "installed"
    case = session.snapshot().get_object("support_case")
    assert case.package == "case_management" and case.owner.value == "standard"


def test_case_management_end_to_end(session):
    install(session, "case_management")
    acme = session.records.create("account", {"name": "Acme"})
    jane = session.records.create("contact", {"family_name": "Doe", "account": acme["id"]})
    case = session.records.create(
        "support_case",
        {
            "subject": "Cannot export statements",
            "account": acme["id"],
            "contact": jane["id"],
            "origin": "email",
        },
    )
    assert case["status"] == "new" and case["priority"] == "medium"
    session.records.create("case_comment", {"support_case": case["id"], "body": "Looking."})
    report = session.records.retire("support_case", case["id"], expected_row_version=1)
    assert {r["object"] for r in report["retired"]} == {"support_case", "case_comment"}


def test_installing_twice_is_rejected(crm):
    with pytest.raises(SchemaChangeError) as exc:
        crm.packages.propose_install(["crm"])
    assert exc.value.code == "PACKAGE_ALREADY_INSTALLED"


def test_unknown_package(session):
    with pytest.raises(NotFoundError) as exc:
        session.packages.propose_install(["erp"])
    assert exc.value.code == "PACKAGE_NOT_FOUND"
    assert "crm" in exc.value.expected


def test_package_operations_cannot_be_tampered_with(session):
    ops = REGISTRY["crm"].install_operations()
    ops[1] = {**ops[1], "label": "Hacked"}
    with pytest.raises(SchemaChangeError) as exc:
        session.schema.propose(ops, source="package")
    assert exc.value.code == "PACKAGE_MISMATCH"


def test_package_change_sets_cannot_smuggle_extra_objects(session):
    ops = [*REGISTRY["crm"].install_operations(), obj("backdoor", "Backdoor Object")]
    with pytest.raises(SchemaChangeError) as exc:
        session.schema.propose(ops, source="package")
    assert exc.value.code == "PACKAGE_MISMATCH"


def test_dependency_must_be_installed_when_proposing_directly(session):
    with pytest.raises(SchemaChangeError) as exc:
        session.schema.propose(REGISTRY["case_management"].install_operations(), source="package")
    assert exc.value.code == "PACKAGE_DEPENDENCY_MISSING"


def test_agent_can_propose_install_but_not_apply(agent, admin):
    proposal = agent.packages.propose_install(["case_management"])
    with pytest.raises(ApprovalError) as exc:
        agent.packages.apply(proposal.id, diff_hash=proposal.diff_hash)
    assert exc.value.code == "APPROVAL_REQUIRES_HUMAN"
    agent.as_principal(admin).packages.apply(proposal.id, diff_hash=proposal.diff_hash)
    assert states(agent)["case_management"] == "installed"


def test_uninstall_retires_objects_and_keeps_data(session, conn):
    install(session, "case_management")
    session.records.create("support_case", {"subject": "Keep me"})
    proposal = session.packages.propose_uninstall("case_management")
    assert proposal.can_apply
    session.packages.apply(proposal.id, diff_hash=proposal.diff_hash)
    assert states(session)["case_management"] == "uninstalled"
    assert "support_case" not in {o["api_name"] for o in session.describe_schema()["objects"]}
    count = conn.execute(
        f'SELECT count(*) FROM "{session.tenant.schema}".support_case'  # noqa: S608
    ).fetchone()[0]
    assert count == 1  # data is kept
    with pytest.raises(SchemaChangeError) as exc:
        session.packages.propose_install(["case_management"])
    assert exc.value.code == "PACKAGE_REINSTALL_NOT_SUPPORTED"


def test_uninstall_blocked_by_dependent_package(session):
    install(session, "case_management")
    with pytest.raises(SchemaChangeError) as exc:
        session.packages.propose_uninstall("crm")
    assert exc.value.code == "PACKAGE_HAS_DEPENDENTS"


def test_uninstall_blocked_by_custom_lookup(crm):
    apply(crm, [obj("c__loan", "Loan")])
    apply(crm, [add("c__loan", "c__borrower", "lookup", options={"target": "account"})])
    proposal = crm.packages.propose_uninstall("crm")
    assert not proposal.can_apply
    assert "c__loan.c__borrower" in proposal.blockers[0]["fields"]


def test_core_cannot_be_uninstalled(session):
    with pytest.raises(SchemaChangeError) as exc:
        session.packages.propose_uninstall("core")
    assert exc.value.code == "PACKAGE_NOT_REMOVABLE"


def test_tenants_can_extend_package_objects(crm):
    apply(crm, [add("opportunity", "c__competitor_name", "text")])
    assert "c__competitor_name" in crm.snapshot().get_object("opportunity").active_fields


@pytest.mark.parametrize("name", sorted(REGISTRY))
def test_every_package_field_is_documented(name):
    for op in REGISTRY[name].operations:
        assert len(op["description"]) >= 20
        for f in op["fields"]:
            assert len(f["description"].split()) >= 4, (op["api_name"], f["api_name"])
            ref = f.get("semantic_ref")
            assert ref is None or ref.startswith("https://schema.org/")
