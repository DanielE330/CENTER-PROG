"""Команда компании: приглашения, роли, tenant в сессии и токенах."""
import pytest
from fastapi.testclient import TestClient

import auth.app as auth_app

P = "/internal/v1"
PWD = "correct-horse-1"


@pytest.fixture
def c(tmp_path, monkeypatch):
    monkeypatch.setattr(auth_app, "DB_PATH", str(tmp_path / "auth.db"))
    auth_app._fails.clear()
    return TestClient(auth_app.app)


def user(c, email):
    uid = c.post(f"{P}/register", json={"email": email, "password": PWD}).json()["user_id"]
    token = c.post(f"{P}/login", json={"email": email, "password": PWD}).json()["token"]
    return uid, token


def join(c, owner, uid, role):
    code = c.post(f"{P}/tenants/{owner}/invites", json={"role": role, "actor": owner}).json()["code"]
    return c.post(f"{P}/invites/accept", json={"code": code, "user_id": uid})


def test_owner_by_default_and_session_carries_tenant(c):
    uid, tok = user(c, "o@x.ru")
    assert c.get(f"{P}/users/{uid}/tenant").json() == {"tenant_id": uid, "role": "owner"}
    s = c.post(f"{P}/session", json={"token": tok}).json()
    assert s["tenant_id"] == uid and s["role"] == "owner"


def test_invite_flow_member_uses_owner_tenant_in_session_and_tokens(c):
    o, _ = user(c, "o@x.ru")
    m, mtok = user(c, "m@x.ru")
    r = join(c, o, m, "analyst")
    assert r.status_code == 200 and r.json() == {"tenant_id": o, "role": "analyst"}
    s = c.post(f"{P}/session", json={"token": mtok}).json()
    assert s["user_id"] == m and s["tenant_id"] == o and s["role"] == "analyst"
    api = c.post(f"{P}/users/{m}/tokens", json={"name": "bi"}).json()["token"]
    v = c.post(f"{P}/tokens/verify", json={"token": api}).json()
    assert v["tenant_id"] == o and v["role"] == "analyst"
    mem = c.get(f"{P}/tenants/{o}/members").json()["items"]
    assert [(x["role"], x["email"]) for x in mem] == [("owner", "o@x.ru"), ("analyst", "m@x.ru")]


def test_invite_is_one_time_expires_and_is_hashed(c, db_path=None):
    o, _ = user(c, "o@x.ru")
    a, _ = user(c, "a@x.ru")
    b, _ = user(c, "b@x.ru")
    code = c.post(f"{P}/tenants/{o}/invites", json={"role": "observer", "actor": o}).json()["code"]
    assert c.post(f"{P}/invites/accept", json={"code": code, "user_id": a}).status_code == 200
    assert c.post(f"{P}/invites/accept", json={"code": code, "user_id": b}).status_code == 404
    assert c.post(f"{P}/invites/accept", json={"code": "nope", "user_id": b}).status_code == 404
    code2 = c.post(f"{P}/tenants/{o}/invites", json={"role": "observer", "actor": o}).json()["code"]
    with auth_app.db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM invites WHERE code_hash=?", (code2,)).fetchone()[0] == 0   # в базе только хэш
        conn.execute("UPDATE invites SET expires_at=1")
    assert c.post(f"{P}/invites/accept", json={"code": code2, "user_id": b}).status_code == 404


def test_cannot_accept_twice_or_into_own_team_or_with_own_team(c):
    o, _ = user(c, "o@x.ru")
    m, _ = user(c, "m@x.ru")
    join(c, o, m, "observer")
    assert join(c, o, m, "admin").status_code == 409                       # уже участник
    other, _ = user(c, "z@x.ru")
    join(c, other, user(c, "q@x.ru")[0], "observer")                       # у other есть своя команда
    assert join(c, o, other, "observer").status_code == 409
    code = c.post(f"{P}/tenants/{o}/invites", json={"role": "observer", "actor": o}).json()["code"]
    assert c.post(f"{P}/invites/accept", json={"code": code, "user_id": o}).status_code == 409


def test_role_hierarchy_in_invites_and_changes(c):
    o, _ = user(c, "o@x.ru")
    adm, _ = user(c, "a@x.ru")
    an, _ = user(c, "n@x.ru")
    ob, _ = user(c, "b@x.ru")
    join(c, o, adm, "admin")
    join(c, o, an, "analyst")
    join(c, o, ob, "observer")
    inv = lambda actor, role: c.post(f"{P}/tenants/{o}/invites", json={"role": role, "actor": actor}).status_code  # noqa: E731
    assert inv(adm, "analyst") == 201 and inv(adm, "admin") == 403         # админ зовёт не выше аналитика
    assert inv(an, "observer") == 403 and inv(ob, "observer") == 403
    assert inv(o, "owner") == 422
    patch = lambda actor, uid, role: c.patch(f"{P}/tenants/{o}/members/{uid}", json={"role": role, "actor": actor}).status_code  # noqa: E731
    assert patch(adm, ob, "analyst") == 200
    assert patch(adm, an, "admin") == 403                                  # админ не делает админов
    assert patch(adm, adm, "observer") == 403                              # и не меняет равных
    assert patch(an, ob, "observer") == 403
    assert patch(o, adm, "analyst") == 200
    assert patch(o, o, "observer") == 409                                  # владельца менять нельзя
    assert patch(o, "ghost", "observer") == 404
    assert patch(o, an, "boss") == 422


def test_outsider_cannot_manage_foreign_tenant(c):
    o, _ = user(c, "o@x.ru")
    x, _ = user(c, "x@x.ru")
    assert c.post(f"{P}/tenants/{o}/invites", json={"role": "observer", "actor": x}).status_code == 403
    assert c.patch(f"{P}/tenants/{o}/members/{x}", json={"role": "observer", "actor": x}).status_code == 403
    assert c.delete(f"{P}/tenants/{o}/members/{x}", params={"actor": x}).status_code == 403


def test_remove_member_and_leave_restore_own_tenant(c):
    o, _ = user(c, "o@x.ru")
    m, mtok = user(c, "m@x.ru")
    k, ktok = user(c, "k@x.ru")
    join(c, o, m, "analyst")
    join(c, o, k, "observer")
    assert c.delete(f"{P}/tenants/{o}/members/{m}", params={"actor": o}).status_code == 204
    assert c.post(f"{P}/session", json={"token": mtok}).json()["tenant_id"] == m   # вернулся в свою компанию
    assert c.delete(f"{P}/tenants/{o}/members/{o}", params={"actor": o}).status_code == 409
    assert c.delete(f"{P}/tenants/{o}/members/{m}", params={"actor": o}).status_code == 404
    assert c.post(f"{P}/users/{k}/leave").status_code == 204
    assert c.post(f"{P}/users/{k}/leave").status_code == 409
    assert c.post(f"{P}/session", json={"token": ktok}).json()["role"] == "owner"


def test_invites_list_and_revoke(c):
    o, _ = user(c, "o@x.ru")
    c.post(f"{P}/tenants/{o}/invites", json={"role": "observer", "actor": o})
    items = c.get(f"{P}/tenants/{o}/invites").json()["items"]
    assert len(items) == 1 and "code" not in items[0]
    assert c.delete(f"{P}/tenants/{o}/invites/{items[0]['id']}").status_code == 204
    assert c.get(f"{P}/tenants/{o}/invites").json()["items"] == []
    assert c.delete(f"{P}/tenants/{o}/invites/zzz").status_code == 404


def test_transfer_ownership(c):
    o, otok = user(c, "o@x.ru")
    n, ntok = user(c, "n@x.ru")
    a, _ = user(c, "a@x.ru")
    join(c, o, n, "admin")
    join(c, o, a, "analyst")
    tr = lambda actor, new: c.post(f"{P}/tenants/{o}/transfer", json={"actor": actor, "new_owner": new}).status_code  # noqa: E731
    assert tr(n, a) == 403 and tr(a, n) == 403                             # передать может только владелец
    assert tr(o, o) == 422 and tr(o, "ghost") == 404
    assert tr(o, n) == 200
    assert c.post(f"{P}/session", json={"token": ntok}).json()["role"] == "owner"
    s = c.post(f"{P}/session", json={"token": otok}).json()
    assert s["role"] == "admin" and s["tenant_id"] == o                    # компания прежняя, бывший владелец — админ
    items = {m["email"]: m["role"] for m in c.get(f"{P}/tenants/{o}/members").json()["items"]}
    assert items == {"n@x.ru": "owner", "o@x.ru": "admin", "a@x.ru": "analyst"}
    assert c.get(f"{P}/tenants/{o}/members").json()["items"][0]["role"] == "owner"
    # новый владелец управляет всеми, а прежнего можно только понизить
    assert c.post(f"{P}/tenants/{o}/invites", json={"role": "admin", "actor": n}).status_code == 201
    assert c.patch(f"{P}/tenants/{o}/members/{o}", json={"role": "observer", "actor": n}).status_code == 200
    assert c.delete(f"{P}/tenants/{o}/members/{o}", params={"actor": n}).status_code == 409
    assert c.post(f"{P}/users/{o}/leave").status_code == 409               # и сам уйти не может: права вернулись бы
    assert c.patch(f"{P}/tenants/{o}/members/{n}", json={"role": "observer", "actor": n}).status_code == 409
    assert c.post(f"{P}/users/{n}/leave").status_code == 409               # владелец не выходит, пока не передал владение
    assert tr(o, a) == 403


def test_new_owner_cannot_join_another_team(c):
    o, _ = user(c, "o@x.ru")
    n, _ = user(c, "n@x.ru")
    other, _ = user(c, "z@x.ru")
    join(c, o, n, "admin")
    c.post(f"{P}/tenants/{o}/transfer", json={"actor": o, "new_owner": n})
    assert join(c, other, n, "observer").status_code == 409
    assert join(c, other, o, "observer").status_code == 409


def test_email_bound_invite(c):
    o, _ = user(c, "o@x.ru")
    right, _ = user(c, "right@x.ru")
    wrong, _ = user(c, "wrong@x.ru")
    r = c.post(f"{P}/tenants/{o}/invites", json={"role": "analyst", "actor": o, "email": " Right@X.ru "})
    assert r.status_code == 201 and r.json()["email"] == "right@x.ru"
    code = r.json()["code"]
    assert c.get(f"{P}/tenants/{o}/invites").json()["items"][0]["email"] == "right@x.ru"
    assert c.post(f"{P}/invites/accept", json={"code": code, "user_id": wrong}).status_code == 403
    assert c.post(f"{P}/invites/accept", json={"code": code, "user_id": right}).status_code == 200  # неудачная попытка код не сжигает
    assert c.post(f"{P}/tenants/{o}/invites", json={"role": "analyst", "actor": o, "email": "not-an-email"}).status_code == 422


def test_email_bound_invite_rejects_ecp_user_without_email(c):
    o, _ = user(c, "o@x.ru")
    ecp = c.post(f"{P}/users/external", json={"external_id": "thumb-1", "display_name": "ООО"}).json()["user_id"]
    code = c.post(f"{P}/tenants/{o}/invites", json={"role": "observer", "actor": o, "email": "a@x.ru"}).json()["code"]
    assert c.post(f"{P}/invites/accept", json={"code": code, "user_id": ecp}).status_code == 403
