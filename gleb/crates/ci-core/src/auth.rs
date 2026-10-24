//! Access-token checks against Keycloak's JWKS. Each service verifies the token
//! itself and never trusts identity headers set by the gateway.

use std::{
    collections::HashMap,
    sync::Arc,
    time::{Duration, Instant},
};

use anyhow::{Context, Result, anyhow};
use axum::{
    extract::{FromRequestParts, OptionalFromRequestParts},
    http::request::Parts,
};
use jsonwebtoken::{Algorithm, DecodingKey, Validation, decode, decode_header};
use serde::Deserialize;
use tokio::sync::RwLock;
use uuid::Uuid;

use crate::{client::MtlsClient, error::ApiError, server::Peer};

const JWKS_TTL: Duration = Duration::from_secs(300);

#[derive(Deserialize)]
struct Jwks {
    keys: Vec<Jwk>,
}

#[derive(Deserialize)]
struct Jwk {
    kid: Option<String>,
    kty: String,
    #[serde(rename = "use")]
    usage: Option<String>,
    n: Option<String>,
    e: Option<String>,
}

#[derive(Debug, Deserialize)]
struct RealmAccess {
    #[serde(default)]
    roles: Vec<String>,
}

#[derive(Debug, Deserialize)]
pub struct Claims {
    pub sub: Option<String>,
    pub azp: Option<String>,
    #[serde(default)]
    pub scope: String,
    realm_access: Option<RealmAccess>,
    /// Keycloak organization mapper with "add organization id": `{"alias": {"id": "..."}}`.
    organization: Option<serde_json::Value>,
}

impl Claims {
    pub fn tenant_id(&self) -> Option<Uuid> {
        // Keycloak merges multivalued mappers: with the built-in `organization` scope the claim
        // becomes `["alias", {"alias": {"id": "..."}}]`. Accept the object form and that list.
        fn from_object(v: &serde_json::Value) -> Option<Uuid> {
            v.as_object()?
                .values()
                .find_map(|org| org.get("id").and_then(|id| id.as_str()))
                .and_then(|id| Uuid::parse_str(id).ok())
        }
        let claim = self.organization.as_ref()?;
        match claim {
            serde_json::Value::Array(items) => items.iter().find_map(from_object),
            other => from_object(other),
        }
    }

    pub fn roles(&self) -> Vec<String> {
        self.realm_access.as_ref().map(|r| r.roles.clone()).unwrap_or_default()
    }
}

pub struct JwtVerifier {
    issuer: String,
    audience: String,
    jwks_url: String,
    client: MtlsClient,
    keys: RwLock<(Instant, HashMap<String, DecodingKey>)>,
}

impl JwtVerifier {
    pub fn new(issuer: &str, audience: &str, jwks_url: &str, client: MtlsClient) -> Arc<Self> {
        Arc::new(Self {
            issuer: issuer.to_owned(),
            audience: audience.to_owned(),
            jwks_url: jwks_url.to_owned(),
            client,
            keys: RwLock::new((Instant::now() - JWKS_TTL * 2, HashMap::new())),
        })
    }

    async fn refresh(&self) -> Result<()> {
        let jwks: Jwks = self
            .client
            .get()
            .get(&self.jwks_url)
            .send()
            .await
            .context("fetching JWKS")?
            .error_for_status()?
            .json()
            .await?;
        let mut keys = HashMap::new();
        for key in jwks.keys {
            if key.kty != "RSA" || key.usage.as_deref().is_some_and(|u| u != "sig") {
                continue;
            }
            if let (Some(kid), Some(n), Some(e)) = (key.kid, key.n, key.e) {
                keys.insert(kid, DecodingKey::from_rsa_components(&n, &e)?);
            }
        }
        *self.keys.write().await = (Instant::now(), keys);
        Ok(())
    }

    async fn key(&self, kid: &str) -> Result<DecodingKey> {
        {
            let guard = self.keys.read().await;
            if guard.0.elapsed() < JWKS_TTL
                && let Some(k) = guard.1.get(kid)
            {
                return Ok(k.clone());
            }
        }
        self.refresh().await?;
        self.keys
            .read()
            .await
            .1
            .get(kid)
            .cloned()
            .ok_or_else(|| anyhow!("unknown signing key"))
    }

    pub async fn verify(&self, token: &str) -> Result<Claims> {
        let header = decode_header(token).context("token header")?;
        let kid = header.kid.ok_or_else(|| anyhow!("token without kid"))?;
        let key = self.key(&kid).await?;
        let mut validation = Validation::new(Algorithm::RS256);
        validation.set_issuer(&[&self.issuer]);
        validation.set_audience(&[&self.audience]);
        validation.leeway = 30;
        Ok(decode::<Claims>(token, &key, &validation)?.claims)
    }
}

fn bearer(parts: &Parts) -> Result<String, ApiError> {
    parts
        .headers
        .get(axum::http::header::AUTHORIZATION)
        .and_then(|v| v.to_str().ok())
        .and_then(|v| v.strip_prefix("Bearer "))
        .map(str::to_owned)
        .ok_or(ApiError::Unauthorized)
}

fn verifier(parts: &Parts) -> Result<Arc<JwtVerifier>, ApiError> {
    parts
        .extensions
        .get::<Arc<JwtVerifier>>()
        .cloned()
        .ok_or_else(|| ApiError::Internal(anyhow!("JwtVerifier extension missing")))
}

/// A user acting inside one organization (tenant).
#[derive(Clone, Debug)]
pub struct AuthUser {
    pub sub: String,
    pub tenant_id: Uuid,
    pub roles: Vec<String>,
    pub scopes: Vec<String>,
    /// Raw token, forwarded when this service calls another one on the user's behalf.
    pub token: String,
}

impl AuthUser {
    pub fn can_write(&self) -> bool {
        self.roles.iter().any(|r| r == "owner" || r == "accountant")
    }

    pub fn require_write(&self) -> Result<(), ApiError> {
        if self.can_write() {
            Ok(())
        } else {
            Err(ApiError::Forbidden)
        }
    }
}

impl<S: Send + Sync> FromRequestParts<S> for AuthUser {
    type Rejection = ApiError;

    async fn from_request_parts(parts: &mut Parts, _state: &S) -> Result<Self, Self::Rejection> {
        let token = bearer(parts)?;
        let claims = verifier(parts)?.verify(&token).await.map_err(|err| {
            tracing::info!(error = %err, "rejected token");
            ApiError::Unauthorized
        })?;
        let tenant_id = claims.tenant_id().ok_or(ApiError::Forbidden)?;
        // Tokens obtained by the agent through token exchange are read-only everywhere,
        // whatever roles the user has: the agent proposes, the user applies.
        let roles = if claims.azp.as_deref() == Some("advisor-tools") {
            vec!["viewer".to_owned()]
        } else {
            claims.roles()
        };
        Ok(Self {
            sub: claims.sub.clone().unwrap_or_default(),
            tenant_id,
            roles,
            scopes: claims.scope.split_whitespace().map(str::to_owned).collect(),
            token,
        })
    }
}

/// `Option<AuthUser>`: no Authorization header gives `None`; a present but invalid token is still rejected.
impl<S: Send + Sync> OptionalFromRequestParts<S> for AuthUser {
    type Rejection = ApiError;

    async fn from_request_parts(parts: &mut Parts, state: &S) -> Result<Option<Self>, Self::Rejection> {
        if !parts.headers.contains_key(axum::http::header::AUTHORIZATION) {
            return Ok(None);
        }
        <Self as FromRequestParts<S>>::from_request_parts(parts, state)
            .await
            .map(Some)
    }
}

/// Another service calling an internal endpoint: allowed mTLS peer plus a
/// Keycloak client-credentials token from the same client.
#[derive(Clone, Debug)]
pub struct ServiceCaller {
    pub service: String,
}

impl<S: Send + Sync> FromRequestParts<S> for ServiceCaller {
    type Rejection = ApiError;

    async fn from_request_parts(parts: &mut Parts, _state: &S) -> Result<Self, Self::Rejection> {
        let peer = parts.extensions.get::<Peer>().cloned().ok_or(ApiError::Unauthorized)?;
        let token = bearer(parts)?;
        let claims = verifier(parts)?
            .verify(&token)
            .await
            .map_err(|_| ApiError::Unauthorized)?;
        // The token must belong to the same service as the certificate.
        if claims.azp.as_deref() != Some(peer.0.as_str()) {
            return Err(ApiError::Forbidden);
        }
        Ok(Self { service: peer.0 })
    }
}

/// Client-credentials tokens for this service, cached until shortly before expiry.
pub struct ServiceTokenSource {
    token_url: String,
    client_id: String,
    client: MtlsClient,
    cached: RwLock<Option<(String, Instant)>>,
}

#[derive(Deserialize)]
struct TokenResponse {
    access_token: String,
    expires_in: u64,
}

impl ServiceTokenSource {
    pub fn new(token_url: &str, client_id: &str, client: MtlsClient) -> Arc<Self> {
        Arc::new(Self {
            token_url: token_url.to_owned(),
            client_id: client_id.to_owned(),
            client,
            cached: RwLock::new(None),
        })
    }

    /// Keycloak authenticates the client by its mTLS certificate (`tls_client_auth`).
    pub async fn token(&self) -> Result<String> {
        if let Some((token, until)) = self.cached.read().await.as_ref()
            && Instant::now() < *until
        {
            return Ok(token.clone());
        }
        let resp: TokenResponse = self
            .client
            .get()
            .post(&self.token_url)
            .form(&[
                ("grant_type", "client_credentials"),
                ("client_id", self.client_id.as_str()),
            ])
            .send()
            .await?
            .error_for_status()
            .context("client credentials")?
            .json()
            .await?;
        let until = Instant::now() + Duration::from_secs(resp.expires_in.saturating_sub(30));
        *self.cached.write().await = Some((resp.access_token.clone(), until));
        Ok(resp.access_token)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn tenant_id_is_read_from_organization_claim() {
        let claims: Claims = serde_json::from_value(serde_json::json!({
            "organization": {"demo-a": {"id": "6f1c1e4e-8d7a-4b0e-9a37-1f6c2b9d0a11"}},
            "realm_access": {"roles": ["owner"]}
        }))
        .unwrap();
        assert_eq!(
            claims.tenant_id().map(|t| t.to_string()).as_deref(),
            Some("6f1c1e4e-8d7a-4b0e-9a37-1f6c2b9d0a11")
        );
    }

    #[test]
    fn tenant_id_is_read_from_merged_keycloak_claim() {
        let claims: Claims = serde_json::from_value(serde_json::json!({
            "organization": ["demo-a", {"demo-a": {"id": "2b64b9ff-4562-4131-a164-0c59c67eb768"}}]
        }))
        .unwrap();
        assert_eq!(
            claims.tenant_id().map(|t| t.to_string()).as_deref(),
            Some("2b64b9ff-4562-4131-a164-0c59c67eb768")
        );
    }

    #[test]
    fn missing_organization_means_no_tenant() {
        let claims: Claims = serde_json::from_value(serde_json::json!({"sub": "x"})).unwrap();
        assert!(claims.tenant_id().is_none());
    }
}
