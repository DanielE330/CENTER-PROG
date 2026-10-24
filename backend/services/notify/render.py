"""Письма: темы, HTML (вёрстка таблицами, inline-CSS) и текстовая версия из шаблонов Jinja2. Только по-русски."""

import html as html_lib
import os
import re
from html.parser import HTMLParser
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape
from markupsafe import Markup, escape

PUBLIC_URL = os.environ.get("PUBLIC_URL", "http://localhost:3000").rstrip("/")
PRODUCT = "Центр Прогнозирования"
ROLES = {"director": "владелец", "admin": "администратор", "editor": "аналитик", "viewer": "наблюдатель"}

# kind -> тема письма; подставляются те же переменные, что и в шаблон
SUBJECTS = {
    "verify_email": "Подтвердите адрес почты",
    "reset_password": "Восстановление пароля",
    "invite": "Вас пригласили в «{company}»",
    "team_invite": "Приглашение в команду «{company}»",
    "member_reset": "Задайте новый пароль",
    "company_banned": "Доступ к «{company}» приостановлен",
    "test": "Тестовое письмо",
    "report": "{title}",
}
# рассылки (алерты и отчёты notifier): у них есть отписка и заголовок List-Unsubscribe
BULK = {"report"}

_env = Environment(
    loader=FileSystemLoader(Path(__file__).parent / "templates"),
    autoescape=select_autoescape(["html"]),
    undefined=StrictUndefined,
    trim_blocks=True,
    lstrip_blocks=True,
)


def link(path: str) -> str:
    return f"{PUBLIC_URL}{path}"


def one_line(s: str, limit: int = 200) -> str:
    """Одна строка без управляющих символов: названия приходят от пользователей и не должны ломать заголовки письма."""
    return " ".join("".join(ch for ch in str(s) if ch.isprintable()).split())[:limit]


class _TgHtml(HTMLParser):
    """Telegram-HTML (b, i, u, s, code, pre, a) -> безопасный HTML письма и текст. Остальные теги отбрасываются,
    текст экранируется, ссылки — только http(s)."""

    TAGS = {"b": "strong", "strong": "strong", "i": "em", "em": "em", "u": "u", "s": "s", "code": "code", "pre": "pre"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.html: list[str] = []
        self.text: list[str] = []
        self.open: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            href = dict(attrs).get("href") or ""
            if re.match(r"^https?://", href):
                self.html.append(f'<a href="{escape(href)}" style="color:#176b45">')
                self.open.append("a")
            return
        if tag in self.TAGS:
            self.html.append(f"<{self.TAGS[tag]}>")
            self.open.append(self.TAGS[tag])

    def handle_endtag(self, tag):
        want = "a" if tag == "a" else self.TAGS.get(tag)
        if want and want in self.open:
            while self.open:  # закрываем всё, что открыто внутри, чтобы разметка не «протекла»
                t = self.open.pop()
                self.html.append(f"</{t}>")
                if t == want:
                    break

    def handle_data(self, data):
        self.text.append(data)
        self.html.append(str(escape(data)).replace("\n", "<br>\n"))

    def result(self) -> tuple[Markup, str]:
        while self.open:
            self.html.append(f"</{self.open.pop()}>")
        return Markup("".join(self.html)), "".join(self.text)


def tg_to_mail(tg_html: str) -> tuple[Markup, str]:
    p = _TgHtml()
    p.feed(tg_html)
    p.close()
    return p.result()


def plain(tg_html: str) -> str:
    return html_lib.unescape(re.sub(r"<[^>]+>", "", tg_html))


def render(kind: str, data: dict, unsub_token: str | None = None) -> tuple[str, str, str, str]:
    """-> (subject, html, text, headers). Для рассылок добавляются ссылка отписки и List-Unsubscribe."""
    if kind not in SUBJECTS:
        raise KeyError(kind)
    ctx = {"product": PRODUCT, "public_url": PUBLIC_URL, "settings_url": link("/settings#notifications"), **data}
    ctx["unsubscribe_url"] = link(f"/unsubscribe?token={unsub_token}") if kind in BULK and unsub_token else None
    subject = one_line(SUBJECTS[kind].format(**{k: v for k, v in ctx.items() if isinstance(v, str)}))
    html = _env.get_template(f"{kind}.html").render(**ctx, subject=subject)
    text = _env.get_template(f"{kind}.txt").render(**ctx, subject=subject)
    headers = ""
    if ctx["unsubscribe_url"]:
        headers = f"List-Unsubscribe: <{ctx['unsubscribe_url']}>"
    return subject, html, text, headers
