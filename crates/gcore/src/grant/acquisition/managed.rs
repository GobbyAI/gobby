//! Managed grant acquisition and capability-authenticated renewal.
use super::super::bundle::validate_for_construction as validate_grant;
use super::super::cache::{CachePair, inspect_cache_pair, newer_generation};
use super::super::handshake::{challenge_and_handshake, parse_capability_token as parse_envelope};
use super::super::inspection::annotate_source;
use super::super::{
    AcquiredGrant, CapabilityClaims, GrantBundle, GrantError, GrantSource, load_grant_file,
};
use super::{
    AcquireCtx, fetch_settings_coherent, finish_loaded, finish_loaded_with_settings,
    refresh_or_fail, with_presentation_retry,
};
use std::path::{Path, PathBuf};

pub(super) fn acquire_managed(ctx: &AcquireCtx, path: &Path) -> Result<AcquiredGrant, GrantError> {
    let source_error =
        |error| annotate_source(error, &format!("managed grant file {}", path.display()));
    let (grant, settings, incoherent) = match inspect_cache_pair(path).map_err(source_error)? {
        Some(CachePair::Coherent(grant, settings)) => (grant, Some(settings), false),
        Some(CachePair::Incoherent(grant)) => (grant, None, true),
        Some(CachePair::GrantOnly(grant)) => (grant, None, false),
        None => (load_grant_file(path).map_err(source_error)?, None, false),
    };
    let validation = validate_grant(
        &grant,
        &ctx.project_id,
        &ctx.machine_id,
        Some(&grant.deployment.token),
        None,
        true,
    );
    validate_managed_identity(&grant, ctx.expected_execution_id.as_deref())?;
    let destination = path.to_path_buf();
    match validation {
        Err(GrantError::SchemaMismatch { .. }) if ctx.reachable() => {
            return refresh_or_fail(
                ctx,
                Some(&grant),
                GrantSource::ManagedFile,
                destination,
                true,
                true,
                None,
            );
        }
        result => result?,
    }
    // Use the exact grant validated above; a second read could replace its identity.
    match settings {
        Some(settings) => finish_loaded_with_settings(
            ctx,
            grant,
            settings,
            GrantSource::ManagedFile,
            destination,
            true,
        ),
        None if incoherent && ctx.reachable() => handshake_managed(ctx, Some(&grant)),
        _ => finish_loaded(ctx, grant, GrantSource::ManagedFile, destination, true),
    }
}

fn matches_managed_principal(claims: &CapabilityClaims, grant: &GrantBundle) -> bool {
    let expected_session = if grant.principal.kind == super::super::PrincipalKind::Maintenance {
        None
    } else {
        Some(claims.session_id.as_str())
    };
    claims.matches_principal(&grant.principal)
        && grant.principal.session_id.as_deref() == expected_session
}

pub(super) fn managed_envelope(
    ctx: &AcquireCtx,
    existing: Option<&GrantBundle>,
) -> Result<(String, CapabilityClaims), GrantError> {
    let owned = ctx
        .managed_envelope
        .clone()
        .ok_or(GrantError::ManagedCapabilityMissing)?;
    let envelope = owned.trim();
    if envelope.is_empty() {
        return Err(GrantError::ManagedCapabilityMissing);
    }
    let claims = parse_envelope(envelope)?;
    if claims.exp <= ctx.now {
        return Err(GrantError::Expired);
    }
    if let Some(existing) = existing
        && !matches_managed_principal(&claims, existing)
    {
        return Err(GrantError::Malformed(
            "envelope token principal mismatch".to_string(),
        ));
    }
    Ok((envelope.to_string(), claims))
}

pub(super) fn handshake_managed(
    ctx: &AcquireCtx,
    existing: Option<&GrantBundle>,
) -> Result<AcquiredGrant, GrantError> {
    with_presentation_retry(|| handshake_managed_once(ctx, existing))
}

pub(super) fn validate_managed_refresh(
    ctx: &AcquireCtx,
    grant: &GrantBundle,
    claims: &CapabilityClaims,
    existing: Option<&GrantBundle>,
) -> Result<(), GrantError> {
    let validation = validate_grant(
        grant,
        &ctx.project_id,
        &ctx.machine_id,
        existing.map(|grant| grant.deployment.token.as_str()),
        None,
        true,
    );
    if !matches_managed_principal(claims, grant) {
        return Err(GrantError::Malformed(
            "refreshed grant does not match capability principal".to_string(),
        ));
    }
    validate_managed_identity(grant, ctx.expected_execution_id.as_deref())?;
    validation
}

fn handshake_managed_once(
    ctx: &AcquireCtx,
    existing: Option<&GrantBundle>,
) -> Result<AcquiredGrant, GrantError> {
    let (envelope, claims) = managed_envelope(ctx, existing)?;
    let grant = challenge_and_handshake(
        &ctx.daemon_url,
        &envelope,
        ctx.managed_identity(&claims.session_id),
        Some(&claims),
        ctx.deadline,
    )?;
    validate_managed_refresh(ctx, &grant, &claims, existing)?;
    if !newer_generation(existing, &grant) {
        let existing = existing.cloned().ok_or(GrantError::Malformed(
            "managed refresh refused a generation downgrade".to_string(),
        ))?;
        validate_grant(
            &existing,
            &ctx.project_id,
            &ctx.machine_id,
            Some(&existing.deployment.token),
            None,
            true,
        )?;
        return Ok(AcquiredGrant {
            bundle: existing,
            source: GrantSource::ManagedFile,
            settings: None,
            daemon_reachable: true,
            now: ctx.now,
        });
    }
    let (grant, settings) =
        fetch_settings_coherent(ctx, grant, Some(&envelope), existing.cloned(), true)?;
    validate_managed_refresh(ctx, &grant, &claims, existing)?;
    if !newer_generation(existing, &grant) {
        return Err(GrantError::Malformed(
            "managed refresh refused a generation downgrade".to_string(),
        ));
    }
    // The daemon publishes the managed launch grant when it rotates credentials.
    // Clients only read that asset; persisting here fails under the managed sandbox
    // while the predecessor database credential is on its five-minute drain.
    Ok(AcquiredGrant {
        bundle: grant,
        source: GrantSource::ManagedFile,
        settings,
        daemon_reachable: true,
        now: ctx.now,
    })
}

pub(super) fn managed_bootstrap_path(ctx: &AcquireCtx) -> Option<PathBuf> {
    ctx.managed_bootstrap.clone()
}

fn validate_managed_identity(
    grant: &GrantBundle,
    expected: Option<&str>,
) -> Result<(), GrantError> {
    if let Some(expected) = expected.filter(|value| !value.trim().is_empty())
        && grant.principal.execution_id.as_deref() != Some(expected.trim())
    {
        return Err(GrantError::Malformed(
            "managed grant execution identity mismatch".to_string(),
        ));
    }
    Ok(())
}
