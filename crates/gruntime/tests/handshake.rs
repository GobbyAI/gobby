//! Native `POST /api/runtime/handshake`: the operator handshake issues a signed
//! interactive grant, reuses the principal it already issued, and rejects with the
//! Python codes. Each case runs against an isolated schema rendered from the baseline.

mod common;

use anyhow::{Result, anyhow, bail, ensure};
use axum::body::Body;
use axum::http::header::{CACHE_CONTROL, COOKIE};
use axum::http::{Method, Request, Response, StatusCode};
use common::{
    CONFIG_REVISION, EPOCH, HANDSHAKE_PATH, SECRET, call, claims, daemon_home, database_case,
    family, handshake_request, json_body, pool, unix_now, vector_case,
};
use gobby_core::grant::presentation::{PresentationContext, verify_static};
use gobby_core::grant::{
    EXPECTED_API_CONTRACT, GRANT_VERSION, GrantBundle, GrantCapabilities, PostgresCapability,
    PrincipalKind, deployment_token, expected_schema_identity,
};
use gobby_runtime::secrets::SecretStore;
use serde_json::{Value, json};
use uuid::Uuid;

fn body(machine: Uuid, project: Uuid, session: Option<Uuid>) -> Value {
    json!({
        "machine_id": machine.to_string(),
        "project_id": project.to_string(),
        "session_id": session.map(|id| id.to_string()),
    })
}

async fn issue(
    family: &gobby_runtime::RuntimeFamily,
    machine: Uuid,
    project: Uuid,
) -> Result<GrantBundle> {
    let response = call(
        family,
        handshake_request(&machine.to_string(), &body(machine, project, None)),
    )
    .await;
    ensure!(
        response.status() == StatusCode::OK,
        "handshake status {}",
        response.status()
    );
    let issued = json_body(response).await;
    Ok(serde_json::from_value(issued["grant"].clone())?)
}

/// The interactive DSN, role, and generation the grant carries.
fn interactive(grant: &GrantBundle) -> Result<(String, String, i64)> {
    let PostgresCapability::Direct {
        dsn,
        role_name,
        credential_generation,
        ..
    } = &grant.capabilities.postgres
    else {
        bail!("interactive grants issue PostgresDirect");
    };
    Ok((dsn.clone(), role_name.clone(), *credential_generation))
}

fn password(dsn: &str) -> Result<String> {
    let config: tokio_postgres::Config = dsn.parse()?;
    Ok(String::from_utf8(
        config.get_password().unwrap_or_default().to_vec(),
    )?)
}

async fn rejected(response: Response<Body>) -> (StatusCode, Value) {
    (response.status(), json_body(response).await)
}

#[tokio::test]
async fn interactive_handshake_issues_signed_grant() -> Result<()> {
    database_case(async |db| {
        let (machine, project, session) = (Uuid::new_v4(), Uuid::new_v4(), Uuid::new_v4());
        let home = daemon_home(machine, &db.url)?;
        let token = deployment_token(home.path());
        db.seed_machine(machine).await?;
        db.seed_project(project).await?;
        db.seed_deployment(&token).await?;
        db.publish(&token, &vector_case("loopback_direct")?["values"])
            .await?;
        let family = family(&db.url, home.path())?;

        let before = unix_now();
        let response = call(
            &family,
            handshake_request(&machine.to_string(), &body(machine, project, Some(session))),
        )
        .await;
        assert_eq!(response.status(), StatusCode::OK);
        assert_eq!(response.headers()[CACHE_CONTROL], "no-store");
        let issued = json_body(response).await;
        assert_eq!(issued["deployment_token"], token);
        assert_eq!(issued["fencing_epoch"], EPOCH);

        let grant: GrantBundle = serde_json::from_value(issued["grant"].clone())?;
        let context = PresentationContext {
            token: &token,
            fencing_epoch: EPOCH,
            signing_secret: SECRET.as_bytes(),
        };
        verify_static(&grant, &context, unix_now())
            .map_err(|rejection| anyhow!("{rejection:?}"))?;
        assert_eq!(grant.version, GRANT_VERSION);
        assert_eq!(grant.api_contract, EXPECTED_API_CONTRACT);
        assert_eq!(grant.config_revision, CONFIG_REVISION);
        assert_eq!(grant.deployment.token, token);
        assert_eq!(grant.deployment.fencing_epoch, EPOCH);
        assert_eq!(grant.schema_identity, expected_schema_identity());
        assert_eq!(grant.principal.kind, PrincipalKind::Interactive);
        assert_eq!(grant.principal.machine_id, machine.to_string());
        assert_eq!(grant.principal.project_id, project.to_string());
        assert_eq!(grant.principal.execution_id, None);
        assert_eq!(grant.principal.session_id, Some(session.to_string()));
        assert_eq!(grant.principal.code_overlay_project_id, None);
        assert!((before..=unix_now()).contains(&grant.issued_at));

        let (dsn, role_name, generation) = interactive(&grant)?;
        let PostgresCapability::Direct { valid_until, .. } = grant.capabilities.postgres.clone()
        else {
            bail!("interactive grants issue PostgresDirect");
        };
        assert_eq!(grant.expires_at, (grant.issued_at + 3600).min(valid_until));
        let config: tokio_postgres::Config = dsn.parse()?;
        assert_eq!(config.get_user(), Some(role_name.as_str()));
        assert_eq!(config.get_dbname(), Some("gobby_test"));

        // The capabilities are Python's for the same published state, with the
        // issued Postgres grant substituted for the fixture's.
        let mut expected: GrantCapabilities =
            serde_json::from_value(vector_case("loopback_direct")?["expected"].clone())?;
        expected.postgres = grant.capabilities.postgres.clone();
        assert_eq!(grant.capabilities, expected);
        assert_eq!(generation, 1);
        Ok(())
    })
    .await
}

#[tokio::test]
async fn reused_principal_returns_the_sealed_password() -> Result<()> {
    database_case(async |db| {
        let (machine, project) = (Uuid::new_v4(), Uuid::new_v4());
        let home = daemon_home(machine, &db.url)?;
        let token = deployment_token(home.path());
        db.seed_machine(machine).await?;
        db.seed_project(project).await?;
        db.seed_deployment(&token).await?;
        db.publish(&token, &vector_case("loopback_direct")?["values"])
            .await?;
        let family = family(&db.url, home.path())?;

        let fresh = issue(&family, machine, project).await?;
        let reused = issue(&family, machine, project).await?;
        let (fresh_dsn, fresh_role, fresh_generation) = interactive(&fresh)?;
        let (reused_dsn, reused_role, reused_generation) = interactive(&reused)?;
        assert_eq!(reused_role, fresh_role);
        assert_eq!(reused_generation, fresh_generation);
        let sealed = password(&fresh_dsn)?;
        assert_eq!(password(&reused_dsn)?, sealed);

        // The fresh issuance stored material that the daemon's own secret store opens.
        let (ciphertext, aad) = db
            .stored_material(&token, machine, project, fresh_generation)
            .await?;
        assert_eq!(
            aad,
            format!("{token}:{machine}:{project}:{fresh_generation}")
        );
        let store = SecretStore::new(pool(&db.url)?, home.path().to_owned());
        let opened = store.open_sealed(&ciphertext, aad.as_bytes()).await?;
        assert_eq!(String::from_utf8(opened)?, sealed);
        Ok(())
    })
    .await
}

#[tokio::test]
async fn handshake_rejections_match_python_codes() -> Result<()> {
    database_case(async |db| {
        let (machine, project) = (Uuid::new_v4(), Uuid::new_v4());
        let home = daemon_home(machine, &db.url)?;
        let token = deployment_token(home.path());
        db.seed_machine(machine).await?;
        db.seed_project(project).await?;
        db.seed_deployment(&token).await?;
        db.publish(&token, &vector_case("loopback_direct")?["values"]).await?;
        let family = family(&db.url, home.path())?;

        let forwarded = Uuid::new_v4();
        let mismatched = handshake_request(&forwarded.to_string(), &body(machine, project, None));
        let (status, rejection) = rejected(call(&family, mismatched).await).await;
        assert_eq!(status, StatusCode::FORBIDDEN);
        assert_eq!(
            rejection,
            json!({
                "code": "claims_mismatch",
                "error": "claims_mismatch",
                "message": "operator machine_id must match the authenticated machine",
            })
        );

        let invalid = handshake_request(
            &machine.to_string(),
            &json!({"machine_id": machine.to_string(), "project_id": project.to_string(), "extra": 1}),
        );
        let (status, rejection) = rejected(call(&family, invalid).await).await;
        assert_eq!(status, StatusCode::UNPROCESSABLE_ENTITY);
        assert_eq!(rejection, json!({"detail": "invalid handshake request"}));

        let unseeded = Uuid::new_v4();
        let (status, rejection) = rejected(
            call(&family, claim_for(machine, unseeded)).await,
        )
        .await;
        assert_eq!(status, StatusCode::FORBIDDEN);
        assert_eq!(rejection["code"], "claims_mismatch");
        assert_eq!(rejection["message"], "project is not admitted");

        let retired = Uuid::new_v4();
        db.seed_project(retired).await?;
        db.retire_project(retired).await?;
        let (_, rejection) = rejected(call(&family, claim_for(machine, retired)).await).await;
        assert_eq!(rejection["message"], "project is not admitted");

        for (overlay, message) in [
            (project.to_string(), "code_overlay_project_id must differ from project_id"),
            ("not-a-uuid".to_owned(), "code_overlay_project_id must be a UUID"),
        ] {
            let mut overlay_body = body(machine, project, None);
            overlay_body["code_overlay_project_id"] = json!(overlay);
            let request = handshake_request(&machine.to_string(), &overlay_body);
            let (status, rejection) = rejected(call(&family, request).await).await;
            assert_eq!(status, StatusCode::FORBIDDEN);
            assert_eq!(rejection["code"], "claims_mismatch");
            assert_eq!(rejection["message"], message);
        }

        // An unregistered overlay is refused by the issuer; its SQLSTATE maps to the claim.
        let mut unregistered = body(machine, project, None);
        unregistered["code_overlay_project_id"] = json!(Uuid::new_v4().to_string());
        let (status, rejection) = rejected(
            call(&family, handshake_request(&machine.to_string(), &unregistered)).await,
        )
        .await;
        assert_eq!(status, StatusCode::FORBIDDEN);
        assert_eq!(rejection["code"], "claims_mismatch");

        db.unpublish(&token).await?;
        let (status, rejection) = rejected(call(&family, claim_for(machine, project)).await).await;
        assert_eq!(status, StatusCode::SERVICE_UNAVAILABLE);
        assert_eq!(rejection, json!({"detail": "handshake service unavailable"}));
        db.publish(&token, &vector_case("loopback_direct")?["values"]).await?;

        db.unseed_deployment(&token).await?;
        let (status, rejection) = rejected(call(&family, claim_for(machine, project)).await).await;
        assert_eq!(status, StatusCode::SERVICE_UNAVAILABLE);
        assert_eq!(rejection, json!({"detail": "grant service unavailable"}));
        db.seed_deployment(&token).await?;

        db.drop_credential_writer().await?;
        let (status, rejection) = rejected(call(&family, claim_for(machine, project)).await).await;
        assert_eq!(status, StatusCode::FORBIDDEN);
        assert_eq!(
            rejection,
            json!({
                "code": "credential_issuance_failed",
                "error": "credential_issuance_failed",
                "message": "credential issuance failed",
            })
        );
        Ok(())
    })
    .await
}

fn claim_for(machine: Uuid, project: Uuid) -> Request<Body> {
    handshake_request(&machine.to_string(), &body(machine, project, None))
}

#[tokio::test]
async fn callerless_handshakes_are_not_claimed() -> Result<()> {
    let home = tempfile::tempdir()?;
    let family = family(
        "postgresql://gobby_test:gobby_test@127.0.0.1:1/gobby_test",
        home.path(),
    )?;
    let identified = handshake_request(
        &Uuid::new_v4().to_string(),
        &body(Uuid::new_v4(), Uuid::new_v4(), None),
    );
    assert!(claims(&family, &identified.into_parts().0));

    let cookie_only = Request::builder()
        .method(Method::POST)
        .uri(HANDSHAKE_PATH)
        .header(COOKIE, "gobby_session=abc")
        .body(Body::empty())?;
    assert!(!claims(&family, &cookie_only.into_parts().0));

    let unauthenticated = Request::builder()
        .method(Method::POST)
        .uri(HANDSHAKE_PATH)
        .body(Body::empty())?;
    assert!(!claims(&family, &unauthenticated.into_parts().0));
    Ok(())
}
