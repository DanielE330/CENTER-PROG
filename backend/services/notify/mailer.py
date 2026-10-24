"""Отправка по SMTP (stdlib smtplib в отдельном потоке), разбор ошибок и фоновая обработка outbox с повторами.

Ошибки делятся на три рода (см. classify):
  * постоянные для письма (адрес не существует, письмо отклонено как спам) — сразу failed, повторять бесполезно;
  * сбои связи, TLS, входа, отправителя, 4xx-ограничения — письмо остаётся в очереди и повторяется с нарастающей
    паузой (store.RETRY_DELAYS), после последней попытки — failed; пока сервер недоступен, за один проход очереди
    пробуется только одно письмо, чтобы не долбить сервер и не получить блокировку за неверный пароль;
  * ошибка настроек (SMTP_*) — то же, плюс понятная подсказка в журнале (раз в 5 минут) и в /internal/v1/smtp/check.
В журнал попадают id письма, род ошибки и код ответа сервера; адресов, паролей и содержимого там нет."""

import asyncio
import os
import re
import smtplib
import socket
import ssl
import time
from collections import deque
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid, parseaddr
from urllib.parse import urlparse

from . import render, store

DEFAULT_PORTS = {"none": 1025, "starttls": 587, "ssl": 465}
DEFAULT_SENDER = "noreply@centr-prognoz.local"
SECURITY = ("none", "starttls", "ssl")
EMAIL_IN_TEXT = re.compile(r"[^\s<>\"',;()]+@[^\s<>\"',;()]+")


class ConfigError(Exception):
    """Настройки SMTP_* заданы неверно; текст понятен владельцу и не содержит секретов."""


def env(key: str) -> str:
    """Значение переменной; текст, начинающийся с «#», — это комментарий из .env, который Docker Compose принял за значение
    («SMTP_HOST=   # пусто — Mailpit» превращается в «# пусто — Mailpit»), и считается пустым."""
    value = os.environ.get(key, "").strip()
    return "" if value.startswith("#") else value


def number(key: str, default: float) -> float:
    raw = env(key)
    try:
        value = float(raw) if raw else default
    except ValueError:
        raise ConfigError(f"{key} должен быть числом, сейчас «{raw[:20]}»")
    if value < 0:
        raise ConfigError(f"{key} не может быть отрицательным")
    return value


def ascii_addr(addr: str) -> str:
    """Домен адреса в punycode (почта@пример.рф -> почта@xn--e1afmkfd.xn--p1ai): иначе нужен SMTPUTF8, который есть не везде."""
    local, sep, domain = addr.strip().rpartition("@")
    if not sep:
        return addr.strip()
    try:
        domain = domain.encode("idna").decode("ascii")
    except UnicodeError:
        pass
    return f"{local}@{domain}"


def helo_name(sender_addr: str) -> str:
    """Имя в EHLO. smtplib берёт socket.getfqdn(), а в контейнере это id контейнера без точки: Mail.ru и Postfix с
    reject_non_fqdn_helo_hostname отвечают отказом. Порядок: SMTP_HELO, хост из PUBLIC_URL, домен отправителя, localhost."""
    if env("SMTP_HELO"):
        return env("SMTP_HELO")
    for host in (urlparse(render.PUBLIC_URL).hostname or "", sender_addr.rpartition("@")[2]):
        host = host.lower().strip(".")
        if "." in host and not host.endswith((".local", ".localhost", ".invalid", ".test")) and not host.replace(".", "").isdigit():
            return host.encode("idna").decode("ascii")
    return "localhost"


def config() -> dict:
    """По умолчанию письма уходят в Mailpit (SMTP без TLS на mailpit:1025); реальный SMTP задаётся переменными SMTP_*.
    Понимает и прежние переменные notifier: SMTP_FROM, SMTP_SSL=1, SMTP_STARTTLS=1.
    Режим по умолчанию (SMTP_SECURITY пусто): порт 465 -> ssl, 587 -> starttls, реальный хост без порта -> starttls:587,
    иначе none. Отправитель по умолчанию — SMTP_USER (Gmail, Яндекс и Mail.ru не принимают чужой From)."""
    host = env("SMTP_HOST") or "mailpit"
    real = host != "mailpit"
    port_raw = env("SMTP_PORT")
    try:
        port = int(port_raw) if port_raw else 0
    except ValueError:
        raise ConfigError(f"SMTP_PORT должен быть числом, сейчас «{port_raw[:20]}»")
    if port_raw and not 0 < port < 65536:
        raise ConfigError("SMTP_PORT должен быть от 1 до 65535")
    security = env("SMTP_SECURITY").lower()
    if security and security not in SECURITY:
        raise ConfigError("SMTP_SECURITY должен быть none, starttls или ssl")
    if not security:
        if env("SMTP_SSL") in ("1", "true"):
            security = "ssl"
        elif env("SMTP_STARTTLS") in ("1", "true"):
            security = "starttls"
        elif port == 465:
            security = "ssl"
        elif port == 587 or (real and not port):
            security = "starttls"
        else:
            security = "none"
    user = env("SMTP_USER")
    password = os.environ.get("SMTP_PASSWORD", "")
    if user and not password:
        raise ConfigError("Задан SMTP_USER, но SMTP_PASSWORD пуст")
    if password and not user:
        raise ConfigError("Задан SMTP_PASSWORD, но SMTP_USER пуст")
    if not (user + password).isascii():
        raise ConfigError("SMTP_USER и SMTP_PASSWORD должны состоять из латиницы, цифр и знаков (ASCII): для паролей приложений это так")
    sender_raw = env("MAIL_FROM") or env("SMTP_FROM")
    if not sender_raw:
        sender_raw = user if "@" in user else DEFAULT_SENDER
    name, addr = parseaddr(sender_raw)
    if not re.match(r"^[^@\s<>]+@[^@\s<>]+$", addr):
        raise ConfigError("MAIL_FROM должен быть адресом вида noreply@example.ru или «Имя <noreply@example.ru>»")
    addr = ascii_addr(addr)
    return {
        "host": host,
        "port": port or DEFAULT_PORTS[security],
        "user": user,
        "password": password,
        "security": security,  # none | starttls | ssl
        "sender": formataddr((name or render.PRODUCT, addr)),  # имя кириллицей кодируется как =?utf-8?...?=
        "sender_addr": addr,
        "helo": helo_name(addr),
        "timeout": number("SMTP_TIMEOUT_SECONDS", 15.0) or 15.0,
        "tls_verify": env("SMTP_TLS_VERIFY") not in ("0", "false"),
        "ca_file": env("SMTP_CA_FILE"),
        "max_per_minute": int(number("SMTP_MAX_PER_MINUTE", 30)),
    }


def build(row, sender: str, attachments=()) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = ascii_addr(row["to_addr"])
    msg["Subject"] = row["subject"]
    msg["Date"] = formatdate(localtime=True)
    domain = parseaddr(sender)[1].rpartition("@")[2]
    msg["Message-ID"] = make_msgid(domain=domain or "localhost")
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


def tls_context(cfg: dict) -> ssl.SSLContext:
    ctx = ssl.create_default_context(cafile=cfg.get("ca_file") or None)
    if not cfg.get("tls_verify", True):
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def connect(cfg: dict, stage: list | None = None) -> smtplib.SMTP:
    """Соединение, EHLO, STARTTLS и вход. stage[0] показывает, на каком шаге остановились (для диагностики)."""

    def at(name: str) -> None:
        if stage is not None:
            stage[0] = name

    ctx = tls_context(cfg)
    at("connect")
    if cfg["security"] == "ssl":
        smtp = smtplib.SMTP_SSL(
            cfg["host"], cfg["port"], local_hostname=cfg["helo"], timeout=cfg["timeout"], context=ctx
        )
    else:
        smtp = smtplib.SMTP(cfg["host"], cfg["port"], local_hostname=cfg["helo"], timeout=cfg["timeout"])
    try:
        at("ehlo")
        smtp.ehlo()
        if cfg["security"] == "starttls":
            at("starttls")
            smtp.starttls(context=ctx)
            smtp.ehlo()
        if cfg["user"]:
            at("auth")
            smtp.login(cfg["user"], cfg["password"])
    except BaseException:
        try:
            smtp.close()
        except OSError:
            pass
        raise
    return smtp


def deliver(msg: EmailMessage, cfg: dict) -> None:
    smtp = connect(cfg)
    with smtp:
        smtp.send_message(msg, from_addr=cfg["sender_addr"])


# подменяется в тестах
send_impl = deliver


# ---------- разбор ошибок ----------
HOST_HINTS = (
    ("gmail.com", "Gmail: включите двухэтапную аутентификацию и создайте «пароль приложения» (16 символов); обычный пароль не подходит."),
    ("yandex.", "Яндекс: в настройках почты включите «Почтовые программы» и пароль приложения; адрес отправителя должен совпадать с SMTP_USER."),
    ("mail.ru", "Mail.ru: нужен «пароль для внешнего приложения» (Настройки -> Безопасность); отправитель должен совпадать с SMTP_USER."),
)


def host_hint(host: str) -> str:
    return next((h for key, h in HOST_HINTS if key in host.lower()), "")


def redact(text: str) -> str:
    return EMAIL_IN_TEXT.sub("<адрес>", text)


def reply_of(e: BaseException) -> tuple[int, str]:
    """(код, текст) ответа сервера без адресов; 0, если ответа нет."""
    if isinstance(e, smtplib.SMTPRecipientsRefused):
        code, text = next(iter(e.recipients.values()), (0, b""))
    elif isinstance(e, smtplib.SMTPResponseException):
        code, text = e.smtp_code, e.smtp_error
    else:
        return 0, ""
    if isinstance(text, bytes):
        text = text.decode(errors="replace")
    return int(code), redact(str(text))[:200]


def classify(e: BaseException, cfg: dict | None = None, stage: str = "") -> dict:
    """-> {cls, permanent, service, code, reply, hint}. permanent: повтор бесполезен, письмо сразу failed.
    service: сбой общий для всех писем (связь, TLS, вход, отправитель, настройки) — очередь на этом проходе останавливается."""
    cfg = cfg or {}
    code, reply = reply_of(e)
    port, sec = cfg.get("port"), cfg.get("security")
    extra = host_hint(cfg.get("host", ""))

    def out(cls, hint, permanent=False, service=False):
        return {"cls": cls, "permanent": permanent, "service": service, "code": code, "reply": reply, "hint": hint}

    if isinstance(e, ConfigError):
        return out("config", str(e), service=True)
    if isinstance(e, smtplib.SMTPAuthenticationError):
        return out("auth_failed", f"Сервер отклонил логин или пароль (SMTP_USER/SMTP_PASSWORD). {extra}".strip(), service=True)
    if isinstance(e, smtplib.SMTPRecipientsRefused):
        if code >= 500:
            return out("recipient_refused", "Адрес получателя отклонён сервером (нет такого ящика или он закрыт)", permanent=True)
        return out("rate_limited", "Сервер временно не принял получателя (серая очередь или лимит), повторим позже")
    if isinstance(e, smtplib.SMTPSenderRefused):
        if code >= 500:
            return out(
                "sender_refused",
                "Сервер отклонил адрес отправителя. Провайдеры принимают только свой адрес: задайте MAIL_FROM равным SMTP_USER "
                "(или не задавайте MAIL_FROM вовсе)",
                service=True,
            )
        return out("rate_limited", "Сервер временно отклонил отправителя (лимит), повторим позже", service=True)
    if isinstance(e, smtplib.SMTPNotSupportedError):
        if "internationalized" in str(e) or "SMTPUTF8" in str(e):
            return out("unicode_address", "Адрес с кириллицей до @ сервер не поддерживает (нет SMTPUTF8)", permanent=True)
        if "STARTTLS" in str(e):
            return out("starttls_unsupported", "Сервер не поддерживает STARTTLS на этом порту: попробуйте SMTP_SECURITY=ssl (порт 465) или none", service=True)
        return out("auth_unsupported", "Сервер не предлагает вход по логину и паролю на этом порту: проверьте SMTP_SECURITY и порт", service=True)
    if isinstance(e, smtplib.SMTPDataError):
        if code >= 500:
            return out("message_rejected", "Сервер отклонил само письмо (спам-фильтр или политика отправителя)", permanent=True)
        return out("rate_limited", "Сервер временно не принял письмо, повторим позже", service=True)
    if isinstance(e, smtplib.SMTPConnectError):
        return out("connect_refused", "Сервер отказал в соединении при приветствии: проверьте адрес и порт", service=True)
    if isinstance(e, smtplib.SMTPHeloError):
        return out("helo_refused", "Сервер отклонил EHLO: задайте SMTP_HELO — полное имя вашего хоста (mail.example.ru)", service=True)
    if isinstance(e, smtplib.SMTPResponseException):
        if 400 <= code < 500:
            return out("rate_limited", "Сервер временно отказал (4xx: лимит или перегрузка), повторим позже", service=True)
        return out("smtp_error", "Сервер ответил отказом", permanent=False, service=True)
    if isinstance(e, ssl.SSLCertVerificationError):
        return out(
            "tls_cert",
            "Сертификат сервера не прошёл проверку (самоподписанный, просрочен или имя не совпадает с SMTP_HOST). "
            "Для своего сервера укажите SMTP_CA_FILE с корневым сертификатом; в крайнем случае SMTP_TLS_VERIFY=0",
            service=True,
        )
    if isinstance(e, ssl.SSLError):
        if sec == "ssl":
            hint = "На этом порту нет TLS с самого начала. Порт 587 и 25 работают через STARTTLS: SMTP_SECURITY=starttls; порт 465 — ssl"
        else:
            hint = "Ошибка TLS при STARTTLS: проверьте порт и SMTP_SECURITY"
        return out("tls", hint, service=True)
    if isinstance(e, smtplib.SMTPServerDisconnected):
        if sec != "ssl" and port == 465:
            return out("tls_mismatch", "Порт 465 — это TLS с самого начала: задайте SMTP_SECURITY=ssl (не starttls и не none)", service=True)
        return out("disconnected", "Сервер закрыл соединение. Частые причины: неверный режим SMTP_SECURITY для порта, блокировка по IP или частые входы", service=True)
    if isinstance(e, socket.gaierror):
        return out("dns", "Имя SMTP_HOST не находится в DNS: проверьте написание хоста и выход контейнера в интернет (сеть mail)", service=True)
    if isinstance(e, (TimeoutError, socket.timeout)):
        if sec != "ssl" and port == 465:
            return out("tls_mismatch", "Порт 465 — это TLS с самого начала: задайте SMTP_SECURITY=ssl (не starttls и не none)", service=True)
        return out(
            "timeout",
            "Сервер не ответил за SMTP_TIMEOUT_SECONDS. Часто хостинг блокирует исходящие порты 25/465/587 или неверный порт/режим",
            service=True,
        )
    if isinstance(e, ConnectionRefusedError):
        return out("connect_refused", "Соединение отклонено: на этом хосте и порту SMTP-сервера нет (проверьте SMTP_HOST и SMTP_PORT)", service=True)
    if isinstance(e, OSError):
        return out("network", "Нет сетевого соединения с SMTP-сервером (маршрут, файрвол)", service=True)
    return out("internal", "Письмо не удалось собрать", permanent=True)


def describe(info: dict) -> str:
    """Строка для last_error и журнала: род ошибки, код и ответ сервера (адреса вычищены)."""
    return f"{info['cls']}" + (f" [{info['code']}]" if info["code"] else "") + (f": {info['reply']}" if info["reply"] else "")


# ---------- проверка без отправки письма ----------
def check(cfg: dict | None = None) -> dict:
    """Соединение, EHLO, STARTTLS, вход и проверка отправителя (MAIL FROM + RSET) без отправки письма.
    -> {ok, stage, error_class, hint, detail, host, port, security, sender, user_set}. Пароля в ответе нет."""
    stage = ["config"]
    info: dict = {"ok": True, "stage": "done", "error_class": None, "hint": "", "detail": ""}
    try:
        cfg = cfg or config()
        view = {k: cfg[k] for k in ("host", "port", "security")}
        view |= {"sender": cfg["sender_addr"], "user_set": bool(cfg["user"]), "helo": cfg["helo"]}
        hints = []
        if cfg["host"] != "mailpit" and cfg["sender_addr"] == DEFAULT_SENDER:
            hints.append("Отправитель по умолчанию (noreply@centr-prognoz.local) чужой серверу: задайте MAIL_FROM.")
        if cfg["user"] and cfg["user"].lower() != cfg["sender_addr"].lower() and "@" in cfg["user"]:
            hints.append("MAIL_FROM отличается от SMTP_USER: Gmail подменит адрес, Яндекс и Mail.ru отклонят письмо.")
        if cfg["user"] and cfg["security"] == "none" and cfg["host"] != "mailpit":
            hints.append("Пароль уходит без шифрования (SMTP_SECURITY=none).")
        if not cfg["tls_verify"]:
            hints.append("Проверка сертификата выключена (SMTP_TLS_VERIFY=0).")
        info["warnings"] = hints
        info.update(view)
        smtp = connect(cfg, stage)
        with smtp:
            stage[0] = "sender"
            code, reply = smtp.mail(cfg["sender_addr"])
            if code >= 400:
                raise smtplib.SMTPSenderRefused(code, reply, cfg["sender_addr"])
            smtp.rset()
    except Exception as e:  # noqa: BLE001
        c = classify(e, cfg if isinstance(cfg, dict) else {}, stage[0])
        info.update(ok=False, stage=stage[0], error_class=c["cls"], hint=c["hint"], detail=describe(c))
    return info


# ---------- очередь ----------
_busy = False  # идёт обход очереди: параллельный запуск отправил бы те же письма второй раз
_sent_at: deque = deque()  # время последних отправок для лимита в минуту
_last_config_log = 0.0


def log(msg: str) -> None:
    print(f"notify: {msg}", flush=True)  # без адресов, паролей и содержимого писем


async def flush(limit: int = 20) -> dict:
    """Один проход очереди: отправляет созревшие письма. Ошибка одного письма не мешает остальным; параллельные
    вызовы (письмо поставили в очередь во время обхода) пропускаются: письмо уйдёт на следующем проходе."""
    global _busy
    if _busy:
        return {"sent": 0, "failed": 0, "busy": True}
    _busy = True
    try:
        return await _flush(limit)
    finally:
        _busy = False


async def _flush(limit: int) -> dict:
    global _last_config_log
    sent = failed = 0
    rows = store.due(limit)
    if not rows:
        return {"sent": 0, "failed": 0}
    try:
        cfg = config()
    except ConfigError as e:
        if time.time() - _last_config_log > 300:
            _last_config_log = time.time()
            log(f"smtp config error: {e}")
        return {"sent": 0, "failed": 0, "config_error": True}
    for row in rows:
        now = time.monotonic()
        while _sent_at and _sent_at[0] < now - 60:
            _sent_at.popleft()
        if cfg["max_per_minute"] and len(_sent_at) >= cfg["max_per_minute"]:
            break  # лимит скорости: остальное уйдёт на следующих проходах
        try:
            msg = build(row, cfg["sender"], store.attachments_of(row["id"]))
            await asyncio.to_thread(send_impl, msg, cfg)
        except Exception as e:  # noqa: BLE001 любой сбой одного письма (в т.ч. не-SMTP) не должен ломать очередь
            info = classify(e, cfg)
            state = store.mark_failed(row["id"], describe(info), permanent=info["permanent"])
            log(f"message {row['id']} not sent ({info['cls']}{' ' + str(info['code']) if info['code'] else ''}), now {state}")
            failed += 1
            if info["service"]:
                break  # сервер недоступен для всех: не пробуем остальные письма на этом проходе
        else:
            _sent_at.append(time.monotonic())
            store.mark_sent(row["id"])
            sent += 1
    return {"sent": sent, "failed": failed}
