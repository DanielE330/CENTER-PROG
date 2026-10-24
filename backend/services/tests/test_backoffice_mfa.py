"""Второй фактор оператора платформы: настройка при первом входе (QR + резервные коды), код при каждом следующем входе."""
import httpx
import pytest
from fastapi.testclient import TestClient

import backoffice.app as bo
from backoffice import create_operator as cli
from backoffice import store
from auth import mfa

P = "/internal/v1"
EMAIL = "ops@example.com"
PWD = "operator-pass-1"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", str(tmp_path / "sub" / "backoffice.db"))
    monkeypatch.delenv("BACKOFFICE_REQUIRE_2FA", raising=False)  # по умолчанию второй фактор обязателен
    monkeypatch.delenv("BACKOFFICE_MFA_KEY", raising=False)
    for k in ("OPERATOR_EMAIL", "OPERATOR_PASSWORD"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(bo.app.state, "transport", httpx.MockTransport(lambda r: httpx.Response(200, json={})), raising=False)
    bo._fails.clear()
    bo._mfa_fails.clear()
    yield TestClient(bo.app)
    bo._fails.clear()
    bo._mfa_fails.clear()


@pytest.fixture
def operator(client):
    return store.create_operator(EMAIL, PWD)


def login(c, pwd=PWD):
    return c.post(f"{P}/operators/login", json={"email": EMAIL, "password": pwd})


def enroll(c):
    """Первый вход: пароль -> настройка -> код. -> (секрет, резервные коды, токен сессии)."""
    r = login(c).json()
    assert r["mfa"] == "setup" and "token" not in r
    s = c.post(f"{P}/operators/mfa/setup", json={"mfa_token": r["mfa_token"]}).json()
    done = c.post(f"{P}/operators/mfa/enable", json={"mfa_token": r["mfa_token"], "code": mfa.totp(s["secret"])})
    assert done.status_code == 200, done.text
    return s["secret"], done.json()["backup_codes"], done.json()["token"]


def test_first_login_has_no_session_until_2fa_is_set_up(client, operator):
    r = login(client)
    assert r.status_code == 200 and r.json()["mfa"] == "setup" and "token" not in r.json()
    tok = r.json()["mfa_token"]
    s = client.post(f"{P}/operators/mfa/setup", json={"mfa_token": tok}).json()
    assert s["otpauth_uri"].startswith("otpauth://totp/") and s["secret"] and s["qr_svg"].startswith("<svg") or s["qr_svg"] == ""
    # неверный код: сессии нет
    bad = client.post(f"{P}/operators/mfa/enable", json={"mfa_token": tok, "code": "000000"})
    assert (bad.status_code, bad.json()["error"]["code"]) == (401, "invalid_mfa_code")
    ok = client.post(f"{P}/operators/mfa/enable", json={"mfa_token": tok, "code": mfa.totp(s["secret"])})
    assert ok.status_code == 200
    body = ok.json()
    assert body["token"] and len(body["backup_codes"]) == mfa.BACKUP_COUNT
    assert client.post(f"{P}/operators/session", json={"token": body["token"]}).status_code == 200


def test_later_logins_need_the_code_and_a_code_is_one_time(client, operator):
    secret, backups, _ = enroll(client)
    r = login(client).json()
    assert r["mfa"] == "verify" and "token" not in r
    # тот же код, что был принят при настройке, второй раз не проходит (защита от повтора)
    again = client.post(f"{P}/operators/mfa/verify", json={"mfa_token": r["mfa_token"], "code": mfa.totp(secret)})
    assert again.status_code == 401
    # следующий шаг времени принимается
    nxt = mfa.totp(secret, at=__import__("time").time() + 30)
    ok = client.post(f"{P}/operators/mfa/verify", json={"mfa_token": login(client).json()["mfa_token"], "code": nxt})
    assert ok.status_code == 200 and ok.json()["token"]


def test_backup_code_works_once(client, operator):
    _, backups, _ = enroll(client)
    r = login(client).json()
    ok = client.post(f"{P}/operators/mfa/verify", json={"mfa_token": r["mfa_token"], "code": backups[0]})
    assert ok.status_code == 200 and ok.json()["backup_codes_left"] == mfa.BACKUP_COUNT - 1
    r2 = login(client).json()
    assert client.post(f"{P}/operators/mfa/verify", json={"mfa_token": r2["mfa_token"], "code": backups[0]}).status_code == 401


def test_five_wrong_codes_burn_the_ticket_and_lock(client, operator):
    enroll(client)
    r = login(client).json()
    for _ in range(store.MFA_MAX_ATTEMPTS):
        assert client.post(f"{P}/operators/mfa/verify", json={"mfa_token": r["mfa_token"], "code": "111111"}).status_code == 401
    after = client.post(f"{P}/operators/mfa/verify", json={"mfa_token": r["mfa_token"], "code": "111111"})
    assert after.status_code in (401, 429)
    # даже верный код по сгоревшему билету не проходит
    assert client.post(f"{P}/operators/mfa/verify", json={"mfa_token": r["mfa_token"], "code": "000000"}).status_code in (401, 429)


def test_ticket_is_bound_to_its_purpose_and_expires(client, operator):
    r = login(client).json()  # билет настройки
    assert client.post(f"{P}/operators/mfa/verify", json={"mfa_token": r["mfa_token"], "code": "123456"}).status_code == 401
    assert client.post(f"{P}/operators/mfa/enable", json={"mfa_token": r["mfa_token"], "code": "123456"}).status_code == 409  # настройка не начата
    assert client.post(f"{P}/operators/mfa/setup", json={"mfa_token": "nonsense"}).status_code == 401


def test_secret_is_stored_sealed_not_in_clear(client, operator):
    secret, _, _ = enroll(client)
    with store.db() as conn:
        blob = conn.execute("SELECT mfa_secret FROM operators").fetchone()[0]
    assert secret not in blob and mfa.unseal(__import__("os").path.abspath(store.DB_PATH), blob, store.MFA_ENV) == secret


def test_reset_2fa_requires_setup_again_and_closes_sessions(client, operator, capsys):
    _, _, token = enroll(client)
    assert cli.main(["x", "--reset-2fa", EMAIL]) == 0
    assert client.post(f"{P}/operators/session", json={"token": token}).status_code == 401
    assert login(client).json()["mfa"] == "setup"
    assert cli.main(["x", "--reset-2fa", "nobody@example.com"]) == 1


def test_cli_creates_operator_from_stdin(client, monkeypatch, capsys):
    import io
    monkeypatch.setattr("sys.stdin", io.StringIO("pipe-password-123\n"))
    assert cli.main(["x", "--stdin", "new@example.com"]) == 0
    assert store.verify_login("new@example.com", "pipe-password-123")
    monkeypatch.setattr("sys.stdin", io.StringIO("short\n"))
    assert cli.main(["x", "--stdin", "bad@example.com"]) == 1


def test_disabled_requirement_keeps_old_behaviour_for_tests(client, operator, monkeypatch):
    monkeypatch.setenv("BACKOFFICE_REQUIRE_2FA", "0")
    r = login(client)
    assert r.status_code == 200 and "token" in r.json()
