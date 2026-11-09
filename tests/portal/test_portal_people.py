"""Portal: people and roles."""

import pytest
from portal_helpers import post, sign_in


def role_names(client, email):
    """The roles shown as ticked on the People page for one person."""
    import re

    table = client.get("/people").text.split("<tbody>")[1]  # not the sidebar's copy of an email
    row = table.split(email)[1].split("</tr>")[0]
    return sorted(re.findall(r'value="(\w+)" checked', row))


def person_id(crm, email):
    return crm.records.list("app_user", where={"email": email})[0]["id"]


def test_members_cannot_open_the_people_page(client, make_user):
    sign_in(client, make_user("rep@acme.test", "member").user_id)
    assert client.get("/people").status_code == 403
    assert post(client, "/people/new", name="X", email="x@acme.test").status_code == 403


def test_the_admin_sees_everyone_with_their_roles(admin_client, make_user):
    make_user("rep@acme.test", "member")
    page = admin_client.get("/people")
    assert page.status_code == 200 and "rep@acme.test" in page.text and "Ada Admin" in page.text
    assert role_names(admin_client, "rep@acme.test") == ["member"]
    assert role_names(admin_client, "ada@acme.test") == ["admin"]


def test_adding_a_person_with_roles_lets_them_sign_in(admin_client, client, made, crm):
    response = post(
        admin_client, "/people/new", name="Sam Sales", email="sam@acme.test", roles="member"
    )
    assert response.status_code == 303
    assert role_names(admin_client, "sam@acme.test") == ["member"]
    from starlette.testclient import TestClient

    other = TestClient(made[0], base_url=str(client.base_url), follow_redirects=False)
    sign_in(other, person_id(crm, "sam@acme.test"))
    assert "Sam Sales" in other.get("/").text


def test_changing_roles_grants_and_removes_them(admin_client, crm, make_user):
    make_user("rep@acme.test", "member")
    uid = person_id(crm, "rep@acme.test")
    post(admin_client, f"/people/{uid}/roles", roles=["developer", "member"])
    assert role_names(admin_client, "rep@acme.test") == ["developer", "member"]
    post(admin_client, f"/people/{uid}/roles", roles="developer")
    assert role_names(admin_client, "rep@acme.test") == ["developer"]


def test_a_removed_role_cannot_yet_be_granted_again_and_nothing_else_changes(
    admin_client, crm, make_user
):
    """Known limitation (uniqueness counts retired records). When the engine lets retired
    assignments be re-granted, this test should change to expect success."""
    make_user("rep@acme.test", "member")
    uid = person_id(crm, "rep@acme.test")
    post(admin_client, f"/people/{uid}/roles", roles="developer")  # removes member
    response = post(admin_client, f"/people/{uid}/roles", roles=["developer", "member", "admin"])
    assert response.status_code == 400 and "granted again yet" in response.text
    assert role_names(admin_client, "rep@acme.test") == ["developer"]  # admin was not granted


def test_the_last_administrator_cannot_be_demoted_or_suspended(admin_client, crm, boot):
    admin_id = str(boot.admin_user_id)
    response = post(admin_client, f"/people/{admin_id}/roles", roles="member")
    assert response.status_code == 400 and "without an active administrator" in response.text
    assert role_names(admin_client, "ada@acme.test") == ["admin"]
    response = post(admin_client, f"/people/{admin_id}/status", status="suspended")
    assert response.status_code == 400  # also yourself
    assert crm.records.get("app_user", admin_id)["status"] == "active"


def test_a_second_admin_makes_demotion_possible(admin_client, crm, boot, make_user):
    make_user("two@acme.test", "admin")
    admin_id = str(boot.admin_user_id)
    assert post(admin_client, f"/people/{admin_id}/roles", roles="member").status_code == 303


def test_you_cannot_suspend_yourself_even_with_other_admins(admin_client, boot, make_user):
    make_user("two@acme.test", "admin")
    response = post(admin_client, f"/people/{boot.admin_user_id}/status", status="suspended")
    assert response.status_code == 400 and "yourself" in response.text


def test_suspending_someone_stops_them_signing_in(admin_client, client, made, crm, make_user):
    make_user("rep@acme.test", "member")
    uid = person_id(crm, "rep@acme.test")
    assert post(admin_client, f"/people/{uid}/status", status="suspended").status_code == 303
    from starlette.testclient import TestClient

    other = TestClient(made[0], base_url=str(client.base_url), follow_redirects=False)
    other.get("/login?key=test-launch-key")
    refused = other.post("/login", data={"key": "test-launch-key", "user": uid})
    assert refused.status_code == 400


def test_unknown_people_and_statuses(admin_client):
    assert post(admin_client, "/people/not-a-person/roles", roles="member").status_code == 400
    uid = "00000000-0000-0000-0000-000000000009"
    assert post(admin_client, f"/people/{uid}/status", status="banished").status_code == 400


@pytest.mark.parametrize("path", ["/people/new"])
def test_csrf_is_required(admin_client, path):
    assert admin_client.post(path, data={"name": "X", "email": "x@acme.test"}).status_code == 403
