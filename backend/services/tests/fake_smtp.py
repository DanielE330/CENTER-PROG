"""Маленький настоящий SMTP-сервер для тестов notify: EHLO, STARTTLS, AUTH PLAIN/LOGIN, implicit TLS (порт 465),
правила провайдеров (чужой From, отказ получателя, FQDN в EHLO, 4xx-ограничения). Только стандартная библиотека;
самоподписанный сертификат делает openssl. Слушает 127.0.0.1 на свободном порту."""

import base64
import shutil
import socket
import ssl
import subprocess
import threading
from dataclasses import dataclass, field


def make_cert(directory) -> tuple[str, str]:
    """Самоподписанный сертификат для localhost/127.0.0.1 -> (cert.pem, key.pem)."""
    if not shutil.which("openssl"):
        raise RuntimeError("openssl не найден")
    cert, key = str(directory / "cert.pem"), str(directory / "key.pem")
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2", "-subj", "/CN=localhost",
            "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1", "-keyout", key, "-out", cert,
        ],
        check=True,
        capture_output=True,
    )
    return cert, key


@dataclass
class Got:
    mail_from: str
    rcpts: list[str]
    data: bytes
    auth_user: str | None
    tls: bool
    helo: str


@dataclass
class FakeSmtp:
    mode: str = "plain"  # plain | starttls | ssl
    cert: str = ""
    key: str = ""
    user: str | None = None  # требовать вход этим логином
    password: str = "secret"
    only_sender: str | None = None  # 553/550: MAIL FROM только с этого адреса (как Яндекс, Mail.ru)
    reject_rcpt: dict = field(default_factory=dict)  # адрес -> (код, текст)
    data_reply: str | None = None  # например "554 5.7.1 spam"
    require_fqdn_helo: bool = False
    banner_delay: float = 0.0
    hang_after_connect: bool = False  # принять соединение и молчать (порт 465 при STARTTLS)
    smtputf8: bool = False
    max_per_conn: int = 0  # >0: после стольких писем 421
    auth_mechs: str = "PLAIN LOGIN"

    def __post_init__(self):
        self.messages: list[Got] = []
        self.helos: list[str] = []
        self.commands: list[str] = []
        self.connections = 0
        self._sock = socket.socket()
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(16)
        self.port = self._sock.getsockname()[1]
        self._stop = False
        self._ctx = None
        if self.mode in ("starttls", "ssl"):
            self._ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            self._ctx.load_cert_chain(self.cert, self.key)
        self._thread = threading.Thread(target=self._accept, daemon=True)
        self._thread.start()

    def close(self):
        self._stop = True
        try:
            self._sock.close()
        except OSError:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    def _accept(self):
        while not self._stop:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    # ---------- один клиент ----------
    def _serve(self, conn: socket.socket):
        self.connections += 1
        conn.settimeout(10)
        try:
            if self.hang_after_connect:
                threading.Event().wait(5)
                return
            tls = False
            if self.mode == "ssl":
                conn = self._ctx.wrap_socket(conn, server_side=True)
                tls = True
            self._session(conn, tls)
        except (OSError, ssl.SSLError):
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def _session(self, conn, tls: bool):
        f = conn.makefile("rwb", buffering=0)
        if self.banner_delay:
            threading.Event().wait(self.banner_delay)

        def send(line: str):
            f.write(line.encode() + b"\r\n")

        def readline() -> str:
            raw = f.readline(4096)
            if not raw:
                raise EOFError
            return raw.decode(errors="replace").rstrip("\r\n")

        send("220 fake.smtp ESMTP")
        helo, authed, mail_from, rcpts, count = "", None, None, [], 0
        try:
            while True:
                line = readline()
                self.commands.append(line.split(" ")[0].upper())
                cmd = line.upper()
                if cmd.startswith(("EHLO", "HELO")):
                    helo = line.split(" ", 1)[1].strip() if " " in line else ""
                    self.helos.append(helo)
                    if self.require_fqdn_helo and "." not in helo:
                        send("550 5.7.1 helo must be FQDN")
                        continue
                    caps = ["250-fake.smtp", "250-8BITMIME", "250-SIZE 20971520"]
                    if self.smtputf8:
                        caps.append("250-SMTPUTF8")
                    if self.mode == "starttls" and not tls:
                        caps.append("250-STARTTLS")
                    if self.user and (tls or self.mode == "plain"):
                        caps.append(f"250-AUTH {self.auth_mechs}")
                    caps[-1] = "250 " + caps[-1][4:]
                    f.write(("\r\n".join(caps) + "\r\n").encode())
                elif cmd == "STARTTLS":
                    if self.mode != "starttls" or tls:
                        send("502 5.5.1 not available")
                        continue
                    send("220 2.0.0 ready to start TLS")
                    conn = self._ctx.wrap_socket(conn, server_side=True)
                    f = conn.makefile("rwb", buffering=0)
                    tls, helo, authed, mail_from, rcpts = True, "", None, None, []
                elif cmd.startswith("AUTH"):
                    if not self.user or (self.mode == "starttls" and not tls):
                        send("530 5.7.0 Must issue a STARTTLS command first")
                        continue
                    parts = line.split()
                    mech = parts[1].upper() if len(parts) > 1 else ""
                    if mech == "PLAIN":
                        blob = parts[2] if len(parts) > 2 else (send("334 ") or readline())
                        try:
                            _, u, p = base64.b64decode(blob).decode().split("\0")
                        except Exception:  # noqa: BLE001
                            send("501 5.5.2 bad auth")
                            continue
                    elif mech == "LOGIN":
                        send("334 " + base64.b64encode(b"Username:").decode())
                        u = base64.b64decode(readline()).decode()
                        send("334 " + base64.b64encode(b"Password:").decode())
                        p = base64.b64decode(readline()).decode()
                    else:
                        send("504 5.5.4 mechanism not supported")
                        continue
                    if u == self.user and p == self.password:
                        authed = u
                        send("235 2.7.0 Authentication successful")
                    else:
                        send("535 5.7.8 Username and Password not accepted")
                elif cmd.startswith("MAIL FROM:"):
                    if self.user and not authed:
                        send("530 5.7.0 Authentication Required")
                        continue
                    addr = line[10:].strip().split(" ")[0].strip("<>")
                    if self.only_sender and addr.lower() != self.only_sender.lower():
                        send("553 5.7.1 Sender address rejected: not owned by user")
                        continue
                    if "SMTPUTF8" in cmd and not self.smtputf8:
                        send("555 5.6.7 SMTPUTF8 not supported")
                        continue
                    mail_from, rcpts = addr, []
                    send("250 2.1.0 OK")
                elif cmd.startswith("RCPT TO:"):
                    addr = line[8:].strip().split(" ")[0].strip("<>")
                    if addr in self.reject_rcpt:
                        code, text = self.reject_rcpt[addr]
                        send(f"{code} {text}")
                        continue
                    rcpts.append(addr)
                    send("250 2.1.5 OK")
                elif cmd == "DATA":
                    if not rcpts:
                        send("554 5.5.1 no valid recipients")
                        continue
                    send("354 go ahead")
                    buf = []
                    while True:
                        ln = f.readline(65536)
                        if ln in (b".\r\n", b""):
                            break
                        buf.append(ln[1:] if ln.startswith(b"..") else ln)
                    if self.data_reply:
                        send(self.data_reply)
                        continue
                    self.messages.append(Got(mail_from or "", list(rcpts), b"".join(buf), authed, tls, helo))
                    count += 1
                    send("250 2.0.0 OK queued")
                    if self.max_per_conn and count >= self.max_per_conn:
                        send("421 4.7.0 too many messages, closing")
                        return
                elif cmd == "RSET":
                    mail_from, rcpts = None, []
                    send("250 OK")
                elif cmd == "NOOP":
                    send("250 OK")
                elif cmd == "QUIT":
                    send("221 bye")
                    return
                else:
                    send("500 5.5.1 unknown command")
        except EOFError:
            return
