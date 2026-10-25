//! Compare mode for `POST /api/runtime/handshake`: the family checks a proxied grant
//! against the request, the fence and a fresh capability computation, and reports a
//! mismatch by JSON pointer only.
//!
//! The proxied response is the native handshake's reply. Python and Rust sign with the
//! same `grant_signing_secret` over the same canonical payload, and the check skips
//! `postgres` on recompute, so a natively issued reply stands in for Python's.

mod common;

use anyhow::{Result, bail, ensure};
use axum::http::StatusCode;
use axum::http::request::Parts;
use common::{
    CONFIG_REVISION, EPOCH, SECRET, TestDb, call, daemon_home, database_case, family,
    handshake_request, json_body, vector_case,
};
use gobby_core::grant::presentation::sign_grant;
use gobby_core::grant::{GrantBundle, PrincipalKind, QdrantCapability, deployment_token};
use gobby_core::route_family::{ProxiedCheck, RouteFamily};
use gobby_runtime::RuntimeFamily;
use serde_json::{Value, json};
use uuid::Uuid;

/// Runs the family's compare check on a proxied response to `parts` and `request`.
async fn check(
    family: &RuntimeFamily,
    parts: &Parts,
    request: &[u8],
    status: StatusCode,
    response: &[u8],
) -> ProxiedCheck {
    <RuntimeFamily as RouteFamily<()>>::check_proxied(family, parts, request, status, response)
        .await
}

/// A published deployment, its family, and one proxied handshake exchange.
struct Exchange {
    family: RuntimeFamily,
    token: String,
    machine: Uuid,
    request: Value,
    response: Value,
    _home: tempfile::TempDir,
}

impl Exchange {
    async fn issue(db: &TestDb) -> Result<Self> {
        let (machine, project, session) = (Uuid::new_v4(), Uuid::new_v4(), Uuid::new_v4());
        let home = daemon_home(machine, &db.url)?;
        let token = deployment_token(home.path());
        db.seed_machine(machine).await?;
        db.seed_project(project).await?;
        db.seed_deployment(&token).await?;
        db.publish(&token, &vector_case("loopback_direct")?["values"])
            .await?;
        let family = family(&db.url, home.path())?;
        let request = json!({
            "machine_id": machine.to_string(),
            "project_id": project.to_string(),
            "session_id": session.to_string(),
        });
        let reply = call(&family, handshake_request(&machine.to_string(), &request)).await;
        ensure!(
            reply.status() == StatusCode::OK,
            "handshake status {}",
            reply.status()
        );
        let response = json_body(reply).await;
        Ok(Self {
            family,
            token,
            machine,
            request,
            response,
            _home: home,
        })
    }

    fn parts(&self, machine_header: &str) -> Parts {
        handshake_request(machine_header, &self.request)
            .into_parts()
            .0
    }

    /// Checks `response` as the proxied 200 reply to `request` from this machine.
    async fn check(&self, request: &[u8], response: &Value) -> Result<ProxiedCheck> {
        let parts = self.parts(&self.machine.to_string());
        Ok(check(
            &self.family,
            &parts,
            request,
            StatusCode::OK,
            &serde_json::to_vec(response)?,
        )
        .await)
    }

    async fn check_untouched(&self) -> Result<ProxiedCheck> {
        self.check(&serde_json::to_vec(&self.request)?, &self.response)
            .await
    }

    async fn check_response(&self, response: &Value) -> Result<ProxiedCheck> {
        self.check(&serde_json::to_vec(&self.request)?, response)
            .await
    }

    /// The reply with its grant edited and signed again with the deployment's secret.
    fn resigned(&self, edit: impl FnOnce(&mut GrantBundle) -> Result<()>) -> Result<Value> {
        let mut grant: GrantBundle = serde_json::from_value(self.response["grant"].clone())?;
        edit(&mut grant)?;
        let mut response = self.response.clone();
        response["grant"] = serde_json::to_value(sign_grant(&grant, SECRET.as_bytes())?)?;
        Ok(response)
    }
}

fn mismatch(pointer: &str) -> ProxiedCheck {
    ProxiedCheck::Mismatch(pointer.to_owned())
}

#[tokio::test]
async fn canonical_overlay_claims_verify() -> Result<()> {
    database_case(async |db| {
        let canonical = "3f2504e0-4f89-11d3-9a0c-0305e82c3301";
        let exchange = Exchange::issue(db).await?;
        // Both issuers put the canonical UUID in the signed grant, even when
        // the request uses another accepted spelling.
        let response = exchange.resigned(|grant| {
            grant.principal.code_overlay_project_id = Some(canonical.to_owned());
            Ok(())
        })?;
        for overlay in [
            "3F2504E0-4F89-11D3-9A0C-0305E82C3301",
            "{3F2504E0-4F89-11D3-9A0C-0305E82C3301}",
            "urn:uuid:3F2504E0-4F89-11D3-9A0C-0305E82C3301",
        ] {
            let mut request = exchange.request.clone();
            request["code_overlay_project_id"] = json!(overlay);
            assert_eq!(
                exchange
                    .check(&serde_json::to_vec(&request)?, &response)
                    .await?,
                ProxiedCheck::Verified
            );

            let mut different = request.clone();
            different["code_overlay_project_id"] = json!(Uuid::new_v4().to_string());
            assert_eq!(
                exchange
                    .check(&serde_json::to_vec(&different)?, &response)
                    .await?,
                mismatch("/grant/principal/code_overlay_project_id")
            );

            let mut invalid = request;
            invalid["code_overlay_project_id"] = json!("invalid-overlay");
            assert_eq!(
                exchange
                    .check(&serde_json::to_vec(&invalid)?, &response)
                    .await?,
                mismatch("/request")
            );
        }
        Ok(())
    })
    .await
}

#[tokio::test]
async fn python_issued_grant_verifies() -> Result<()> {
    database_case(async |db| {
        let exchange = Exchange::issue(db).await?;
        assert_eq!(exchange.check_untouched().await?, ProxiedCheck::Verified);

        // Only a 200 carries a grant to check.
        let parts = exchange.parts(&exchange.machine.to_string());
        let request = serde_json::to_vec(&exchange.request)?;
        let refused = json!({"code": "claims_mismatch", "error": "claims_mismatch"});
        assert_eq!(
            check(
                &exchange.family,
                &parts,
                &request,
                StatusCode::FORBIDDEN,
                &serde_json::to_vec(&refused)?,
            )
            .await,
            ProxiedCheck::Skipped
        );
        Ok(())
    })
    .await
}

#[tokio::test]
async fn mismatches_name_the_field_without_values() -> Result<()> {
    database_case(async |db| {
        let exchange = Exchange::issue(db).await?;
        assert_eq!(exchange.check_untouched().await?, ProxiedCheck::Verified);

        // A tampered capability, re-signed so only the recompute catches it.
        let tampered = exchange.resigned(|grant| {
            let QdrantCapability::Direct { url, .. } = &mut grant.capabilities.qdrant else {
                bail!("loopback_direct issues direct qdrant");
            };
            *url = "http://127.0.0.1:6334".to_owned();
            Ok(())
        })?;
        assert_eq!(
            exchange.check_response(&tampered).await?,
            mismatch("/grant/capabilities/qdrant/url")
        );

        // A grant signed for another fencing epoch fails the static checks.
        let stale = exchange.resigned(|grant| {
            grant.deployment.fencing_epoch = EPOCH + 1;
            Ok(())
        })?;
        assert_eq!(
            exchange.check_response(&stale).await?,
            mismatch("/grant/deployment/fencing_epoch")
        );

        // The envelope's epoch must equal the grant's.
        let mut envelope = exchange.response.clone();
        envelope["fencing_epoch"] = json!(EPOCH + 1);
        assert_eq!(
            exchange.check_response(&envelope).await?,
            mismatch("/fencing_epoch")
        );

        // A signature that does not verify under the deployment's secret.
        let mut forged = exchange.response.clone();
        forged["grant"]["signature"] = json!("0".repeat(64));
        assert_eq!(
            exchange.check_response(&forged).await?,
            mismatch("/grant/signature")
        );

        // A lifetime beyond the one-hour issuance window.
        let long_lived = exchange.resigned(|grant| {
            grant.expires_at = grant.issued_at + 3601;
            Ok(())
        })?;
        assert_eq!(
            exchange.check_response(&long_lived).await?,
            mismatch("/grant/expires_at")
        );

        // The principal must carry the request's session claim.
        let mut other_session = exchange.request.clone();
        other_session["session_id"] = json!(Uuid::new_v4().to_string());
        assert_eq!(
            exchange
                .check(&serde_json::to_vec(&other_session)?, &exchange.response)
                .await?,
            mismatch("/grant/principal/session_id")
        );

        // The machine claim comes from the forwarded machine header.
        let parts = exchange.parts(&Uuid::new_v4().to_string());
        assert_eq!(
            check(
                &exchange.family,
                &parts,
                &serde_json::to_vec(&exchange.request)?,
                StatusCode::OK,
                &serde_json::to_vec(&exchange.response)?,
            )
            .await,
            mismatch("/grant/principal/machine_id")
        );

        // A principal of another kind is not the interactive principal requested.
        let managed = exchange.resigned(|grant| {
            grant.principal.kind = PrincipalKind::AgentRun;
            Ok(())
        })?;
        assert_eq!(
            exchange.check_response(&managed).await?,
            mismatch("/grant/principal/kind")
        );

        // An unparseable request body cannot supply the claims.
        assert_eq!(
            exchange.check(b"{not json", &exchange.response).await?,
            mismatch("/request")
        );
        Ok(())
    })
    .await
}

#[tokio::test]
async fn moved_config_revision_skips() -> Result<()> {
    database_case(async |db| {
        let exchange = Exchange::issue(db).await?;
        assert_eq!(exchange.check_untouched().await?, ProxiedCheck::Verified);

        // An activation landed after issuance: the grant's revision can no longer be
        // recomputed, so the check skips instead of comparing another revision.
        db.set_revision(&exchange.token, CONFIG_REVISION + 1)
            .await?;
        assert_eq!(exchange.check_untouched().await?, ProxiedCheck::Skipped);
        Ok(())
    })
    .await
}
