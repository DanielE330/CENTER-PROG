"""Защита от SSRF для адреса 1С, который вводит пользователь компании.

Integrator ходит по этому адресу из внутренней сети, поэтому пользователь не должен суметь направить его на
внутренние сервисы (ledger, auth, облачные метаданные 169.254.169.254) или на чужие порты. Правила:

- только `https` (`http` — лишь при `INTEGRATIONS_ALLOW_HTTP=1`, для разработки), без логина и пароля в адресе;
- порт 443 или 8443 (при `INTEGRATIONS_ALLOW_HTTP=1` — любой);
- имя хоста с точкой, без `localhost`, `.local`, `.internal` и т. п.; числовые формы IP (`2130706433`, `0x7f.1`) не допускаются;
- все адреса, в которые имя разрешилось в DNS, должны быть публичными: не loopback, не link-local, не частные
  сети (10/8, 172.16/12, 192.168/16, fc00::/7), не CGNAT, не multicast, не зарезервированные; IPv6, в который вложен
  IPv4 (::ffff:a.b.c.d, 6to4, NAT64), проверяется по вложенному адресу;
- проверка выполняется ещё раз при каждом соединении (`GuardedBackend`): подключение идёт именно к проверенному IP,
  поэтому подмена DNS между проверкой и запросом (DNS rebinding) не помогает;
- редиректы не выполняются (`LimitedTransport` отвечает ошибкой `redirect`): иначе 1С могла бы перенаправить на внутренний
  адрес, а httpx вместе с редиректом снова отправил бы логин и пароль;
- размер ответа (`INTEGRATIONS_MAX_BYTES`, 20 МБ) и общее время получения ответа (`INTEGRATIONS_MAX_SECONDS`, 60 с) ограничены.

`INTEGRATIONS_ALLOW_LOOPBACK=1` (только вместе с `INTEGRATIONS_ALLOW_HTTP=1`) разрешает одни loopback-адреса (127.0.0.0/8, ::1,
`localhost`): нужен лишь для проверки на локальном имитаторе, на сервере не включайте. Частные сети и link-local остаются закрытыми.
"""
import ipaddress
import os
import re
import socket
import time
from urllib.parse import urlsplit

import httpcore
import httpx

from .clients.base import SourceError

MAX_URL = 300
PORTS = {443, 8443}
BAD_SUFFIXES = (".localhost", ".local", ".localdomain", ".internal", ".intranet", ".lan", ".home", ".corp", ".docker", ".arpa")
LABEL = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")
NAT64 = ipaddress.ip_network("64:ff9b::/96")


class GuardError(SourceError):
    """Запрос к адресу 1С остановлен защитой; `code` — машинный код для интерфейса."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class ResponseTooLarge(GuardError):
    def __init__(self):
        super().__init__("too_large", "Ответ 1С слишком большой")


def allow_http() -> bool:
    return os.environ.get("INTEGRATIONS_ALLOW_HTTP") == "1"


def allow_loopback() -> bool:
    return allow_http() and os.environ.get("INTEGRATIONS_ALLOW_LOOPBACK") == "1"


def forbidden(why: str) -> GuardError:
    return GuardError("url_forbidden", why)


# ---------- адрес ----------
def is_public(ip: str) -> bool:
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if isinstance(a, ipaddress.IPv6Address):
        if a.ipv4_mapped:
            a = a.ipv4_mapped
        elif a.sixtofour:
            a = a.sixtofour
        elif a in NAT64:
            a = ipaddress.IPv4Address(int(a) & 0xFFFFFFFF)
        elif a.teredo:
            return False
    return a.is_global and not (a.is_multicast or a.is_reserved or a.is_loopback or a.is_link_local or a.is_unspecified)


def check_ip(ip: str) -> None:
    if allow_loopback():
        try:
            if ipaddress.ip_address(ip).is_loopback:
                return
        except ValueError:
            pass
    if not is_public(ip):
        raise forbidden("Адрес ведёт во внутреннюю или служебную сеть: так подключиться нельзя")


def normalize_url(raw: str) -> str:
    """Проверка формы адреса без обращения к сети; возвращает канонический вид (схема и хост в нижнем регистре, без `/` в конце)."""
    raw = (raw or "").strip()
    if not raw or len(raw) > MAX_URL or any(c.isspace() or ord(c) < 32 or c == "\\" for c in raw):
        raise GuardError("invalid_url", "Укажите адрес базы 1С ссылкой вида https://… (без пробелов, не длиннее 300 символов)")
    try:
        p = urlsplit(raw)
        port = p.port
    except ValueError:
        raise GuardError("invalid_url", "Адрес базы 1С записан неверно") from None
    scheme = p.scheme.lower()
    if scheme != "https" and not (scheme == "http" and allow_http()):
        raise GuardError("url_scheme", "Адрес должен начинаться с https://: по http пароль пошёл бы открытым текстом")
    if p.username is not None or p.password is not None or "@" in p.netloc:
        raise GuardError("url_userinfo", "Логин и пароль в адресе не указываются: введите их в отдельных полях")
    host = (p.hostname or "").rstrip(".").lower()
    if not host:
        raise GuardError("invalid_url", "В адресе нет имени сервера")
    port = port or (443 if scheme == "https" else 80)
    if port not in PORTS and not allow_http():
        raise forbidden("Порт адреса не разрешён: используйте 443 или 8443")
    if any(seg == ".." for seg in p.path.split("/")):
        raise GuardError("invalid_url", "Адрес записан неверно")
    try:
        ipaddress.ip_address(host)
        literal = True
    except ValueError:
        literal = False
    if literal:
        check_ip(host)
    else:
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError:
            raise GuardError("invalid_url", "Имя сервера в адресе записано неверно") from None
        labels = host.split(".")
        numeric_like = labels[-1].isdigit() or labels[-1].startswith("0x")
        bad_name = len(labels) < 2 or numeric_like or not all(LABEL.match(x) for x in labels) or host == "localhost" or host.endswith(BAD_SUFFIXES)
        if bad_name and not (allow_loopback() and host == "localhost"):
            raise forbidden("Имя сервера не подходит: нужен публичный адрес базы 1С")
    default = 443 if scheme == "https" else 80
    netloc = f"[{host}]" if ":" in host else host
    if port != default:
        netloc += f":{port}"
    return f"{scheme}://{netloc}{p.path.rstrip('/')}"


def origin(url: str) -> str:
    """Схема, хост и порт канонического адреса: пароль к другому серверу не переносится."""
    p = urlsplit(normalize_url(url))
    return f"{p.scheme}://{p.netloc}"


def resolve_public(host: str, port: int, resolver=None) -> list[str]:
    """IP-адреса хоста; если хоть один не публичный — отказ. Нет адресов — `unreachable`."""
    try:
        ipaddress.ip_address(host)
        ips = [host]
    except ValueError:
        try:
            infos = (resolver or socket.getaddrinfo)(host, port, type=socket.SOCK_STREAM)
        except OSError:
            raise GuardError("unreachable", "Сервер с таким именем не найден: проверьте адрес") from None
        ips = list(dict.fromkeys(i[4][0] for i in infos))
    if not ips:
        raise GuardError("unreachable", "Сервер с таким именем не найден: проверьте адрес")
    for ip in ips:
        check_ip(ip)
    return ips


def validate_url(raw: str, resolver=None) -> str:
    """Форма адреса + DNS: все адреса хоста публичные. Возвращает канонический адрес."""
    url = normalize_url(raw)
    p = urlsplit(url)
    resolve_public(p.hostname or "", p.port or (443 if p.scheme == "https" else 80), resolver)
    return url


# ---------- соединение ----------
class GuardedBackend(httpcore.NetworkBackend):
    """Сетевой слой httpcore: имя разрешается и проверяется здесь же, соединение идёт к проверенному IP (TLS и Host остаются по имени)."""

    def __init__(self, resolver=None, inner: httpcore.NetworkBackend | None = None):
        self.resolver = resolver
        self.inner = inner or httpcore.SyncBackend()

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        last: Exception | None = None
        for ip in resolve_public(host, port, self.resolver):
            try:
                return self.inner.connect_tcp(ip, port, timeout=timeout, local_address=local_address, socket_options=socket_options)
            except httpcore.ConnectError as e:
                last = e
        raise last or httpcore.ConnectError("no address")

    def connect_unix_socket(self, path, timeout=None, socket_options=None):
        raise forbidden("Unix-сокеты не поддерживаются")

    def sleep(self, seconds):
        self.inner.sleep(seconds)


def guarded_transport(resolver=None) -> httpx.HTTPTransport:
    t = httpx.HTTPTransport(retries=0)
    pool = t._pool  # httpx не даёт задать сетевой слой снаружи; тест test_guarded_transport_blocks_private_addresses следит за совместимостью
    if not hasattr(pool, "_network_backend"):
        raise RuntimeError("httpcore: неизвестное устройство пула, защита SSRF неприменима")
    pool._network_backend = GuardedBackend(resolver)
    return t


class _LimitedStream(httpx.SyncByteStream):
    def __init__(self, inner: httpx.SyncByteStream, max_bytes: int, started: float, max_seconds: float):
        self.inner, self.max_bytes, self.started, self.max_seconds, self.seen = inner, max_bytes, started, max_seconds, 0

    def __iter__(self):
        for chunk in self.inner:
            self.seen += len(chunk)
            if self.seen > self.max_bytes:
                raise ResponseTooLarge()
            if time.monotonic() - self.started > self.max_seconds:
                raise GuardError("timeout", "1С отвечает слишком долго")
            yield chunk

    def close(self):
        self.inner.close()


class LimitedTransport(httpx.BaseTransport):
    """Ограничения ответа 1С: без редиректов, размер и общее время. Оборачивает любой транспорт (в том числе тестовый)."""

    def __init__(self, inner: httpx.BaseTransport, max_bytes: int | None = None, max_seconds: float | None = None):
        self.inner = inner
        self.max_bytes = max_bytes or int(os.environ.get("INTEGRATIONS_MAX_BYTES", str(20 * 1024 * 1024)))
        self.max_seconds = max_seconds or float(os.environ.get("INTEGRATIONS_MAX_SECONDS", "60"))

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        started = time.monotonic()
        resp = self.inner.handle_request(request)
        if 300 <= resp.status_code < 400:
            resp.close()
            raise GuardError("redirect", "1С отвечает перенаправлением на другой адрес. Укажите конечный адрес приложения без перенаправлений")
        length = resp.headers.get("content-length", "")
        if length.isdigit() and int(length) > self.max_bytes:
            resp.close()
            raise ResponseTooLarge()
        resp.stream = _LimitedStream(resp.stream, self.max_bytes, started, self.max_seconds)
        return resp

    def close(self):
        self.inner.close()
