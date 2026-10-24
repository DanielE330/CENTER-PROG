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


def test_external_user_idempotent_and_no_password_login(client):
    a = client.post(f"{P}/users/external", json={"external_id": "abc123", "display_name": "X"})
    assert a.status_code == 200
    v = a.json()
    assert v["created"] is True and v["email"].startswith("ecp-") and v["email"].endswith("@ecp.invalid")
    assert len(v["email"]) == len("ecp-") + 16 + len("@ecp.invalid")
    b = client.post(f"{P}/users/external", json={"external_id": "abc123", "display_name": "X"}).json()
    assert b == {"user_id": v["user_id"], "email": v["email"], "created": False}
    other = client.post(f"{P}/users/external", json={"external_id": "def"}).json()
    assert other["user_id"] != v["user_id"]
    assert login(client, v["email"], "anything-123").status_code == 401


def test_external_user_empty_id(client):
    assert client.post(f"{P}/users/external", json={"external_id": " "}).status_code == 422


def test_sessions_issue(client):
    u = client.post(f"{P}/users/external", json={"external_id": "abc"}).json()
    r = client.post(f"{P}/sessions/issue", json={"user_id": u["user_id"]})
    assert r.status_code == 200
    v = r.json()
    assert v["user_id"] == u["user_id"] and v["email"] == u["email"] and v["ttl"] == auth_app.SESSION_TTL
    s = client.post(f"{P}/session", json={"token": v["token"]})
    assert s.status_code == 200 and s.json()["user_id"] == u["user_id"]


def test_sessions_issue_unknown_user(client):
    r = client.post(f"{P}/sessions/issue", json={"user_id": "nope"})
    assert r.status_code == 404 and r.json()["detail"]["code"] == "user_not_found"


def test_external_id_migration_of_old_db(db_path):
    import os
    os.makedirs(os.path.dirname(db_path))
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE users(id TEXT PRIMARY KEY, email TEXT UNIQUE NOT NULL,"
                 " salt BLOB NOT NULL, pwd_hash BLOB NOT NULL, created_at REAL NOT NULL)")
    conn.execute("INSERT INTO users VALUES('u1','old@example.com',x'00',x'00',0)")
    conn.commit(); conn.close()
    c = TestClient(auth_app.app)
    assert c.post(f"{P}/users/external", json={"external_id": "z"}).json()["created"] is True
    cols = [r[1] for r in sqlite3.connect(db_path).execute("PRAGMA table_info(users)")]
    assert "external_id" in cols


def _uid(c, ext="sig-1"):
    return c.post(f"{P}/users/external", json={"external_id": ext, "display_name": "ООО Ромашка"}).json()["user_id"]


def test_methods_email_user_and_add_signature(client):
    uid = reg(client).json()["user_id"]
    m = client.get(f"{P}/users/{uid}/methods").json()
    assert m == {"email": "user@example.com", "signatures": []}
    m = client.post(f"{P}/users/{uid}/signatures", json={"external_id": "sig-9", "label": "ЭЦП"}).json()
    assert [s["id"] for s in m["signatures"]] == ["sig-9"]
    # повтор идемпотентен; чужую подпись привязать нельзя
    assert client.post(f"{P}/users/{uid}/signatures", json={"external_id": "sig-9"}).status_code == 200
    other = reg(client, "o@example.com").json()["user_id"]
    r = client.post(f"{P}/users/{other}/signatures", json={"external_id": "sig-9"})
    assert (r.status_code, r.json()["detail"]["code"]) == (409, "signature_taken")
    # вход по подписи теперь ведёт к этому же пользователю
    assert client.post(f"{P}/users/external", json={"external_id": "sig-9"}).json()["user_id"] == uid


def test_cannot_remove_last_method(client):
    uid = reg(client).json()["user_id"]
    r = client.delete(f"{P}/users/{uid}/email")
    assert (r.status_code, r.json()["detail"]["code"]) == (409, "last_method")
    client.post(f"{P}/users/{uid}/signatures", json={"external_id": "s1"})
    assert client.delete(f"{P}/users/{uid}/email").json() == {"email": None, "signatures": [{"id": "s1", "label": "", "created_at": pytest.approx(time.time(), abs=60)}]}
    assert login(client).status_code == 401  # пароля больше нет
    r = client.delete(f"{P}/users/{uid}/signatures/s1")
    assert (r.status_code, r.json()["detail"]["code"]) == (409, "last_method")


def test_ecp_user_can_add_email_then_remove_signature(client):
    uid = _uid(client)
    assert client.get(f"{P}/users/{uid}/methods").json()["email"] is None
    assert login(client, "ecp@x.invalid").status_code == 401
    r = client.post(f"{P}/users/{uid}/email", json={"email": "New@Example.com", "password": PWD})
    assert r.json()["email"] == "new@example.com"
    assert login(client, "new@example.com").status_code == 200
    assert client.post(f"{P}/users/{uid}/email", json={"email": "z@example.com", "password": PWD}).status_code == 409
    assert client.delete(f"{P}/users/{uid}/signatures/sig-1").json()["signatures"] == []
    assert client.get(f"{P}/users/{uid}/methods").json()["email"] == "new@example.com"


def test_add_email_taken_and_validation(client):
    reg(client)
    uid = _uid(client)
    r = client.post(f"{P}/users/{uid}/email", json={"email": "user@example.com", "password": PWD})
    assert (r.status_code, r.json()["detail"]["code"]) == (409, "email_taken")
    assert client.post(f"{P}/users/{uid}/email", json={"email": "bad", "password": PWD}).status_code == 422
    assert client.post(f"{P}/users/{uid}/email", json={"email": "a@b.co", "password": "short"}).status_code == 422


def test_api_tokens_lifecycle(client):
    uid = reg(client).json()["user_id"]
    t = client.post(f"{P}/users/{uid}/tokens", json={"name": "1С"}).json()
    assert t["token"].startswith("ckt_") and t["prefix"] == t["token"][:8]
    items = client.get(f"{P}/users/{uid}/tokens").json()["items"]
    assert len(items) == 1 and "token" not in items[0] and items[0]["last_used"] is None
    v = client.post(f"{P}/tokens/verify", json={"token": t["token"]})
    assert v.status_code == 200 and v.json()["user_id"] == uid
    assert client.get(f"{P}/users/{uid}/tokens").json()["items"][0]["last_used"] is not None
    assert client.post(f"{P}/tokens/verify", json={"token": "ckt_wrong"}).status_code == 401
    assert client.post(f"{P}/tokens/verify", json={"token": "other"}).status_code == 401
    assert client.delete(f"{P}/users/{uid}/tokens/{t['id']}").status_code == 204
    assert client.post(f"{P}/tokens/verify", json={"token": t["token"]}).status_code == 401
    assert client.delete(f"{P}/users/{uid}/tokens/{t['id']}").status_code == 404
