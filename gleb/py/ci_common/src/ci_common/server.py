"""Uvicorn with mandatory client certificates and a SPIFFE allow-list checked
right after the TLS handshake, before any HTTP is parsed."""

import asyncio
import logging
import ssl
import sys

import uvicorn
from fastapi import FastAPI
from uvicorn.protocols.http.h11_impl import H11Protocol

from ci_common import tls
from ci_common.config import Config

log = logging.getLogger(__name__)


def _protocol(allowed: frozenset[str]) -> type[H11Protocol]:
    class AllowListProtocol(H11Protocol):
        def connection_made(self, transport: asyncio.Transport) -> None:  # type: ignore[override]
            super().connection_made(transport)
            ssl_object = transport.get_extra_info("ssl_object")
            peer = tls.spiffe_service(ssl_object.getpeercert() if ssl_object else None)
            if peer not in allowed:
                log.warning("client certificate not in allow-list, closing")
                transport.close()

    return AllowListProtocol


def run(app: FastAPI, config: Config) -> None:
    cert, key, ca = tls.paths(config.cert_dir)
    uv_config = uvicorn.Config(
        app,
        host=config.host,
        port=config.port,
        ssl_certfile=str(cert),
        ssl_keyfile=str(key),
        ssl_ca_certs=str(ca),
        ssl_cert_reqs=ssl.CERT_REQUIRED,
        http=_protocol(config.allowed_clients),
        access_log=False,
        log_config=None,
        proxy_headers=False,
        server_header=False,
    )
    uv_config.load()
    if uv_config.ssl is not None:
        tls.watch_and_reload(uv_config.ssl, config.cert_dir)
    uvicorn.Server(uv_config).run()


def healthcheck(config: Config) -> None:
    """`python -m <svc> --healthcheck`: calls our own /healthz over mTLS."""
    import httpx

    try:
        resp = httpx.get(
            f"https://{config.service}:{config.port}/healthz", verify=tls.client_context(config.cert_dir), timeout=5
        )
        sys.exit(0 if resp.status_code == 200 else 1)
    except httpx.HTTPError:
        sys.exit(1)
