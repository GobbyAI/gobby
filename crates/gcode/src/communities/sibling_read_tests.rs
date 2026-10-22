use super::*;

#[test]
#[serial_test::serial(serial_db, serial_env)]
fn managed_main_checkout_reads_sibling_commands_without_refresh_writes() {
    use crate::commands::grep::{GrepOptions, grep_repo};
    use crate::config::ServiceConfigSelection;
    use crate::freshness::{FreshnessScope, FreshnessStatus, ensure_fresh};
    use crate::index::api::{IndexOptions, IndexRequest};
    use crate::output::Format;
    use gobby_core::grant::{DirectConnections, managed_direct_grant, write_managed_bootstrap};

    let (mut owner, database_url, parent_id, _parent_cleanup) = seeded_project("sibling-reads");
    let tmp = tempfile::tempdir().expect("temporary workspace");
    let root = tmp.path().canonicalize().expect("canonical root");
    let parent_root = root.join("parent");
    let sibling_root = root.join("sibling");
    let rel = "src/lib.rs";
    for workspace in [&parent_root, &sibling_root] {
        fs::create_dir_all(workspace.join("src")).unwrap();
        fs::create_dir_all(workspace.join(".gobby")).unwrap();
        fs::write(
            workspace.join(".gobby/project.json"),
            serde_json::json!({"id": parent_id}).to_string(),
        )
        .unwrap();
        fs::write(
            workspace.join(rel),
            if workspace == &parent_root {
                "pub fn parent_only() {}\n"
            } else {
                "pub fn sibling_only() {}\n"
            },
        )
        .unwrap();
    }
    fs::write(
        sibling_root.join(".gobby/isolation.json"),
        serde_json::json!({
            "parent_project_id": parent_id, "parent_project_path": parent_root
        })
        .to_string(),
    )
    .unwrap();
    // Give the parent its own repository boundary, independent of the lane.
    let git = std::process::Command::new("git")
        .current_dir(&parent_root)
        .args([
            "-c",
            "core.hooksPath=/dev/null",
            "init",
            "--quiet",
            "--template=",
            "--separate-git-dir",
        ])
        .arg(root.join("gitdir"))
        .output()
        .unwrap();
    assert!(
        git.status.success(),
        "{}",
        String::from_utf8_lossy(&git.stderr)
    );
    let mut managed = managed_principal(
        &mut owner,
        &database_url,
        &parent_id,
        &parent_root,
        &sibling_root,
        None,
    );
    let sibling_id = crate::project::code_index_id_for_root(&sibling_root);
    let _sibling_cleanup = ProjectCleanup {
        database_url: database_url.clone(),
        project_id: sibling_id.clone(),
    };
    let worktree_id = uuid::Uuid::new_v4();
    owner
        .execute(
            "INSERT INTO worktrees(id, project_id, machine_id, branch_name, worktree_path)
        VALUES ($1, $2, $3, $4, $5)",
            &[
                &worktree_id,
                &db::id_param(&parent_id).unwrap(),
                &managed.machine_id,
                &worktree_id.to_string(),
                &sibling_root.to_string_lossy().as_ref(),
            ],
        )
        .unwrap();
    for (id, workspace, scope) in [
        (&parent_id, &parent_root, ProjectIndexScope::Single),
        (
            &sibling_id,
            &sibling_root,
            ProjectIndexScope::Overlay {
                overlay_project_id: sibling_id.clone(),
                overlay_root: sibling_root.clone(),
                parent_project_id: parent_id.clone(),
                parent_root: parent_root.clone(),
            },
        ),
    ] {
        let mut ctx = test_context(database_url.clone(), id, scope);
        ctx.project_root = workspace.clone();
        api::index_files(
            IndexRequest {
                project_root: workspace.clone(),
                path_filter: None,
                explicit_files: vec![PathBuf::from(rel)],
                full: true,
                require_cpp_semantics: false,
                sync_projections: false,
            },
            &ctx,
            IndexOptions::default(),
        )
        .unwrap();
    }
    let home = root.join("home");
    fs::create_dir_all(&home).unwrap();
    fs::write(home.join("machine_id"), managed.machine_id.to_string()).unwrap();
    let mut grant = managed_direct_grant(
        &parent_id,
        &managed.machine_id.to_string(),
        &DirectConnections::postgres(&managed.managed_database_url),
    );
    grant.principal.kind = gobby_core::grant::PrincipalKind::ToolChat;
    grant.principal.execution_id = Some(managed.execution_id.to_string());
    grant.principal.session_id = Some(managed.session_id.to_string());
    assert!(grant.principal.code_overlay_project_id.is_none());
    let bootstrap = write_managed_bootstrap(&home, &grant.with_checksum()).unwrap();
    let daemon = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let daemon_url = format!("http://{}", daemon.local_addr().unwrap());
    temp_env::with_vars(
        [
            ("GOBBY_HOME", Some(home.as_os_str())),
            (
                gobby_core::grant::MANAGED_BOOTSTRAP_ENV,
                Some(bootstrap.as_os_str()),
            ),
            ("GOBBY_DAEMON_URL", Some(std::ffi::OsStr::new(&daemon_url))),
            ("GOBBY_AGENT_RUN_ID", None),
            ("GOBBY_MANAGED_EXECUTION_ID", None),
            ("GOBBY_AGENT_API_TOKEN", None),
            ("GCODE_FRESHNESS_INFLIGHT", None),
        ],
        || {
            let ctx = Context::resolve_with_services(
                Some(sibling_root.to_str().unwrap()),
                true,
                ServiceConfigSelection::database_only(),
            )
            .expect("main-checkout role admits active sibling");
            assert_eq!(ctx.project_id, sibling_id);
            assert!(!ctx.can_refresh_index().unwrap());
            let tree = crate::visibility::visible_tree(managed.client(), &ctx).unwrap();
            assert_eq!(tree.len(), 1);
            assert_eq!(tree[0].file_path, rel);
            let symbols =
                crate::visibility::visible_symbols_for_file(managed.client(), &ctx, rel).unwrap();
            assert_eq!(symbols.len(), 1);
            assert_eq!(symbols[0].name, "sibling_only");
            let options = GrepOptions {
                pattern: "sibling_only",
                paths: &[],
                globs: &[],
                fixed_strings: true,
                ignore_case: false,
                word: false,
                context: None,
                before_context: None,
                after_context: None,
                max_count: None,
                offset: 0,
                token_budget: None,
                files_with_matches: false,
                format: Format::Json,
            };
            assert_eq!(
                grep_repo(&ctx, managed.client(), &options)
                    .unwrap()
                    .matched_lines,
                1
            );
            crate::commands::grep::run(&ctx, options).unwrap();
            crate::commands::symbols::tree(&ctx, &[], None, 0, None, Format::Json).unwrap();
            crate::commands::symbols::outline(&ctx, rel, None, 0, None, Format::Json, false)
                .unwrap();
            crate::commands::symbol_at::run(
                &ctx,
                &sibling_root,
                "src/lib.rs:1",
                None,
                Format::Json,
            )
            .unwrap();
            fs::write(
                sibling_root.join(rel),
                "// stale\npub fn sibling_only() {}\n",
            )
            .unwrap();
            crate::test_env::clear_captured_logs();
            assert_eq!(
                ensure_fresh(&ctx, FreshnessScope::Project).unwrap(),
                FreshnessStatus::Checked
            );
            crate::dispatch::ensure_project_fresh(&ctx, false).unwrap();
            assert!(crate::test_env::captured_logs(log::Level::Warn).is_empty());
            let error = crate::commands::index::run(
                &ctx,
                &sibling_root,
                None,
                None,
                true,
                false,
                false,
                false,
                Format::Json,
            )
            .expect_err("sibling writes remain refused");
            assert!(
                format!("{error:#}").contains("row-level security"),
                "{error:#}"
            );
        },
    );
    owner
        .execute("DELETE FROM worktrees WHERE id = $1", &[&worktree_id])
        .unwrap();
}
