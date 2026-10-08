use super::*;

#[test]
fn missing_binding_fields_offer_complete_request_recovery() -> anyhow::Result<()> {
    for field in ["project_id", "commit_oid", "tree_oid"] {
        let mut document = serde_json::json!({
            "schema_version": 1,
            "operation": "read",
            "binding": {"project_id": "project-1", "commit_oid": "a".repeat(40), "tree_oid": "b".repeat(40)},
            "read": {"kind": "range", "path": "src/lib.rs", "start_line": 1, "end_line": 1}
        });
        document["binding"].as_object_mut().unwrap().remove(field);
        let error = preflight(&document.to_string(), Format::Json, false, None)
            .err()
            .expect("incomplete binding must fail");
        let error = error
            .downcast_ref::<CliError>()
            .expect("structured CLI error");
        assert_eq!(error.code, "invalid_evidence_request");
        assert!(error.message.contains(&format!("missing field `{field}`")));
        let recovery = error.recovery.as_ref().expect("actionable recovery");
        let example = recovery
            .split('`')
            .find(|part| part.starts_with('{'))
            .expect("complete JSON example");
        let request: EvidenceRequest = serde_json::from_str(example)?;
        assert!(!request.binding.project_id.is_empty());
        assert!(!request.binding.commit_oid.is_empty());
        assert!(!request.binding.tree_oid.is_empty());
        assert!(recovery.contains("git rev-parse"));
    }
    Ok(())
}

#[test]
fn binding_mismatch_recovery_explains_identity_refresh() {
    let error = cli_error(EvidenceError::BindingMismatch {
        detail: "fact project differs from caller project".to_string(),
    });
    assert_eq!(error.code, "repository_binding_mismatch");
    let recovery = error.recovery.expect("actionable recovery");
    for instruction in [
        "binding omitted",
        "response.binding.project_id",
        "commit_oid",
        "tree_oid",
        "git rev-parse",
        "task project IDs",
    ] {
        assert!(recovery.contains(instruction), "missing {instruction}");
    }
}

#[test]
fn preflight_preserves_explicit_and_omitted_binding_intent() -> anyhow::Result<()> {
    let temporary = tempfile::tempdir()?;
    let root = temporary.path();
    for arguments in [
        vec!["init", "--quiet"],
        vec![
            "-c",
            "user.name=test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "--quiet",
            "--allow-empty",
            "-m",
            "fixture",
        ],
    ] {
        let output = std::process::Command::new("git")
            .env("GIT_CONFIG_GLOBAL", "/dev/null")
            .env("GIT_CONFIG_NOSYSTEM", "1")
            .arg("-C")
            .arg(root)
            .args(arguments)
            .output()?;
        anyhow::ensure!(
            output.status.success(),
            "{}",
            String::from_utf8_lossy(&output.stderr)
        );
    }
    std::fs::create_dir(root.join(".gobby"))?;
    std::fs::write(
        root.join(".gobby/project.json"),
        r#"{"id":"e62aef83-2575-53b9-830c-2e88a81d7a35","name":"fixture"}"#,
    )?;
    let request_json = r#"{"schema_version":1,"operation":"read","read":{"kind":"range","path":"src/lib.rs","start_line":1,"end_line":1}}"#;
    let unbound = preflight(request_json, Format::Json, false, root.to_str())?;
    assert!(!unbound.commit_bound);
    assert_eq!(
        unbound.request.binding.commit_oid,
        crate::git::git_output(root, &["rev-parse", "HEAD"])?
    );
    let explicit = preflight(
        &serde_json::to_string(&unbound.request)?,
        Format::Json,
        false,
        root.to_str(),
    )?;
    assert!(explicit.commit_bound);
    assert_eq!(explicit.request, unbound.request);
    Ok(())
}
