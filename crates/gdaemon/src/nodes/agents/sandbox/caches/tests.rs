use super::*;
use crate::node_commands::{SandboxBackend, SandboxMode};
use crate::nodes::agents::sandbox::compile_policy;
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
        operator_bootstrap: Some(gobby_home.join("home/.gobby/bootstrap.yaml")),
        credential_authority_roots: Vec::new(),
        env: BTreeMap::new(),
        provider_executable: None,
        allow_run_unix_sockets: false,
        daemon_port: 1,
        websocket_port: 2,
        run_id: "run-one".into(),
        prewarm_pre_commit_store: false,
        platform: "darwin".into(),
        darwin_user_dirs: None,
        definition: None,
        session_seq: None,
        epic_seq: None,
        persistent_workspace: false,
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

/// A native darwin srt spawn with the run tmp socket grant.
fn native_inputs(gobby_home: &Path, workspace: &Path) -> PolicyInputs {
    let mut inputs =
        inputs_with_build_root(gobby_home, Path::new("/gobby-test/GobbyBuild"), workspace);
    inputs.run_id = "r1".into();
    inputs.platform = if cfg!(target_os = "macos") {
        "darwin"
    } else {
        "linux"
    }
    .into();
    inputs.allow_run_unix_sockets = true;
    inputs
        .env
        .insert(SESSION_ENV.to_string(), "session-one".to_string());
    inputs
}

/// The run layout of native run `r1` under `gobby_home`, built without touching the disk.
fn synthetic_layout(gobby_home: &Path) -> RunLayout {
    let run = gobby_home.join("run").join("sandbox").join("r1");
    let shared_cache = gobby_home.join("cache").join("sandbox");
    let build_sandbox = gobby_home.join("GobbyBuild").join("sandbox");
    RunLayout {
        root: run.clone(),
        tmp: run.join("tmp"),
        hooks: run.join("hooks"),
        logs: run.join("logs"),
        cache: run.join("cache"),
        cargo_target: build_sandbox.join("cargo-target").join("checkout"),
        build_sandbox,
        shared_cache,
    }
}

/// Agent Cargo builds are plain incremental builds. The launch environment sets only the
/// job limit: a set `CARGO_INCREMENTAL`, blank included, turns incremental off, and the
/// launch removes the wrapper keys at exec instead of blanking them.
fn plain_cargo_env() -> BTreeMap<String, String> {
    BTreeMap::from([("CARGO_BUILD_JOBS".to_string(), "4".to_string())])
}

#[test]
fn a_native_launch_sets_no_cargo_wrapper_or_incremental_key_and_keeps_the_socket_grant() {
    let gobby_home = Path::new("/gobby-test");
    let inputs = native_inputs(gobby_home, Path::new("/gobby-test/checkout"));

    let (socket_root, env) = native_cargo_grants(&inputs, &synthetic_layout(gobby_home));

    // The run tmp socket grant predates sccache (#21752) and stays.
    assert_eq!(
        socket_root.as_deref(),
        Some("/gobby-test/run/sandbox/r1/tmp")
    );
    assert_eq!(env, plain_cargo_env());
}

#[test]
fn a_native_launch_without_the_socket_grant_gets_no_socket_root() {
    let gobby_home = Path::new("/gobby-test");
    let mut inputs = native_inputs(gobby_home, Path::new("/gobby-test/checkout"));
    inputs.allow_run_unix_sockets = false;

    let (socket_root, env) = native_cargo_grants(&inputs, &synthetic_layout(gobby_home));

    assert_eq!(socket_root, None);
    assert_eq!(env, plain_cargo_env());
}

#[test]
fn a_compiled_native_policy_builds_plain_incremental() -> Result<()> {
    for socket_granted in [true, false] {
        let dir = tempfile::tempdir()?;
        let gobby_home = dir.path().join("gobby");
        let workspace = dir.path().join("checkout");
        let build_root = dir.path().join("GobbyBuild");
        fs::create_dir_all(&gobby_home)?;
        fs::create_dir_all(&workspace)?;
        fs::create_dir_all(&build_root)?;
        let mut inputs = native_inputs(&gobby_home, &workspace);
        inputs.build_root = build_root.canonicalize()?;
        inputs.allow_run_unix_sockets = socket_granted;
        let run_tmp = text(&resolve_nonstrict(
            &gobby_home
                .join("run")
                .join("sandbox")
                .join("r1")
                .join("tmp"),
        ));

        let policy = compile_policy(&inputs, &[], &[])?;

        for key in [
            "CARGO_INCREMENTAL",
            "RUSTC_WRAPPER",
            "SCCACHE_DIR",
            "SCCACHE_SERVER_UDS",
        ] {
            assert!(
                !policy.env.contains_key(key),
                "{key} (socket {socket_granted})"
            );
        }
        assert_eq!(
            policy.env.get("CARGO_BUILD_JOBS").map(String::as_str),
            Some("4")
        );
        assert_eq!(
            policy.sandbox.allow_unix_sockets.contains(&run_tmp),
            socket_granted
        );
    }
    Ok(())
}

#[test]
fn a_missing_build_root_is_refused_and_not_created() -> Result<()> {
    let dir = tempfile::tempdir()?;
    let gobby_home = dir.path().join("gobby");
    let workspace = dir.path().join("checkout");
    fs::create_dir_all(&gobby_home)?;
    fs::create_dir_all(&workspace)?;
    let build_root = dir.path().join("GobbyBuild");
    let inputs = inputs_with_build_root(&gobby_home, &build_root, &workspace);

    let error = run_layout(&inputs, &BTreeMap::new())
        .err()
        .context("a missing build root must be refused")?;

    assert!(error.to_string().contains("is missing"), "{error}");
    assert!(
        !build_root.exists(),
        "the node created the missing build root"
    );
    Ok(())
}

#[test]
fn a_build_root_on_an_unmounted_volume_is_refused_without_creating_the_mount_point() -> Result<()> {
    let dir = tempfile::tempdir()?;
    let gobby_home = dir.path().join("gobby");
    let workspace = dir.path().join("checkout");
    fs::create_dir_all(&gobby_home)?;
    fs::create_dir_all(&workspace)?;
    let mount_parent = dir.path().join("Volumes");
    let build_root = mount_parent.join("GobbyBuild");
    let inputs = inputs_with_build_root(&gobby_home, &build_root, &workspace);

    let error = run_layout(&inputs, &BTreeMap::new())
        .err()
        .context("a build root on an absent volume must be refused")?;

    assert!(error.to_string().contains("volume"), "{error}");
    assert!(
        !mount_parent.exists(),
        "the node created the absent volume's mount point"
    );
    Ok(())
}

#[test]
fn a_non_canonical_build_root_is_refused() -> Result<()> {
    let dir = tempfile::tempdir()?;
    let gobby_home = dir.path().join("gobby");
    let workspace = dir.path().join("checkout");
    let build_root = dir.path().join("GobbyBuild");
    fs::create_dir_all(&gobby_home)?;
    fs::create_dir_all(&workspace)?;
    fs::create_dir_all(&build_root)?;
    let canonical = build_root.canonicalize()?;
    let name = canonical.file_name().context("the build root has a name")?;
    let spelled = canonical.join("..").join(name);
    let inputs = inputs_with_build_root(&gobby_home, &spelled, &workspace);

    let error = run_layout(&inputs, &BTreeMap::new())
        .err()
        .context("a non-canonical build root must be refused")?;

    assert!(error.to_string().contains("not canonical"), "{error}");
    Ok(())
}

#[test]
fn a_symlinked_build_root_is_refused_and_never_followed() -> Result<()> {
    let dir = tempfile::tempdir()?;
    let gobby_home = dir.path().join("gobby");
    let workspace = dir.path().join("checkout");
    let real = dir.path().join("GobbyBuild");
    fs::create_dir_all(&gobby_home)?;
    fs::create_dir_all(&workspace)?;
    fs::create_dir_all(&real)?;
    let link = dir.path().join("linked-build");
    symlink(&real, &link)?;
    let inputs = inputs_with_build_root(&gobby_home, &link, &workspace);

    let error = run_layout(&inputs, &BTreeMap::new())
        .err()
        .context("a symlinked build root must be refused")?;

    assert!(error.to_string().contains("symbolic link"), "{error}");
    Ok(())
}

#[test]
fn a_native_launch_write_grants_follow_the_configured_build_root() -> Result<()> {
    let dir = tempfile::tempdir()?;
    let gobby_home = dir.path().join("gobby");
    let workspace = dir.path().join("checkout");
    let build_root = dir.path().join("GobbyBuild");
    fs::create_dir_all(&gobby_home)?;
    fs::create_dir_all(&workspace)?;
    fs::create_dir_all(&build_root)?;
    let mut inputs = native_inputs(&gobby_home, &workspace);
    inputs.build_root = build_root.canonicalize()?;
    let sandbox_target = run_layout(&inputs, &BTreeMap::new())?.cargo_target;

    let policy = compile_policy(&inputs, &[], &[])?;

    assert_eq!(policy.env["CARGO_TARGET_DIR"], text(&sandbox_target));
    assert!(
        policy
            .sandbox
            .extra_write_paths
            .contains(&text(&sandbox_target))
    );
    // Every cargo-target write grant names the configured root, never the gobby cache.
    let stray: Vec<&String> = policy
        .sandbox
        .extra_write_paths
        .iter()
        .filter(|path| {
            path.contains("cargo-target") && !Path::new(path).starts_with(&inputs.build_root)
        })
        .collect();
    assert!(
        stray.is_empty(),
        "a cargo-target grant escapes the build root: {stray:?}"
    );
    Ok(())
}

#[test]
fn a_seat_grant_cannot_reach_the_unsandboxed_cargo_target_root() -> Result<()> {
    let dir = tempfile::tempdir()?;
    let gobby_home = dir.path().join("gobby");
    let workspace = dir.path().join("checkout");
    let build_root = dir.path().join("GobbyBuild");
    fs::create_dir_all(&gobby_home)?;
    fs::create_dir_all(&workspace)?;
    fs::create_dir_all(&build_root)?;
    let mut inputs = native_inputs(&gobby_home, &workspace);
    inputs.build_root = build_root.canonicalize()?;
    // The unsandboxed trust domain's target root; a sandboxed seat must never be granted into it.
    let unsandboxed = inputs.build_root.join("cargo-target-v2");

    let policy = compile_policy(&inputs, &[], &[])?;

    let reaching: Vec<&String> = policy
        .sandbox
        .extra_write_paths
        .iter()
        .filter(|path| Path::new(path).starts_with(&unsandboxed))
        .collect();
    assert!(
        reaching.is_empty(),
        "a seat grant reaches the unsandboxed cargo target root: {reaching:?}"
    );
    Ok(())
}

#[test]
fn a_seat_grant_never_covers_the_spawn_env_cargo_home() -> Result<()> {
    let dir = tempfile::tempdir()?;
    let gobby_home = dir.path().join("gobby");
    let workspace = dir.path().join("checkout");
    let build_root = dir.path().join("GobbyBuild");
    fs::create_dir_all(&gobby_home)?;
    fs::create_dir_all(&workspace)?;
    fs::create_dir_all(&build_root)?;
    let gobby_home = gobby_home.canonicalize()?;
    let mut inputs = native_inputs(&gobby_home, &workspace);
    inputs.build_root = build_root.canonicalize()?;
    // The spawn env's CARGO_HOME is the unsandboxed trust domain's shared cargo home.
    let unsandboxed = shared_cargo_home(&gobby_home);

    let policy = compile_policy(&inputs, &[], &[])?;

    let reaching: Vec<&String> = policy
        .sandbox
        .extra_write_paths
        .iter()
        .filter(|path| Path::new(path).starts_with(&unsandboxed))
        .collect();
    assert!(
        reaching.is_empty(),
        "a seat grant reaches the unsandboxed cargo home: {reaching:?}"
    );
    Ok(())
}

#[test]
fn a_seat_grant_never_includes_the_unsandboxed_session_uv_cache() -> Result<()> {
    let dir = tempfile::tempdir()?;
    let gobby_home = dir.path().join("gobby");
    let workspace = dir.path().join("checkout");
    let build_root = dir.path().join("GobbyBuild");
    let temp = dir.path().join("tmp");
    fs::create_dir_all(&gobby_home)?;
    fs::create_dir_all(&workspace)?;
    fs::create_dir_all(&build_root)?;
    let mut inputs = native_inputs(&gobby_home, &workspace);
    inputs.build_root = build_root.canonicalize()?;
    inputs.env.insert("TMPDIR".to_string(), text(&temp));
    // Unsandboxed agents install into each session's uv cache under this root, so no sandbox may
    // write any of them.
    let unsandboxed = temp.join("gobby").join("uv-cache");
    // The sandboxed uv cache is the run's own, which the run layout already grants.
    let run_cache = gobby_home
        .join("run")
        .join("sandbox")
        .join("r1")
        .join("cache");

    let policy = compile_policy(&inputs, &[], &[])?;

    let reaching: Vec<&String> = policy
        .sandbox
        .extra_write_paths
        .iter()
        .filter(|path| Path::new(path).starts_with(&unsandboxed))
        .collect();
    assert!(
        reaching.is_empty(),
        "a seat grant reaches the unsandboxed session uv cache: {reaching:?}"
    );
    assert_eq!(
        policy.env[UV_CACHE_DIR],
        text(&run_cache.join("uv-cache-dir"))
    );
    assert!(
        policy.sandbox.extra_write_paths.contains(&text(&run_cache)),
        "the run cache holding the sandboxed uv cache is not granted"
    );
    Ok(())
}

#[test]
fn a_launch_drops_the_per_session_toolchain_grants_the_run_supersedes() -> Result<()> {
    let dir = tempfile::tempdir()?;
    let gobby_home = dir.path().join("gobby");
    let workspace = dir.path().join("checkout");
    let build_root = dir.path().join("GobbyBuild");
    let temp = dir.path().join("tmp");
    fs::create_dir_all(&gobby_home)?;
    fs::create_dir_all(&workspace)?;
    fs::create_dir_all(&build_root)?;
    let mut inputs = native_inputs(&gobby_home, &workspace);
    inputs.build_root = build_root.canonicalize()?;
    inputs.env.insert("TMPDIR".to_string(), text(&temp));

    // The spawn env carries each per-session toolchain cache, as `fill_spawn_env` materializes it.
    let mut spawn_env = inputs.env.clone();
    let spawned = spawn_grants(inputs.sandbox.clone(), &[], &mut spawn_env, &inputs)?;
    let per_session: Vec<String> = SANDBOX_CACHE_POLICY
        .iter()
        .map(|(name, _)| spawn_env[*name].clone())
        .collect();
    for path in &per_session {
        assert!(
            spawned.extra_write_paths.contains(path),
            "the spawn level must keep {path}"
        );
    }

    // The run's env points each of these at its own cache, so the launch must not grant them.
    let policy = compile_policy(&inputs, &[], &[])?;
    let kept: Vec<&String> = policy
        .sandbox
        .extra_write_paths
        .iter()
        .filter(|path| per_session.contains(path))
        .collect();
    assert!(
        kept.is_empty(),
        "a launch keeps per-session toolchain grants the run supersedes: {kept:?}"
    );
    Ok(())
}

#[test]
fn a_spawn_without_a_session_id_is_refused_before_any_grant() -> Result<()> {
    let dir = tempfile::tempdir()?;
    let gobby_home = dir.path().join("gobby");
    let workspace = dir.path().join("checkout");
    let build_root = dir.path().join("GobbyBuild");
    fs::create_dir_all(&gobby_home)?;
    fs::create_dir_all(&workspace)?;
    fs::create_dir_all(&build_root)?;
    let mut inputs = native_inputs(&gobby_home, &workspace);
    inputs.build_root = build_root.canonicalize()?;
    inputs.env.remove(SESSION_ENV);

    let error = compile_policy(&inputs, &[], &[])
        .err()
        .context("a spawn without a session id must be refused")?;

    assert!(error.to_string().contains(SESSION_ENV), "{error}");
    Ok(())
}

#[test]
fn a_session_id_with_no_safe_characters_is_refused() -> Result<()> {
    let dir = tempfile::tempdir()?;
    let gobby_home = dir.path().join("gobby");
    let workspace = dir.path().join("checkout");
    let build_root = dir.path().join("GobbyBuild");
    fs::create_dir_all(&gobby_home)?;
    fs::create_dir_all(&workspace)?;
    fs::create_dir_all(&build_root)?;
    let mut inputs = native_inputs(&gobby_home, &workspace);
    inputs.build_root = build_root.canonicalize()?;
    inputs
        .env
        .insert(SESSION_ENV.to_string(), "///".to_string());

    let error = compile_policy(&inputs, &[], &[])
        .err()
        .context("a session id with no safe characters must be refused")?;

    assert!(error.to_string().contains("safe characters"), "{error}");
    Ok(())
}
