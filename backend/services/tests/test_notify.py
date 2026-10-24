"""notify: очередь писем и повторы, шаблоны, подписки и отписка, рассылки notifier письмом (Telegram-HTML, график,
Excel), приглашения в команду с лимитом. SMTP и внутренние сервисы — заглушки."""

import asyncio
import base64
import smtplib

import httpx
import pytest
from fastapi.testclient import TestClient

import notify.app as na
from notify import mailer, render, store

P = "/internal/v1"


class Mesh:
    """ledger (:3002) — компании и участники, auth (:8001) — статус почты."""

    def __init__(self):
        self.companies = [{"id": "c1", "name": "Ромашка", "banned": False}]
        self.members = {
            "c1": [
                {"user_id": "u1", "email": "boss@romashka.ru", "role": "director"},
                {"user_id": "u2", "email": "ed@romashka.ru", "role": "editor"},
            ]
        }
        self.verified = {"u1": True, "u2": True}

    def handler(self, req: httpx.Request) -> httpx.Response:
        path, port = req.url.path, req.url.port
        if port == 3002 and path == "/internal/v1/admin/companies":
            assert req.url.params["banned"] == "false"
            return httpx.Response(200, json={"total": len(self.companies), "items": self.companies})
        if port == 3002 and path.endswith("/members"):
            return httpx.Response(200, json={"items": self.members.get(path.split("/")[4], [])})
        if port == 8001 and path == "/internal/v1/users/email-status":
            ids = req.url.params["user_ids"].split(",")
            emails = {m["user_id"]: m["email"] for ms in self.members.values() for m in ms}
            return httpx.Response(
                200,
                json={
                    "items": [{"user_id": i, "email": emails[i], "verified": self.verified.get(i, False)} for i in ids]
                },
            )
        return httpx.Response(404, json={"error": {"code": "not_found", "message": path}})


class Smtp:
    def __init__(self):
        self.sent, self.fail = [], 0

    def __call__(self, msg, cfg):
        if self.fail:
            self.fail -= 1
            raise smtplib.SMTPServerDisconnected("down")
        self.sent.append(msg)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", str(tmp_path / "sub" / "notify.db"))
    monkeypatch.setattr(na, "SCHEDULER", False)
    na._subscribed.clear()
    mailer._sent_at.clear()
    mesh, smtp = Mesh(), Smtp()
    monkeypatch.setattr(na.app.state, "transport", httpx.MockTransport(mesh.handler), raising=False)
    monkeypatch.setattr(mailer, "send_impl", smtp)
    with TestClient(na.app) as c:
        yield c, mesh, smtp


def subjects(smtp):
    return [str(m["Subject"]) for m in smtp.sent]


def body(msg, kind="html"):
    return msg.get_body((kind,)).get_content()


# ---------- служебные письма ----------
def test_send_builds_link_and_both_parts(env):
    c, _, smtp = env
    r = c.post(f"{P}/send", json={"kind": "verify_email", "to": "User@Example.com", "data": {"token": "abc"}})
    assert r.status_code == 202
    msg = smtp.sent[0]
    assert msg["To"] == "user@example.com" and msg["Subject"] == "Подтвердите адрес почты"
    assert "http://localhost:3000/verify-email?token=abc" in body(msg) and "verify-email?token=abc" in body(
        msg, "plain"
    )
    assert "List-Unsubscribe" not in msg, "у служебных писем нет отписки"


def test_send_validates_kind_email_and_data(env):
    c, _, _ = env
    assert c.post(f"{P}/send", json={"kind": "gap_alert", "to": "a@b.ru"}).json()["error"]["code"] == "invalid_kind"
    assert c.post(f"{P}/send", json={"kind": "test", "to": "x@ecp.invalid"}).json()["error"]["code"] == "invalid_email"
    assert c.post(f"{P}/send", json={"kind": "reset_password", "to": "a@b.ru"}).status_code == 422, "нет токена"
    assert c.post(f"{P}/send", json={"kind": "invite", "to": "a@b.ru", "data": {"token": "t"}}).status_code == 422, (
        "нет компании"
    )


def test_templates_escape_user_input(env):
    c, _, smtp = env
    data = {"company": "<script>alert(1)</script>", "reason": "<b>спам</b>"}
    c.post(f"{P}/send", json={"kind": "company_banned", "to": "a@b.ru", "data": data})
    html = body(smtp.sent[0])
    assert "<script>" not in html and "&lt;script&gt;" in html and "&lt;b&gt;спам" in html


def test_dedup_key_gives_one_letter(env):
    c, _, smtp = env
    for _ in range(3):
        c.post(f"{P}/send", json={"kind": "test", "to": "a@b.ru", "dedup_key": "k1"})
    assert len(smtp.sent) == 1


def test_retry_schedule_then_failed(env, monkeypatch):
    _, _, smtp = env
    smtp.fail = 99
    subject, html, text, headers = render.render("test", {"email": "a@b.ru"})
    mid = store.enqueue("test", "a@b.ru", subject, html, text)["id"]
    clock = [1000.0]
    monkeypatch.setattr(store.time, "time", lambda: clock[0])
    store.enqueue("noop", "x@y.ru", "s", "h", "t", dedup_key="force-next-try")  # другое письмо не мешает
    with store.db() as conn:
        conn.execute("UPDATE outbox SET next_try=0")
    states = []
    for delay in (0, *store.RETRY_DELAYS):
        clock[0] += delay
        asyncio.run(mailer.flush())
        states.append(store.message(mid)["status"])
    assert states == ["queued"] * 4 + ["failed"]
    assert store.message(mid)["attempts"] == 5


def test_retry_succeeds_after_outage(env):
    _, _, smtp = env
    smtp.fail = 1
    s, h, t, _ = render.render("test", {"email": "a@b.ru"})
    mid = store.enqueue("test", "a@b.ru", s, h, t)["id"]
    asyncio.run(mailer.flush())
    assert store.message(mid)["status"] == "queued"
    with store.db() as conn:
        conn.execute("UPDATE outbox SET next_try=0")
    asyncio.run(mailer.flush())
    assert store.message(mid)["status"] == "sent" and len(smtp.sent) == 1


# ---------- подписки ----------
def test_prefs_defaults_by_role(env):
    c, _, _ = env
    q = {"company_id": "c1"}
    assert c.get(f"{P}/prefs/u1", params=q | {"role": "director"}).json() == {"alerts": True, "reports": True}
    assert c.get(f"{P}/prefs/u3", params=q | {"role": "admin"}).json() == {"alerts": True, "reports": True}
    assert c.get(f"{P}/prefs/u2", params=q | {"role": "editor"}).json() == {"alerts": False, "reports": False}
    r = c.put(f"{P}/prefs/u2", params=q | {"role": "editor"}, json={"reports": True})
    assert r.json() == {"alerts": False, "reports": True}
    assert c.get(f"{P}/prefs/bad id").status_code == 404


def test_unsubscribe_by_token_turns_everything_off(env):
    c, _, _ = env
    c.get(f"{P}/prefs/u1", params={"company_id": "c1", "role": "director"})
    token = store.known_prefs(["u1"])["u1"]["unsub_token"]
    assert c.post(f"{P}/unsubscribe", json={"token": "nope"}).status_code == 404
    assert c.post(f"{P}/unsubscribe", json={"token": token}).json() == {"ok": True}
    assert c.get(f"{P}/prefs/u1").json() == {"alerts": False, "reports": False}


def test_migration_from_first_version_keeps_choices(tmp_path, monkeypatch):
    import sqlite3

    path = str(tmp_path / "v1.db")
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE prefs(user_id TEXT PRIMARY KEY, company_id TEXT, alerts INTEGER, digest INTEGER, threshold INTEGER,"
        " digest_day INTEGER, unsub_token TEXT, updated_at REAL)"
    )
    conn.execute("INSERT INTO prefs VALUES('u9','c1',0,1,50,0,'tok9',1)")
    conn.execute("CREATE TABLE alert_state(user_id TEXT)")
    conn.commit()
    conn.close()
    monkeypatch.setattr(store, "DB_PATH", path)
    assert store.get_prefs("u9", "c1", "director") == {"alerts": False, "reports": True, "unsub_token": "tok9"}


# ---------- рассылки notifier ----------
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32
TG = '🚨 <b>Риск кассового разрыва</b>\nКомпания: ООО &lt;Тест&gt;\n<a href="http://localhost:3000/forecast">прогноз</a> <script>x</script>'


def bc(c, **kw):
    body = {"kind": "gap", "key": "2026-11-12:7", "subject": "Риск кассового разрыва", "html": TG} | kw
    return c.post(f"{P}/companies/c1/broadcast", json=body)


def test_broadcast_goes_to_subscribers_once_per_key(env):
    c, _, smtp = env
    r = bc(c)
    assert r.status_code == 200 and r.json() == {"queued": 1, "recipients": 1}, "по умолчанию — только владелец"
    assert [m["To"] for m in smtp.sent] == ["boss@romashka.ru"]
    assert bc(c).json()["queued"] == 0, "тот же key второго письма не даёт"
    msg = smtp.sent[0]
    html = body(msg)
    assert "<strong>Риск кассового разрыва</strong>" in html and "ООО &lt;Тест&gt;" in html
    assert "<script>" not in html and 'href="http://localhost:3000/forecast"' in html
    assert "Риск кассового разрыва" in body(msg, "plain") and "<b>" not in body(msg, "plain")
    assert "List-Unsubscribe" in msg and "/unsubscribe?token=" in html


def test_broadcast_respects_kind_subscription_and_verification(env):
    c, mesh, smtp = env
    c.put(f"{P}/prefs/u1", params={"company_id": "c1", "role": "director"}, json={"alerts": False})
    c.put(f"{P}/prefs/u2", params={"company_id": "c1", "role": "editor"}, json={"reports": True})
    assert bc(c).json()["recipients"] == 0, "алерты выключены у всех"
    assert bc(c, kind="week", key="w1").json()["recipients"] == 2
    mesh.verified["u2"] = False
    assert bc(c, kind="month", key="m1").json()["recipients"] == 1, "без подтверждённой почты писем нет"
    assert c.get(f"{P}/companies/c1/subscribers").json() == {"alerts": 0, "reports": 1}


def test_missed_payments_go_to_alert_subscribers_with_warning_tone(env):
    c, _, smtp = env
    c.put(f"{P}/prefs/u1", params={"company_id": "c1", "role": "director"}, json={"reports": False})
    r = bc(c, kind="missed", key="k1", subject="🔔 Пропущенные платежи: 1")
    assert r.json() == {"queued": 1, "recipients": 1}, "пропуски идут по подписке на алерты"
    assert "#fdf5e6" in body(smtp.sent[0])
    c.put(f"{P}/prefs/u1", params={"company_id": "c1", "role": "director"}, json={"alerts": False, "reports": True})
    assert bc(c, kind="missed", key="k2").json()["recipients"] == 0


def test_broadcast_attaches_chart_inline_and_excel(env):
    c, _, smtp = env
    r = bc(
        c,
        kind="month",
        key="2026-10",
        chart_png_b64=base64.b64encode(PNG).decode(),
        xlsx_b64=base64.b64encode(b"PK-excel").decode(),
        xlsx_name="otchet 10/2026.xlsx",
    )
    assert r.json()["queued"] == 1
    msg = smtp.sent[0]
    assert 'src="cid:chart"' in body(msg)
    parts = {p.get_content_type(): p for p in msg.walk()}
    assert parts["image/png"]["Content-ID"] == "<chart>" and parts["image/png"].get_content() == PNG
    xlsx = parts["application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"]
    assert xlsx.get_filename() == "otchet_10_2026.xlsx" and xlsx.get_content() == b"PK-excel"
    with store.db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM attachments").fetchone()[0] == 0, "после отправки вложения удалены"


def test_broadcast_validates_input(env):
    c, _, _ = env
    assert bc(c, kind="spam").json()["error"]["code"] == "invalid_kind"
    assert bc(c, chart_png_b64="не base64").status_code == 422
    assert bc(c, path="//evil.example").status_code == 422
    assert c.post(f"{P}/companies/../broadcast", json={}).status_code == 404


def test_companies_with_subscribers(env):
    c, mesh, _ = env
    mesh.companies.append({"id": "c2", "name": "Пусто", "banned": False})
    assert c.get(f"{P}/companies-with-subscribers").json() == {"items": ["c1"]}


# ---------- приглашения в команду ----------
def test_team_invite_link_role_and_limit(env, monkeypatch):
    c, _, smtp = env
    data = {
        "token": "CODE123",
        "company": "Ромашка",
        "company_id": "c1",
        "inviter": "boss@romashka.ru",
        "role": "editor",
    }
    r = c.post(f"{P}/send", json={"kind": "team_invite", "to": "new@x.ru", "data": data})
    assert r.status_code == 202
    msg = smtp.sent[0]
    assert msg["Subject"] == "Приглашение в команду «Ромашка»"
    assert "http://localhost:3000/?invite=CODE123" in body(msg) and "аналитик" in body(msg, "plain")
    monkeypatch.setattr(na, "INVITES_PER_HOUR", 2)
    assert c.post(f"{P}/send", json={"kind": "team_invite", "to": "b@x.ru", "data": data}).status_code == 202
    r = c.post(f"{P}/send", json={"kind": "team_invite", "to": "c@x.ru", "data": data})
    assert r.status_code == 429 and r.json()["error"]["code"] == "mail_limited"
    other = data | {"company_id": "c2"}
    assert c.post(f"{P}/send", json={"kind": "team_invite", "to": "d@x.ru", "data": other}).status_code == 202


def test_render_tg_html_closes_tags_and_drops_bad_links():
    html, text = render.tg_to_mail('<b>жирный <i>курсив</b> <a href="javascript:alert(1)">ссылка</a>')
    assert str(html) == "<strong>жирный <em>курсив</em></strong> ссылка"
    assert text == "жирный курсив ссылка"
