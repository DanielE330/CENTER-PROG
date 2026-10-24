"""Приглашения в команду (auth): одноразовые ссылки с ролью, привязка к email, проверка без расходования, миграция старых команд.
Роли и состав команды хранит ledger — см. ledger/src/tests.rs и api/src/team_tests.rs."""
import pytest
from fastapi.testclient import TestClient

import auth.app as auth_app

P = "/internal/v1"
PWD = "correct-horse-1"
CID = "company-1"


@pytest.fixture
def c(tmp_path, monkeypatch):
    monkeypatch.setattr(auth_app, "DB_PATH", str(tmp_path / "auth.db"))
    auth_app._fails.clear()
    return TestClient(auth_app.app)


def user(c, email):
    uid = c.post(f"{P}/register", json={"email": email, "password": PWD}).json()["user_id"]
    return uid


def invite(c, role="viewer", email="", cid=CID):
    return c.post(f"{P}/companies/{cid}/invites", json={"role": role, "actor": "a", "email": email})


def test_session_and_token_do_not_carry_tenant_any_more(c):
    uid = user(c, "o@x.ru")
    tok = c.post(f"{P}/login", json={"email": "o@x.ru", "password": PWD}).json()["token"]
    s = c.post(f"{P}/session", json={"token": tok}).json()
    assert "tenant_id" not in s and "role" not in s and s["user_id"] == uid
    api = c.post(f"{P}/users/{uid}/tokens", json={"name": "bi"}).json()["token"]
    assert set(c.post(f"{P}/tokens/verify", json={"token": api}).json()) == {"user_id", "email"}


def test_invite_roles_are_validated(c):
    assert invite(c, "admin").status_code == 201
    assert invite(c, "editor").status_code == 201
    assert invite(c, "viewer").status_code == 201
    for bad in ("director", "owner", "analyst", "observer", ""):
        assert invite(c, bad).status_code == 422, bad
    assert invite(c, "viewer", email="not-an-email").status_code == 422


def test_invite_check_does_not_spend_and_consume_is_one_time_and_hashed(c):
    a, b = user(c, "a@x.ru"), user(c, "b@x.ru")
    v = invite(c, "editor").json()
    code = v["code"]
    chk = {"code": code, "user_id": a}
    assert c.post(f"{P}/invites/check", json=chk).json() == {"company_id": CID, "role": "editor"}
    assert c.post(f"{P}/invites/check", json={"code": code, "user_id": b}).status_code == 200  # проверка ничего не расходует
    assert c.post(f"{P}/invites/consume", json=chk).status_code == 204
    assert c.post(f"{P}/invites/check", json=chk).status_code == 404
    assert c.post(f"{P}/invites/consume", json=chk).status_code == 404
    assert c.post(f"{P}/invites/check", json={"code": "nope", "user_id": b}).status_code == 404
    code2 = invite(c).json()["code"]
    with auth_app.db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM invites WHERE code_hash=?", (code2,)).fetchone()[0] == 0  # в базе только хэш


def test_expired_invite_is_invalid(c, monkeypatch):
    a = user(c, "a@x.ru")
    code = invite(c).json()["code"]
    real = auth_app.time.time
    monkeypatch.setattr(auth_app.time, "time", lambda: real() + auth_app.INVITE_TTL + 5)
    assert c.post(f"{P}/invites/check", json={"code": code, "user_id": a}).status_code == 404


def test_invites_list_and_revoke_are_per_company(c):
    invite(c, "viewer")
    invite(c, "admin", email="Boss@X.ru")
    invite(c, "viewer", cid="other")
    items = c.get(f"{P}/companies/{CID}/invites").json()["items"]
    assert sorted((i["role"], i["email"]) for i in items) == [("admin", "boss@x.ru"), ("viewer", None)]
    assert "code" not in items[0]
    iid = items[0]["id"]
    assert c.delete(f"{P}/companies/other/invites/{iid}").status_code == 404  # чужую компанию не затронуть
    assert c.delete(f"{P}/companies/{CID}/invites/{iid}").status_code == 204
    assert c.delete(f"{P}/companies/{CID}/invites/{iid}").status_code == 404
    assert len(c.get(f"{P}/companies/{CID}/invites").json()["items"]) == 1


def test_email_bound_invite(c):
    right, wrong = user(c, "Right@x.ru"), user(c, "wrong@x.ru")
    code = invite(c, "viewer", email="right@x.ru").json()["code"]
    r = c.post(f"{P}/invites/check", json={"code": code, "user_id": wrong})
    assert (r.status_code, r.json()["detail"]["code"]) == (403, "invite_email_mismatch")
    assert c.post(f"{P}/invites/check", json={"code": code, "user_id": right}).status_code == 200


def test_email_bound_invite_rejects_ecp_user_without_email(c):
    ecp = c.post(f"{P}/users/external", json={"external_id": "cert-1", "display_name": "ООО Ромашка"}).json()["user_id"]
    code = invite(c, "viewer", email="right@x.ru").json()["code"]
    assert c.post(f"{P}/invites/check", json={"code": code, "user_id": ecp}).status_code == 403
    open_code = invite(c, "viewer").json()["code"]
    assert c.post(f"{P}/invites/check", json={"code": open_code, "user_id": ecp}).status_code == 200


def test_labels_for_the_activity_log(c):
    a = user(c, "a@x.ru")
    ecp = c.post(f"{P}/users/external", json={"external_id": "cert-1", "display_name": "ООО Ромашка"}).json()["user_id"]
    items = c.get(f"{P}/users/labels", params={"user_ids": f"{a},{ecp},ghost"}).json()["items"]
    assert items[a]["email"] == "a@x.ru"
    assert items[ecp]["label"] == "ООО Ромашка" and items[ecp]["email"] is None
    assert "ghost" not in items


def test_legacy_memberships_are_exposed_for_migration_and_old_invites_dropped(c):
    a = user(c, "a@x.ru")
    with auth_app.db() as conn:
        conn.execute("CREATE TABLE memberships(user_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, role TEXT NOT NULL, created_at REAL NOT NULL)")
        conn.execute("INSERT INTO memberships VALUES(?,?,?,0)", (a, "owner-1", "analyst"))
    assert c.get(f"{P}/memberships/legacy").json()["items"] == [{"user_id": a, "tenant_id": "owner-1", "role": "analyst"}]
    # после миграции таблица откладывается: повторный проход пуст и никого не воскресит
    assert c.post(f"{P}/memberships/legacy/done").status_code == 204
    assert c.get(f"{P}/memberships/legacy").json()["items"] == []
    assert c.post(f"{P}/memberships/legacy/done").status_code == 204  # идемпотентно


def test_old_invites_table_is_renamed_and_emptied(tmp_path, monkeypatch):
    import sqlite3

    path = str(tmp_path / "old.db")
    monkeypatch.setattr(auth_app, "DB_PATH", path)
    with auth_app.db():
        pass
    raw = sqlite3.connect(path)
    raw.executescript(
        "DROP TABLE invites; CREATE TABLE invites(code_hash TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, role TEXT NOT NULL,"
        " created_by TEXT NOT NULL, created_at REAL NOT NULL, expires_at REAL NOT NULL, email TEXT NOT NULL DEFAULT '');"
        "INSERT INTO invites VALUES('h','owner-id','analyst','x',0,9e12,'');"
    )
    raw.commit()
    raw.close()
    with auth_app.db() as conn:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(invites)")]
        assert "company_id" in cols and "tenant_id" not in cols
        assert conn.execute("SELECT COUNT(*) FROM invites").fetchone()[0] == 0
