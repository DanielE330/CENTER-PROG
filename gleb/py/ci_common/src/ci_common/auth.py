"""Access-token checks against Keycloak's JWKS, as FastAPI dependencies."""

import time
import uuid
from dataclasses import dataclass

import jwt
from fastapi import HTTPException, Request

from ci_common.config import Config
from ci_common.tls import MtlsClient

JWKS_TTL = 300


@dataclass(frozen=True)
class User:
    sub: str
    tenant_id: uuid.UUID
    roles: tuple[str, ...]
    token: str

    @property
    def can_write(self) -> bool:
        return any(r in ("owner", "accountant") for r in self.roles)

    def require_write(self) -> None:
        if not self.can_write:
            raise HTTPException(403, "forbidden")


def tenant_from_claims(claims: dict) -> uuid.UUID | None:
    """Keycloak merges multivalued mappers: the claim can be `{"alias": {"id": ...}}` or
    `["alias", {"alias": {"id": ...}}]`. The first organization with an id wins."""
    orgs = claims.get("organization")
    candidates = orgs if isinstance(orgs, list) else [orgs]
    for item in candidates:
        if not isinstance(item, dict):
            continue
        for value in item.values():
            if isinstance(value, dict) and value.get("id"):
                try:
                    return uuid.UUID(value["id"])
                except ValueError:
                    return None
    return None


class Verifier:
    def __init__(self, config: Config, client: MtlsClient):
        self._config = config
        self._client = client
        self._keys: dict[str, object] = {}
        self._fetched = 0.0

    async def _key(self, kid: str) -> object:
        if kid not in self._keys or time.monotonic() - self._fetched > JWKS_TTL:
            resp = await self._client.get().get(self._config.oidc_jwks_url)
            resp.raise_for_status()
            self._keys = {
                k["kid"]: jwt.algorithms.RSAAlgorithm.from_jwk(k)
                for k in resp.json()["keys"]
                if k.get("kty") == "RSA" and k.get("use", "sig") == "sig"
            }
            self._fetched = time.monotonic()
        if kid not in self._keys:
            raise HTTPException(401, "unauthorized")
        return self._keys[kid]

    async def verify(self, token: str) -> dict:
        try:
            kid = jwt.get_unverified_header(token).get("kid", "")
            return jwt.decode(
                token,
                await self._key(kid),
                algorithms=["RS256"],
                audience=self._config.oidc_audience,
                issuer=self._config.oidc_issuer,
                leeway=30,
            )
        except jwt.PyJWTError:
            raise HTTPException(401, "unauthorized") from None

    async def user(self, request: Request) -> User:
        header = request.headers.get("authorization", "")
        if not header.startswith("Bearer "):
            raise HTTPException(401, "unauthorized")
        token = header[7:]
        claims = await self.verify(token)
        tenant = tenant_from_claims(claims)
        if tenant is None:
            raise HTTPException(403, "forbidden")
        roles = tuple(claims.get("realm_access", {}).get("roles", []))
        # Tokens the agent got through token exchange are read-only.
        if claims.get("azp") == "advisor-tools":
            roles = ("viewer",)
        return User(sub=claims.get("sub", ""), tenant_id=tenant, roles=roles, token=token)

    async def service(self, request: Request, allowed: set[str]) -> str:
        """Another service on an internal endpoint: client-credentials token of one of `allowed`."""
        header = request.headers.get("authorization", "")
        if not header.startswith("Bearer "):
            raise HTTPException(401, "unauthorized")
        claims = await self.verify(header[7:])
        caller = claims.get("azp")
        if caller not in allowed:
            raise HTTPException(403, "forbidden")
        return caller


class ServiceTokens:
    """Client credentials for this service; Keycloak authenticates it by certificate."""

    def __init__(self, config: Config, client: MtlsClient):
        self._config = config
        self._client = client
        self._token: str | None = None
        self._until = 0.0

    async def token(self) -> str:
        if self._token and time.monotonic() < self._until:
            return self._token
        resp = await self._client.get().post(
            self._config.token_url, data={"grant_type": "client_credentials", "client_id": self._config.service}
        )
        resp.raise_for_status()
        body = resp.json()
        self._token = body["access_token"]
        self._until = time.monotonic() + max(int(body.get("expires_in", 60)) - 30, 5)
        return self._token
