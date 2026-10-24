import os
from dataclasses import dataclass, field
from pathlib import Path


def secret(name: str, default: str | None = None) -> str:
    """Secrets are files under /run/secrets, never plain environment variables."""
    path = Path(os.environ.get("SECRETS_DIR", "/run/secrets")) / name
    try:
        return path.read_text().strip()
    except FileNotFoundError:
        if default is not None:
            return default
        raise RuntimeError(f"secret {name} is missing") from None


@dataclass(frozen=True)
class Config:
    service: str
    host: str
    port: int
    cert_dir: Path
    allowed_clients: frozenset[str]
    database_url: str | None
    nats_url: str | None
    oidc_issuer: str | None
    oidc_jwks_url: str | None
    oidc_audience: str
    keycloak_internal_url: str | None
    extra: dict[str, str] = field(default_factory=dict)

    @property
    def token_url(self) -> str:
        return f"{self.keycloak_internal_url}/realms/ci1/protocol/openid-connect/token"

    @staticmethod
    def from_env() -> "Config":
        service = os.environ["SERVICE_NAME"]
        listen = os.environ.get("LISTEN_ADDR", "0.0.0.0:8443")  # noqa: S104 - containers listen on all interfaces
        host, port = listen.rsplit(":", 1)
        allowed = {c.strip() for c in os.environ.get("ALLOWED_CLIENTS", "").split(",") if c.strip()}
        allowed.add(service)
        return Config(
            service=service,
            host=host,
            port=int(port),
            cert_dir=Path(os.environ.get("CERT_DIR", "/certs")),
            allowed_clients=frozenset(allowed),
            database_url=os.environ.get("DATABASE_URL"),
            nats_url=os.environ.get("NATS_URL"),
            oidc_issuer=os.environ.get("OIDC_ISSUER"),
            oidc_jwks_url=os.environ.get("OIDC_JWKS_URL"),
            oidc_audience=os.environ.get("OIDC_AUDIENCE", "ci1-api"),
            keycloak_internal_url=os.environ.get("KEYCLOAK_INTERNAL_URL"),
        )
