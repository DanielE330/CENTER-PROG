"""Postgres over mTLS (role = certificate CN, no password) and transactions
pinned to one tenant for row-level security."""

import asyncio
import logging
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import psycopg
from psycopg_pool import AsyncConnectionPool

log = logging.getLogger(__name__)


def conninfo(database_url: str, cert_dir: Path) -> str:
    sep = "&" if "?" in database_url else "?"
    return (
        f"{database_url}{sep}sslmode=verify-full&sslrootcert={cert_dir / 'ca.pem'}"
        f"&sslcert={cert_dir / 'cert.pem'}&sslkey={cert_dir / 'key.pem'}"
    )


async def connect(database_url: str, cert_dir: Path) -> AsyncConnectionPool:
    pool = AsyncConnectionPool(
        conninfo(database_url, cert_dir), min_size=1, max_size=8, max_lifetime=1800, open=False, kwargs={}
    )
    for attempt in range(30):
        try:
            await pool.open(wait=True, timeout=10)
            return pool
        except (psycopg.OperationalError, TimeoutError, psycopg.pool.PoolTimeout) as exc:
            log.warning("database not ready (attempt %s): %s", attempt, exc)
            await asyncio.sleep(2)
    raise RuntimeError("database unavailable")


@asynccontextmanager
async def tenant_tx(pool: AsyncConnectionPool, tenant: uuid.UUID):
    async with pool.connection() as conn, conn.transaction():
        await conn.execute("select set_config('app.tenant_id', %s, true)", (str(tenant),))
        yield conn


async def migrate(pool: AsyncConnectionPool, directory: Path) -> None:
    """Applies `NNNN_*.sql` files in order, once each."""
    async with pool.connection() as conn:
        await conn.execute(
            "create table if not exists schema_migration (name text primary key, at timestamptz default now())"
        )
        done = {r[0] for r in await (await conn.execute("select name from schema_migration")).fetchall()}
        for path in sorted(directory.glob("*.sql")):
            if path.name in done:
                continue
            async with conn.transaction():
                await conn.execute(path.read_text())
                await conn.execute("insert into schema_migration (name) values (%s)", (path.name,))
            log.info("migration applied: %s", path.name)
