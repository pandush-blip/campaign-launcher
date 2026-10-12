import hashlib

import pytest
from fastapi.testclient import TestClient

from tri_launch.web import app


@pytest.fixture()
def authed(tmp_path, monkeypatch):
    monkeypatch.setenv("TRI_LAUNCH_HOME", str(tmp_path))
    monkeypatch.setenv("TRI_LAUNCH_USER", "operator")
    monkeypatch.setenv("TRI_LAUNCH_PASSWORD_SHA256", hashlib.sha256(b"secret123").hexdigest())
    monkeypatch.setenv("TRI_LAUNCH_SECRET", "test-secret")
    with TestClient(app) as c:
        yield c


def test_anonymous_users_are_redirected_to_login(authed):
    r = authed.get("/", follow_redirects=False)
    assert r.status_code == 302
    assert "/login" in r.headers["location"]
    assert authed.get("/api/campaigns").status_code == 401
    assert authed.get("/healthz").status_code == 200  # health stays open


def test_login_page_renders_form(authed):
    r = authed.get("/login")
    assert r.status_code == 200
    assert "<form" in r.text and "Password" in r.text


def test_wrong_password_rejected(authed):
    r = authed.post(
        "/login", data={"username": "operator", "password": "nope"}, follow_redirects=False
    )
    assert r.status_code == 401
    assert "Wrong username or password" in r.text


def test_login_sets_session_and_logout_clears_it(authed):
    r = authed.post(
        "/login",
        data={"username": "operator", "password": "secret123", "next": "/"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "launcher_session" in authed.cookies
    assert authed.get("/").status_code == 200
    authed.get("/logout", follow_redirects=False)
    assert authed.get("/", follow_redirects=False).status_code == 302


def test_tampered_cookie_is_rejected(authed):
    authed.cookies.set("launcher_session", "operator.9999999999.deadbeef")
    assert authed.get("/", follow_redirects=False).status_code == 302
