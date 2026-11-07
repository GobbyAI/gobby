use super::*;
use crate::node_commands::{SandboxBackend, SandboxMode};
use anyhow::Context;
use std::os::unix::fs::symlink;

struct Fixture {
    dir: tempfile::TempDir,
    layout: RunLayout,
}

fn fixture() -> Result<Fixture> {
    let dir = tempfile::tempdir()?;
    let shared_cache = dir.path().join("gobby").join("cache").join("sandbox");
    fs::create_dir_all(&shared_cache)?;
    let run = dir.path().join("run");
    let layout = RunLayout {
        root: run.clone(),
        tmp: run.join("tmp"),
        hooks: run.join("hooks"),
        logs: run.join("logs"),
        cache: run.join("cache"),
        cargo_target: shared_cache.join("cargo-target").join("checkout"),
        build_sandbox: shared_cache.clone(),
        shared_cache,
    };
    Ok(Fixture { dir, layout })
}

fn granted(path: &Path) -> String {
    text(&resolve_nonstrict(path))
}

fn real_grants(layout: &RunLayout) -> Result<()> {
    fs::create_dir_all(layout.cargo_home())?;
    fs::create_dir_all(&layout.cargo_target)?;
    Ok(())
}

#[test]
fn recheck_refuses_a_cargo_home_swapped_for_a_link() -> Result<()> {
    let fixture = fixture()?;
    let layout = &fixture.layout;
    let outside = fixture.dir.path().join("outside");
    fs::create_dir_all(&outside)?;
    symlink(&outside, layout.cargo_home())?;
    fs::create_dir_all(&layout.cargo_target)?;
    let grants = [granted(&layout.cargo_home()), granted(&layout.cargo_target)];
    let error = recheck_cache_grants(layout, &grants)
        .err()
        .context("a cargo-home link must be refused")?;
    assert!(
        error
            .to_string()
            .contains("resolves outside the sandbox cache"),
        "{error}"
    );
    Ok(())
}

#[test]
fn recheck_refuses_a_cargo_target_swapped_for_a_link() -> Result<()> {
    let fixture = fixture()?;
    let layout = &fixture.layout;
    let outside = fixture.dir.path().join("outside");
    fs::create_dir_all(&outside)?;
    fs::create_dir_all(layout.cargo_home())?;
    let target_parent = layout
        .cargo_target
        .parent()
        .context("cargo target has a parent")?;
    fs::create_dir_all(target_parent)?;
    symlink(&outside, &layout.cargo_target)?;
    let grants = [granted(&layout.cargo_home()), granted(&layout.cargo_target)];
    let error = recheck_cache_grants(layout, &grants)
        .err()
        .context("a cargo-target link must be refused")?;
    assert!(
        error
            .to_string()
            .contains("resolves outside the sandbox cache"),
        "{error}"
    );
    Ok(())
}

#[test]
fn recheck_refuses_a_real_grant_the_rendered_policy_omits() -> Result<()> {
    let fixture = fixture()?;
    let layout = &fixture.layout;
    real_grants(layout)?;
    let grants = [granted(&layout.cargo_home())];
    let error = recheck_cache_grants(layout, &grants)
        .err()
        .context("a grant missing from the policy must be refused")?;
    assert!(error.to_string().contains("Sandbox cache grant"), "{error}");
    assert!(error.to_string().contains("cargo-target"), "{error}");
    Ok(())
}

#[test]
fn recheck_accepts_real_grants_the_rendered_policy_carries() -> Result<()> {
    let fixture = fixture()?;
    let layout = &fixture.layout;
    real_grants(layout)?;
    let grants = [granted(&layout.cargo_home()), granted(&layout.cargo_target)];
    recheck_cache_grants(layout, &grants)
}

fn inputs_with_build_root(gobby_home: &Path, build_root: &Path, workspace: &Path) -> PolicyInputs {
    PolicyInputs {
        provider: "claude".into(),
        sandbox: Sandbox {
            enabled: true,
            backend: SandboxBackend::Srt,
            mode: SandboxMode::Restrictive,
            allow_network: false,
            extra_read_paths: Vec::new(),
            extra_write_paths: Vec::new(),
            extra_deny_read_paths: Vec::new(),
            extra_deny_write_paths: Vec::new(),
            allowed_domains: Vec::new(),
            denied_domains: Vec::new(),
            allow_git_network: false,
            allow_package_registries: false,
            allow_unix_sockets: Vec::new(),
        },
        source_root: workspace.to_owned(),
        project_id: "project-one".into(),
        session_id: "session-one".into(),
        workspace: workspace.to_owned(),
        home: gobby_home.join("home"),
        gobby_home: gobby_home.to_owned(),
        build_root: build_root.to_owned(),
        env: BTreeMap::new(),
        provider_executable: None,
        daemon_port: 1,
        websocket_port: 2,
        run_id: "run-one".into(),
        prewarm_pre_commit_store: false,
        platform: "darwin".into(),
        darwin_user_dirs: None,
        account_home: Some(gobby_home.join("home")),
        definition: None,
        session_seq: None,
        epic_seq: None,
        read_all_agent_workspaces: false,
        predecessor_workspace_folders: Vec::new(),
    }
}

#[test]
fn cargo_targets_build_under_the_configured_build_root_not_the_gobby_cache() -> Result<()> {
    let dir = tempfile::tempdir()?;
    let gobby_home = dir.path().join("gobby");
    let build_root = dir.path().join("GobbyBuild");
    let workspace = dir.path().join("checkout");
    fs::create_dir_all(&gobby_home)?;
    fs::create_dir_all(&build_root)?;
    fs::create_dir_all(&workspace)?;
    let build_root = build_root.canonicalize()?;
    let inputs = inputs_with_build_root(&gobby_home, &build_root, &workspace);

    let layout = run_layout(&inputs, &BTreeMap::new())?;

    assert!(
        layout
            .cargo_target
            .starts_with(build_root.join("sandbox").join("cargo-target")),
        "sandboxed target {} is outside the build root",
        layout.cargo_target.display()
    );
    assert_eq!(
        layout.shared_cache,
        gobby_home.join("cache").join("sandbox")
    );
    assert!(
        Path::new(&agent_cargo_target_dir(&inputs)).starts_with(build_root.join("cargo-target-v2")),
        "unsandboxed target is outside the build root"
    );
    Ok(())
}

#[test]
fn a_relative_build_root_is_refused_before_any_grant_is_rendered() -> Result<()> {
    let dir = tempfile::tempdir()?;
    let gobby_home = dir.path().join("gobby");
    let workspace = dir.path().join("checkout");
    fs::create_dir_all(&gobby_home)?;
    fs::create_dir_all(&workspace)?;
    let inputs = inputs_with_build_root(&gobby_home, Path::new("GobbyBuild"), &workspace);

    let error = run_layout(&inputs, &BTreeMap::new())
        .err()
        .context("a relative build root must be refused")?;

    assert!(error.to_string().contains("not absolute"), "{error}");
    Ok(())
}
