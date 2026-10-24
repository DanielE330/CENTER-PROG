import time

import pytest
from fastapi.testclient import TestClient

import auth.app as auth_app
import auth.cli as cli
from auth import mfa

P = "/internal/v1"
PWD = "correct-horse-1"
EMAIL = "op@example.com"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(auth_app, "DB_PATH", str(tmp_path / "d" / "auth.db"))
    monkeypatch.delenv("AUTH_MFA_KEY", raising=False)
    monkeypatch.delenv("AUTH_REQUIRE_2FA", raising=False)
    auth_app._fails.clear()
    auth_app._mfa_fails.clear()
    yield TestClient(auth_app.app)
    auth_app._fails.clear()
    auth_app._mfa_fails.clear()


@pytest.fixture
def operator(client):
    cli.create_operator(EMAIL, PWD)


def login(c):
    return c.post(f"{P}/login", json={"email": EMAIL, "password": PWD})


def enroll(c):
    """Первый вход: пароль → настройка → код. Возвращает (секрет, резервные коды, токен сессии)."""
    r = login(c).json()
    assert r["mfa"] == "setup" and "token" not in r
    s = c.post(f"{P}/mfa/setup", json={"mfa_token": r["mfa_token"]}).json()
    done = c.post(f"{P}/mfa/enable", json={"mfa_token": r["mfa_token"], "code": mfa.totp(s["secret"])})
    assert done.status_code == 200
    return s["secret"], done.json()["backup_codes"], done.json()["token"]


# ---- RFC 6238, приложение B (SHA-1, ключ «12345678901234567890», 8 цифр → берём последние 6) ----
@pytest.mark.parametrize("t,expected", [(59, "287082"), (1111111109, "081804"), (1111111111, "050471"),
                                        (1234567890, "005924"), (2000000000, "279037"), (20000000000, "353130")])
def test_totp_rfc6238_vectors(t, expected):
    import base64
    secret = base64.b32encode(b"12345678901234567890").decode()
    assert mfa.totp(secret, at=t) == expected


def test_window_is_one_step():
    s = mfa.new_secret()
    now = 1_700_000_000
    assert mfa.match_step(s, mfa.totp(s, now - 30), now) is not None
    assert mfa.match_step(s, mfa.totp(s, now + 30), now) is not None
    assert mfa.match_step(s, mfa.totp(s, now - 90), now) is None
    assert mfa.match_step(s, "abcdef", now) is None


def test_seal_roundtrip_and_tamper(tmp_path):
    db = str(tmp_path / "a.db")
    blob = mfa.seal(db, "JBSWY3DPEHPK3PXP")
    assert "JBSWY3" not in blob and mfa.unseal(db, blob) == "JBSWY3DPEHPK3PXP"
    with pytest.raises(ValueError):
        mfa.unseal(db, blob[:-4] + ("AAAA" if not blob.endswith("AAAA") else "BBBB"))


def test_first_login_requires_setup_then_second_requires_code(client, operator):
    secret, codes, token = enroll(client)
    assert client.post(f"{P}/session", json={"token": token}).status_code == 200
    assert len(codes) == 8
    r = login(client).json()
    assert r["mfa"] == "verify" and "token" not in r
    # без кода сессии нет; с неверным кодом — отказ
    bad = client.post(f"{P}/mfa/verify", json={"mfa_token": r["mfa_token"], "code": "000000"})
    assert bad.status_code == 401 and bad.json()["detail"]["code"] == "invalid_mfa_code"


def test_secret_is_not_stored_in_clear(client, operator, tmp_path):
    secret, _, _ = enroll(client)
    import sqlite3
    raw = open(auth_app.DB_PATH, "rb").read()
    assert secret.encode() not in raw
    row = sqlite3.connect(auth_app.DB_PATH).execute("SELECT mfa_secret FROM users").fetchone()[0]
    assert mfa.unseal(auth_app.DB_PATH, row) == secret


def test_setup_secret_is_stable_until_confirmed(client, operator):
    r = login(client).json()
    a = client.post(f"{P}/mfa/setup", json={"mfa_token": r["mfa_token"]}).json()
    b = client.post(f"{P}/mfa/setup", json={"mfa_token": r["mfa_token"]}).json()
    assert a["secret"] == b["secret"] and a["otpauth_uri"].startswith("otpauth://totp/")
    # до подтверждения кодом 2FA не включена
    import sqlite3
    assert sqlite3.connect(auth_app.DB_PATH).execute("SELECT mfa_enabled FROM users").fetchone()[0] == 0


def test_enable_with_wrong_code_does_not_enable(client, operator):
    r = login(client).json()
    client.post(f"{P}/mfa/setup", json={"mfa_token": r["mfa_token"]})
    assert client.post(f"{P}/mfa/enable", json={"mfa_token": r["mfa_token"], "code": "123456"}).status_code == 401
    assert login(client).json()["mfa"] == "setup"


def test_used_code_cannot_be_replayed(client, operator):
    secret, _, _ = enroll(client)  # код этого шага уже принят при включении
    r = login(client).json()
    replay = client.post(f"{P}/mfa/verify", json={"mfa_token": r["mfa_token"], "code": mfa.totp(secret)})
    assert replay.status_code == 401
    # код следующего шага принимается один раз
    nxt = mfa.totp(secret, time.time() + 30)
    r = login(client).json()
    assert client.post(f"{P}/mfa/verify", json={"mfa_token": r["mfa_token"], "code": nxt}).status_code == 200
    r = login(client).json()
    assert client.post(f"{P}/mfa/verify", json={"mfa_token": r["mfa_token"], "code": nxt}).status_code == 401


def test_backup_code_works_once(client, operator):
    _, codes, _ = enroll(client)
    r = login(client).json()
    ok = client.post(f"{P}/mfa/verify", json={"mfa_token": r["mfa_token"], "code": codes[0].upper()})
    assert ok.status_code == 200 and ok.json()["backup_codes_left"] == 7
    r = login(client).json()
    again = client.post(f"{P}/mfa/verify", json={"mfa_token": r["mfa_token"], "code": codes[0]})
    assert again.status_code == 401


def test_attempt_limit_burns_ticket_and_locks(client, operator):
    enroll(client)
    r = login(client).json()
    for _ in range(auth_app.MFA_MAX_ATTEMPTS):
        resp = client.post(f"{P}/mfa/verify", json={"mfa_token": r["mfa_token"], "code": "000000"})
        assert resp.status_code == 401
    # билет сожжён; новый вход по паролю упирается в блокировку пользователя
    r2 = login(client).json()
    locked = client.post(f"{P}/mfa/verify", json={"mfa_token": r2["mfa_token"], "code": "000000"})
    assert locked.status_code == 429
    gone = client.post(f"{P}/mfa/verify", json={"mfa_token": r["mfa_token"], "code": "000000"})
    assert gone.status_code in (401, 429)


def test_ticket_expires(client, operator, monkeypatch):
    enroll(client)
    r = login(client).json()
    real = time.time
    monkeypatch.setattr(auth_app.time, "time", lambda: real() + auth_app.MFA_TTL + 5)
    resp = client.post(f"{P}/mfa/verify", json={"mfa_token": r["mfa_token"], "code": "000000"})
    assert resp.status_code == 401 and resp.json()["detail"]["code"] == "mfa_session_expired"


def test_ticket_of_one_kind_does_not_work_for_other(client, operator):
    r = login(client).json()  # setup-билет
    resp = client.post(f"{P}/mfa/verify", json={"mfa_token": r["mfa_token"], "code": "000000"})
    assert resp.status_code == 401


def test_ordinary_users_are_not_forced(client):
    client.post(f"{P}/register", json={"email": "u@example.com", "password": PWD})
    assert "token" in client.post(f"{P}/login", json={"email": "u@example.com", "password": PWD}).json()


def test_global_switch_forces_password_users(client, monkeypatch):
    monkeypatch.setenv("AUTH_REQUIRE_2FA", "1")
    client.post(f"{P}/register", json={"email": "u@example.com", "password": PWD})
    assert client.post(f"{P}/login", json={"email": "u@example.com", "password": PWD}).json()["mfa"] == "setup"


def test_ecp_session_is_refused_for_2fa_account(client, operator):
    uid = client.post(f"{P}/login", json={"email": EMAIL, "password": PWD}).json()["user_id"]
    r = client.post(f"{P}/sessions/issue", json={"user_id": uid})
    assert r.status_code == 403 and r.json()["detail"]["code"] == "mfa_required"


def test_api_tokens_unaffected(client, operator):
    _, _, token = enroll(client)
    uid = client.post(f"{P}/session", json={"token": token}).json()["user_id"]
    t = client.post(f"{P}/users/{uid}/tokens", json={"name": "ci"}).json()["token"]
    assert client.post(f"{P}/tokens/verify", json={"token": t}).status_code == 200


def test_reset_2fa_forces_new_enrollment_and_kills_sessions(client, operator):
    _, _, token = enroll(client)
    assert cli.reset_2fa(EMAIL) is True
    assert client.post(f"{P}/session", json={"token": token}).status_code == 401
    assert login(client).json()["mfa"] == "setup"
    assert cli.reset_2fa("nobody@example.com") is False


def test_create_operator_is_idempotent_and_keeps_password(client):
    a = cli.create_operator(EMAIL, PWD)
    b = cli.create_operator(EMAIL, "other-password-9")
    assert a == b
    assert login(client).json()["mfa"] == "setup"
    assert client.post(f"{P}/login", json={"email": EMAIL, "password": "other-password-9"}).status_code == 401


def test_create_operator_rejects_weak_password(client):
    with pytest.raises(SystemExit):
        cli.create_operator(EMAIL, "short")


def test_cli_reports_created_then_exists(client, monkeypatch, capsys):
    monkeypatch.setenv("OPERATOR_PASSWORD", PWD)
    assert cli.main(["create-operator", "--email", EMAIL]) == 0
    assert cli.main(["create-operator", "--email", EMAIL]) == 0
    assert capsys.readouterr().out.split() == ["created", "exists"]
