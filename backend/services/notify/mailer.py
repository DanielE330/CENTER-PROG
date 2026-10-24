"""Отправка по SMTP (stdlib smtplib в отдельном потоке) и фоновая обработка outbox с повторами."""

import asyncio
import os
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

from . import store


DEFAULT_PORTS = {"none": 1025, "starttls": 587, "ssl": 465}


def env(key: str) -> str:
    return os.environ.get(key, "").strip()


def config() -> dict:
    """По умолчанию письма уходят в Mailpit (SMTP без TLS на mailpit:1025); реальный SMTP задаётся переменными SMTP_*.
    Понимает и прежние переменные notifier: SMTP_FROM, SMTP_SSL=1, SMTP_STARTTLS=1."""
    security = env("SMTP_SECURITY").lower()
    if not security:
        security = "ssl" if env("SMTP_SSL") == "1" else "starttls" if env("SMTP_STARTTLS") == "1" else "none"
    port = env("SMTP_PORT")
    return {
        "host": env("SMTP_HOST") or "mailpit",
        "port": int(port) if port else DEFAULT_PORTS.get(security, 1025),
        "user": env("SMTP_USER"),
        "password": os.environ.get("SMTP_PASSWORD", ""),
        "security": security,  # none | starttls | ssl
        "sender": env("MAIL_FROM") or env("SMTP_FROM") or "Центр Прогнозирования <noreply@centr-prognoz.local>",
        "timeout": float(env("SMTP_TIMEOUT_SECONDS") or "15"),
    }


def build(row, sender: str, attachments=()) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = row["to_addr"]
    msg["Subject"] = row["subject"]
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=sender.rsplit("@", 1)[-1].strip("> ") or "localhost")
    msg["Auto-Submitted"] = "auto-generated"
    for line in filter(None, (row["headers"] or "").splitlines()):
        name, _, value = line.partition(":")
        msg[name.strip()] = value.strip()
        if name.strip().lower() == "list-unsubscribe":
            msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    msg.set_content(row["text"])
    msg.add_alternative(row["html"], subtype="html")
    inline = [a for a in attachments if a["content_id"]]
    if inline:
        # картинки внутри письма (cid:) — в той же части multipart/related, что и HTML
        html_part = msg.get_payload()[1]
        for a in inline:
            maintype, _, subtype = a["mime"].partition("/")
            html_part.add_related(
                bytes(a["data"]), maintype, subtype, cid=f"<{a['content_id']}>", filename=a["filename"]
            )
    for a in attachments:
        if not a["content_id"]:
            maintype, _, subtype = a["mime"].partition("/")
            msg.add_attachment(bytes(a["data"]), maintype=maintype, subtype=subtype, filename=a["filename"])
    return msg


def deliver(msg: EmailMessage, cfg: dict) -> None:
    ctx = ssl.create_default_context()
    if cfg["security"] == "ssl":
        smtp = smtplib.SMTP_SSL(cfg["host"], cfg["port"], timeout=cfg["timeout"], context=ctx)
    else:
        smtp = smtplib.SMTP(cfg["host"], cfg["port"], timeout=cfg["timeout"])
    with smtp:
        if cfg["security"] == "starttls":
            smtp.starttls(context=ctx)
        if cfg["user"]:
            smtp.login(cfg["user"], cfg["password"])
        smtp.send_message(msg)


# подменяется в тестах
send_impl = deliver


async def flush(limit: int = 20) -> dict:
    """Одна итерация очереди: отправляет созревшие письма. Ошибка одного письма не мешает остальным."""
    cfg = config()
    sent = failed = 0
    for row in store.due(limit):
        try:
            await asyncio.to_thread(send_impl, build(row, cfg["sender"], store.attachments_of(row["id"])), cfg)
        except (OSError, smtplib.SMTPException) as e:
            # в журнал только класс ошибки и id письма, без адреса и содержимого
            state = store.mark_failed(row["id"], f"{type(e).__name__}: {e}")
            print(f"notify: message {row['id']} not sent ({type(e).__name__}), now {state}", flush=True)
            failed += 1
        else:
            store.mark_sent(row["id"])
            sent += 1
    return {"sent": sent, "failed": failed}
