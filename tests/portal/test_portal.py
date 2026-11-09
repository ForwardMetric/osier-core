"""The web portal: sign-in, CSRF, host check, headers, and permission handling."""

import uuid

from portal_helpers import BASE, KEY, csrf_of, post, sign_in
from starlette.testclient import TestClient

from osier.portal import create_app
from osier.portal.security import SessionStore


def test_signing_in_needs_the_launch_key(client):
    for url in ("/login", "/login?key=wrong"):
        response = client.get(url)
        assert response.status_code == 401 and "printed in the terminal" in response.text
    response = client.post("/login", data={"key": "wrong", "user": str(uuid.uuid4())})
    assert response.status_code == 401


def test_pages_need_a_session(client):
    for path in ("/", "/ai"):
        response = client.get(path)
        assert response.status_code in (303, 404)
        if response.status_code == 303:
            assert response.headers["location"] == "/login"


def test_sign_in_sets_a_safe_cookie_and_shows_the_person(client, boot):
    response = sign_in(client, boot.admin_user_id)
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie and "secure" not in cookie
    home = client.get("/")
    assert home.status_code == 200 and "Ada Admin" in home.text and "ada@acme.test" in home.text


def test_the_session_cookie_is_secure_when_served_over_https(config, boot):
    app, _ = create_app(config, launch_key=KEY, secure=True)
    client = TestClient(app, base_url=BASE, follow_redirects=False)
    client.get(f"/login?key={KEY}")
    response = client.post("/login", data={"key": KEY, "user": str(boot.admin_user_id)})
    assert "secure" in response.headers["set-cookie"].lower()


def test_unknown_or_inactive_people_cannot_sign_in(client, session):
    response = client.post("/login", data={"key": KEY, "user": str(uuid.uuid4())})
    assert response.status_code == 400
    response = client.post("/login", data={"key": KEY, "user": "not-a-uuid"})
    assert response.status_code == 400
    gone = session.records.create(
        "app_user", {"display_name": "Gone", "email": "gone@acme.test", "status": "suspended"}
    )
    response = client.post("/login", data={"key": KEY, "user": gone["id"]})
    assert response.status_code == 400


def test_host_header_is_checked_against_dns_rebinding(client):
    for host in ("evil.example", "127.0.0.1:9999", "evil.example:8000"):
        assert client.get("/login", headers={"host": host}).status_code == 400
    assert client.get("/login", headers={"host": "localhost:8000"}).status_code == 401  # allowed


def test_security_headers(admin_client):
    response = admin_client.get("/")
    headers = response.headers
    assert "default-src 'none'" in headers["content-security-policy"]
    assert "script-src" not in headers["content-security-policy"]  # no scripts at all
    assert "frame-ancestors 'none'" in headers["content-security-policy"]
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["referrer-policy"] == "same-origin"
    assert headers["cache-control"] == "no-store"
    assert admin_client.get("/static/portal.css").status_code == 200


def test_pages_have_no_scripts_or_inline_styles(admin_client):
    html = admin_client.get("/").text
    assert "<script" not in html and "style=" not in html


def test_posts_need_the_csrf_token_and_the_same_origin(admin_client):
    assert admin_client.post("/logout", data={}).status_code == 403
    assert admin_client.post("/logout", data={"csrf": "wrong"}).status_code == 403
    token = csrf_of(admin_client.get("/").text)
    cross = admin_client.post(
        "/logout", data={"csrf": token}, headers={"origin": "https://evil.example"}
    )
    assert cross.status_code == 403
    assert admin_client.get("/").status_code == 200  # still signed in
    null = admin_client.post("/logout", data={"csrf": token}, headers={"origin": "null"})
    assert null.status_code == 403  # what a browser sends under Referrer-Policy: no-referrer
    same = admin_client.post("/logout", data={"csrf": token}, headers={"origin": BASE})
    assert same.status_code == 303


def test_signing_out_ends_the_session(admin_client):
    post(admin_client, "/logout")
    response = admin_client.get("/")
    assert response.status_code == 303 and response.headers["location"] == "/login"


def test_sessions_expire(config, boot):
    now = [0.0]
    store = SessionStore(idle_seconds=60, absolute_seconds=300, clock=lambda: now[0])
    app, _ = create_app(config, launch_key=KEY, store=store)
    client = TestClient(app, base_url=BASE, follow_redirects=False)
    sign_in(client, boot.admin_user_id)
    now[0] = 30
    assert client.get("/").status_code == 200
    now[0] = 80  # idle for 50s only: still fine
    assert client.get("/").status_code == 200
    now[0] = 200  # idle for 120s
    assert client.get("/").status_code == 303
    sign_in(client, boot.admin_user_id)
    for t in (250, 300, 350, 400):  # busy, but past the absolute limit
        now[0] = t
    assert client.get("/").status_code == 303


def test_a_member_sees_no_setup_steps(client, make_user):
    member = make_user("rep@acme.test", "member")
    sign_in(client, member.user_id)
    home = client.get("/")
    assert home.status_code == 200 and "Set up" not in home.text
    assert "Connect an AI model" not in home.text


def test_admin_sees_the_setup_checklist(admin_client):
    home = admin_client.get("/").text
    assert "Connect an AI model" in home and "To do" in home
