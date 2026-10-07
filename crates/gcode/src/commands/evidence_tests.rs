use super::*;

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
