//! Server-side grant presentation: signing, header decoding, and the ordered static
//! checks `gdaemon` runs before a native route serves a runtime grant.

use serde_json::{Value, json};

use super::handshake::{b64url_decode, constant_time_eq, hmac_sha256};
use super::{
    EXPECTED_API_CONTRACT, GRANT_VERSION, GrantBundle, GrantError, canonical_payload_bytes,
    expected_schema_identity, hex_encode, payload_checksum,
};

/// Signs `grant` the way Python's `sign_grant` does: a SHA-256 checksum of the
/// canonical payload, then an HMAC-SHA256 over that payload keyed by `secret`.
pub fn sign_grant(grant: &GrantBundle, secret: &[u8]) -> Result<GrantBundle, GrantError> {
    let mut signed = grant.clone();
    signed.payload_checksum = payload_checksum(&signed);
    let payload = canonical_payload_bytes(&signed)?;
    signed.signature = hex_encode(&hmac_sha256(secret, &payload)?);
    Ok(signed)
}

/// Whether `grant.signature` is the HMAC of its canonical payload under `secret`.
pub fn signature_matches(grant: &GrantBundle, secret: &[u8]) -> bool {
    let Ok(payload) = canonical_payload_bytes(grant) else {
        return false;
    };
    let Ok(expected) = hmac_sha256(secret, &payload) else {
        return false;
    };
    constant_time_eq(grant.signature.as_bytes(), hex_encode(&expected).as_bytes())
}

/// Decodes a presented `X-Gobby-Runtime-Grant` value without checking it. A top-level
/// `managed_api_token` is a client-only envelope field: a string or null is dropped,
/// and any other type makes the header malformed.
pub fn decode_grant_header(value: &str) -> Result<GrantBundle, GrantError> {
    let raw = b64url_decode(value)?;
    let mut payload: Value =
        serde_json::from_slice(&raw).map_err(|error| GrantError::Malformed(error.to_string()))?;
    if let Some(object) = payload.as_object_mut()
        && let Some(token) = object.remove("managed_api_token")
        && !(token.is_string() || token.is_null())
    {
        return Err(GrantError::Malformed(
            "managed_api_token must be a string or null".to_owned(),
        ));
    }
    serde_json::from_value(payload).map_err(|error| GrantError::Malformed(error.to_string()))
}

/// A refusal of a presented grant, with the code Python's `grant_error_response` emits.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Rejection {
    InvalidSignature,
    /// The signature verifies, but the stored payload checksum does not match the payload.
    InvalidChecksum,
    WrongDeployment,
    WrongSchema,
    WrongApiContract,
    StaleEpoch,
    Expired,
    Revoked,
    WrongCapability,
    ClaimsMismatch,
    CredentialIssuanceFailed,
    CredentialBeforeProof,
}

impl Rejection {
    pub fn code(self) -> &'static str {
        match self {
            Self::InvalidSignature | Self::InvalidChecksum => "invalid_signature",
            Self::WrongDeployment => "wrong_deployment",
            Self::WrongSchema => "wrong_schema",
            Self::WrongApiContract => "wrong_api_contract",
            Self::StaleEpoch => "stale_epoch",
            Self::Expired => "expired",
            Self::Revoked => "revoked",
            Self::WrongCapability => "wrong_capability",
            Self::ClaimsMismatch => "claims_mismatch",
            Self::CredentialIssuanceFailed => "credential_issuance_failed",
            Self::CredentialBeforeProof => "credential_before_proof",
        }
    }

    pub fn status(self) -> u16 {
        match self {
            Self::StaleEpoch => 409,
            Self::Expired | Self::CredentialBeforeProof => 401,
            _ => 403,
        }
    }

    pub fn message(self) -> &'static str {
        match self {
            Self::InvalidSignature => "grant signature is invalid",
            Self::InvalidChecksum => "grant payload checksum is invalid",
            Self::WrongDeployment => "grant deployment token does not match",
            Self::WrongSchema => "grant schema identity does not match",
            Self::WrongApiContract => "grant API contract is not supported",
            Self::StaleEpoch => "grant fencing epoch is stale",
            Self::Expired => "grant has expired",
            Self::Revoked => "grant has been revoked",
            Self::WrongCapability => "grant lacks the required capability",
            Self::ClaimsMismatch => "claims mismatch",
            Self::CredentialIssuanceFailed => "credential issuance failed",
            Self::CredentialBeforeProof => "no credential attaches before challenge proof",
        }
    }

    pub fn body(self) -> Value {
        json!({"code": self.code(), "error": self.code(), "message": self.message()})
    }
}

/// The daemon-side facts a presented grant is checked against.
pub struct PresentationContext<'a> {
    pub token: &'a str,
    pub fencing_epoch: i64,
    pub signing_secret: &'a [u8],
}

/// Runs the static checks in Python's `GrantService.present` order: signature, checksum,
/// deployment token, schema identity, API contract and version, fencing epoch, expiry.
pub fn verify_static(
    grant: &GrantBundle,
    context: &PresentationContext<'_>,
    now: i64,
) -> Result<(), Rejection> {
    if !signature_matches(grant, context.signing_secret) {
        return Err(Rejection::InvalidSignature);
    }
    if !constant_time_eq(
        grant.payload_checksum.as_bytes(),
        payload_checksum(grant).as_bytes(),
    ) {
        return Err(Rejection::InvalidChecksum);
    }
    if grant.deployment.token != context.token {
        return Err(Rejection::WrongDeployment);
    }
    if grant.schema_identity != expected_schema_identity() {
        return Err(Rejection::WrongSchema);
    }
    if grant.api_contract != EXPECTED_API_CONTRACT || grant.version != GRANT_VERSION {
        return Err(Rejection::WrongApiContract);
    }
    if grant.deployment.fencing_epoch != context.fencing_epoch {
        return Err(Rejection::StaleEpoch);
    }
    if now >= grant.expires_at {
        return Err(Rejection::Expired);
    }
    Ok(())
}

#[cfg(test)]
mod tests;
