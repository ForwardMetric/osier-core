"""Helpers shared by the portal tests."""

import os
import re

DATABASE_URL = os.environ.get(
    "OSIER_TEST_DATABASE_URL", "postgresql://osier:osier@localhost:5432/osier_test"
)
KEY = "test-launch-key"
BASE = "http://127.0.0.1:8000"


def csrf_of(html: str) -> str:
    match = re.search(r'name="csrf" value="([^"]+)"', html)
    assert match, "no CSRF field on the page"
    return match.group(1)


def sign_in(client, user_id):
    page = client.get(f"/login?key={KEY}")
    assert page.status_code == 200
    response = client.post("/login", data={"key": KEY, "user": str(user_id)})
    assert response.status_code == 303 and response.headers["location"] == "/"
    return response


def post(client, path, **data):
    token = csrf_of(client.get("/").text)
    return client.post(path, data={"csrf": token, **data})
