"""Диалог с ботом: привязка чата, команды, кнопки оценки целей, настройки. Обновления приходят long polling'ом."""
import asyncio
import logging
import re
from datetime import date

from . import config, fmt, reports
from .fmt import esc, rub
from .jobs import Jobs
from .store import STATUSES, Store
from .telegram import Bot, TelegramError
from .upstream import UpstreamError

log = logging.getLogger("notifier")
UNLINKED = ("Этот чат ещё не подключён к компании.\n\nОткройте сервис → «Настройки» → «Telegram-уведомления», нажмите «Подключить» "
            "и перейдите по выданной ссылке (она действует 15 минут).")
DOW = ["понедельник", "вторник", "среду", "четверг", "пятницу", "субботу", "воскресенье"]
TIMES = [f"{h:02d}:00" for h in range(6, 23)]
GAP_DAYS = [3, 5, 7, 10, 14]
THRESHOLDS = [0.1, 0.2, 0.3, 0.5, 0.7]
STATUS_TEXT = {"done": "выполнено", "partial": "частично", "failed": "не выполнено", "skipped": "пропущено"}


def cycle(values: list, cur):
    return values[(values.index(cur) + 1) % len(values)] if cur in values else values[0]


class BotApp:
    def __init__(self, store: Store, bot: Bot, jobs: Jobs, username: str = ""):
        self.store, self.bot, self.jobs, self.username = store, bot, jobs, username

    # ---------- опрос Telegram ----------
    async def poll_forever(self) -> None:
        offset = int(self.store.state_get("offset", "0"))
        while True:
            try:
                for u in await self.bot.get_updates(offset):
                    offset = u["update_id"] + 1
                    self.store.state_set("offset", str(offset))
                    try:
                        await self.handle(u)
                    except Exception as e:
                        log.warning("update %s: %s", u.get("update_id"), type(e).__name__)
            except TelegramError as e:
                log.warning("getUpdates: %s", e.code)
                await asyncio.sleep(5)
            except Exception as e:
                log.warning("poll: %s", type(e).__name__)
                await asyncio.sleep(5)

    async def handle(self, u: dict) -> None:
        if "message" in u:
            await self.on_message(u["message"])
        elif "callback_query" in u:
            await self.on_callback(u["callback_query"])
        elif "my_chat_member" in u:
            m = u["my_chat_member"]
            if m["new_chat_member"]["status"] in ("left", "kicked"):
                self.store.unlink_chat(m["chat"]["id"])

    # ---------- сообщения ----------
    async def audit(self, cid: str, uid: int, action: str, obj: str = "") -> None:
        await self.jobs.up.audit(cid, f"tg:{uid}", action, obj)

    async def say(self, chat_id: int, text: str, kb=None):
        try:
            await self.bot.send(chat_id, text, kb)
        except TelegramError as e:
            log.warning("send %s: %s", chat_id, e.code)

    async def on_message(self, m: dict) -> None:
        chat, text = m["chat"], (m.get("text") or "").strip()
        if not text:
            return
        chat_id, uid = chat["id"], m.get("from", {}).get("id", 0)
        m_cmd = re.match(r"^/(\w+)(?:@\w+)?(?:\s+(.*))?$", text, re.S)
        if not m_cmd:
            await self.on_text(chat_id, uid, text)
            return
        cmd, arg = m_cmd.group(1).lower(), (m_cmd.group(2) or "").strip()
        if cmd == "start":
            await self.cmd_start(chat, uid, arg)
            return
        link = self.store.chat(chat_id)
        if not link:
            await self.say(chat_id, UNLINKED)
            return
        cid = link["company_id"]
        self.store.touch_member(cid, uid, m.get("from", {}).get("first_name", ""))
        try:
            await self.run_command({**chat, "link": link}, uid, cid, cmd, arg)
        except UpstreamError as e:
            await self.say(chat_id, "Не удалось получить данные: " + ("загрузите выписку в сервисе, чтобы построить прогноз." if e.status in (404, 422) else "сервис временно недоступен, попробуйте позже."))

    async def run_command(self, chat: dict, uid: int, cid: str, cmd: str, arg: str) -> None:
        chat_id, link = chat["id"], chat["link"]
        need = {"unlink": "admin", "settings": "admin", "tz": "admin", "goal": "rater"}.get(cmd)
        if need and not self.store.can(link, uid, need):
            await self.say(chat_id, "Недостаточно прав: " + ("это действие доступно администратору компании." if need == "admin" else "оценивать и добавлять цели могут участники с ролью «оценивает цели» и выше."))
            return
        kind = {"report": "week", "risk": "risk", "month": "month"}.get(cmd)
        pol = self.store.policy(link)
        if (kind and kind not in pol["kinds"]) or (cmd == "excel" and not pol["allow_excel"]):
            await self.say(chat_id, "Этот отчёт отключён политикой отдела для данного чата.")
            return
        if cmd in ("help", "menu"):
            await self.say(chat_id, reports.help_text())
        elif cmd == "ask":
            if not arg:
                await self.say(chat_id, "Задайте вопрос после команды, например: <code>/ask как закрыть разрыв 9 октября?</code>")
            else:
                await self.ask(chat_id, cid, arg)
        elif cmd == "report":
            await self.jobs.send_week(cid, manual=True)
        elif cmd == "risk":
            await self.jobs.send_risk(cid)
        elif cmd == "month":
            await self.jobs.send_month(cid)
        elif cmd == "excel":
            await self.jobs.send_excel(cid, chat_id, "week")
        elif cmd == "goals":
            await self.show_goals(chat_id, cid)
        elif cmd == "goal":
            if arg:
                await self.add_goal(chat_id, cid, arg, uid)
            else:
                self.store.set_pending(chat_id, "goal", {"cid": cid})
                await self.say(chat_id, "Напишите цель одним сообщением. Можно с суммой: <code>Собрать дебиторку | 250000</code>")
        elif cmd == "settings":
            await self.show_settings(chat_id, cid)
        elif cmd == "tz":
            await self.set_tz(chat_id, cid, arg)
        elif cmd == "unlink":
            self.store.unlink_chat(chat_id)
            await self.audit(cid, uid, "telegram.unlink", str(chat_id))
            await self.say(chat_id, "Чат отключён. Уведомления больше приходить не будут.")
        else:
            await self.say(chat_id, "Не знаю такой команды.\n\n" + reports.help_text())

    async def cmd_start(self, chat: dict, uid: int, code: str) -> None:
        chat_id = chat["id"]
        if code:
            row = self.store.use_link_code(code)
            if not row:
                await self.say(chat_id, "Ссылка устарела или уже использована. Создайте новую в «Настройках» сервиса.")
                return
            title = chat.get("title") or chat.get("first_name") or ""
            self.store.link_chat(chat_id, row["company_id"], "private" if chat["type"] == "private" else "group", title, row["user_id"])
            who = (chat.get("first_name") or chat.get("title") or "")
            if row["web_role"]:
                self.store.bind_web_member(row["company_id"], uid, who, row["user_id"], row["web_role"])  # роль из веб-роли
            else:
                self.store.touch_member(row["company_id"], uid, who, "admin")
                self.store.set_member(row["company_id"], uid, "admin", None, False)
            name = await self.jobs._company(row["company_id"])
            await self.audit(row["company_id"], uid, "telegram.link", str(chat_id))
            await self.say(chat_id, f"✅ <b>Чат подключён</b>{' к «' + esc(name) + '»' if name else ''}.\n\nЗдесь будут приходить:\n"
                           "• алерты о кассовом разрыве за 7, 3 и 1 день;\n• риск-отчёт по понедельникам;\n• итоги недели и план;\n• итоги месяца и план.\n\n"
                           "Расписание меняется командой /settings. Список команд: /help.")
            return
        if self.store.chat(chat_id):
            await self.say(chat_id, "Чат подключён. Команды: /help")
        else:
            await self.say(chat_id, UNLINKED)

    async def ask(self, chat_id: int, cid: str, question: str) -> None:
        try:
            await self.bot.call("sendChatAction", {"chat_id": chat_id, "action": "typing"})
        except TelegramError:
            pass
        try:
            await self.say(chat_id, await self.jobs.ask(cid, chat_id, question))
        except UpstreamError:
            await self.say(chat_id, "Нет данных для ответа: загрузите банковскую выписку в сервисе.")

    async def on_text(self, chat_id: int, uid: int, text: str) -> None:
        link = self.store.chat(chat_id)
        if not link:
            return
        if link["kind"] == "private" and not self.store.pop_pending_peek(chat_id):
            await self.ask(chat_id, link["company_id"], text)  # в личном чате вопрос можно писать без команды
            return
        if not self.store.can(link, uid, "rater"):
            return  # ожидание ответа не снимаем: чужое сообщение в группе не должно его «съесть»
        pend = self.store.pop_pending(chat_id)
        if not pend:
            return
        kind, p = pend
        if kind == "goal":
            await self.add_goal(chat_id, p["cid"], text, uid)
        elif kind == "blockers":
            self.store.save_retro(p["cid"], p["week"], blockers=text[:500])
            await self.say(chat_id, "Записал. Учту при планировании.")

    async def add_goal(self, chat_id: int, cid: str, text: str, uid: int = 0) -> None:
        title, _, amount = text.partition("|")
        title = title.strip()[:200]
        if not title:
            await self.say(chat_id, "Цель пустая. Напишите, например: <code>Собрать дебиторку | 250000</code>")
            return
        kop = None
        if amount.strip():
            digits = re.sub(r"[^\d,.]", "", amount).replace(",", ".")
            try:
                kop = round(float(digits) * 100)
            except ValueError:
                kop = None
        sprint = self.jobs.current_sprint(cid, self._today(cid)) or self._today(cid).isoformat()
        gid = self.store.add_goal(cid, sprint, "custom", title + (f" ({rub(kop)})" if kop else ""), kop, None, "user")
        await self.audit(cid, uid, "goal.add", str(gid))
        await self.say(chat_id, f"➕ Цель добавлена на текущую неделю: <b>{esc(title)}</b>\nВсе цели: /goals")

    def _today(self, cid: str) -> date:
        return config.now_in(config.tz_of(self.store.prefs(cid)["tz"])).date()

    async def show_goals(self, chat_id: int, cid: str) -> None:
        sprint = self.jobs.current_sprint(cid, self._today(cid))
        goals = self.store.goals(cid, sprint) if sprint else []
        if not goals:
            await self.say(chat_id, "Целей на этой неделе пока нет. Они создаются вместе с недельным отчётом (/report), свои — через /goal.")
            return
        rows, lines = [], [f"🎯 <b>Цели недели с {fmt.day(date.fromisoformat(sprint))}</b>", ""]
        for i, g in enumerate(goals, 1):
            lines.append(f"{i}. {reports.STATUS_ICON[g['status']]} {esc(g['title'])}")
            rows.append([{"text": f"{i} ✅", "callback_data": f"g:{g['id']}:done"}, {"text": f"{i} 🌓", "callback_data": f"g:{g['id']}:partial"},
                         {"text": f"{i} ❌", "callback_data": f"g:{g['id']}:failed"}, {"text": f"{i} ➖", "callback_data": f"g:{g['id']}:skipped"}])
        rows.append([{"text": "➕ Цель", "callback_data": "ga"}])
        await self.say(chat_id, "\n".join(lines), rows)

    # ---------- настройки ----------
    def settings_view(self, cid: str) -> tuple[str, list[list[dict]]]:
        p = self.store.prefs(cid)
        on = lambda v: "вкл" if v else "выкл"  # noqa: E731
        text = ("⚙️ <b>Настройки уведомлений</b>\n\n"
                f"🕒 Часовой пояс: <code>{esc(p['tz'])}</code> (сменить: <code>/tz Europe/Berlin</code>)\n"
                f"🛡 Риск-отчёт: {on(p['risk_enabled'])}, {DOW[p['risk_dow']]} в {p['risk_time']}\n"
                f"📅 Недельный отчёт и план: {on(p['week_enabled'])}, {DOW[p['week_dow']]} в {p['week_time']}\n"
                f"🗓 Месячный отчёт и план: {on(p['month_enabled'])}, {p['month_day']}-го числа в {p['month_time']}\n"
                f"🚨 Алерт разрыва: {on(p['gap_enabled'])}, за {p['gap_days']} дн., порог вероятности {fmt.pct(p['gap_threshold'])}\n\n"
                "Нажимайте кнопки — значение переключается по кругу.")
        kb = [
            [{"text": f"🛡 Риск: {on(p['risk_enabled'])}", "callback_data": "s:risk_enabled"}, {"text": f"день: {fmt.WEEKDAYS_SHORT[p['risk_dow']]}", "callback_data": "s:risk_dow"},
             {"text": p["risk_time"], "callback_data": "s:risk_time"}],
            [{"text": f"📅 Неделя: {on(p['week_enabled'])}", "callback_data": "s:week_enabled"}, {"text": f"день: {fmt.WEEKDAYS_SHORT[p['week_dow']]}", "callback_data": "s:week_dow"},
             {"text": p["week_time"], "callback_data": "s:week_time"}],
            [{"text": f"🗓 Месяц: {on(p['month_enabled'])}", "callback_data": "s:month_enabled"}, {"text": f"число: {p['month_day']}", "callback_data": "s:month_day"},
             {"text": p["month_time"], "callback_data": "s:month_time"}],
            [{"text": f"🚨 Алерт: {on(p['gap_enabled'])}", "callback_data": "s:gap_enabled"}, {"text": f"за {p['gap_days']} дн.", "callback_data": "s:gap_days"},
             {"text": f"порог {fmt.pct(p['gap_threshold'])}", "callback_data": "s:gap_threshold"}],
        ]
        return text, kb

    async def show_settings(self, chat_id: int, cid: str) -> None:
        text, kb = self.settings_view(cid)
        await self.say(chat_id, text, kb)

    async def set_tz(self, chat_id: int, cid: str, name: str) -> None:
        try:
            from zoneinfo import ZoneInfo
            ZoneInfo(name)
        except Exception:
            await self.say(chat_id, "Не знаю такой пояс. Пример: <code>/tz Europe/Moscow</code>, <code>/tz Asia/Yekaterinburg</code>.")
            return
        self.store.set_prefs(cid, {"tz": name})
        await self.say(chat_id, f"Часовой пояс: <code>{esc(name)}</code>")

    def toggle(self, cid: str, field: str) -> None:
        p = self.store.prefs(cid)
        v = p[field]
        if field.endswith("_enabled"):
            new = 0 if v else 1
        elif field.endswith("_dow"):
            new = (v + 1) % 7
        elif field.endswith("_time"):
            new = cycle(TIMES, v)
        elif field == "month_day":
            new = {1: 5, 5: 10, 10: 15, 15: 20, 20: 25}.get(v, 1)
        elif field == "gap_days":
            new = cycle(GAP_DAYS, v)
        elif field == "gap_threshold":
            new = cycle(THRESHOLDS, v)
        else:
            return
        self.store.set_prefs(cid, {field: new})

    # ---------- кнопки ----------
    async def on_callback(self, cb: dict) -> None:
        msg = cb.get("message") or {}
        chat = msg.get("chat") or {}
        chat_id, uid, data = chat.get("id"), cb.get("from", {}).get("id", 0), cb.get("data", "")
        link = self.store.chat(chat_id) if chat_id else None
        if not link:
            await self.bot.answer_callback(cb["id"], "Чат не подключён")
            return
        cid = link["company_id"]
        parts = data.split(":")
        need = "admin" if parts[0] == "s" else "rater" if parts[0] in ("g", "r", "b", "ga") else None
        if need and not self.store.can(link, uid, need):
            await self.bot.answer_callback(cb["id"], "Недостаточно прав для этого действия")
            return
        if parts[0] == "x" and not self.store.policy(link)["allow_excel"]:
            await self.bot.answer_callback(cb["id"], "Excel отключён политикой отдела")
            return
        if parts[0] == "s":
            self.toggle(cid, parts[1])
            await self.audit(cid, uid, "telegram.settings", parts[1])
            text, kb = self.settings_view(cid)
            try:
                await self.bot.call("editMessageText", {"chat_id": chat_id, "message_id": msg["message_id"], "text": text, "parse_mode": "HTML",
                                                        "reply_markup": {"inline_keyboard": kb}})
            except TelegramError:
                pass
            await self.bot.answer_callback(cb["id"])
        elif parts[0] == "g" and len(parts) == 3:
            await self.on_goal_status(cb, chat_id, msg, cid, int(parts[1]), parts[2])
            await self.audit(cid, uid, "goal.status", f"{parts[1]}:{parts[2]}")
        elif parts[0] == "r" and len(parts) == 3:
            self.store.save_retro(cid, parts[1], mood=int(parts[2]))
            await self.bot.answer_callback(cb["id"], "Записал, спасибо!")
        elif parts[0] == "b" and len(parts) == 2:
            self.store.set_pending(chat_id, "blockers", {"cid": cid, "week": parts[1]})
            await self.bot.answer_callback(cb["id"])
            await self.say(chat_id, "Что мешало выполнять цели? Напишите одним сообщением (например: «клиенты задерживают оплаты»).")
        elif data == "ga":
            self.store.set_pending(chat_id, "goal", {"cid": cid})
            await self.bot.answer_callback(cb["id"])
            await self.say(chat_id, "Напишите цель одним сообщением. Можно с суммой: <code>Собрать дебиторку | 250000</code>")
        elif parts[0] == "x" and len(parts) == 2:
            await self.bot.answer_callback(cb["id"], "Готовлю отчёт…")
            try:
                await self.jobs.send_excel(cid, chat_id, parts[1])
            except UpstreamError:
                await self.say(chat_id, "Не удалось собрать отчёт: нет данных или сервис недоступен.")
        else:
            await self.bot.answer_callback(cb["id"])

    async def on_goal_status(self, cb: dict, chat_id: int, msg: dict, cid: str, gid: int, status: str) -> None:
        g = self.store.goal(cid, gid)
        if not g or status not in STATUSES:
            await self.bot.answer_callback(cb["id"], "Цель не найдена")
            return
        self.store.update_goal(cid, gid, {"status": status})
        await self.bot.answer_callback(cb["id"], f"Записал: {STATUS_TEXT.get(status, status)}")
        kb = [r for r in (msg.get("reply_markup") or {}).get("inline_keyboard", []) if not any(b.get("callback_data", "").startswith(f"g:{gid}:") for b in r)]
        await self.bot.edit_keyboard(chat_id, msg["message_id"], kb)
        rest = [x for x in self.store.goals(cid, g["week_start"]) if x["status"] == "open"]
        if rest:
            return
        # все цели прошлой недели оценены: пересобираем план текущей
        nxt = self.store.one("SELECT MIN(week_start) AS w FROM goals WHERE company_id=? AND week_start>?", (cid, g["week_start"]))
        if not nxt or not nxt["w"]:
            return
        goals = await self.jobs.replan(cid, nxt["w"])
        done = sum(1 for x in self.store.goals(cid, g["week_start"]) if x["status"] == "done")
        total = sum(1 for x in self.store.goals(cid, g["week_start"]) if x["status"] != "skipped")
        lines = [f"🔄 <b>План обновлён с учётом ваших оценок</b> (выполнено {done} из {total})", ""]
        lines += [f"{i}. {esc(x['title'])}" for i, x in enumerate(goals, 1)]
        await self.jobs.broadcast(cid, lambda c: self.bot.send(c, "\n".join(lines)), "week")
