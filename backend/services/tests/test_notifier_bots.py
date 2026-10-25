"""Свои боты компаний (notifier): подъём и остановка циклов polling без перезапуска, выбор бота по компании, запасной общий бот,
привязка чата и изоляция, очистка токена из ошибок и журналов. Telegram и ledger подменены httpx.MockTransport, сети нет."""
import asyncio
import json
import logging

import httpx
import pytest
from fastapi.testclient import TestClient

from notifier import config
from notifier.app import app
from notifier.bot import BotApp
from notifier.bots import BotManager, RateLimit
from notifier.jobs import Jobs
from notifier.store import Store
from notifier.telegram import Bot, TelegramError, install_log_scrub, scrub
from notifier.upstream import Upstream

SHARED = "111111111:SHAREDshared_SHAREDshared_SHAREDshared"
TOK_A = "222222222:AAAAaaaaBBBBbbbbCCCCccccDDDDdddd_xyz"
TOK_A2 = "222222222:NEWnewNEWnewNEWnewNEWnewNEWnewNEW_abc"
TOK_B = "333333333:BBBBbbbbCCCCccccDDDDddddEEEEeeee_qrs"


def run(coro):
    return asyncio.run(coro)


class Net:
    """Имитаторы Telegram (по токену из адреса) и ledger."""

    def __init__(self):
        self.tokens: list[dict] = []  # что отдаёт ledger
        self.ledger_down = False
        self.polls: dict[str, int] = {}  # токен -> число getUpdates
        self.updates: dict[str, list[dict]] = {}  # токен -> очередь входящих
        self.sent: list[tuple[str, str, dict]] = []  # (токен, метод, тело)
        self.revoked: set[str] = set()
        self.statuses: list[dict] = []
        self.audits: list[dict] = []
        self.proxy_seen = None

    def set_tokens(self, **by_company):
        self.tokens = [{"company_id": c, "token": t, "bot_username": f"{c}_bot"} for c, t in by_company.items()]

    async def telegram(self, req: httpx.Request) -> httpx.Response:
        seg = req.url.path.lstrip("/")
        token, method = seg[3:].rsplit("/", 1)
        if token in self.revoked:
            return httpx.Response(401, json={"ok": False, "error_code": 401, "description": f"Unauthorized (bot{token})"})
        if method == "getUpdates":
            self.polls[token] = self.polls.get(token, 0) + 1
            q = self.updates.get(token, [])
            out, self.updates[token] = list(q), []
            if not out:
                await asyncio.sleep(0.01)
            return httpx.Response(200, json={"ok": True, "result": out})
        body = json.loads(req.content) if req.content else {}
        self.sent.append((token, method, body))
        if method == "getMe":
            return httpx.Response(200, json={"ok": True, "result": {"id": int(token.split(":")[0]), "is_bot": True, "username": f"bot_{token[:3]}"}})
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    def ledger(self, req: httpx.Request) -> httpx.Response:
        p = req.url.path
        if self.ledger_down:
            return httpx.Response(503, json={})
        if p == "/internal/v1/integrations/telegram/credentials":
            if req.headers.get("x-actor") != "svc:notifier":
                return httpx.Response(403, json={"error": {"code": "forbidden", "message": ""}})
            return httpx.Response(200, json={"items": self.tokens, "skipped": 0})
        if p.endswith("/integrations/telegram/status"):
            self.statuses.append({"path": p, **json.loads(req.content)})
            return httpx.Response(200, json={})
        if p.endswith("/audit"):
            self.audits.append(json.loads(req.content))
            return httpx.Response(204)
        if p.startswith("/internal/v1/companies/") and p.count("/") == 4:
            return httpx.Response(200, json={"company_id": p.rsplit("/", 1)[1], "company_name": "ООО Тест", "banned": False})
        return httpx.Response(404, json={})

    def sent_by(self, token, method="sendMessage"):
        return [b for t, m, b in self.sent if t == token and m == method]


@pytest.fixture
def net():
    return Net()


def make(net, shared=True):
    store = Store(":memory:")
    up = Upstream(httpx.MockTransport(net.ledger))
    tr = httpx.MockTransport(net.telegram)
    jobs = Jobs(store, Bot(SHARED, tr) if shared else None, up)
    return store, jobs, BotManager(store, jobs, up, tr), tr


async def settle(n=0.08):
    await asyncio.sleep(n)


# ---------- подъём и остановка ----------
def test_sync_starts_one_polling_loop_per_company_token_and_stops_on_disconnect(net):
    async def go():
        store, jobs, mgr, _ = make(net)
        net.set_tokens(A=TOK_A, B=TOK_B)
        assert (await mgr.sync())["started"] == 2
        await settle()
        assert net.polls.get(TOK_A, 0) > 0 and net.polls.get(TOK_B, 0) > 0, "каждый бот опрашивается своим циклом"
        assert set(jobs.own) == {"A", "B"} and mgr.username("A") == "A_bot"
        # компания B отключила бота: цикл остановлен, общий бот снова её бот
        net.set_tokens(A=TOK_A)
        assert (await mgr.sync())["stopped"] == 1
        n = net.polls[TOK_B]
        await settle()
        assert net.polls[TOK_B] == n, "опрос отключённого бота прекращён"
        assert "B" not in jobs.own and jobs.bot_for("B") is jobs.bot and jobs.bot_for("A") is jobs.own["A"]
        await mgr.stop_all()
        assert not jobs.own and not mgr.runners
    run(go())


def test_changed_token_restarts_only_that_loop_and_same_token_is_kept(net):
    async def go():
        store, jobs, mgr, _ = make(net)
        net.set_tokens(A=TOK_A, B=TOK_B)
        await mgr.sync()
        ra, rb = mgr.runners["A"], mgr.runners["B"]
        assert (await mgr.sync()) == {"started": 0, "stopped": 0, "running": 2}
        assert mgr.runners["A"] is ra and mgr.runners["B"] is rb
        net.set_tokens(A=TOK_A2, B=TOK_B)
        r = await mgr.sync()
        assert (r["started"], r["stopped"]) == (1, 1)
        assert mgr.runners["A"] is not ra and mgr.runners["B"] is rb
        await settle()
        old = net.polls.get(TOK_A, 0)
        await settle()
        assert net.polls.get(TOK_A, 0) == old and net.polls[TOK_A2] > 0
        assert ra.task.cancelled() or ra.task.done()
        await mgr.stop_all()
    run(go())


def test_ledger_outage_does_not_stop_running_bots(net):
    async def go():
        store, jobs, mgr, _ = make(net)
        net.set_tokens(A=TOK_A)
        await mgr.sync()
        net.ledger_down = True
        assert (await mgr.sync()).get("unavailable") and set(jobs.own) == {"A"}
        await mgr.stop_all()
    run(go())


def test_revoked_token_stops_the_loop_and_reports_error_without_token(net):
    async def go():
        store, jobs, mgr, _ = make(net)
        net.revoked.add(TOK_A)
        net.set_tokens(A=TOK_A)
        await mgr.sync()
        await settle(0.15)
        assert mgr.runners["A"].task.done(), "цикл завершился после 401"
        assert net.statuses and net.statuses[0]["ok"] is False and net.statuses[0]["path"].endswith("/companies/A/integrations/telegram/status")
        assert TOK_A not in json.dumps(net.statuses) and "AAAAaaaa" not in json.dumps(net.statuses)
        await mgr.stop_all()
    run(go())


# ---------- выбор бота ----------
def test_broadcast_uses_company_bot_and_falls_back_to_shared(net):
    async def go():
        store, jobs, mgr, _ = make(net)
        net.set_tokens(A=TOK_A)
        await mgr.sync()
        store.link_chat(1, "A", "private", "a", "u", "own")
        store.link_chat(2, "B", "private", "b", "u", "shared")
        store.link_chat(3, "A", "private", "legacy", "u", "shared")  # привязан общим ботом до смены на свой
        await jobs.broadcast("A", lambda c: jobs.bot_for("A").send(c, "для A"))
        await jobs.broadcast("B", lambda c: jobs.bot_for("B").send(c, "для B"))
        assert [b["chat_id"] for b in net.sent_by(TOK_A)] == [1], "компания A: только свой бот и только его чаты"
        assert [b["chat_id"] for b in net.sent_by(SHARED)] == [2], "компания без своего токена: общий бот"
        await mgr.stop_all()
    run(go())


def test_without_shared_bot_company_without_own_token_has_no_telegram(net):
    async def go():
        store, jobs, mgr, _ = make(net, shared=False)
        net.set_tokens(A=TOK_A)
        await mgr.sync()
        store.link_chat(2, "B", "private", "b", "u", "shared")
        assert jobs.bot_for("B") is None and await jobs.broadcast("B", lambda c: None) == 0
        assert jobs.bot_for("A") is not None
        await mgr.stop_all()
    run(go())


# ---------- привязка чата и изоляция ----------
def start(chat_id, code, uid=7):
    return {"update_id": 1, "message": {"chat": {"id": chat_id, "type": "private", "first_name": "Дир"}, "from": {"id": uid, "first_name": "Дир"}, "text": f"/start {code}"}}


def text_update(chat_id, text, uid=7):
    return {"update_id": 2, "message": {"chat": {"id": chat_id, "type": "private", "first_name": "Дир"}, "from": {"id": uid, "first_name": "Дир"}, "text": text}}


def test_link_code_works_in_company_bot_and_is_bound_to_that_company(net):
    async def go():
        store, jobs, mgr, _ = make(net)
        net.set_tokens(A=TOK_A, B=TOK_B)
        await mgr.sync()
        app_a, app_b = mgr.runners["A"].app, mgr.runners["B"].app
        code_a = store.new_link_code("A", "u1", "director")
        # код компании A, открытый в боте компании B: отказ, код не расходуется
        await app_b.handle(start(50, code_a))
        assert store.chat(50) is None and "другой компании" in net.sent_by(TOK_B)[-1]["text"]
        assert store.peek_link_code(code_a) is not None
        # в общем боте код компании со своим ботом тоже не привязывает
        shared_app = BotApp(store, jobs.bot, jobs, "shared_bot")
        await shared_app.handle(start(51, code_a))
        assert store.chat(51) is None and "собственный бот" in net.sent_by(SHARED)[-1]["text"]
        # в своём боте привязывается; чат помечен как привязанный через собственный бот
        await app_a.handle(start(52, code_a))
        ch = store.chat(52)
        assert ch["company_id"] == "A" and ch["via"] == "own"
        assert "Чат подключён" in net.sent_by(TOK_A)[-1]["text"]
        assert store.peek_link_code(code_a) is None, "код одноразовый"
        await mgr.stop_all()
    run(go())


def test_company_bot_sees_only_its_own_company_chats(net):
    async def go():
        store, jobs, mgr, _ = make(net)
        net.set_tokens(A=TOK_A)
        await mgr.sync()
        app_a = mgr.runners["A"].app
        store.link_chat(60, "B", "private", "чужой", "u", "own")      # чат другой компании
        store.link_chat(61, "A", "private", "общий", "u", "shared")   # чат компании A, но привязан общим ботом
        store.link_chat(62, "A", "private", "свой", "u", "own")
        for chat in (60, 61):
            await app_a.handle(text_update(chat, "/goals"))
            assert "ещё не подключён" in net.sent_by(TOK_A)[-1]["text"], chat
        before = len(net.sent)
        await app_a.handle(text_update(62, "/goals"))
        assert len(net.sent) == before + 1 and "ещё не подключён" not in net.sent_by(TOK_A)[-1]["text"]
        # кнопки чужого чата тоже не работают
        await app_a.handle({"update_id": 3, "callback_query": {"id": "cb", "from": {"id": 7}, "data": "g:1:done", "message": {"chat": {"id": 60}, "message_id": 1}}})
        assert net.sent_by(TOK_A, "answerCallbackQuery")[-1]["text"] == "Чат не подключён"
        await mgr.stop_all()
    run(go())


def test_polling_handles_updates_of_the_right_bot_with_separate_offsets(net):
    async def go():
        store, jobs, mgr, _ = make(net)
        net.set_tokens(A=TOK_A, B=TOK_B)
        code_a = store.new_link_code("A", "u1", "director")
        net.updates[TOK_A] = [{**start(70, code_a), "update_id": 10}]
        await mgr.sync()
        await settle(0.15)
        assert store.chat(70)["via"] == "own"
        assert store.state_get("offset:A") == "11" and store.state_get("offset:B") == "" and store.state_get("offset") == ""
        await mgr.stop_all()
    run(go())


# ---------- токен не попадает в ошибки и журналы ----------
def test_telegram_errors_never_contain_the_token():
    async def boom(req):
        raise httpx.ConnectError(f"cannot connect to {req.url}")

    bot = Bot(TOK_A, httpx.MockTransport(boom))
    with pytest.raises(TelegramError) as e:
        run(bot.get_me())
    assert TOK_A not in str(e.value) and "AAAAaaaa" not in str(e.value) and e.value.description == "ConnectError"
    assert "AAAAaaaa" not in repr(e.value) and e.value.__cause__ is None
    err = TelegramError(401, f"Unauthorized https://api.telegram.org/bot{TOK_A}/getMe")
    assert TOK_A not in str(err) and "[token]" in err.description


def test_log_records_are_scrubbed_including_httpx_url_logs(caplog):
    install_log_scrub()
    with caplog.at_level(logging.DEBUG):
        logging.getLogger("httpx").warning('HTTP Request: POST https://api.telegram.org/bot%s/getMe "200 OK"', TOK_A)
        logging.getLogger("notifier").warning("ошибка %s", f"url=bot{TOK_B}/x")
        logging.getLogger("notifier").warning(f"прямо в тексте {TOK_A}")
    out = caplog.text
    assert "AAAAaaaa" not in out and "BBBBbbbb" not in out and out.count("[token]") == 3, out
    assert scrub("время 12:30 и id 123456:abc") == "время 12:30 и id 123456:abc"


# ---------- прокси ----------
def test_proxy_is_used_for_real_clients_and_not_with_mock_transport(monkeypatch):
    monkeypatch.setenv("TELEGRAM_PROXY", "socks5h://127.0.0.1:1")
    assert config.telegram_proxy() == "socks5h://127.0.0.1:1"
    b = Bot(TOK_A)
    assert b.proxy == "socks5h://127.0.0.1:1"
    assert b.client(5)._mounts, "socks5h:// поддерживается (httpx[socks])"
    http_proxy = Bot(TOK_A, proxy="http://proxy.local:3128")
    assert http_proxy.client(5)._mounts
    assert not Bot(TOK_A, proxy="").client(5)._mounts
    assert not Bot(TOK_A, httpx.MockTransport(lambda r: httpx.Response(200)), "http://p:1").client(5)._mounts
    # каждый бот компании получает тот же прокси
    monkeypatch.delenv("TELEGRAM_PROXY")
    assert Bot(TOK_B).proxy == ""


# ---------- HTTP: проверка токена, синхронизация, статус ----------
@pytest.fixture
def client(net):
    store = Store(":memory:")
    app.state.store = store
    app.state.bot = Bot(SHARED, httpx.MockTransport(net.telegram))
    app.state.up = Upstream(httpx.MockTransport(net.ledger))
    app.state.tg_transport = httpx.MockTransport(net.telegram)
    app.state.disable_tasks = True
    with TestClient(app) as c:
        app.state.username = "shared_bot"  # lifespan берёт имя из окружения: подставляем после запуска
        yield c
    app.state.store = app.state.bot = app.state.up = None
    app.state.tg_transport = None
    app.state.disable_tasks = False


def verify(c, cid, token=None):
    return c.post(f"/internal/v1/companies/{cid}/telegram/verify", json={"token": token} if token else {})


def test_verify_checks_getme_and_never_echoes_the_token(client, net):
    r = verify(client, "A", TOK_A)
    assert r.status_code == 200 and r.json() == {"ok": True, "bot_username": "bot_222", "bot_id": 222222222}
    assert TOK_A not in r.text
    for bad in ("abc", "123:short", TOK_A + "!!"):
        r = verify(client, "A", bad)
        assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_bot_token" and bad not in r.text
    net.revoked.add(TOK_A)
    r = verify(client, "A", TOK_A)
    assert r.status_code == 422 and r.json()["error"]["code"] == "telegram_token_invalid"
    assert TOK_A not in r.text and "AAAAaaaa" not in r.text


def test_verify_unreachable_telegram_suggests_proxy_without_token(client, net, monkeypatch):
    async def boom(req):
        raise httpx.ConnectTimeout(f"timeout {req.url}")

    app.state.tg_transport = httpx.MockTransport(boom)
    app.state.bots.transport = app.state.tg_transport
    r = verify(client, "A", TOK_A)
    assert r.status_code == 503 and r.json()["error"]["code"] == "telegram_unreachable"
    assert "TELEGRAM_PROXY" in r.json()["error"]["message"] and TOK_A not in r.text


def test_verify_saved_token_is_taken_from_ledger(client, net):
    assert verify(client, "A").json()["error"]["code"] == "telegram_not_connected"
    net.set_tokens(A=TOK_A)
    r = verify(client, "A")
    assert r.status_code == 200 and r.json()["bot_username"] == "bot_222"
    assert any(t == TOK_A and m == "getMe" for t, m, _ in net.sent)
    net.ledger_down = True
    assert verify(client, "A").status_code == 503


def test_verify_is_rate_limited_per_company(client, net):
    assert [verify(client, "A", TOK_A).status_code for _ in range(10)] == [200] * 10
    r = verify(client, "A", TOK_A)
    assert r.status_code == 429 and r.json()["error"]["code"] == "rate_limited"
    assert verify(client, "B", TOK_B).status_code == 200, "лимит у каждой компании свой"
    lim = RateLimit(2, 60)
    assert [lim.allow("x"), lim.allow("x"), lim.allow("x"), lim.allow("y")] == [True, True, False, True]


def test_one_bot_token_cannot_serve_two_companies_or_replace_the_shared_bot(client, net, monkeypatch):
    net.set_tokens(A=TOK_A)
    r = verify(client, "B", TOK_A)
    assert r.status_code == 409 and r.json()["error"]["code"] == "telegram_token_in_use" and TOK_A not in r.text
    assert verify(client, "A", TOK_A).status_code == 200, "своя компания может перепроверить свой токен"
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", SHARED)
    assert verify(client, "B", SHARED).status_code == 409


def test_sync_endpoint_starts_the_bot_and_status_and_link_use_it(client, net):
    s = client.get("/internal/v1/companies/A/notifications").json()
    assert (s["configured"], s["bot_kind"], s["bot_username"]) == (True, "shared", "shared_bot")
    net.set_tokens(A=TOK_A)

    r = client.post("/internal/v1/telegram/sync")
    assert r.status_code == 200 and r.json()["started"] == 1
    s = client.get("/internal/v1/companies/A/notifications").json()
    assert (s["configured"], s["bot_kind"], s["bot_username"]) == (True, "own", "A_bot")
    other = client.get("/internal/v1/companies/B/notifications").json()
    assert (other["bot_kind"], other["bot_username"]) == ("shared", "shared_bot"), "соседняя компания остаётся на общем боте"
    link = client.post("/internal/v1/companies/A/notifications/link", json={})
    assert link.status_code == 200 and link.json()["link"].startswith("https://t.me/A_bot?start=")
    assert client.post("/internal/v1/companies/B/notifications/link", json={}).json()["link"].startswith("https://t.me/shared_bot?start=")
    # токен отключён: ответ sync останавливает бот, ссылка снова ведёт к общему боту
    net.set_tokens()
    assert client.post("/internal/v1/telegram/sync").json()["stopped"] == 1
    assert client.get("/internal/v1/companies/A/notifications").json()["bot_kind"] == "shared"
    assert TOK_A not in client.get("/internal/v1/companies/A/notifications").text
    run(app.state.bots.stop_all())


def test_company_without_any_bot_gets_503_with_a_hint(client, net):
    app.state.bot = None
    app.state.jobs.bot = None
    r = client.post("/internal/v1/companies/A/notifications/link", json={})
    assert r.status_code == 503 and r.json()["error"]["code"] == "bot_not_configured" and "Интеграции" in r.json()["error"]["message"]
    s = client.get("/internal/v1/companies/A/notifications").json()
    assert (s["configured"], s["bot_kind"]) == (False, "none")
