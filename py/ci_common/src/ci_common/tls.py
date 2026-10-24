"""mTLS for Python services.

The server requires a client certificate from our CA and closes the connection
unless its SPIFFE ID is in the allow-list. The certificate is re-read in place
when the cert-agent renews it: `SSLContext.load_cert_chain` affects new handshakes.
"""

import logging
import os
import ssl
import threading
import time
from pathlib import Path

import httpx

SPIFFE_PREFIX = "spiffe://ci1/svc/"
log = logging.getLogger(__name__)


def paths(cert_dir: Path) -> tuple[Path, Path, Path]:
    return cert_dir / "cert.pem", cert_dir / "key.pem", cert_dir / "ca.pem"


def spiffe_service(peercert: dict | None) -> str | None:
    if not peercert:
        return None
    for kind, value in peercert.get("subjectAltName", ()):
        if kind == "URI" and value.startswith(SPIFFE_PREFIX):
            return value[len(SPIFFE_PREFIX) :]
    return None


def client_context(cert_dir: Path) -> ssl.SSLContext:
    cert, key, ca = paths(cert_dir)
    ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=str(ca))
    ctx.load_cert_chain(str(cert), str(key))
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx


class MtlsClient:
    """httpx client rebuilt when the certificate file changes."""

    def __init__(self, cert_dir: Path, timeout: float = 30.0):
        self._cert_dir = cert_dir
        self._timeout = timeout
        self._stamp = 0.0
        self._client: httpx.AsyncClient | None = None

    def get(self) -> httpx.AsyncClient:
        stamp = os.stat(paths(self._cert_dir)[0]).st_mtime
        if self._client is None or stamp != self._stamp:
            self._client = httpx.AsyncClient(verify=client_context(self._cert_dir), timeout=self._timeout)
            self._stamp = stamp
        return self._client


def watch_and_reload(ctx: ssl.SSLContext, cert_dir: Path, interval: float = 15.0) -> threading.Thread:
    cert, key, _ = paths(cert_dir)

    def loop() -> None:
        stamp = os.stat(cert).st_mtime
        while True:
            time.sleep(interval)
            try:
                now = os.stat(cert).st_mtime
                if now != stamp:
                    ctx.load_cert_chain(str(cert), str(key))
                    stamp = now
                    log.info("server certificate reloaded")
            except (OSError, ssl.SSLError) as exc:
                log.warning("certificate reload failed: %s", exc)

    thread = threading.Thread(target=loop, name="cert-reload", daemon=True)
    thread.start()
    return thread
