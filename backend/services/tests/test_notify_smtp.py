"""notify: настоящая отправка по SMTP на локальный тестовый сервер (tests/fake_smtp.py): режимы none|starttls|ssl, вход,
самоподписанный сертификат, отправитель = SMTP_USER, кириллица в заголовках и адресах, EHLO, разбор ошибок провайдеров
(вход, отправитель, получатель, неверный порт и режим), повторы, лимит скорости, проверка /smtp/check и python -m notify.check."""

import asyncio
import email
import email.policy
import time

import pytest
from fastapi.testclient import TestClient

import notify.app as na
from fake_smtp import FakeSmtp, make_cert
from notify import check as check_cli
from notify import mailer, render, store


@pytest.fixture
def cert(tmp_path_factory):
    return make_cert(tmp_path_factory.mktemp("tls"))


@pytest.fixture
def env(tmp_path, monkeypatch):
    import os

    for k in [k for k in os.environ if k.startswith(("SMTP_", "MAIL_"))]:
        monkeypatch.delenv(k)
    monkeypatch.setattr(store, "DB_PATH", str(tmp_path / "n" / "notify.db"))
    monkeypatch.setattr(na, "SCHEDULER", False)
    monkeypatch.setattr(mailer, "_busy", False)
    mailer._sent_at.clear()
    monkeypatch.setenv("SMTP_HOST", "127.0.0.1")
    monkeypatch.setenv("SMTP_TIMEOUT_SECONDS", "2")

    def setup(srv, **kw):
        monkeypatch.setenv("SMTP_PORT", str(srv.port))
        for k, v in kw.items():
            monkeypatch.setenv(k, v)

    return setup


def enqueue(to="a@b.ru", kind="test", dedup=None):
    s, h, t, hd = render.render(kind, {"email": to})
    return store.enqueue(kind, to, s, h, t, hd, dedup)["id"]


def deliver_one(to="a@b.ru"):
    mid = enqueue(to)
    res = asyncio.run(mailer.flush())
    return mid, res


def parsed(srv, i=0):
    return email.message_from_bytes(srv.messages[i].data, policy=email.policy.default)


# ---------- режимы и вход ----------
@pytest.mark.parametrize("mode", ["none", "starttls", "ssl"])
@pytest.mark.parametrize("login", [False, True])
def test_delivery_in_every_security_mode(env, cert, mode, login):
    srv_mode = {"none": "plain", "starttls": "starttls", "ssl": "ssl"}[mode]
    with FakeSmtp(srv_mode, cert=cert[0], key=cert[1], user="bot@firm.ru" if login else None) as srv:
        extra = {"SMTP_SECURITY": mode, "SMTP_CA_FILE": cert[0]}
        if login:
            extra |= {"SMTP_USER": "bot@firm.ru", "SMTP_PASSWORD": "secret"}
        env(srv, **extra)
        mid, res = deliver_one()
        assert res == {"sent": 1, "failed": 0}, store.message(mid)
        got = srv.messages[0]
        assert got.tls == (mode != "none") and got.auth_user == ("bot@firm.ru" if login else None)
        assert store.message(mid)["status"] == "sent"


def test_env_comments_taken_as_values_by_compose_are_ignored(env, monkeypatch):
    # Compose читает «SMTP_HOST=   # пусто — Mailpit» как значение «# пусто — Mailpit»
    for k in ("SMTP_HOST", "SMTP_PORT", "SMTP_SECURITY", "MAIL_FROM", "SMTP_USER"):
        monkeypatch.setenv(k, "# пусто — Mailpit")
    c = mailer.config()
    assert (c["host"], c["port"], c["security"], c["user"]) == ("mailpit", 1025, "none", "")
    assert c["sender_addr"] == mailer.DEFAULT_SENDER


def test_security_is_guessed_from_port_and_host(env, monkeypatch):
    monkeypatch.setenv("SMTP_PORT", "465")
    assert mailer.config()["security"] == "ssl"
    monkeypatch.setenv("SMTP_PORT", "587")
    assert mailer.config()["security"] == "starttls"
    monkeypatch.setenv("SMTP_PORT", "")
    monkeypatch.setenv("SMTP_HOST", "smtp.yandex.ru")
    assert (mailer.config()["security"], mailer.config()["port"]) == ("starttls", 587)
    monkeypatch.delenv("SMTP_HOST")  # Mailpit по умолчанию: без TLS, порт 1025
    c = mailer.config()
    assert (c["host"], c["port"], c["security"]) == ("mailpit", 1025, "none")
    monkeypatch.setenv("SMTP_SSL", "1")  # прежние переменные notifier
    assert mailer.config()["security"] == "ssl"


def test_self_signed_certificate_needs_ca_or_explicit_opt_out(env, cert):
    with FakeSmtp("starttls", cert=cert[0], key=cert[1]) as srv:
        env(srv, SMTP_SECURITY="starttls")
        res = mailer.check()
        assert (res["ok"], res["error_class"], res["stage"]) == (False, "tls_cert", "starttls")
        assert "SMTP_CA_FILE" in res["hint"]
        env(srv, SMTP_TLS_VERIFY="0")
        assert mailer.check()["ok"]
        env(srv, SMTP_TLS_VERIFY="1", SMTP_CA_FILE=cert[0])
        assert mailer.check()["ok"]


# ---------- отправитель, кодировки, EHLO ----------
def test_sender_defaults_to_smtp_user_and_name_is_encoded(env):
    with FakeSmtp("plain", user="bot@firm.ru", only_sender="bot@firm.ru") as srv:
        env(srv, SMTP_USER="bot@firm.ru", SMTP_PASSWORD="secret")
        _, res = deliver_one()
        assert res["sent"] == 1
        got = srv.messages[0]
        assert got.mail_from == "bot@firm.ru"
        raw = got.data.decode("ascii")  # заголовки целиком в ASCII
        assert "=?utf-8?" in raw.split("\r\n\r\n")[0].lower()
        msg = parsed(srv)
        assert msg["From"].addresses[0].display_name == "Центр Прогнозирования"
        assert msg["From"].addresses[0].addr_spec == "bot@firm.ru"
        assert msg["Subject"] == "Тестовое письмо"
        assert msg["Message-ID"].endswith("@firm.ru>")


def test_plain_mail_from_gets_product_name_and_foreign_sender_is_reported(env):
    with FakeSmtp("plain", user="bot@firm.ru", only_sender="bot@firm.ru") as srv:
        env(srv, SMTP_USER="bot@firm.ru", SMTP_PASSWORD="secret", MAIL_FROM="noreply@other.ru")
        assert mailer.config()["sender"].endswith("<noreply@other.ru>")
        res = mailer.check()
        assert (res["ok"], res["stage"], res["error_class"]) == (False, "sender", "sender_refused")
        assert "MAIL_FROM" in res["hint"] and any("MAIL_FROM отличается" in w for w in res["warnings"])
        mid, out = deliver_one()
        assert out["failed"] == 1 and store.message(mid)["last_error"].startswith("sender_refused [553]")


def test_default_sender_with_real_host_is_flagged(env):
    with FakeSmtp("plain") as srv:
        env(srv)
        assert mailer.check()["ok"]  # Mailpit-подобный сервер принимает всё
        assert any("noreply@centr-prognoz.local" in w for w in mailer.check()["warnings"])


def test_ehlo_uses_fqdn_not_container_id(env, monkeypatch):
    monkeypatch.setattr(mailer.socket, "getfqdn", lambda *a: "3f2a1b9c0d11")  # имя контейнера без точки
    with FakeSmtp("plain", require_fqdn_helo=True) as srv:
        env(srv, MAIL_FROM="Отчёты <noreply@firm.ru>")
        _, res = deliver_one()
        assert res["sent"] == 1 and srv.helos[0] == "firm.ru"
    with FakeSmtp("plain", require_fqdn_helo=True) as srv:
        env(srv, SMTP_HELO="mail.example.org")
        deliver_one()
        assert srv.helos[0] == "mail.example.org"


def test_idn_recipient_domain_is_sent_as_punycode(env):
    with FakeSmtp("plain") as srv:  # SMTPUTF8 сервер не объявляет
        env(srv)
        _, res = deliver_one("user@пример.рф")
        assert res["sent"] == 1
        assert srv.messages[0].rcpts == ["user@xn--e1afmkfd.xn--p1ai"]
        assert parsed(srv)["To"].addresses[0].domain == "xn--e1afmkfd.xn--p1ai"


def test_cyrillic_local_part_without_smtputf8_fails_at_once_with_clear_status(env):
    with FakeSmtp("plain") as srv:
        env(srv)
        mid, res = deliver_one("почта@firm.ru")
        assert res["failed"] == 1
        m = store.message(mid)
        assert m["status"] == "failed" and m["last_error"].startswith("unicode_address")


# ---------- ошибки провайдеров ----------
def test_wrong_password_is_reported_once_per_pass_and_not_retried_forever(env, capsys):
    with FakeSmtp("plain", user="bot@firm.ru") as srv:
        env(srv, SMTP_USER="bot@firm.ru", SMTP_PASSWORD="wrong-pass")
        first, second = enqueue("a@b.ru"), enqueue("c@d.ru")
        res = asyncio.run(mailer.flush())
        # сервер недоступен для всех: на этом проходе пробовали одно письмо, второе не тронуто
        assert res == {"sent": 0, "failed": 1}
        assert store.message(first)["attempts"] == 1 and store.message(second)["attempts"] == 0
        assert store.message(first)["last_error"].startswith("auth_failed [535]")
        assert srv.connections == 1
        for _ in range(len(store.RETRY_DELAYS) + 2):  # дальше расписание: через 1, 5, 30, 120 минут и failed
            with store.db() as conn:
                conn.execute("UPDATE outbox SET next_try=0 WHERE id=?", (first,))
            asyncio.run(mailer.flush())
        assert store.message(first)["status"] == "failed"
        logs = capsys.readouterr().out
        assert "auth_failed 535" in logs
        assert "wrong-pass" not in logs and "a@b.ru" not in logs and "bot@firm.ru" not in logs


def test_recipient_refused_is_permanent_and_address_is_not_logged(env, capsys):
    with FakeSmtp("plain", reject_rcpt={"nobody@b.ru": (550, "5.1.1 <nobody@b.ru> user unknown")}) as srv:
        env(srv)
        bad, good = enqueue("nobody@b.ru"), enqueue("ok@b.ru")
        res = asyncio.run(mailer.flush())
        assert res == {"sent": 1, "failed": 1}  # отказ получателя не останавливает остальных
        m = store.message(bad)
        assert m["status"] == "failed" and m["attempts"] == 1
        assert m["last_error"].startswith("recipient_refused [550]") and "nobody@b.ru" not in m["last_error"]
        assert store.message(good)["status"] == "sent"
        assert "nobody@b.ru" not in capsys.readouterr().out


def test_temporary_recipient_error_is_retried(env):
    with FakeSmtp("plain", reject_rcpt={"slow@b.ru": (451, "4.7.1 greylisted, try later")}) as srv:
        env(srv)
        mid, res = deliver_one("slow@b.ru")
        m = store.message(mid)
        assert m["status"] == "queued" and m["last_error"].startswith("rate_limited [451]")


def test_data_rejected_as_spam_is_permanent(env):
    with FakeSmtp("plain", data_reply="554 5.7.1 message looks like spam") as srv:
        env(srv)
        mid, _ = deliver_one()
        m = store.message(mid)
        assert m["status"] == "failed" and m["last_error"].startswith("message_rejected [554]")


def test_nothing_listens_on_port(env):
    import socket

    with socket.socket() as sk:
        sk.bind(("127.0.0.1", 0))
        port = sk.getsockname()[1]
    env(type("S", (), {"port": port})())  # порт свободен, слушателя нет
    res = mailer.check()
    assert (res["ok"], res["stage"], res["error_class"]) == (False, "connect", "connect_refused")
    mid, _ = deliver_one()
    assert store.message(mid)["status"] == "queued" and store.message(mid)["last_error"].startswith("connect_refused")


def test_timeout_when_server_is_silent(env, cert, monkeypatch):
    with FakeSmtp("ssl", cert=cert[0], key=cert[1], hang_after_connect=True) as srv:
        env(srv, SMTP_SECURITY="starttls", SMTP_TIMEOUT_SECONDS="0.5")
        t = time.time()
        res = mailer.check()
        assert time.time() - t < 5
        assert res["ok"] is False and res["error_class"] in ("timeout", "disconnected") and res["stage"] == "connect"
        assert "SMTP_SECURITY" in res["hint"] or "порт" in res["hint"]


def test_port_465_with_wrong_mode_gives_clear_hint():
    cfg = {"host": "smtp.yandex.ru", "port": 465, "security": "starttls"}
    for exc in (TimeoutError("timed out"), mailer.smtplib.SMTPServerDisconnected("closed")):
        c = mailer.classify(exc, cfg)
        assert c["cls"] == "tls_mismatch" and "SMTP_SECURITY=ssl" in c["hint"]


def test_ssl_mode_against_starttls_port_and_starttls_against_plain(env, cert):
    with FakeSmtp("starttls", cert=cert[0], key=cert[1]) as srv:
        env(srv, SMTP_SECURITY="ssl")
        res = mailer.check()
        assert res["error_class"] == "tls" and "SMTP_SECURITY=starttls" in res["hint"]
    with FakeSmtp("plain") as srv:
        env(srv, SMTP_SECURITY="starttls")
        res = mailer.check()
        assert (res["error_class"], res["stage"]) == ("starttls_unsupported", "starttls")


def test_auth_failure_hint_mentions_app_password_for_known_hosts(env):
    c = mailer.classify(
        mailer.smtplib.SMTPAuthenticationError(535, b"5.7.8 nope"), {"host": "smtp.gmail.com", "port": 587}
    )
    assert c["cls"] == "auth_failed" and "пароль приложения" in c["hint"]


# ---------- настройки ----------
@pytest.mark.parametrize(
    "bad, text",
    [
        ({"SMTP_PORT": "abc"}, "SMTP_PORT"),
        ({"SMTP_PORT": "70000"}, "SMTP_PORT"),
        ({"SMTP_SECURITY": "tls"}, "SMTP_SECURITY"),
        ({"SMTP_USER": "bot@firm.ru"}, "SMTP_PASSWORD пуст"),
        ({"SMTP_PASSWORD": "x"}, "SMTP_USER пуст"),
        ({"SMTP_USER": "bot@firm.ru", "SMTP_PASSWORD": "пароль"}, "ASCII"),
        ({"MAIL_FROM": "noreply"}, "MAIL_FROM"),
        ({"SMTP_TIMEOUT_SECONDS": "быстро"}, "SMTP_TIMEOUT_SECONDS"),
    ],
)
def test_bad_settings_are_explained_and_do_not_break_the_queue(env, monkeypatch, capsys, bad, text):
    for k, v in bad.items():
        monkeypatch.setenv(k, v)
    with pytest.raises(mailer.ConfigError, match=text):
        mailer.config()
    res = mailer.check()
    assert (res["ok"], res["stage"], res["error_class"]) == (False, "config", "config") and text in res["hint"]
    mid = enqueue()
    assert asyncio.run(mailer.flush()) == {"sent": 0, "failed": 0, "config_error": True}
    assert store.message(mid)["status"] == "queued" and store.message(mid)["attempts"] == 0
    assert "smtp config error" in capsys.readouterr().out or mailer._last_config_log


def test_password_never_appears_in_check_result(env):
    with FakeSmtp("plain", user="bot@firm.ru") as srv:
        env(srv, SMTP_USER="bot@firm.ru", SMTP_PASSWORD="Sup3rSecretValue")
        res = mailer.check()
        assert "Sup3rSecretValue" not in repr(res) and res["error_class"] == "auth_failed"


# ---------- очередь ----------
def test_non_smtp_exception_does_not_wedge_the_queue(env, monkeypatch):
    calls = []

    def boom(msg, cfg):
        calls.append(1)
        if len(calls) == 1:
            raise UnicodeEncodeError("ascii", "п", 0, 1, "ordinal not in range")
        return None

    monkeypatch.setattr(mailer, "send_impl", boom)
    first, second = enqueue("a@b.ru"), enqueue("c@d.ru")
    res = asyncio.run(mailer.flush())
    assert res == {"sent": 1, "failed": 1}
    assert store.message(first)["status"] == "failed" and store.message(second)["status"] == "sent"
    assert asyncio.run(mailer.flush()) == {"sent": 0, "failed": 0}  # зависшее письмо не крутится каждые 5 секунд


def test_parallel_flushes_send_each_letter_once(env, monkeypatch):
    sent = []

    def slow(msg, cfg):
        time.sleep(0.2)
        sent.append(msg["To"])

    monkeypatch.setattr(mailer, "send_impl", slow)
    for i in range(3):
        enqueue(f"u{i}@b.ru")

    async def go():
        return await asyncio.gather(mailer.flush(), mailer.flush(), mailer.flush())

    asyncio.run(go())
    assert sorted(map(str, sent)) == ["u0@b.ru", "u1@b.ru", "u2@b.ru"]


def test_rate_limit_spreads_letters_over_passes(env):
    with FakeSmtp("plain") as srv:
        env(srv, SMTP_MAX_PER_MINUTE="2")
        for i in range(5):
            enqueue(f"u{i}@b.ru")
        assert asyncio.run(mailer.flush())["sent"] == 2
        assert asyncio.run(mailer.flush())["sent"] == 0  # минута ещё не прошла
        assert len(srv.messages) == 2 and store.stats()["queued"] == 3
        mailer._sent_at.clear()  # «прошла минута»
        assert asyncio.run(mailer.flush())["sent"] == 2


def test_backoff_grows_and_dedup_gives_one_letter(env):
    env(type("S", (), {"port": 1})(), SMTP_TIMEOUT_SECONDS="0.5")  # на порту 1 никого нет
    mid = enqueue(dedup="once")
    assert enqueue(dedup="once") == mid
    delays, now = [], time.time()
    for _ in store.RETRY_DELAYS:
        with store.db() as conn:
            conn.execute("UPDATE outbox SET next_try=0 WHERE id=?", (mid,))
        asyncio.run(mailer.flush())
        with store.db() as conn:
            delays.append(round(conn.execute("SELECT next_try FROM outbox WHERE id=?", (mid,)).fetchone()[0] - now))
    assert delays[0] in range(59, 63) and delays == sorted(delays) and delays[-1] >= 7000


def test_requeue_failed_after_fixing_settings(env):
    with FakeSmtp("plain", user="bot@firm.ru") as srv:
        env(srv, SMTP_USER="bot@firm.ru", SMTP_PASSWORD="wrong")
        mid = enqueue()
        with store.db() as conn:
            conn.execute("UPDATE outbox SET status='failed', attempts=5, last_error='auth_failed [535]' WHERE id=?", (mid,))
        assert store.stats()["errors"] == {"auth_failed": 1}
        env(srv, SMTP_PASSWORD="secret")
        assert store.requeue_failed() == 1
        assert asyncio.run(mailer.flush())["sent"] == 1 and store.message(mid)["status"] == "sent"


# ---------- HTTP и консоль ----------
def test_check_endpoint_and_outbox_stats(env, monkeypatch):
    monkeypatch.setattr(na, "SCHEDULER", False)
    with FakeSmtp("plain", user="bot@firm.ru") as srv, TestClient(na.app) as c:
        env(srv, SMTP_USER="bot@firm.ru", SMTP_PASSWORD="wrong-pass")
        r = c.get("/internal/v1/smtp/check")
        j = r.json()
        assert r.status_code == 200 and j["ok"] is False and j["stage"] == "auth" and j["error_class"] == "auth_failed"
        assert j["hint"] and "wrong-pass" not in r.text and j["port"] == srv.port and j["user_set"] is True
        env(srv, SMTP_PASSWORD="secret")
        j = c.get("/internal/v1/smtp/check").json()
        assert j["ok"] is True and j["stage"] == "done" and srv.messages == []  # письмо не отправлялось
        assert c.get("/internal/v1/outbox").json()["queued"] == 0
        assert c.post("/internal/v1/outbox/requeue").json() == {"requeued": 0}


def test_send_returns_before_smtp_finishes(env, monkeypatch):
    def slow(msg, cfg):
        time.sleep(1.0)

    monkeypatch.setattr(mailer, "send_impl", slow)
    with TestClient(na.app) as c:
        t = time.time()
        r = c.post("/internal/v1/send", json={"kind": "test", "to": "a@b.ru"})
        assert r.status_code == 202 and time.time() - t < 0.7


def test_address_with_list_or_header_characters_is_rejected(env):
    with TestClient(na.app) as c:
        for bad in ("a,b@c.ru", "a;b@c.ru", "<a>@c.ru", 'a"b@c.ru', "a b@c.ru"):
            r = c.post("/internal/v1/send", json={"kind": "test", "to": bad})
            assert r.status_code == 422, bad
        assert c.post("/internal/v1/send", json={"kind": "test", "to": "ivan.petrov+x@firm-1.ru"}).status_code == 202


def test_cli_check_and_test_letter(env, capsys):
    with FakeSmtp("plain", user="bot@firm.ru") as srv:
        env(srv, SMTP_USER="bot@firm.ru", SMTP_PASSWORD="wrong-pass")
        assert check_cli.main([]) == 1
        out = capsys.readouterr().out
        assert "вход по логину и паролю" in out and "auth_failed" in out and "wrong-pass" not in out
        env(srv, SMTP_PASSWORD="secret")
        assert check_cli.main([]) == 0 and srv.messages == []
        assert check_cli.main(["--to", "me@firm.ru"]) == 0
        assert srv.messages[0].rcpts == ["me@firm.ru"]
        assert "secret" not in capsys.readouterr().out
