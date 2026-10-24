"""Events over NATS JetStream with TLS client certificates."""

import asyncio
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import nats
from nats.js import JetStreamContext
from nats.js.api import AckPolicy, ConsumerConfig, StreamConfig
from nats.js.errors import NotFoundError

from ci_common.tls import client_context

log = logging.getLogger(__name__)


def envelope(kind: str, tenant_id: uuid.UUID | str, data: Any) -> dict:
    return {
        "id": str(uuid.uuid7() if hasattr(uuid, "uuid7") else uuid.uuid4()),
        "type": kind,
        "schema": 1,
        "tenant_id": str(tenant_id),
        "occurred_at": datetime.now(UTC).isoformat(),
        "data": data,
    }


async def connect(url: str, cert_dir: Path) -> tuple[nats.NATS, JetStreamContext]:
    for attempt in range(30):
        try:
            nc = await nats.connect(url, tls=client_context(cert_dir), max_reconnect_attempts=-1)
            return nc, nc.jetstream()
        except Exception as exc:  # noqa: BLE001 - any connect failure means "retry later"
            log.warning("NATS not ready (attempt %s): %s", attempt, exc)
            await asyncio.sleep(2)
    raise RuntimeError("NATS unavailable")


async def ensure_stream(js: JetStreamContext, name: str, subjects: list[str]) -> None:
    try:
        await js.stream_info(name)
    except NotFoundError:
        await js.add_stream(StreamConfig(name=name, subjects=subjects, max_age=14 * 24 * 3600, duplicate_window=120))


async def publish(js: JetStreamContext, env: dict) -> None:
    await js.publish(f"ci.v1.{env['type']}", json.dumps(env, default=str).encode(), headers={"Nats-Msg-Id": env["id"]})


async def consume(
    js: JetStreamContext,
    stream: str,
    durable: str,
    subject: str,
    handler: Callable[[dict], Awaitable[None]],
) -> None:
    """Durable pull consumer; the handler must be idempotent (at-least-once delivery)."""
    # Events that failed five times land here instead of blocking the stream.
    await ensure_stream(js, "DLQ", ["ci.dlq.>"])
    while True:
        try:
            await js.stream_info(stream)
            break
        except NotFoundError:
            log.info("waiting for stream %s", stream)
            await asyncio.sleep(2)
    sub = await js.pull_subscribe(
        subject,
        durable=durable,
        stream=stream,
        config=ConsumerConfig(ack_policy=AckPolicy.EXPLICIT, max_deliver=5, ack_wait=120),
    )
    while True:
        try:
            msgs = await sub.fetch(10, timeout=5)
        except TimeoutError:
            continue
        except Exception as exc:  # noqa: BLE001 - keep the consumer alive across transient errors
            log.warning("fetch failed: %s", exc)
            await asyncio.sleep(2)
            continue
        for msg in msgs:
            try:
                await handler(json.loads(msg.data))
                await msg.ack()
            except Exception:  # noqa: BLE001 - one bad event must not stop the stream
                delivered = msg.metadata.num_delivered if msg.metadata else 1
                if delivered >= 5:
                    log.exception("giving up on event, moving to DLQ")
                    await js.publish(f"ci.dlq.{durable}", msg.data)
                    await msg.ack()
                else:
                    log.exception("handler failed, will retry")
                    await msg.nak(delay=5)
