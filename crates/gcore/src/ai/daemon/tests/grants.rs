use super::super::operations::{grant_error_from_ai, presented_grant};
use super::*;
use crate::ai_types::AiError;
use crate::config::AiCapability;
use crate::grant::GrantError;

fn context_without_grant() -> AiContext {
    let mut cfg = test_context(None);
    cfg.grant = None;
    cfg
}

fn deny_bundle_capability(cfg: &mut AiContext, capability: AiCapability) {
    let Some(state) = cfg.grant.as_mut() else {
        panic!("test context must carry a grant snapshot");
    };
    match capability {
        AiCapability::Embed => {
            state.bundle.capabilities.embed = crate::grant::AiCapability::Unavailable {};
        }
        AiCapability::VisionExtract => {
            state.bundle.capabilities.vision_extract = crate::grant::AiCapability::Unavailable {};
        }
        AiCapability::AudioTranscribe | AiCapability::AudioTranslate => {
            state.bundle.capabilities.audio_transcribe = crate::grant::AiCapability::Unavailable {};
        }
        AiCapability::TextGenerate | AiCapability::ToolChat => {
            state.bundle.capabilities.text_generate = crate::grant::AiCapability::Unavailable {};
        }
    }
}

fn isolated_home() -> (tempfile::TempDir, EnvGuard) {
    let home = temp_home();
    let env = EnvGuard::set_home(home.path());
    write_daemon_files(home.path(), 9, "unused-token");
    (home, env)
}

#[test]
fn presented_grant_reuses_caller_root_and_preserves_grant_errors() {
    let (home, _env) = isolated_home();
    let cfg = context_without_grant();

    let missing_root = presented_grant(&cfg, None).expect_err("no root cannot acquire");
    assert!(
        matches!(missing_root, AiError::NotConfigured { .. }),
        "{missing_root:?}"
    );

    let error = presented_grant(&cfg, Some(home.path())).expect_err("temp root is not a project");
    match error {
        AiError::Grant { source } => {
            assert!(
                matches!(source, GrantError::Malformed(_)),
                "typed grant error, got {source:?}"
            );
        }
        other => panic!("acquire failure must stay typed Grant, got {other:?}"),
    }
}

#[test]
fn grant_error_from_ai_preserves_taxonomy() {
    assert_eq!(
        grant_error_from_ai(&AiError::from(GrantError::Expired)),
        Some(GrantError::Expired)
    );
    assert_eq!(
        grant_error_from_ai(&AiError::from(GrantError::DaemonRequired)),
        Some(GrantError::DaemonRequired)
    );
    assert_eq!(
        grant_error_from_ai(&AiError::HttpStatus {
            status: 409,
            body: Some("stale_epoch".to_string()),
        }),
        Some(GrantError::Malformed("stale_epoch".to_string()))
    );
    assert_eq!(
        grant_error_from_ai(&AiError::not_configured(None, "grant expired")),
        None
    );
}

#[test]
fn embed_gates_acquired_bundle_when_context_grant_is_none() {
    let (_home, _env) = isolated_home();
    let cfg = context_without_grant();
    let error = embed_via_daemon(&cfg, &["query".to_string()], false)
        .expect_err("missing grant must not send");
    assert!(
        matches!(error, AiError::Grant { .. } | AiError::NotConfigured { .. }),
        "{error:?}"
    );
}

#[test]
fn embed_gates_acquired_bundle_capability() {
    let (_home, _env) = isolated_home();
    let mut cfg = test_context(None);
    deny_bundle_capability(&mut cfg, AiCapability::Embed);
    let error = embed_via_daemon(&cfg, &["query".to_string()], false)
        .expect_err("bundle without embed must fail");
    match error {
        AiError::CapabilityUnavailable { capability, .. } => {
            assert_eq!(capability, "embed");
        }
        other => panic!("expected CapabilityUnavailable, got {other:?}"),
    }
}

#[test]
fn describe_gates_acquired_bundle_capability() {
    let (_home, _env) = isolated_home();
    let mut cfg = test_context(None);
    deny_bundle_capability(&mut cfg, AiCapability::VisionExtract);
    let error = describe_image_via_daemon(&cfg, b"png".to_vec(), "figure.png", "image/png")
        .expect_err("bundle without vision must fail");
    match error {
        AiError::CapabilityUnavailable { capability, .. } => {
            assert_eq!(capability, "vision_extract");
        }
        other => panic!("expected CapabilityUnavailable, got {other:?}"),
    }
}

#[test]
fn transcribe_gates_acquired_bundle_capability() {
    let (_home, _env) = isolated_home();
    let mut cfg = test_context(None);
    deny_bundle_capability(&mut cfg, AiCapability::AudioTranscribe);
    let error = transcribe_via_daemon(
        &cfg,
        b"audio".to_vec(),
        "clip.m4a",
        "audio/mp4",
        DaemonTranscriptionOptions::default(),
    )
    .expect_err("bundle without audio must fail");
    match error {
        AiError::CapabilityUnavailable { capability, .. } => {
            assert_eq!(capability, "audio_transcribe");
        }
        other => panic!("expected CapabilityUnavailable, got {other:?}"),
    }
}

#[test]
fn generate_gates_acquired_bundle_capability() {
    let (_home, _env) = isolated_home();
    let mut cfg = test_context(None);
    deny_bundle_capability(&mut cfg, AiCapability::TextGenerate);
    let error =
        generate_via_daemon(&cfg, "prompt", None).expect_err("bundle without text must fail");
    match error {
        AiError::CapabilityUnavailable { capability, .. } => {
            assert_eq!(capability, "text_generate");
        }
        other => panic!("expected CapabilityUnavailable, got {other:?}"),
    }
}
