"""Portal: workflows, runs, and trying one out."""

from portal_helpers import post, sign_in

WORKFLOW = {
    "name": "qualify-lead",
    "description": "Ask for approval, then mark a lead as qualified.",
    "inputs": {"lead_id": {"type": "string", "description": "The lead to qualify."}},
    "steps": [
        {"id": "approve", "type": "approval", "message": "Qualify this lead?"},
        {
            "id": "mark",
            "type": "update_record",
            "object": "lead",
            "record_id": "{{inputs.lead_id}}",
            "values": {"status": "qualified"},
        },
    ],
}


def test_the_list_and_detail_pages(admin_client, crm):
    assert "No workflows yet" in admin_client.get("/workflows").text
    crm.workflows.save(WORKFLOW)
    page = admin_client.get("/workflows")
    assert "qualify-lead" in page.text and "Nothing has run yet" in page.text
    detail = admin_client.get("/workflows/qualify-lead")
    assert detail.status_code == 200 and "The lead to qualify." in detail.text
    assert "record_id" in detail.text  # the definition is shown
    assert admin_client.get("/workflows/nope").status_code == 404


def test_members_can_see_and_run_workflows_but_not_manage_them(client, crm, make_user):
    crm.workflows.save(WORKFLOW)
    sign_in(client, make_user("rep@acme.test", "member").user_id)
    detail = client.get("/workflows/qualify-lead")
    assert detail.status_code == 200 and "Disable" not in detail.text
    assert post(client, "/workflows/qualify-lead/status", status="disabled").status_code == 403


def test_trying_a_workflow_changes_nothing(admin_client, crm):
    crm.workflows.save(WORKFLOW)
    lead = crm.records.create("lead", {"family_name": "Doe"})
    page = post(admin_client, "/workflows/qualify-lead/test", in_lead_id=lead["id"])
    assert page.status_code == 200 and "nothing was saved or sent" in page.text.lower()
    assert "simulated" in page.text
    assert crm.records.get("lead", lead["id"])["status"] == "new"
    assert crm.workflows.runs() == []


def test_trying_it_and_simulating_a_rejection(admin_client, crm):
    crm.workflows.save(WORKFLOW)
    lead = crm.records.create("lead", {"family_name": "Doe"})
    page = post(
        admin_client, "/workflows/qualify-lead/test", in_lead_id=lead["id"], approve="reject"
    )
    assert "rejected" in page.text


def test_running_for_real_pauses_for_approval_and_the_run_page_shows_it(admin_client, crm):
    crm.workflows.save(WORKFLOW)
    lead = crm.records.create("lead", {"family_name": "Doe"})
    response = post(admin_client, "/workflows/qualify-lead/start", in_lead_id=lead["id"])
    assert response.status_code == 303
    run_url = response.headers["location"]
    page = admin_client.get(run_url)
    assert page.status_code == 200 and "Qualify this lead?" in page.text
    assert "Cancel run" in page.text and "waiting" in page.text
    assert post(admin_client, run_url + "/cancel").status_code == 303
    assert "cancelled" in admin_client.get(run_url).text


def test_missing_inputs_are_explained(admin_client, crm):
    crm.workflows.save(WORKFLOW)
    response = post(admin_client, "/workflows/qualify-lead/start")
    assert response.status_code == 400 and "lead_id" in response.text


def test_disabling_and_enabling(admin_client, crm):
    crm.workflows.save(WORKFLOW)
    assert (
        post(admin_client, "/workflows/qualify-lead/status", status="disabled").status_code == 303
    )
    assert "disabled" in admin_client.get("/workflows").text
    assert post(admin_client, "/workflows/qualify-lead/status", status="active").status_code == 303


def test_a_person_only_sees_runs_they_may_see(client, admin_client, crm, make_user, made):
    from starlette.testclient import TestClient

    crm.workflows.save(WORKFLOW)
    lead = crm.records.create("lead", {"family_name": "Doe"})
    run = crm.workflows.start("qualify-lead", {"lead_id": lead["id"]})
    other = TestClient(made[0], base_url=str(client.base_url), follow_redirects=False)
    sign_in(other, make_user("rep@acme.test", "member").user_id)
    assert other.get(f"/workflows/runs/{run['run_id']}").status_code == 404
