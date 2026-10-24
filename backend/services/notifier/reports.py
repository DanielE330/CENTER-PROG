"""Тексты сообщений (Telegram HTML) и кнопки. Всё на русском."""
from datetime import date, timedelta

from . import analytics as an, config, fmt
from .fmt import esc, rub
from .service import Bundle

LVL = {"low": "🟢 Низкий", "medium": "🟡 Средний", "high": "🔴 Высокий"}
LVL_DOT = {"low": "🟢", "medium": "🟡", "high": "🔴"}
STATUS_ICON = {"open": "⬜", "done": "✅", "partial": "🌓", "failed": "❌", "skipped": "➖"}
STAGE_HEAD = {1: "🆘 <b>Кассовый разрыв завтра или уже сегодня</b>", 3: "🚨 <b>Кассовый разрыв через 3 дня</b>"}


def can_url_button() -> bool:
    u = config.public_url()
    return u.startswith("https://") and "localhost" not in u and "127.0.0.1" not in u


def web_button(path: str = "") -> list[dict]:
    """Telegram отклоняет http/localhost в кнопках-ссылках, поэтому кнопка есть только при публичном https-адресе."""
    return [{"text": "🌐 Открыть в сервисе", "url": config.public_url() + path}] if can_url_button() else []


def delta(cur: int, base: int) -> str:
    if not base:
        return ""
    p = round((cur / base - 1) * 100)
    return f" ({'+' if p > 0 else '−' if p < 0 else ''}{abs(p)}% к средней)"


def gap_alert_text(company: str, a: dict, days_ahead: int) -> str:
    head = STAGE_HEAD.get(a["stage"]) or f"⚠️ <b>Кассовый разрыв через {fmt.days_word(a['days_left'])}</b>"
    when = "сегодня" if a["days_left"] <= 0 else fmt.day_wd(a["date"])
    lines = [head, esc(company), "",
             f"📆 Ожидаемая дата: <b>{when}</b>", f"🎲 Вероятность: <b>{fmt.pct(a['prob'])}</b>",
             f"💸 Дефицит: <b>{rub(a['deficit'])}</b> (в худшем случае до {rub(a['deficit_p90'])})"]
    if a["reasons"]:
        lines += ["", "<b>Из-за чего:</b>"] + [f"• {fmt.day(r['date'])} — {esc(r['title'])}: {rub(r['amount'])}" for r in a["reasons"]]
    lines += ["", "<b>Что сделать сейчас:</b>",
              "• договориться об отсрочке крупного платежа;", "• ускорить оплату от клиентов (предоплата, скидка за срочность);",
              "• подготовить овердрафт или краткосрочный заём, пока остаток положительный."]
    return "\n".join(lines)


def gap_recovered_text(company: str) -> str:
    return f"✅ <b>Риск кассового разрыва снизился</b>\n{esc(company)}\n\nПрогноз больше не показывает разрыв в ближайшие дни. Продолжаем следить."


MISSED_SHOWN = 5


def missed_text(company: str, items: list[dict]) -> str:
    """Новые пропуски план-факта одним сообщением: до MISSED_SHOWN строк, остальные — числом."""
    lines = [f"🔔 <b>Пропущенные платежи: {len(items)}</b>", esc(company), ""]
    for i in items[:MISSED_SHOWN]:
        if i["plan"] > 0:
            lines.append(f"• Не поступила оплата: <b>{esc(i['title'])}</b> — {rub(i['plan'])}, ожидали {fmt.day(i['date'])}")
        else:
            lines.append(f"• Не прошло плановое списание: <b>{esc(i['title'])}</b> — {rub(-i['plan'])}, {fmt.day(i['date'])}")
    if len(items) > MISSED_SHOWN:
        lines.append(f"…и ещё {len(items) - MISSED_SHOWN}")
    lines += ["", "<b>Что сделать:</b>"]
    if any(i["plan"] > 0 for i in items):
        lines.append("• напомнить клиенту об оплате и договориться о новой дате;")
        lines.append("• посмотреть прогноз: пропущенные поступления уже учтены в нём как задержка оплаты.")
    if any(i["plan"] < 0 for i in items):
        lines.append("• проверить, не просрочен ли платёж: за просроченные налоги, аренду и кредит начисляют пени;")
        lines.append("• когда платёж пройдёт, остаток станет ниже — заложите эту сумму в план.")
    return "\n".join(lines)


def risk_text(b: Bundle) -> str:
    r = b.risk
    lines = [f"🛡 <b>Риск-менеджмент: {fmt.period(r.start, r.end)}</b>", esc(b.company), "",
             f"Уровень риска недели: <b>{LVL[r.level]}</b>",
             f"💰 Остаток по данным: {rub(b.fc.opening)} на {fmt.day(b.fc.as_of)}",
             f"📉 Минимум за неделю: {rub(r.min_p50)}" + (f" ({fmt.day_wd(r.min_p50_date)})" if r.min_p50_date else ""),
             f"⚠️ В худшем сценарии (p10): {rub(r.min_p10)}"]
    if r.items:
        lines += ["", "<b>Реестр рисков:</b>"]
        for i, it in enumerate(r.items[:6], 1):
            lines += [f"{i}. {LVL_DOT[it.level]} <b>{esc(it.title)}</b> — {esc(it.detail)}", f"   ↳ {esc(it.action)}"]
    else:
        lines += ["", "🟢 Существенных рисков на неделю не найдено."]
    if r.outflows:
        lines += ["", "<b>Крупнейшие списания недели:</b>"] + [f"• {fmt.day_wd(p['date'])} — {esc(p['title'])}: {rub(p['amount'])}" for p in r.outflows[:4]]
    if r.inflows:
        lines += ["", "<b>Ожидаемые поступления:</b>"] + [f"• {fmt.day_wd(p['date'])} — {esc(p['title'])}: {rub(p['amount'], True)}" for p in r.inflows[:4]]
    return "\n".join(lines)


def week_text(b: Bundle) -> str:
    w, ai_ae = b.week, b.week_avg
    lines = [f"📅 <b>Итоги недели {fmt.period(w['start'], w['end'])}</b>", esc(b.company), "",
             f"📥 Поступления: <b>{rub(w['income'])}</b>{delta(w['income'], ai_ae[0])}",
             f"📤 Расходы: <b>{rub(w['expense'])}</b>{delta(w['expense'], ai_ae[1])}",
             f"⚖️ Чистый поток: <b>{rub(w['net'], True)}</b>"]
    if w["closing"] is not None:
        lines.append(f"💰 Остаток на конец периода: {rub(w['closing'])}")
    if w["by_cat_out"]:
        top = list(w["by_cat_out"].items())[:3]
        lines.append("Главные расходы: " + ", ".join(f"{fmt.category(c).lower()} {rub(v)}" for c, v in top))
    if b.pf_week and b.pf_week["total"]:
        p = b.pf_week
        lines.append(f"📋 План-факт: оплачено {p['paid']}, с отклонением {p['deviation']}, не состоялось {p['missed']}")
    if b.goals_prev:
        lines += ["", "<b>Цели прошлой недели:</b>"]
        for i, g in enumerate(b.goals_prev, 1):
            lines.append(f"{i}. {STATUS_ICON[g['status']]} {esc(g['title'])}")
        if any(g["status"] == "open" for g in b.goals_prev):
            lines.append("❓ Отметьте результат кнопками ниже — от этого зависит план.")
        if b.vel is not None:
            lines.append(f"⚡ Выполнено целей: <b>{fmt.pct(b.vel)}</b>" + (" — берём меньше целей." if b.vel < 0.5 else ""))
    end = b.today + timedelta(days=6)
    lines += ["", f"🎯 <b>План на {fmt.period(b.today, end)}</b>"]
    if b.goals_now:
        lines += [f"{i}. {esc(g['title'])}" for i, g in enumerate(b.goals_now, 1)]
        lines.append("Свои цели: кнопка «➕ Цель» или команда /goal.")
    else:
        lines.append("Особых целей нет — держим текущий курс.")
    return "\n".join(lines)


def week_keyboard(b: Bundle) -> list[list[dict]]:
    rows: list[list[dict]] = []
    for i, g in enumerate(b.goals_prev, 1):
        if g["status"] == "open":
            rows.append([{"text": f"{i} ✅", "callback_data": f"g:{g['id']}:done"}, {"text": f"{i} 🌓", "callback_data": f"g:{g['id']}:partial"},
                         {"text": f"{i} ❌", "callback_data": f"g:{g['id']}:failed"}, {"text": f"{i} ➖", "callback_data": f"g:{g['id']}:skipped"}])
    if b.goals_prev_week:
        rows.append([{"text": "😀 Отлично", "callback_data": f"r:{b.goals_prev_week}:5"}, {"text": "🙂 Норм", "callback_data": f"r:{b.goals_prev_week}:4"},
                     {"text": "😐 Так себе", "callback_data": f"r:{b.goals_prev_week}:3"}, {"text": "😟 Плохо", "callback_data": f"r:{b.goals_prev_week}:2"}])
        rows.append([{"text": "✍️ Что помешало", "callback_data": f"b:{b.goals_prev_week}"}])
    rows.append([{"text": "➕ Цель", "callback_data": "ga"}, {"text": "📎 Excel", "callback_data": "x:week"}])
    if wb := web_button("/plan-vs-actual"):
        rows.append(wb)
    return rows


def month_text(b: Bundle) -> str:
    m, pm = b.month_last, b.month_prev
    ai, ae = b.month_avg3
    title = fmt.month_title(m["start"])
    lines = [f"🗓 <b>Итоги месяца: {title}</b>", esc(b.company), "",
             f"📥 Поступления: <b>{rub(m['income'])}</b>{_mdelta(m['income'], pm['income'])}",
             f"📤 Расходы: <b>{rub(m['expense'])}</b>{_mdelta(m['expense'], pm['expense'])}",
             f"⚖️ Чистый поток: <b>{rub(m['net'], True)}</b>"]
    if m["opening"] is not None and m["closing"] is not None:
        lines.append(f"💰 Остаток: {rub(m['opening'])} → {rub(m['closing'])}")
    if m["by_cat_out"]:
        lines += ["", "<b>Структура расходов:</b>"]
        tot = m["expense"] or 1
        lines += [f"• {fmt.category(c)}: {rub(v)} ({round(v / tot * 100)}%)" for c, v in list(m["by_cat_out"].items())[:5]]
    if b.pf_month and b.pf_month["total"]:
        p = b.pf_month
        lines += ["", f"📋 План-факт: выполнено {p['done']} из {p['total']}, не состоялось {p['missed']}"]
    start = b.today
    end = an.month_end(b.today)
    lines += ["", f"🎯 <b>План на {fmt.MONTHS_NOM[start.month - 1]}</b> ({fmt.period(start, end)})"]
    lines += [f"{i}. {t}" for i, t in enumerate(month_goals(b), 1)]
    obl = [p for p in b.fc.planned if start <= p["date"] <= end and p["amount"] < 0]
    if obl:
        lines += ["", "<b>Ключевые платежи месяца:</b>"] + [f"• {fmt.day(p['date'])} — {esc(p['title'])}: {rub(p['amount'])}" for p in sorted(obl, key=lambda p: p["amount"])[:4]]
    return "\n".join(lines)


def _mdelta(cur: int, prev: int) -> str:
    if not prev:
        return ""
    p = round((cur / prev - 1) * 100)
    return f" ({'+' if p > 0 else '−' if p < 0 else ''}{abs(p)}% к прошлому месяцу)"


def month_goals(b: Bundle) -> list[str]:
    ai, ae = b.month_avg3
    start, end = b.today, an.month_end(b.today)
    goals = []
    if ai:
        goals.append(f"Поступления не ниже {rub(ai)} (среднее за 3 месяца)")
    if ae:
        goals.append(f"Расходы не выше {rub(ae)} (среднее за 3 месяца)")
    win = [d for d in b.fc.days if start <= d.date <= end]
    if win:
        low = min(win, key=lambda d: d.p10)
        goals.append(f"Не допустить дефицита: минимальный остаток по прогнозу {rub(low.p50)} ({fmt.day(low.date)}), в худшем случае {rub(low.p10)}"
                     if low.p10 < 0 else f"Удерживать остаток не ниже {rub(max(low.p10, 0))} — прогнозный минимум месяца")
    tax = -sum(p["amount"] for p in b.fc.planned if start <= p["date"] <= end and p["amount"] < 0 and an.is_tax(p["title"]))
    if tax:
        goals.append(f"Обеспечить налоги и взносы {rub(tax)} в срок")
    return goals or ["Сохранять текущий курс и обновлять данные каждую неделю"]


def advice_lines(a: dict) -> list[str]:
    lines = ["", "🧠 <b>Консультант Hermes</b>", esc(a["summary"])]
    if a["priorities"]:
        lines += ["", "<b>Приоритеты:</b>"] + [f"{i}. {esc(x)}" for i, x in enumerate(a["priorities"], 1)]
    if a["actions"]:
        lines += ["", "<b>Что сделать на этой неделе:</b>"] + [f"• {esc(x)}" for x in a["actions"]]
    if a["consulting"]:
        lines += ["", f"💡 {esc(a['consulting'])}"]
    return lines


def with_advice(text: str, b: Bundle) -> str:
    return text + "\n" + "\n".join(advice_lines(b.advice)) if b.advice else text


def help_text() -> str:
    return ("<b>Что умеет бот</b>\n\n"
            "/report — свежий недельный отчёт и план\n/risk — риск-менеджмент недели\n/month — отчёт за месяц и план\n"
            "/goals — цели текущей недели\n/ask вопрос — спросить консультанта (в личном чате можно просто написать вопрос)\n/goal — добавить свою цель\n/excel — Excel-отчёт с диаграммами и рекомендациями\n"
            "/settings — расписание и пороги\n/unlink — отключить этот чат\n\n"
            "Алерт о кассовом разрыве приходит за 7, 3 и 1 день до даты разрыва.")
