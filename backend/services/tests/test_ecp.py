import base64
import contextlib
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

import auth.app as auth_app
import ecp.app as ecp_app
import ecp.cades as ecp

P = "/internal/v1"
pytestmark = pytest.mark.skipif(shutil.which("openssl") is None, reason="нужен openssl для тестового CMS")


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(auth_app, "DB_PATH", str(tmp_path / "auth.db"))
    monkeypatch.setattr(ecp_app, "DB_PATH", str(tmp_path / "ecp.db"))
    auth = TestClient(auth_app.app)  # auth вызывается по HTTP; в тесте подменяем транспорт на TestClient
    monkeypatch.setattr(ecp_app, "auth_client", lambda: contextlib.nullcontext(auth))
    # криптографическая проверка подписи делегирована cryptcp (КриптоПро CSP); здесь проверяем остальное
    monkeypatch.setattr(ecp, "verify_detached", lambda msg, sig: None)
    c = TestClient(ecp_app.app)
    c.authsvc = auth
    return c


def make_cms(tmp_path, message: str, cn="Ivan", days=30, requisites=None) -> str:
    """requisites: {'1.2.643.100.4': '...'} — OID российских реквизитов; задаём их числовыми именами полей
    (регистрировать OID в конфиге нельзя: новые версии openssl уже знают часть из них и отвечают «oid exists»)."""
    d = tmp_path
    conf = "[req]\ndistinguished_name=dn\nprompt=no\n[dn]\n" f"CN={cn}\nO=Romashka\n"
    # префикс «0.» нужен: openssl читает «1.2.…» как «<номер>.<имя поля>» и отбрасывает первую дугу OID
    conf += "".join(f"0.{oid}={v}\n" for oid, v in (requisites or {}).items())
    (d / "req.cnf").write_text(conf)
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", d / "k.pem", "-out", d / "c.pem",
         "-config", d / "req.cnf", "-days", str(days)], check=True, capture_output=True)
    (d / "m").write_bytes(message.encode())
    subprocess.run(["openssl", "cms", "-sign", "-binary", "-in", d / "m", "-signer", d / "c.pem", "-inkey", d / "k.pem",
                    "-outform", "DER", "-out", d / "m.sgn"], check=True, capture_output=True)
    return base64.b64encode((d / "m.sgn").read_bytes()).decode()


def login(client, tmp_path, cn="Ivan"):
    ch = client.post(f"{P}/ecp/challenge").json()["challenge"]
    return ch, client.post(f"{P}/ecp/login", json={"challenge": ch, "signature": make_cms(tmp_path, ch, cn)})


def test_ecp_login_creates_account_and_session(client, tmp_path):
    ch, r = login(client, tmp_path)
    assert r.status_code == 200, r.text
    v = r.json()
    assert v["new"] is True and v["display_name"] == "Romashka" and v["email"].endswith("@ecp.invalid")
    s = client.authsvc.post(f"{P}/session", json={"token": v["token"]})
    assert s.status_code == 200 and s.json()["user_id"] == v["user_id"]


def test_challenge_is_single_use(client, tmp_path):
    ch, r = login(client, tmp_path)
    assert r.status_code == 200
    again = client.post(f"{P}/ecp/login", json={"challenge": ch, "signature": make_cms(tmp_path, ch)})
    assert again.status_code == 401 and again.json()["detail"]["code"] == "invalid_challenge"


def test_unknown_challenge_rejected(client, tmp_path):
    r = client.post(f"{P}/ecp/login", json={"challenge": "ecp-login:forged", "signature": make_cms(tmp_path, "x")})
    assert r.status_code == 401


def test_expired_challenge_rejected(client, tmp_path, monkeypatch):
    ch = client.post(f"{P}/ecp/challenge").json()["challenge"]
    monkeypatch.setattr(ecp, "CHALLENGE_TTL", 0)
    real = ecp_app.time.time
    monkeypatch.setattr(ecp_app.time, "time", lambda: real() + 400)
    r = client.post(f"{P}/ecp/login", json={"challenge": ch, "signature": make_cms(tmp_path, ch)})
    assert r.status_code == 401


def test_bad_signature_format(client):
    ch = client.post(f"{P}/ecp/challenge").json()["challenge"]
    r = client.post(f"{P}/ecp/login", json={"challenge": ch, "signature": base64.b64encode(b"garbage").decode()})
    assert r.status_code == 422


def test_bad_verification_rejected(client, tmp_path, monkeypatch):
    def fail(msg, sig):
        raise ecp.EcpError("signature_invalid")
    monkeypatch.setattr(ecp, "verify_detached", fail)
    ch, r = login(client, tmp_path)
    assert r.status_code == 401 and r.json()["detail"]["code"] == "signature_invalid"


def test_same_certificate_same_account_independent_of_password_login(client, tmp_path):
    ch1 = client.post(f"{P}/ecp/challenge").json()["challenge"]
    sig1 = make_cms(tmp_path, ch1)
    ch2 = client.post(f"{P}/ecp/challenge").json()["challenge"]
    # тот же сертификат подписывает второй challenge
    (tmp_path / "m").write_bytes(ch2.encode())
    subprocess.run(["openssl", "cms", "-sign", "-binary", "-in", tmp_path / "m", "-signer", tmp_path / "c.pem",
                    "-inkey", tmp_path / "k.pem", "-outform", "DER", "-out", tmp_path / "m2.sgn"], check=True, capture_output=True)
    sig2 = base64.b64encode((tmp_path / "m2.sgn").read_bytes()).decode()
    a = client.post(f"{P}/ecp/login", json={"challenge": ch1, "signature": sig1}).json()
    b = client.post(f"{P}/ecp/login", json={"challenge": ch2, "signature": sig2}).json()
    assert a["user_id"] == b["user_id"] and a["new"] is True and b["new"] is False
    # по email/паролю в такую запись войти нельзя
    r = client.authsvc.post(f"{P}/login", json={"email": a["email"], "password": "anything-123"})
    assert r.status_code == 401


def test_auth_unavailable(client, tmp_path, monkeypatch):
    def down():
        raise ecp_app.httpx.ConnectError("down")
    monkeypatch.setattr(ecp_app, "auth_client", down)
    ch, r = login(client, tmp_path)
    assert r.status_code == 503 and r.json()["detail"]["code"] == "auth_unavailable"


def test_identity_recorded_in_ecp_db(client, tmp_path):
    import sqlite3
    ch, r = login(client, tmp_path)
    rows = sqlite3.connect(ecp_app.DB_PATH).execute("SELECT user_id FROM ecp_identities").fetchall()
    assert rows == [(r.json()["user_id"],)]


def test_login_returns_company_data_from_certificate(client, tmp_path):
    ch = client.post(f"{P}/ecp/challenge").json()["challenge"]
    sig = make_cms(tmp_path, ch, requisites={"1.2.643.100.4": "001234567890", "1.2.643.100.1": "1027700132195"})
    v = client.post(f"{P}/ecp/login", json={"challenge": ch, "signature": sig}).json()
    assert v["company"] == {"name": "Romashka", "inn": "1234567890", "ogrn": "1027700132195"}


def test_login_without_requisites_gives_empty_inn(client, tmp_path):
    _, r = login(client, tmp_path)
    assert r.json()["company"] == {"name": "Romashka", "inn": "", "ogrn": ""}


def test_verify_does_not_create_user_and_burns_challenge(client, tmp_path):
    ch = client.post(f"{P}/ecp/challenge").json()["challenge"]
    sig = make_cms(tmp_path, ch)
    r = client.post(f"{P}/ecp/verify", json={"challenge": ch, "signature": sig})
    assert r.status_code == 200 and len(r.json()["external_id"]) == 64
    assert client.post(f"{P}/ecp/verify", json={"challenge": ch, "signature": sig}).status_code == 401
