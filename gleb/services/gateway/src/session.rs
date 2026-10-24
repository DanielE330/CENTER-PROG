//! Server-side sessions: tokens live in NATS KV, encrypted with AES-256-GCM.
//! The browser only holds a random session id; the KV key is its SHA-256.

use std::time::Duration;

use aes_gcm::{
    Aes256Gcm, KeyInit, Nonce,
    aead::{Aead, OsRng, rand_core::RngCore},
};
use anyhow::{Context, Result, anyhow};
use async_nats::jetstream::{self, kv};
use base64::{Engine, engine::general_purpose::URL_SAFE_NO_PAD};
use serde::{Deserialize, Serialize, de::DeserializeOwned};
use sha2::{Digest, Sha256};

pub const SESSION_TTL: Duration = Duration::from_secs(8 * 3600);
const LOGIN_TTL: Duration = Duration::from_secs(600);

#[derive(Clone, Serialize, Deserialize)]
pub struct Session {
    pub access_token: String,
    pub refresh_token: String,
    pub id_token: String,
    /// Unix seconds when the access token expires.
    pub access_exp: i64,
}

#[derive(Serialize, Deserialize)]
pub struct LoginState {
    pub verifier: String,
    pub nonce: String,
    pub return_to: String,
}

pub struct Store {
    sessions: kv::Store,
    logins: kv::Store,
    cipher: Aes256Gcm,
}

pub fn random_token(bytes: usize) -> String {
    let mut buf = vec![0u8; bytes];
    OsRng.fill_bytes(&mut buf);
    URL_SAFE_NO_PAD.encode(buf)
}

fn kv_key(id: &str) -> String {
    format!("{:x}", Sha256::digest(id.as_bytes()))
}

async fn bucket(js: &jetstream::Context, name: &str, ttl: Duration) -> Result<kv::Store> {
    match js.get_key_value(name).await {
        Ok(store) => Ok(store),
        Err(_) => js
            .create_key_value(kv::Config {
                bucket: name.to_owned(),
                max_age: ttl,
                history: 1,
                ..Default::default()
            })
            .await
            .map_err(|e| anyhow!("kv {name}: {e}")),
    }
}

impl Store {
    pub async fn new(js: &jetstream::Context, key_hex: &str) -> Result<Self> {
        let key = hex::decode(key_hex.trim()).context("session_key must be hex")?;
        if key.len() != 32 {
            return Err(anyhow!("session_key must be 32 bytes"));
        }
        Ok(Self {
            sessions: bucket(js, "bff_sessions", SESSION_TTL).await?,
            logins: bucket(js, "bff_logins", LOGIN_TTL).await?,
            cipher: Aes256Gcm::new_from_slice(&key).map_err(|e| anyhow!("{e}"))?,
        })
    }

    fn seal<T: Serialize>(&self, value: &T) -> Result<Vec<u8>> {
        let mut nonce = [0u8; 12];
        OsRng.fill_bytes(&mut nonce);
        let plain = serde_json::to_vec(value)?;
        let mut out = nonce.to_vec();
        out.extend(
            self.cipher
                .encrypt(Nonce::from_slice(&nonce), plain.as_slice())
                .map_err(|_| anyhow!("encrypt"))?,
        );
        Ok(out)
    }

    fn open<T: DeserializeOwned>(&self, sealed: &[u8]) -> Result<T> {
        if sealed.len() < 12 {
            return Err(anyhow!("sealed value too short"));
        }
        let (nonce, body) = sealed.split_at(12);
        let plain = self
            .cipher
            .decrypt(Nonce::from_slice(nonce), body)
            .map_err(|_| anyhow!("decrypt"))?;
        Ok(serde_json::from_slice(&plain)?)
    }

    pub async fn put_session(&self, id: &str, session: &Session) -> Result<()> {
        self.sessions
            .put(kv_key(id), self.seal(session)?.into())
            .await
            .map_err(|e| anyhow!("{e}"))?;
        Ok(())
    }

    pub async fn get_session(&self, id: &str) -> Result<Option<Session>> {
        match self.sessions.get(kv_key(id)).await.map_err(|e| anyhow!("{e}"))? {
            Some(bytes) => Ok(Some(self.open(&bytes)?)),
            None => Ok(None),
        }
    }

    pub async fn delete_session(&self, id: &str) -> Result<()> {
        self.sessions.purge(kv_key(id)).await.map_err(|e| anyhow!("{e}"))?;
        Ok(())
    }

    pub async fn put_login(&self, state: &str, login: &LoginState) -> Result<()> {
        self.logins
            .put(kv_key(state), self.seal(login)?.into())
            .await
            .map_err(|e| anyhow!("{e}"))?;
        Ok(())
    }

    /// One-time: the state is deleted as soon as it is read.
    pub async fn take_login(&self, state: &str) -> Result<Option<LoginState>> {
        let key = kv_key(state);
        let value = self.logins.get(&key).await.map_err(|e| anyhow!("{e}"))?;
        self.logins.purge(&key).await.map_err(|e| anyhow!("{e}"))?;
        value.map(|b| self.open(&b)).transpose()
    }
}
