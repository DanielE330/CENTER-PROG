import asyncio
import logging
import os
import sys
import uuid

from ci_common import logs, server
from ci_common.config import Config, secret


async def seed() -> None:
    """One-shot: two demo organizations from the dataset, owners in Keycloak,
    history replayed up to the chosen "today"."""
    from connectors import app as c
    from connectors import dataset

    config = Config.from_env()
    await c.startup(config)
    log = logging.getLogger("seed")
    password = secret("demo_password")
    for attempt in range(60):
        try:
            await c.internal("GET", "tenancy", "/healthz")
            break
        except Exception:  # noqa: BLE001 - services are still starting
            log.info("waiting for services (attempt %s)", attempt)
            await asyncio.sleep(5)
    for i, demo in enumerate(dataset.pick_demo_companies(c.DATA_DIR, os.environ.get("DEMO_COMPANIES"))):
        alias = f"demo-{'abcdefgh'[i]}"
        created = await c.internal(
            "POST",
            "tenancy",
            "/internal/v1/organizations",
            {
                "name": demo["name"],
                "alias": alias,
                "owner_email": f"owner@{alias}.ci1.local",
                "owner_password": password,
                "as_of": str(demo["as_of"]),
                "demo_company": demo["company"],
            },
        )
        tenant = created["tenant_id"]
        result = await c.push_period(uuid.UUID(tenant), demo["company"], None, demo["as_of"])
        log.info("seeded %s (%s): %s operations, as_of %s", alias, demo["industry"], result["rows"], result["as_of"])


def main() -> None:
    config = Config.from_env()
    if "--healthcheck" in sys.argv:
        server.healthcheck(config)
    logs.init(config.service)
    if "--seed" in sys.argv:
        asyncio.run(seed())
        return
    from connectors.app import app

    server.run(app, config)


if __name__ == "__main__":
    main()
