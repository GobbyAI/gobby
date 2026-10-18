use std::path::PathBuf;

use serde_json::json;

use super::*;
use crate::grant::{EXPECTED_API_CONTRACT, GRANT_VERSION, expected_schema_identity};

const SECRET: &[u8] = b"presentation-test-secret";
/// Checksum and signature Python's `sign_grant` produces for the golden grant.
const PYTHON_CHECKSUM: &str = "78fff739ded921fbbd21a9c769a4bc60d75d0e8c6bea1559c2c60d786dc61715";
const PYTHON_SIGNATURE: &str = "3a3e0ce6f0c046c017779f6c01a02ac2780a2be0cb47784f3efc590a9b89de60";
const TOKEN: &str = "cafebabedeadbeef";
const EPOCH: i64 = 7;
const NOW: i64 = 1_700_000_100;

fn golden() -> GrantBundle {
    let path = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../../tests/runtime_grants/golden/direct_datastores.json");
    let raw = std::fs::read(path).expect("golden grant");
    serde_json::from_slice(raw.trim_ascii()).expect("parse golden grant")
}

fn context() -> PresentationContext<'static> {
    PresentationContext {
        token: TOKEN,
        fencing_epoch: EPOCH,
        signing_secret: SECRET,
    }
}

/// The golden grant, edited to be presentable at `NOW`, left with a stale signature.
fn edited(edit: impl FnOnce(&mut GrantBundle)) -> GrantBundle {
    let mut grant = golden();
    grant.deployment.token = TOKEN.into();
    grant.deployment.fencing_epoch = EPOCH;
    grant.schema_identity = expected_schema_identity();
    grant.api_contract = EXPECTED_API_CONTRACT;
    grant.version = GRANT_VERSION;
    grant.expires_at = NOW + 3_500;
    edit(&mut grant);
    grant
}

/// The edited grant, signed the way the daemon signs, so only the edit can fail.
fn resigned(edit: impl FnOnce(&mut GrantBundle)) -> GrantBundle {
    sign_grant(&edited(edit), SECRET).expect("sign")
}

#[test]
fn signature_matches_python_vector() {
    let grant = golden();
    let signed = sign_grant(&grant, SECRET).expect("sign");

    assert_eq!(signed.payload_checksum, PYTHON_CHECKSUM);
    assert_eq!(signed.signature, PYTHON_SIGNATURE);
    assert!(signature_matches(&signed, SECRET));
    assert!(!signature_matches(&signed, b"wrong-secret"));
    assert!(
        !signature_matches(&grant, SECRET),
        "the golden file is signed with another secret"
    );
}

#[test]
fn rejection_matrix_maps_codes_to_statuses() {
    let matrix = [
        (Rejection::InvalidSignature, "invalid_signature", 403),
        (Rejection::WrongDeployment, "wrong_deployment", 403),
        (Rejection::WrongSchema, "wrong_schema", 403),
        (Rejection::WrongApiContract, "wrong_api_contract", 403),
        (Rejection::StaleEpoch, "stale_epoch", 409),
        (Rejection::Expired, "expired", 401),
        (Rejection::Revoked, "revoked", 403),
        (Rejection::WrongCapability, "wrong_capability", 403),
        (Rejection::ClaimsMismatch, "claims_mismatch", 403),
        (
            Rejection::CredentialIssuanceFailed,
            "credential_issuance_failed",
            403,
        ),
        (Rejection::CredentialBeforeProof, "credential_before_proof", 401),
    ];
    for (rejection, code, status) in matrix {
        assert_eq!(rejection.code(), code);
        assert_eq!(rejection.status(), status, "{code}");
        assert!(!rejection.message().is_empty(), "{code}");
        assert_eq!(
            rejection.body(),
            json!({"code": code, "error": code, "message": rejection.message()}),
            "{code}"
        );
    }
}

#[test]
fn checks_run_in_python_order() {
    let context = context();
    assert_eq!(verify_static(&resigned(|_| {}), &context, NOW), Ok(()));

    let cases = [
        (
            "zeroed signature",
            edited(|grant| grant.signature = "00".repeat(32)),
            "invalid_signature",
        ),
        (
            "zeroed checksum under a valid signature",
            edited(|grant| grant.payload_checksum = "00".repeat(32)),
            "invalid_signature",
        ),
        (
            "signature before deployment",
            edited(|grant| {
                grant.deployment.token = "other-deployment".into();
                grant.signature = "00".repeat(32);
            }),
            "invalid_signature",
        ),
        (
            "deployment before schema",
            resigned(|grant| {
                grant.deployment.token = "other-deployment".into();
                grant.schema_identity.latest_version += 1;
            }),
            "wrong_deployment",
        ),
        (
            "schema before contract",
            resigned(|grant| {
                grant.schema_identity.latest_version += 1;
                grant.api_contract = 2;
            }),
            "wrong_schema",
        ),
        (
            "contract before epoch",
            resigned(|grant| {
                grant.api_contract = 2;
                grant.deployment.fencing_epoch = EPOCH - 1;
            }),
            "wrong_api_contract",
        ),
        (
            "grant version counts as contract",
            resigned(|grant| grant.version = GRANT_VERSION - 1),
            "wrong_api_contract",
        ),
        (
            "epoch before expiry",
            resigned(|grant| {
                grant.deployment.fencing_epoch = EPOCH - 1;
                grant.expires_at = NOW;
            }),
            "stale_epoch",
        ),
        (
            "expiry",
            resigned(|grant| grant.expires_at = NOW),
            "expired",
        ),
    ];
    for (name, grant, code) in cases {
        let rejection = verify_static(&grant, &context, NOW).expect_err(name);
        assert_eq!(rejection.code(), code, "{name}");
    }
}
