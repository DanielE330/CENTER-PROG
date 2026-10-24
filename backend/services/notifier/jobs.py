"""Рассылки и расписание: алерт разрыва, риск-отчёт, недельный и месячный отчёты, ретро-напоминание.
Два канала: Telegram (если задан токен бота) и почта через notify (участникам, у которых она включена)."""
import asyncio
import logging
import re
import time
from datetime import date, datetime, timedelta

import json

from . import analytics as an, charts, config, excel, fmt, reports
from .advisor import Advisor
from .service import Bundle, collect
from .store import Store
from .telegram import Bot, TelegramError
from .upstream import Upstream, UpstreamError

log = logging.getLogger("notifier")
CATCH_UP_MIN = 360  # опоздавший отчёт отправляем не позже, чем через 6 часов после времени по расписанию
GAP_CHECK_EVERY = 3 * 3600
GAP_HOURS = range(8, 21)


class Jobs:
    def __init__(self, store: Store, bot: Bot | None, up: Upstream, advisor: Advisor | None = None):
        self.store, self.bot, self.up, self.advisor = store, bot, up, advisor or Advisor()
        self._bundles: dict[str, tuple[float, Bundle]] = {}
        self._bans: dict[str, tuple[float, str | None]] = {}

    BAN_TTL = 60

    async def banned(self, cid: str) -> str | None:
        """Причина блокировки компании оператором платформы или None. Заблокированной компании не шлём ничего и не отвечаем в боте.
        Если ledger недоступен, считаем компанию доступной: сбой не должен глушить уведомления всем."""
        hit = self._bans.get(cid)
        if hit and time.time() - hit[0] < self.BAN_TTL:
            return hit[1]
        try:
            c = await self.up.company(cid)
        except Exception:
            return None
        reason = (c.get("ban_reason") or "без указания причины") if c.get("banned") else None
        self._bans[cid] = (time.time(), reason)
        return reason

    # ---------- доставка ----------
    async def broadcast(self, cid: str, fn, kind: str | None = None, excel_only: bool = False) -> int:
        """Вызывает fn(chat_id) для каждого активного чата компании, которому политика отдела разрешает этот вид рассылки.
        Возвращает число доставленных. Без токена бота Telegram-канал выключен."""
        if self.bot is None or await self.banned(cid):
            return 0
        ok = 0
        for ch in self.store.chats(cid):
            pol = self.store.policy(ch)
            if (kind and kind not in pol["kinds"]) or (excel_only and not pol["allow_excel"]):
                continue
            try:
                await fn(ch["chat_id"])
                ok += 1
            except TelegramError as e:
                if e.migrate_to:
                    self.store.link_chat(e.migrate_to, cid, ch["kind"], ch["title"], ch["linked_by"])
                    self.store.unlink_chat(ch["chat_id"])
                elif e.chat_gone:
                    self.store.unlink_chat(ch["chat_id"])
                    log.warning("чат %s отключён: %s", ch["chat_id"], e.description)
                else:
                    log.warning("не доставлено в %s: %s", ch["chat_id"], e)
        return ok

    async def mail(self, cid: str, kind: str, key: str, text: str, path: str = "/", png: bytes | None = None,
                   chart_alt: str = "", xlsx: bytes | None = None, xlsx_name: str = "") -> int:
        """То же сообщение письмом: первая строка — тема, остальное — текст. Возвращает число получателей."""
        if await self.banned(cid):
            return 0
        head, _, body = text.partition("\n")
        subject = re.sub(r"<[^>]+>", "", head).replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&").strip()
        return await self.up.mail(cid, kind, key, subject or "Уведомление", body.strip("\n"), path, png, chart_alt, xlsx, xlsx_name)

    async def _bundle(self, cid: str, anchor: date | None = None, now_goals: str | None = None) -> Bundle:
        prefs = self.store.prefs(cid)
        today = anchor or config.now_in(config.tz_of(prefs["tz"])).date()
        return await collect(self.up, self.store, cid, today, prefs, now_goals)

    # ---------- алерт разрыва ----------
    async def check_gap(self, cid: str, force_text: bool = False) -> str:
        """Один проход проверки. Возвращает 'sent' | 'recovered' | 'none' | 'dup'."""
        prefs = self.store.prefs(cid)
        today = config.now_in(config.tz_of(prefs["tz"])).date()
        fc = an.parse_forecast(await self.up.forecast(cid))
        alert = an.gap_alert(fc, today, prefs["gap_days"], prefs["gap_threshold"])
        if alert:
            if not self.store.claim(cid, "gap", alert["key"]):
                return "dup"
            company = (await self._company(cid))
            text = reports.gap_alert_text(company, alert, prefs["gap_days"])
            kb = [[{"text": "📊 График и причины", "callback_data": "x:risk"}] + reports.web_button("/forecast")]
            tg = await self.broadcast(cid, lambda c: self.bot.send(c, text, kb), "gap")
            mailed = await self.mail(cid, "gap", alert["key"], text, "/forecast")
            if not tg and not mailed:
                self.store.release(cid, "gap", alert["key"])  # никто не получил — повторим в следующую проверку
                return "none"
            self.store.kv_set(cid, "gap_active", alert["date"].isoformat())
            return "sent"
        active = self.store.kv_get(cid, "gap_active")
        if active:
            self.store.kv_set(cid, "gap_active", None)
            if fc.break_date is None or fc.prob < prefs["gap_threshold"]:
                text = reports.gap_recovered_text(await self._company(cid))
                await self.broadcast(cid, lambda c: self.bot.send(c, text), "gap")
                await self.mail(cid, "gap_resolved", f"{active}:{today.isoformat()}", text, "/forecast")
                return "recovered"
        return "none"

    async def _advise(self, cid: str, b: Bundle, kind: str) -> None:
        """Совет Hermes кэшируется на день по виду отчёта: повторный запрос отчёта не гоняет модель заново."""
        if not self.advisor.enabled or not self.store.prefs(cid)["ai_consent"]:
            return  # облачная модель получает данные компании только с согласия администратора
        key = f"advice:{kind}:{b.today.isoformat()}"
        cached = self.store.kv_get(cid, key)
        if cached:
            b.advice = json.loads(cached)
            return
        b.advice = await self.advisor.advise(b, kind)
        if b.advice:
            self.store.kv_set(cid, key, json.dumps(b.advice, ensure_ascii=False))

    ASK_PER_HOUR = 10
    BUNDLE_TTL = 600

    async def ask(self, cid: str, chat_id: int, question: str) -> str:
        """Ответ консультанта на вопрос. Возвращает готовый текст для чата (HTML)."""
        if await self.banned(cid):
            return "Компания заблокирована оператором платформы."
        p = self.store.prefs(cid)
        if not self.advisor.enabled:
            return "Консультант Hermes не подключён администратором сервиса."
        if not p["ai_consent"]:
            return "Консультант выключен: администратор компании должен разрешить передачу статистики облачной модели в «Настройки → Telegram-уведомления»."
        now = config.now_in(config.tz_of(p["tz"]))
        if not self.store.hit(cid, f"ask:{chat_id}:{now:%Y%m%d%H}", self.ASK_PER_HOUR):
            return f"Лимит вопросов исчерпан ({self.ASK_PER_HOUR} в час на чат). Попробуйте позже."
        hit = self._bundles.get(cid)
        if hit and time.time() - hit[0] < self.BUNDLE_TTL:
            b = hit[1]
        else:
            b = await self._bundle(cid)
            self._bundles[cid] = (time.time(), b)
        ans = await self.advisor.answer(b, question)
        if not ans:
            return "Не удалось получить ответ консультанта, попробуйте позже."
        return "🧠 <b>Консультант Hermes</b>\n" + fmt.esc(ans)

    async def _company(self, cid: str) -> str:
        try:
            return (await self.up.company(cid)).get("company_name", "")
        except UpstreamError:
            return ""

    # ---------- отчёты ----------
    async def send_risk(self, cid: str, b: Bundle | None = None) -> None:
        b = b or await self._bundle(cid)
        await self._advise(cid, b, "risk")
        png, text = charts.risk_chart(b), reports.with_advice(reports.risk_text(b), b)
        kb = [[{"text": "📎 Excel", "callback_data": "x:risk"}] + reports.web_button("/forecast")]
        await self.broadcast(cid, lambda c: self.bot.send_photo(c, png, "🛡 Прогноз остатка на ближайший месяц"), "risk")
        await self.broadcast(cid, lambda c: self.bot.send(c, text, kb), "risk")
        await self.mail(cid, "risk", self._mail_key(b), text, "/forecast", png, "Прогноз остатка на ближайший месяц")

    def _ensure_sprint(self, cid: str, b: Bundle) -> Bundle:
        """Создаёт цели нового спринта (один раз) и подтягивает их в b."""
        key = b.today.isoformat()
        if not self.store.goals(cid, key):
            for g in an.propose_goals(b.fc, b.txs, b.pf, b.today, b.goals_prev):
                self.store.add_goal(cid, key, g["kind"], g["title"], g["target_kop"], g["due"], "auto")
        b.goals_now = self.store.goals(cid, key)
        b.tips = an.best_practices(b.fc, b.risk, b.week, b.month_last, b.pf_week, b.vel, b.today)
        return b

    def current_sprint(self, cid: str, today: date) -> str | None:
        """Начало текущего спринта, если он начался меньше 7 дней назад."""
        row = self.store.one("SELECT MAX(week_start) AS w FROM goals WHERE company_id=? AND week_start<=?", (cid, today.isoformat()))
        if row and row["w"] and 0 <= (today - date.fromisoformat(row["w"])).days < 7:
            return row["w"]
        return None

    async def send_week(self, cid: str, manual: bool = False) -> None:
        prefs = self.store.prefs(cid)
        today = config.now_in(config.tz_of(prefs["tz"])).date()
        sprint = self.current_sprint(cid, today) if manual else None
        b = await self._bundle(cid, date.fromisoformat(sprint) if sprint else None)
        b = self._ensure_sprint(cid, b)
        await self._advise(cid, b, "week")
        png, text, kb = charts.week_chart(b), reports.with_advice(reports.week_text(b), b), reports.week_keyboard(b)
        await self.broadcast(cid, lambda c: self.bot.send_photo(c, png, "📅 Неделя: факт против нормы"), "week")
        await self.broadcast(cid, lambda c: self.bot.send(c, text, kb), "week")
        await self.mail(cid, "week", self._mail_key(b), text, "/plan-vs-actual", png, "Неделя: факт против нормы")

    async def send_month(self, cid: str) -> None:
        b = await self._bundle(cid)
        b.tips = an.best_practices(b.fc, b.risk, b.week, b.month_last, b.pf_month, b.vel, b.today)
        await self._advise(cid, b, "month")
        png, text = charts.month_chart(b), reports.with_advice(reports.month_text(b), b)
        xls = excel.build_xlsx(b, "month")
        name = f"otchet_{b.month_last['start'].strftime('%Y-%m')}.xlsx"
        kb = [[{"text": "📎 Excel", "callback_data": "x:month"}] + reports.web_button("/dashboard")]
        await self.broadcast(cid, lambda c: self.bot.send_photo(c, png, f"🗓 {fmt.month_title(b.month_last['start'])}: потоки и структура расходов"), "month")
        await self.broadcast(cid, lambda c: self.bot.send(c, text, kb), "month")
        await self.broadcast(cid, lambda c: self.bot.send_document(c, xls, name, "📎 Подробный отчёт: диаграммы, план-факт и рекомендации"), "month", excel_only=True)
        await self.mail(cid, "month", self._mail_key(b), text, "/dashboard", png, "Потоки и структура расходов за месяц", xls, name)

    @staticmethod
    def _mail_key(b: Bundle) -> str:
        """Ключ письма отчёта: плановую отправку от повтора защищает расписание (claim), ручная «Отправить сейчас»
        допускает новое письмо раз в минуту."""
        return f"{b.today.isoformat()}:{int(time.time() // 60)}"

    async def send_excel(self, cid: str, chat_id: int, kind: str) -> None:
        prefs = self.store.prefs(cid)
        today = config.now_in(config.tz_of(prefs["tz"])).date()
        sprint = self.current_sprint(cid, today)
        b = await self._bundle(cid, date.fromisoformat(sprint) if sprint and kind == "week" else None, sprint)
        b.goals_now = self.store.goals(cid, sprint) if sprint else []
        await self._advise(cid, b, "week" if kind == "week" else kind)
        data = excel.build_xlsx(b, kind)
        await self.bot.send_document(chat_id, data, f"otchet_{b.today.isoformat()}.xlsx", "📎 Excel-отчёт: диаграммы, план-факт, цели и рекомендации")

    async def replan(self, cid: str, sprint: str) -> list[dict]:
        """После оценки целей прошлого спринта пересобираем автоплан текущего: так результаты влияют на следующую неделю."""
        self.store.x("DELETE FROM goals WHERE company_id=? AND week_start=? AND source='auto' AND status='open'", (cid, sprint))
        b = await self._bundle(cid, date.fromisoformat(sprint))
        have = {g["kind"] for g in self.store.goals(cid, sprint)}
        for g in an.propose_goals(b.fc, b.txs, b.pf, b.today, b.goals_prev):
            if g["kind"] not in have:
                self.store.add_goal(cid, sprint, g["kind"], g["title"], g["target_kop"], g["due"], "auto")
        return self.store.goals(cid, sprint)

    # ---------- расписание ----------
    @staticmethod
    def due(local: datetime, hhmm: str) -> bool:
        h, m = map(int, hhmm.split(":"))
        since = local.hour * 60 + local.minute - (h * 60 + m)
        return 0 <= since <= CATCH_UP_MIN

    async def tick(self) -> None:
        # компании с Telegram-чатами и компании, где кто-то получает рассылки письмом
        cids = list(dict.fromkeys(self.store.companies() + await self.up.mail_companies()))
        for cid in cids:
            try:
                await self.tick_company(cid)
            except Exception as e:  # одна компания не должна ронять рассылку остальным
                log.warning("tick %s: %s", cid, type(e).__name__)

    async def tick_company(self, cid: str) -> None:
        if await self.banned(cid):
            return
        p = self.store.prefs(cid)
        local = config.now_in(config.tz_of(p["tz"]))
        day_key = local.date().isoformat()

        async def once(kind: str, key: str, fn):
            if not self.store.claim(cid, kind, key):
                return
            try:
                await fn()
            except Exception as e:
                self.store.release(cid, kind, key)  # повторим на следующем тике
                log.warning("%s %s: %s", kind, cid, type(e).__name__)

        if p["risk_enabled"] and local.weekday() == p["risk_dow"] and self.due(local, p["risk_time"]):
            await once("risk", day_key, lambda: self.send_risk(cid))
        if p["week_enabled"] and local.weekday() == p["week_dow"] and self.due(local, p["week_time"]):
            await once("week", day_key, lambda: self.send_week(cid))
        last = (local.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
        if p["month_enabled"] and local.day == min(p["month_day"], last.day) and self.due(local, p["month_time"]):
            await once("month", local.strftime("%Y-%m"), lambda: self.send_month(cid))
        if p["gap_enabled"] and local.hour in GAP_HOURS and time.time() - float(self.store.kv_get(cid, "gap_checked") or 0) >= GAP_CHECK_EVERY:
            self.store.kv_set(cid, "gap_checked", str(time.time()))
            await self.check_gap(cid)
        await self.retro_nudge(cid, local)

    async def retro_nudge(self, cid: str, local: datetime) -> None:
        """Через сутки после недельного отчёта напоминаем оценить цели, если они всё ещё без оценки."""
        p = self.store.prefs(cid)
        sprint = self.current_sprint(cid, local.date())
        if not sprint or sprint != (local.date() - timedelta(days=1)).isoformat() or not self.due(local, p["week_time"]):
            return
        _, prev = self.store.prev_goals(cid, sprint)
        if not any(g["status"] == "open" for g in prev) or not self.store.claim(cid, "nudge", sprint):
            return
        rows = [[{"text": f"{i} ✅", "callback_data": f"g:{g['id']}:done"}, {"text": f"{i} 🌓", "callback_data": f"g:{g['id']}:partial"},
                 {"text": f"{i} ❌", "callback_data": f"g:{g['id']}:failed"}, {"text": f"{i} ➖", "callback_data": f"g:{g['id']}:skipped"}]
                for i, g in enumerate(prev, 1) if g["status"] == "open"]
        lines = ["⏰ <b>Не забудьте оценить цели прошлой недели</b>", "От оценок зависит план, поэтому это занимает минуту:", ""]
        lines += [f"{i}. {fmt.esc(g['title'])}" for i, g in enumerate(prev, 1) if g["status"] == "open"]
        await self.broadcast(cid, lambda c: self.bot.send(c, "\n".join(lines), rows), "week")

    async def run_scheduler(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception as e:
                log.warning("scheduler: %s", type(e).__name__)
            await asyncio.sleep(60)
