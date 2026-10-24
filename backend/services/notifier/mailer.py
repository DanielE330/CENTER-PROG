"""Письма с приглашениями в команду (SMTP). Без SMTP_HOST и SMTP_FROM сервис письма не шлёт: ссылку отдаёт сайт."""
import asyncio
import logging
import re
import smtplib
import ssl
from email.message import EmailMessage

from . import config

log = logging.getLogger("notifier")
EMAIL_RE = re.compile(r"^[^@\s<>\"',;]+@[^@\s<>\"',;]+\.[^@\s<>\"',;]+$")
ROLES_RU = {"admin": "администратор", "editor": "аналитик", "viewer": "наблюдатель", "analyst": "аналитик", "observer": "наблюдатель"}


def configured() -> bool:
    return bool(config.env("SMTP_HOST") and config.env("SMTP_FROM"))


def _line(s: str, limit: int = 120) -> str:
    """Одна строка без управляющих символов: названия приходят от пользователей и не должны ломать заголовки письма."""
    return " ".join("".join(ch for ch in s if ch.isprintable()).split())[:limit]


def build_invite(to: str, code: str, company: str, role: str, inviter: str) -> EmailMessage:
    link = f"{config.public_url()}/?invite={code}"
    company, inviter = _line(company) or "компания", _line(inviter)
    msg = EmailMessage()
    msg["From"], msg["To"] = config.env("SMTP_FROM"), to
    msg["Subject"] = f"Приглашение в команду «{company}»"
    who = f"{inviter} приглашает" if inviter else "Вас приглашают"
    msg.set_content(
        f"{who} вас в сервис прогноза денежного потока: компания «{company}», роль — {ROLES_RU.get(role, role)}.\n\n"
        f"Перейдите по ссылке, войдите или зарегистрируйтесь с этим адресом почты и примите приглашение:\n{link}\n\n"
        "Ссылка действует 7 дней и сработает один раз только для этого адреса. Если приглашение вам не нужно, просто проигнорируйте письмо."
    )
    return msg


def _send(msg: EmailMessage) -> None:
    host, port = config.env("SMTP_HOST"), int(config.env("SMTP_PORT", "587"))
    user, pwd = config.env("SMTP_USER"), config.env("SMTP_PASSWORD")
    ctx = ssl.create_default_context()
    if config.env("SMTP_SSL") == "1":
        server = smtplib.SMTP_SSL(host, port, timeout=20, context=ctx)
    else:
        server = smtplib.SMTP(host, port, timeout=20)
    with server:
        if config.env("SMTP_SSL") != "1" and config.env("SMTP_STARTTLS", "1") == "1":
            server.starttls(context=ctx)
        if user:
            server.login(user, pwd)
        server.send_message(msg)


async def send_invite(to: str, code: str, company: str, role: str, inviter: str) -> bool:
    """True, если письмо принято SMTP-сервером. Пароль и тело письма (в нём ссылка с кодом) в логи не попадают."""
    if not configured() or not EMAIL_RE.match(to) or len(to) > 254:
        return False
    try:
        await asyncio.to_thread(_send, build_invite(to, code, company, role, inviter))
        return True
    except (OSError, smtplib.SMTPException, ValueError) as e:
        log.warning("письмо не отправлено: %s", type(e).__name__)
        return False
