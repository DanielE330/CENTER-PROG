"""Внутренний вызов demo/trust (auth): снимает обязательную смену пароля только у демо-сотрудников и только при DEMO_LOGIN=1."""
import pytest
from fastapi.testclient import TestClient

import auth.app as auth_app

P = "/internal/v1"
PWD = "demo12345"
EDITOR, VIEWER = "demo-editor@example.com", "demo-viewer@example.com"


@pytest.fixture
def c(tmp_path, monkeypatch):
    monkeypatch.setattr(auth_app, "DB_PATH", str(tmp_path / "auth.db"))
    for k in ("DEMO_DIRECTOR_EMAIL", "DEMO_EDITOR_EMAIL", "DEMO_VIEWER_EMAIL"):
        monkeypatch.delenv(k, raising=False)
    auth_app._fails.clear()
    return TestClient(auth_app.app)


def create(c, email, **kw):
    r = c.post(f"{P}/users/create", json={"email": email, **kw})
    assert r.status_code == 201, r.text
    return r.json()


def flags(c, email):
    with auth_app.db() as conn:
        row = conn.execute("SELECT must_change_password, password_generated, email_verified FROM users WHERE email=?", (email,)).fetchone()
    return tuple(bool(x) for x in row)


def test_trust_clears_forced_password_change_for_demo_staff(c, monkeypatch):
    monkeypatch.setenv("DEMO_LOGIN", "1")
    create(c, EDITOR, password=PWD)
    assert create(c, VIEWER, password=PWD)["must_change_password"] is True
    assert flags(c, EDITOR) == (True, False, False)
    r = c.post(f"{P}/demo/trust", json={"email": EDITOR.upper()})
    assert (r.status_code, r.json()) == (200, {"ok": True, "updated": 1})
    assert flags(c, EDITOR) == (False, False, True)
    assert flags(c, VIEWER) == (True, False, False), "чужой пользователь не затронут"
    login = c.post(f"{P}/login", json={"email": EDITOR, "password": PWD}).json()
    assert login["must_change_password"] is False


def test_trust_is_closed_when_demo_login_is_off(c, monkeypatch):
    monkeypatch.delenv("DEMO_LOGIN", raising=False)
    create(c, EDITOR, password=PWD)
    r = c.post(f"{P}/demo/trust", json={"email": EDITOR})
    assert r.status_code == 404 and r.json()["detail"]["code"] == "demo_disabled"
    assert flags(c, EDITOR)[0] is True


def test_trust_refuses_everyone_outside_the_whitelist(c, monkeypatch):
    monkeypatch.setenv("DEMO_LOGIN", "1")
    create(c, "real@company.ru", password="long-enough-1")
    for email in ("real@company.ru", "", "demo-admin@example.com"):
        assert c.post(f"{P}/demo/trust", json={"email": email}).status_code == 404, email
    assert flags(c, "real@company.ru")[0] is True


def test_trust_never_clears_a_generated_password(c, monkeypatch):
    monkeypatch.setenv("DEMO_LOGIN", "1")
    create(c, EDITOR, generate=True)
    assert c.post(f"{P}/demo/trust", json={"email": EDITOR}).json()["updated"] == 0
    assert flags(c, EDITOR) == (True, True, False)


def test_trust_unknown_demo_email_is_a_noop(c, monkeypatch):
    monkeypatch.setenv("DEMO_LOGIN", "1")
    assert c.post(f"{P}/demo/trust", json={"email": VIEWER}).json() == {"ok": True, "updated": 0}
