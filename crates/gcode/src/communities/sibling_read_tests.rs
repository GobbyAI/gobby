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
            let mut ctx = Context::resolve_with_services(
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
            assert_eq!(
                ensure_fresh(&ctx, FreshnessScope::Project).unwrap(),
                FreshnessStatus::Checked,
                "an unchanged worktree is not stale"
            );
            fs::write(
                sibling_root.join(rel),
                "// stale\npub fn sibling_only() {}\npub fn caller() { sibling_only(); }\n",
            )
            .unwrap();
            ctx.quiet = false;
            crate::test_env::clear_captured_logs();
            assert_eq!(
                ensure_fresh(&ctx, FreshnessScope::Project).unwrap(),
                FreshnessStatus::Stale(vec![rel.to_string()])
            );
            crate::dispatch::ensure_project_fresh(&ctx, false).unwrap();
            let warnings = crate::test_env::captured_logs(log::Level::Warn);
            assert!(
                warnings
                    .iter()
                    .any(|line| line.contains("gcode index is stale for 1 path(s)")),
                "{warnings:?}"
            );
            // The sibling changed on disk after its rows were written. A reader that
            // cannot refresh must not slice the live file with the stale byte offsets.
            let located = crate::commands::symbol_at::symbol_at_repo(
                &ctx,
                managed.client(),
                &sibling_root,
                "src/lib.rs:1",
                None,
            )
            .unwrap();
            assert_eq!(
                located.snippet.trim_end(),
                "pub fn sibling_only() {}",
                "symbol-at must return the current body after the sibling changed"
            );
            let current_callers = GrepOptions {
                pattern: "sibling_only",
                paths: &[],
                globs: &[],
                fixed_strings: true,
                ignore_case: false,
                word: true,
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
                grep_repo(&ctx, managed.client(), &current_callers)
                    .unwrap()
                    .matched_lines,
                2,
                "grep -w must return the sibling's current definition and caller"
            );
            // A file the merge added is served by no row, so grep reads it live too.
            fs::write(
                sibling_root.join("src/extra.rs"),
                "pub fn later() { sibling_only(); }\n",
            )
            .unwrap();
            assert_eq!(
                grep_repo(&ctx, managed.client(), &current_callers)
                    .unwrap()
                    .matched_lines,
                3,
                "grep -w must also read a file no row serves"
            );
            fs::remove_file(sibling_root.join("src/extra.rs")).unwrap();
            let outside = root.join("outside.rs");
            fs::write(&outside, "pub fn blocked_live_token() {}\n").unwrap();
            let rejected_content = GrepOptions {
                pattern: "blocked_live_token|sibling_only",
                fixed_strings: false,
                ..current_callers
            };
            let mut leaked = Vec::new();
            for replacement in [
                #[cfg(unix)]
                "external-symlink",
                "binary",
                "oversize",
                "excluded",
            ] {
                let file = sibling_root.join(rel);
                fs::remove_file(&file).unwrap();
                match replacement {
                    "external-symlink" => {
                        #[cfg(unix)]
                        std::os::unix::fs::symlink(&outside, &file).unwrap();
                    }
                    "binary" => fs::write(&file, b"blocked_live_token\0\n").unwrap(),
                    "oversize" => {
                        let mut bytes = b"blocked_live_token\n".to_vec();
                        bytes.resize(crate::index::MAX_FILE_SIZE as usize + 1, b'x');
                        fs::write(&file, bytes).unwrap();
                    }
                    "excluded" => {
                        fs::write(&file, "pub fn blocked_live_token() {}\n").unwrap();
                        ctx.indexing.extra_excludes.push("lib.rs".to_string());
                    }
                    _ => unreachable!(),
                }
                assert_eq!(
                    ensure_fresh(&ctx, FreshnessScope::Project).unwrap(),
                    FreshnessStatus::Stale(vec![rel.to_string()]),
                    "a rejected served path remains stale: {replacement}"
                );
                crate::test_env::clear_captured_logs();
                crate::dispatch::ensure_project_fresh(&ctx, false).unwrap();
                assert!(
                    crate::test_env::captured_logs(log::Level::Warn)
                        .iter()
                        .any(|line| line.contains("gcode index is stale for 1 path(s)")),
                    "rejected content must retain the stale warning: {replacement}"
                );
                let result = grep_repo(&ctx, managed.client(), &rejected_content).unwrap();
                if result.matched_lines != 0 {
                    leaked.push((replacement, result.matched_lines));
                }
            }
            assert!(
                leaked.is_empty(),
                "grep must drop old chunks and never read rejected live content: {leaked:?}"
            );
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
