import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

import auth.app as auth_app

P = "/internal/v1"
PWD = "correct-horse-1"


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    path = str(tmp_path / "sub" / "auth.db")
    monkeypatch.setattr(auth_app, "DB_PATH", path)
    auth_app._fails.clear()
    yield path
    auth_app._fails.clear()


@pytest.fixture
def client(db_path):
    return TestClient(auth_app.app)


def reg(c, email="user@example.com", pwd=PWD):
    return c.post(f"{P}/register", json={"email": email, "password": pwd})


def login(c, email="user@example.com", pwd=PWD):
    return c.post(f"{P}/login", json={"email": email, "password": pwd})


def test_register_ok(client):
    r = reg(client, " User@Example.com ")
    assert r.status_code == 201
    assert r.json()["email"] == "user@example.com"
    assert r.json()["user_id"]


def test_register_duplicate(client):
    assert reg(client).status_code == 201
    r = reg(client, "USER@example.com")
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "email_taken"


@pytest.mark.parametrize("email", ["nope", "a@b", "a b@c.d", "@x.com", ""])
def test_register_bad_email(client, email):
    r = reg(client, email)
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "invalid_email"


@pytest.mark.parametrize("pwd", ["short", "1234567", "x" * 129])
def test_register_bad_password(client, pwd):
    r = reg(client, pwd=pwd)
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "invalid_password"


def test_login_ok(client):
    reg(client)
    r = login(client)
    assert r.status_code == 200
    body = r.json()
    assert len(body["token"]) >= 32
    assert body["email"] == "user@example.com"
    assert body["ttl"] == auth_app.SESSION_TTL


def test_wrong_password_does_not_reveal_user(client):
    reg(client)
    a = login(client, pwd="wrong-password")
    b = login(client, email="ghost@example.com", pwd="wrong-password")
    assert a.status_code == b.status_code == 401
    assert a.json() == b.json()


def test_lockout_after_five_failures(client):
    reg(client)
    for _ in range(5):
        assert login(client, pwd="bad-password").status_code == 401
    assert login(client, pwd="bad-password").status_code == 429
    # even the correct password is refused while locked
    assert login(client).status_code == 429


def test_success_resets_failures(client):
    reg(client)
    for _ in range(4):
        login(client, pwd="bad-password")
    assert login(client).status_code == 200
    for _ in range(4):
        assert login(client, pwd="bad-password").status_code == 401


def test_lockout_expires(client):
    reg(client)
    for _ in range(5):
        login(client, pwd="bad-password")
    email = "user@example.com"
    cnt, _ = auth_app._fails[email]
    auth_app._fails[email] = (cnt, time.time() - 1)
    assert login(client).status_code == 200


def test_session_valid_invalid(client):
    reg(client)
    tok = login(client).json()["token"]
    r = client.post(f"{P}/session", json={"token": tok})
    assert r.status_code == 200
    assert r.json()["email"] == "user@example.com"
    r = client.post(f"{P}/session", json={"token": "garbage"})
    assert r.status_code == 401
    assert r.json()["detail"]["code"] == "invalid_session"


def test_session_expired(client, db_path):
    reg(client)
    tok = login(client).json()["token"]
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE sessions SET expires_at=?", (time.time() - 10,))
    conn.commit()
    conn.close()
    assert client.post(f"{P}/session", json={"token": tok}).status_code == 401


def test_logout_invalidates(client):
    reg(client)
    tok = login(client).json()["token"]
    assert client.post(f"{P}/logout", json={"token": tok}).status_code == 204
    assert client.post(f"{P}/session", json={"token": tok}).status_code == 401


def test_token_stored_only_hashed(client, db_path):
    reg(client)
    tok = login(client).json()["token"]
    conn = sqlite3.connect(db_path)
    rows = conn.execute("SELECT token_hash, user_id, expires_at FROM sessions").fetchall()
    dump = "\n".join(conn.iterdump())
    conn.close()
    assert len(rows) == 1
    assert rows[0][0] == auth_app.token_hash(tok) != tok
    assert tok not in dump
    assert PWD not in dump


def test_password_not_stored_plain(client, db_path):
    reg(client)
    conn = sqlite3.connect(db_path)
    salt, h = conn.execute("SELECT salt, pwd_hash FROM users").fetchone()
    conn.close()
    assert h == auth_app.hash_password(PWD, salt)
    assert PWD.encode() not in h
