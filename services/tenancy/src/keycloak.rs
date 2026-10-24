//! Minimal Keycloak admin client: organizations, users, membership, roles.
//! Authenticates as the `tenancy` service account with its mTLS certificate.

use std::sync::Arc;

use anyhow::{Context, Result, anyhow};
use ci_core::{auth::ServiceTokenSource, client::MtlsClient};
use reqwest::StatusCode;
use serde_json::{Value, json};
use uuid::Uuid;

pub struct Admin {
    base: String,
    client: MtlsClient,
    tokens: Arc<ServiceTokenSource>,
}

impl Admin {
    pub fn new(internal_base: &str, realm: &str, client: MtlsClient, tokens: Arc<ServiceTokenSource>) -> Self {
        Self {
            base: format!("{internal_base}/admin/realms/{realm}"),
            client,
            tokens,
        }
    }

    async fn request(&self, method: reqwest::Method, path: &str) -> Result<reqwest::RequestBuilder> {
        let token = self.tokens.token().await?;
        Ok(self
            .client
            .get()
            .request(method, format!("{}{path}", self.base))
            .bearer_auth(token))
    }

    /// Keycloak's `search` matches names and domains, not aliases: list and filter.
    async fn find_organization(&self, alias: &str) -> Result<Option<Uuid>> {
        let orgs: Vec<Value> = self
            .request(reqwest::Method::GET, "/organizations")
            .await?
            .query(&[("first", "0"), ("max", "1000"), ("briefRepresentation", "true")])
            .send()
            .await?
            .error_for_status()?
            .json()
            .await?;
        Ok(orgs
            .iter()
            .find(|o| o["alias"] == alias)
            .and_then(|o| o["id"].as_str())
            .and_then(|id| Uuid::parse_str(id).ok()))
    }

    pub async fn ensure_organization(&self, name: &str, alias: &str) -> Result<Uuid> {
        if let Some(id) = self.find_organization(alias).await? {
            return Ok(id);
        }
        let resp = self
            .request(reqwest::Method::POST, "/organizations")
            .await?
            .json(&json!({
                "name": name,
                "alias": alias,
                "enabled": true,
                "domains": [{ "name": format!("{alias}.ci1.local"), "verified": false }]
            }))
            .send()
            .await?;
        if !resp.status().is_success() && resp.status() != StatusCode::CONFLICT {
            return Err(anyhow!("create organization: {}", resp.status()));
        }
        self.find_organization(alias)
            .await?
            .ok_or_else(|| anyhow!("organization not found after create"))
    }

    async fn find_user(&self, email: &str) -> Result<Option<String>> {
        let users: Vec<Value> = self
            .request(reqwest::Method::GET, "/users")
            .await?
            .query(&[("email", email), ("exact", "true")])
            .send()
            .await?
            .error_for_status()?
            .json()
            .await?;
        Ok(users.first().and_then(|u| u["id"].as_str()).map(str::to_owned))
    }

    /// Creates the owner if needed, adds them to the organization and grants `owner`.
    pub async fn ensure_owner(&self, email: &str, password: &str, org: Uuid) -> Result<String> {
        let user_id = match self.find_user(email).await? {
            Some(id) => id,
            None => {
                self.request(reqwest::Method::POST, "/users")
                    .await?
                    .json(&json!({
                        "username": email,
                        "email": email,
                        "enabled": true,
                        "emailVerified": true,
                        "firstName": "Demo",
                        "lastName": "Owner",
                        "credentials": [{ "type": "password", "value": password, "temporary": false }]
                    }))
                    .send()
                    .await?
                    .error_for_status()
                    .context("create user")?;
                self.find_user(email)
                    .await?
                    .ok_or_else(|| anyhow!("user not found after create"))?
            }
        };
        let resp = self
            .request(reqwest::Method::POST, &format!("/organizations/{org}/members"))
            .await?
            .json(&user_id)
            .send()
            .await?;
        if !resp.status().is_success() && resp.status() != StatusCode::CONFLICT {
            return Err(anyhow!("add member: {}", resp.status()));
        }
        let role: Value = self
            .request(reqwest::Method::GET, "/roles/owner")
            .await?
            .send()
            .await?
            .error_for_status()?
            .json()
            .await?;
        self.request(reqwest::Method::POST, &format!("/users/{user_id}/role-mappings/realm"))
            .await?
            .json(&json!([{ "id": role["id"], "name": "owner" }]))
            .send()
            .await?
            .error_for_status()
            .context("grant owner")?;
        Ok(user_id)
    }
}
