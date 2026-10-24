//! Domain types shared by the Rust services.
//!
//! Sensitive values (INN, payment purpose, counterparty name) mask themselves in
//! `Debug` and `Display`, so they cannot leak into logs through `?value` or `{}`.

use std::fmt;

use hmac::{Hmac, Mac};
use serde::{Deserialize, Serialize};
use sha2::Sha256;

#[derive(Debug, thiserror::Error, PartialEq, Eq)]
pub enum DomainError {
    #[error("invalid amount: {0}")]
    InvalidAmount(String),
    #[error("invalid INN")]
    InvalidInn,
}

/// Money in kopecks. Floats never touch stored amounts.
#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize, Default)]
#[serde(transparent)]
pub struct Kopecks(pub i64);

impl Kopecks {
    /// Parses "12345.67", "12345,67" or "12345" into kopecks without going through f64.
    pub fn parse(raw: &str) -> Result<Self, DomainError> {
        let s: String = raw.trim().chars().filter(|c| !c.is_whitespace()).collect();
        let s = s.replace(',', ".");
        let (neg, body) = match s.strip_prefix('-') {
            Some(rest) => (true, rest),
            None => (false, s.as_str()),
        };
        let mut parts = body.splitn(2, '.');
        let whole = parts.next().unwrap_or_default();
        let frac = parts.next().unwrap_or("");
        if whole.is_empty()
            || !whole.chars().all(|c| c.is_ascii_digit())
            || frac.len() > 2
            || !frac.chars().all(|c| c.is_ascii_digit())
        {
            return Err(DomainError::InvalidAmount(raw.chars().take(32).collect()));
        }
        let whole: i64 = whole
            .parse()
            .map_err(|_| DomainError::InvalidAmount(raw.chars().take(32).collect()))?;
        let frac: i64 = match frac.len() {
            0 => 0,
            1 => frac.parse::<i64>().unwrap_or(0) * 10,
            _ => frac.parse::<i64>().unwrap_or(0),
        };
        let value = whole
            .checked_mul(100)
            .and_then(|w| w.checked_add(frac))
            .ok_or_else(|| DomainError::InvalidAmount(raw.chars().take(32).collect()))?;
        Ok(Self(if neg { -value } else { value }))
    }
}

impl fmt::Display for Kopecks {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        let sign = if self.0 < 0 { "-" } else { "" };
        let abs = self.0.unsigned_abs();
        write!(f, "{sign}{}.{:02}", abs / 100, abs % 100)
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Direction {
    Inflow,
    Outflow,
}

/// Taxpayer number. Never printed in clear text.
#[derive(Clone, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(transparent)]
pub struct Inn(String);

impl Inn {
    /// Accepts 10 or 12 digits. Checksums are not enforced: the dataset uses fictitious INNs.
    pub fn parse(raw: &str) -> Result<Self, DomainError> {
        let s = raw.trim();
        if (s.len() == 10 || s.len() == 12) && s.chars().all(|c| c.is_ascii_digit()) {
            Ok(Self(s.to_owned()))
        } else {
            Err(DomainError::InvalidInn)
        }
    }

    pub fn expose(&self) -> &str {
        &self.0
    }

    /// Pseudonymous reference used outside the ingest service.
    pub fn counterparty_ref(&self, tenant: &str, key: &[u8]) -> String {
        counterparty_ref(tenant, &self.0, key)
    }
}

impl fmt::Debug for Inn {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("Inn(***)")
    }
}

impl fmt::Display for Inn {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("***")
    }
}

/// HMAC-SHA256 of tenant and identifier, truncated to 16 bytes, hex-encoded.
pub fn counterparty_ref(tenant: &str, identifier: &str, key: &[u8]) -> String {
    #[expect(clippy::expect_used, reason = "HMAC accepts keys of any length")]
    let mut mac = Hmac::<Sha256>::new_from_slice(key).expect("hmac key");
    mac.update(tenant.as_bytes());
    mac.update(b":");
    mac.update(identifier.as_bytes());
    let out = mac.finalize().into_bytes();
    format!("cp_{}", hex::encode(&out[..16]))
}

/// Free text that may contain personal or commercial data.
#[derive(Clone, PartialEq, Eq, Serialize, Deserialize, Default)]
#[serde(transparent)]
pub struct Sensitive(String);

impl Sensitive {
    pub fn new(value: impl Into<String>) -> Self {
        Self(value.into())
    }

    pub fn expose(&self) -> &str {
        &self.0
    }
}

impl fmt::Debug for Sensitive {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "Sensitive({} chars)", self.0.chars().count())
    }
}

impl fmt::Display for Sensitive {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("***")
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn kopecks_parse_handles_dot_comma_and_spaces() {
        assert_eq!(Kopecks::parse("12345.67"), Ok(Kopecks(1_234_567)));
        assert_eq!(Kopecks::parse("1 000,5"), Ok(Kopecks(100_050)));
        assert_eq!(Kopecks::parse("-990"), Ok(Kopecks(-99_000)));
    }

    #[test]
    fn kopecks_parse_rejects_garbage() {
        assert!(Kopecks::parse("12.345").is_err());
        assert!(Kopecks::parse("abc").is_err());
        assert!(Kopecks::parse("").is_err());
    }

    #[test]
    fn kopecks_display_round_trips() {
        assert_eq!(Kopecks(-5).to_string(), "-0.05");
        assert_eq!(Kopecks(1_234_567).to_string(), "12345.67");
    }

    #[test]
    fn inn_never_prints_digits() {
        let inn = Inn::parse("0098524470").unwrap_or_else(|_| unreachable!());
        assert_eq!(format!("{inn} {inn:?}"), "*** Inn(***)");
    }

    #[test]
    fn sensitive_debug_hides_text() {
        let s = Sensitive::new("Оплата по договору");
        assert!(!format!("{s:?}").contains("Оплата"));
    }

    #[test]
    fn counterparty_ref_depends_on_tenant() {
        let a = counterparty_ref("t1", "0098524470", b"k");
        let b = counterparty_ref("t2", "0098524470", b"k");
        assert_ne!(a, b);
        assert!(a.starts_with("cp_"));
    }
}
