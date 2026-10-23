use std::collections::BTreeSet;
use std::fs;
use std::path::Path;
use std::process::Command;
use std::time::{SystemTime, UNIX_EPOCH};

use postgres::error::SqlState;

use super::{PinSource, apply_pin, derive_gaps};
use crate::db;
use crate::index::api;
use crate::models::{IndexedFile, IndexedProject};
use crate::test_env::seed_test_checkout;

const MISSING_COMMIT: &str = "0000000000000000000000000000000000000001";
const MISMATCHED_BLOB: &str = "ffffffffffffffffffffffffffffffffffffffff";

fn unique_id(prefix: &str) -> String {
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("epoch")
        .as_nanos();
    uuid::Uuid::new_v5(
        &crate::models::CODE_INDEX_UUID_NAMESPACE,
        format!("{prefix}-{}-{nanos}", std::process::id()).as_bytes(),
    )
    .to_string()
}

fn indexed_file(project_id: &str, file_path: &str, content_hash: &str) -> IndexedFile {
    IndexedFile {
        id: IndexedFile::make_id(project_id, file_path, content_hash),
        project_id: project_id.to_string(),
        file_path: file_path.to_string(),
        language: "rust".to_string(),
        content_hash: content_hash.to_string(),
        symbol_count: 0,
        byte_size: 0,
        indexed_at: String::new(),
    }
}

fn git(root: &Path, args: &[&str]) -> String {
    let output = Command::new("git")
        .arg("-C")
        .arg(root)
        .args(["-c", "commit.gpgsign=false"])
        .args(args)
        .output()
        .expect("run git");
    assert!(
        output.status.success(),
        "git {args:?} failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    String::from_utf8(output.stdout).expect("utf8 git output")
}

/// A real Git checkout whose `HEAD` commits the given files.
struct Repo {
    dir: tempfile::TempDir,
    commit: String,
}

impl Repo {
    fn new(files: &[(&str, &str)]) -> Self {
        let dir = tempfile::tempdir().expect("tempdir");
        git(dir.path(), &["init", "-q"]);
        for (rel, body) in files {
            write_file(dir.path(), rel, body);
        }
        git(dir.path(), &["add", "-A"]);
        git(
            dir.path(),
            &[
                "-c",
                "user.name=pin-test",
                "-c",
                "user.email=pin-test@example.invalid",
                "commit",
                "-q",
                "-m",
                "base",
            ],
        );
        let commit = git(dir.path(), &["rev-parse", "HEAD"]).trim().to_string();
        Self { dir, commit }
    }

    fn root(&self) -> &Path {
        self.dir.path()
    }

    fn blob(&self, rel: &str) -> String {
        git(
            self.root(),
            &["rev-parse", &format!("{}:{rel}", self.commit)],
        )
        .trim()
        .to_string()
    }
}

fn source_of<'a>(ancestor: &'a str, repo: &'a Repo) -> PinSource<'a> {
    PinSource {
        project_id: ancestor,
        root: repo.root(),
        base_commit: &repo.commit,
    }
}

fn write_file(root: &Path, rel: &str, body: &str) {
    let path = root.join(rel);
    fs::create_dir_all(path.parent().expect("parent")).expect("create dirs");
    fs::write(path, body).expect("write file");
}

struct Fixture {
    conn: postgres::Client,
    machine_id: String,
    projects: Vec<String>,
}

impl Fixture {
    fn new() -> Self {
        let database_url = crate::test_env::postgres_test_database_url("gcode pin tests");
        let conn = db::connect_readwrite(&database_url).expect("connect");
        let machine_id = gobby_core::machine::read_local_machine_id().expect("machine");
        Self {
            conn,
            machine_id,
            projects: Vec::new(),
        }
    }

    fn project(&mut self, prefix: &str) -> String {
        let id = unique_id(prefix);
        self.register(&id);
        id
    }

    fn register(&mut self, id: &str) {
        api::upsert_project_stats(
            &mut self.conn,
            &self.machine_id,
            &IndexedProject {
                id: id.to_string(),
                root_path: format!("/tmp/{id}"),
                total_files: 0,
                total_symbols: 0,
                last_indexed_at: String::new(),
                index_duration_ms: 0,
                total_eligible_files: None,
                indexer_version: None,
            },
            api::IndexWriteMode::Overlay,
            true,
        )
        .expect("seed project");
        self.projects.push(id.to_string());
    }

    fn checkout(&mut self, project: &str, root: &Path) {
        seed_test_checkout(&mut self.conn, project, root).expect("seed checkout");
    }

    /// Index one file and record its selector with the given Git blob ID.
    fn index_selector(
        &mut self,
        project: &str,
        root: &Path,
        rel: &str,
        hash: &str,
        blob: Option<&str>,
    ) {
        let file = indexed_file(project, rel, hash);
        api::upsert_file(&mut self.conn, &file).expect("index file");
        api::file_state::upsert_file_state(
            &mut self.conn,
            &self.machine_id,
            &file,
            root,
            api::IndexWriteMode::Overlay,
        )
        .expect("write selector");
        self.set_blob(project, rel, hash, blob);
    }

    fn set_blob(&mut self, project: &str, rel: &str, hash: &str, blob: Option<&str>) {
        api::file_state::set_selector_git_blob(
            &mut self.conn,
            &self.machine_id,
            project,
            rel,
            hash,
            blob,
        )
        .expect("record selector blob");
    }

    /// Write an overlay pin row directly, as an author overlay's earlier pin would have.
    fn insert_pin(&mut self, overlay: &str, source: &str, rel: &str, hash: &str, blob: &str) {
        self.conn
            .execute(
                "INSERT INTO code_overlay_pins (
                    machine_id, overlay_project_id, file_path, source_project_id,
                    content_hash, git_blob_id
                 ) VALUES ($1, $2, $3, $4, $5, $6)",
                &[
                    &db::id_param(&self.machine_id).expect("machine id"),
                    &db::id_param(overlay).expect("overlay id"),
                    &rel,
                    &db::id_param(source).expect("source id"),
                    &hash,
                    &blob,
                ],
            )
            .expect("insert pin row");
    }

    fn drop_checkout(&mut self, project: &str) {
        self.conn
            .execute(
                "DELETE FROM project_checkouts WHERE machine_id = $1 AND project_id = $2",
                &[
                    &db::id_param(&self.machine_id).expect("machine id"),
                    &db::id_param(project).expect("project id"),
                ],
            )
            .expect("drop checkout");
    }

    fn pin_rows(&mut self, overlay: &str) -> Vec<(String, String, String, Option<String>)> {
        self.conn
            .query(
                "SELECT file_path, source_project_id::text, content_hash, git_blob_id
                 FROM code_overlay_pins
                 WHERE overlay_project_id = $1
                 ORDER BY file_path",
                &[&db::id_param(overlay).expect("overlay id")],
            )
            .expect("read pin rows")
            .iter()
            .map(|row| (row.get(0), row.get(1), row.get(2), row.get(3)))
            .collect()
    }
}

impl Drop for Fixture {
    fn drop(&mut self) {
        for project in &self.projects {
            let Ok(id) = db::id_param(project) else {
                continue;
            };
            for sql in [
                "DELETE FROM code_overlay_pins WHERE overlay_project_id = $1 OR source_project_id = $1",
                "DELETE FROM code_indexed_file_states WHERE project_id = $1",
                "DELETE FROM code_indexed_files WHERE project_id = $1",
                "DELETE FROM code_indexed_project_states WHERE project_id = $1",
                "DELETE FROM project_checkouts WHERE project_id = $1",
                "DELETE FROM projects WHERE id = $1",
            ] {
                let _ = self.conn.execute(sql, &[&id]);
            }
        }
    }
}

#[test]
#[serial_test::serial(serial_db)]
#[cfg_attr(
    not(gcode_postgres_tests),
    ignore = "requires a PostgreSQL test database URL"
)]
fn pin_inherits_effective_selectors() {
    let repo = Repo::new(&[
        ("src/clean.rs", "pub fn clean() {}\n"),
        ("src/inherited.rs", "pub fn inherited() {}\n"),
    ]);
    let mut fx = Fixture::new();
    let ancestor = fx.project("pin-ancestor");
    let root = fx.project("pin-root");
    let child = fx.project("pin-child");
    fx.checkout(&ancestor, repo.root());
    fx.index_selector(
        &ancestor,
        repo.root(),
        "src/clean.rs",
        "hash-clean",
        Some(&repo.blob("src/clean.rs")),
    );
    // The ancestor is itself an overlay: its pin supplies `src/inherited.rs` from the root.
    api::upsert_file(
        &mut fx.conn,
        &indexed_file(&root, "src/inherited.rs", "hash-inherited"),
    )
    .expect("index root file");
    fx.insert_pin(
        &ancestor,
        &root,
        "src/inherited.rs",
        "hash-inherited",
        &repo.blob("src/inherited.rs"),
    );

    let report = apply_pin(
        &mut fx.conn,
        &fx.machine_id.clone(),
        &child,
        Some(PinSource {
            project_id: &ancestor,
            root: repo.root(),
            base_commit: &repo.commit,
        }),
    )
    .expect("pin child");

    assert!(report.warm, "expected a warm pin, got {report:?}");
    assert_eq!(
        report.pinned,
        vec!["src/clean.rs".to_string(), "src/inherited.rs".to_string()]
    );
    assert_eq!(
        fx.pin_rows(&child),
        vec![
            (
                "src/clean.rs".to_string(),
                ancestor.clone(),
                "hash-clean".to_string(),
                Some(repo.blob("src/clean.rs")),
            ),
            (
                "src/inherited.rs".to_string(),
                root.clone(),
                "hash-inherited".to_string(),
                Some(repo.blob("src/inherited.rs")),
            ),
        ]
    );
}

#[test]
#[serial_test::serial(serial_db)]
#[cfg_attr(
    not(gcode_postgres_tests),
    ignore = "requires a PostgreSQL test database URL"
)]
fn pin_records_only_safe_selectors_and_gaps() {
    let repo = Repo::new(&[
        ("src/clean.rs", "pub fn clean() {}\n"),
        ("src/dirty.rs", "pub fn dirty() {}\n"),
        ("src/mismatch.rs", "pub fn mismatch() {}\n"),
        ("src/never.rs", "pub fn never() {}\n"),
    ]);
    // Untracked and ignored files are never in the commit's tree.
    write_file(repo.root(), "src/untracked.rs", "pub fn untracked() {}\n");
    write_file(repo.root(), "src/ignored.rs", "pub fn ignored() {}\n");
    fs::write(repo.root().join(".git/info/exclude"), "src/ignored.rs\n").expect("exclude");
    // The ancestor's working tree now differs from HEAD for `src/dirty.rs`.
    write_file(repo.root(), "src/dirty.rs", "pub fn dirty_edited() {}\n");

    let mut fx = Fixture::new();
    let ancestor = fx.project("pin-ancestor");
    let child = fx.project("pin-child");
    fx.checkout(&ancestor, repo.root());
    let root = repo.root().to_path_buf();
    fx.index_selector(
        &ancestor,
        &root,
        "src/clean.rs",
        "hash-clean",
        Some(&repo.blob("src/clean.rs")),
    );
    fx.index_selector(&ancestor, &root, "src/dirty.rs", "hash-dirty", None);
    fx.index_selector(
        &ancestor,
        &root,
        "src/mismatch.rs",
        "hash-mismatch",
        Some(MISMATCHED_BLOB),
    );
    fx.index_selector(&ancestor, &root, "src/untracked.rs", "hash-untracked", None);
    fx.index_selector(&ancestor, &root, "src/ignored.rs", "hash-ignored", None);

    let report = apply_pin(
        &mut fx.conn,
        &fx.machine_id.clone(),
        &child,
        Some(PinSource {
            project_id: &ancestor,
            root: repo.root(),
            base_commit: &repo.commit,
        }),
    )
    .expect("pin child");

    assert_eq!(report.pinned, vec!["src/clean.rs".to_string()]);
    assert_eq!(
        report.gaps,
        vec![
            "src/dirty.rs".to_string(),
            "src/mismatch.rs".to_string(),
            "src/never.rs".to_string(),
        ]
    );
    assert_eq!(
        fx.pin_rows(&child).len(),
        1,
        "only the clean selector is pinned"
    );
}

#[test]
#[serial_test::serial(serial_db)]
#[cfg_attr(
    not(gcode_postgres_tests),
    ignore = "requires a PostgreSQL test database URL"
)]
fn pin_cold_and_replacement() {
    let repo = Repo::new(&[
        ("src/clean.rs", "pub fn clean() {}\n"),
        ("src/dirty.rs", "pub fn dirty() {}\n"),
    ]);
    let mut fx = Fixture::new();
    let ancestor = fx.project("pin-ancestor");
    let child = fx.project("pin-child");
    fx.checkout(&ancestor, repo.root());
    fx.index_selector(
        &ancestor,
        repo.root(),
        "src/clean.rs",
        "hash-clean",
        Some(&repo.blob("src/clean.rs")),
    );
    fx.index_selector(
        &ancestor,
        repo.root(),
        "src/dirty.rs",
        "hash-dirty",
        Some(&repo.blob("src/dirty.rs")),
    );
    let machine_id = fx.machine_id.clone();

    let first = apply_pin(
        &mut fx.conn,
        &machine_id,
        &child,
        Some(source_of(&ancestor, &repo)),
    )
    .expect("first pin");
    assert!(first.warm, "expected a warm pin, got {first:?}");
    assert_eq!(
        first.pinned,
        vec!["src/clean.rs".to_string(), "src/dirty.rs".to_string()]
    );
    assert!(first.gaps.is_empty());

    // The ancestor edits `src/dirty.rs`, clearing its blob ID. A re-pin replaces the old row.
    fx.set_blob(&ancestor, "src/dirty.rs", "hash-dirty", None);
    let second = apply_pin(
        &mut fx.conn,
        &machine_id,
        &child,
        Some(source_of(&ancestor, &repo)),
    )
    .expect("re-pin");
    assert_eq!(second.pinned, vec!["src/clean.rs".to_string()]);
    assert_eq!(second.gaps, vec!["src/dirty.rs".to_string()]);
    assert_eq!(
        fx.pin_rows(&child).len(),
        1,
        "the stale dirty row is replaced"
    );

    // With no live indexed checkout the pin is cold: nothing is inherited and every tracked path is a gap.
    fx.drop_checkout(&ancestor);
    let cold = apply_pin(
        &mut fx.conn,
        &machine_id,
        &child,
        Some(source_of(&ancestor, &repo)),
    )
    .expect("cold pin");
    assert!(!cold.warm, "expected a cold pin, got {cold:?}");
    assert_eq!(cold.reason, Some("no_live_checkout"));
    assert!(cold.pinned.is_empty());
    assert_eq!(
        cold.gaps,
        vec!["src/clean.rs".to_string(), "src/dirty.rs".to_string()]
    );
    assert!(fx.pin_rows(&child).is_empty());

    // An unresolvable selected commit and a missing marker are cold pins as well.
    fx.checkout(&ancestor, repo.root());
    let missing_commit = apply_pin(
        &mut fx.conn,
        &machine_id,
        &child,
        Some(PinSource {
            project_id: &ancestor,
            root: repo.root(),
            base_commit: MISSING_COMMIT,
        }),
    )
    .expect("cold pin for missing commit");
    assert_eq!(missing_commit.reason, Some("unresolvable_commit"));
    assert!(fx.pin_rows(&child).is_empty());

    let no_provenance =
        apply_pin(&mut fx.conn, &machine_id, &child, None).expect("cold pin without provenance");
    assert_eq!(no_provenance.reason, Some("no_pin_provenance"));
    assert!(no_provenance.pinned.is_empty());
}

#[test]
fn derive_gaps_lists_tracked_paths_without_pins() {
    let tracked: BTreeSet<String> = ["src/a.rs", "src/b.rs", "src/c.rs"]
        .into_iter()
        .map(String::from)
        .collect();
    let pinned: BTreeSet<String> = ["src/b.rs"].into_iter().map(String::from).collect();

    assert_eq!(
        derive_gaps(&tracked, &pinned),
        vec!["src/a.rs".to_string(), "src/c.rs".to_string()]
    );
    // A cold pin pins nothing, so every tracked path is a gap.
    assert_eq!(
        derive_gaps(&tracked, &BTreeSet::new()),
        vec![
            "src/a.rs".to_string(),
            "src/b.rs".to_string(),
            "src/c.rs".to_string(),
        ]
    );
}

/// Revokes the issued principal and removes its agent rows before the fixture drops the projects.
struct PrincipalCleanup {
    database_url: String,
    execution_id: uuid::Uuid,
    session_id: uuid::Uuid,
    parent_id: String,
}

impl Drop for PrincipalCleanup {
    fn drop(&mut self) {
        if let Ok(mut owner) = db::connect_readwrite(&self.database_url) {
            let _ = owner.query_opt(
                "SELECT gobby_agent_auth.revoke_principal($1, 1)",
                &[&self.execution_id],
            );
            if let Ok(parent) = db::id_param(&self.parent_id) {
                let _ = owner.execute("DELETE FROM worktrees WHERE project_id = $1", &[&parent]);
            }
            let _ = owner.execute("DELETE FROM sessions WHERE id = $1", &[&self.session_id]);
        }
    }
}

fn principal_pin_rows(
    scoped: &mut postgres::Client,
    overlay_project_id: &str,
) -> Vec<(String, String, String, Option<String>)> {
    scoped
        .query(
            "SELECT file_path, source_project_id::text, content_hash, git_blob_id
             FROM code_overlay_pins WHERE overlay_project_id = $1 ORDER BY file_path",
            &[&db::id_param(overlay_project_id).expect("overlay project uuid")],
        )
        .expect("read pins as the scoped principal")
        .iter()
        .map(|row| (row.get(0), row.get(1), row.get(2), row.get(3)))
        .collect()
}

#[test]
#[serial_test::serial(serial_db)]
#[cfg_attr(
    not(gcode_postgres_tests),
    ignore = "requires a PostgreSQL test database URL"
)]
fn pin_writes_under_scoped_capability_role() {
    let database_url = crate::test_env::postgres_test_database_url("gcode pin tests");
    let repo = Repo::new(&[
        ("src/clean.rs", "pub fn clean() {}\n"),
        ("src/sibling.rs", "pub fn sibling() {}\n"),
    ]);
    let mut fx = Fixture::new();
    let parent = fx.project("pin-parent");
    fx.checkout(&parent, repo.root());
    fx.index_selector(
        &parent,
        repo.root(),
        "src/clean.rs",
        "hash-clean",
        Some(&repo.blob("src/clean.rs")),
    );

    // The bound overlay is a worktree of the parent. The sibling is another active worktree of
    // the same parent: readable by the principal, but never a pin source for it.
    let overlay_dir = tempfile::tempdir().expect("overlay dir");
    let sibling_dir = tempfile::tempdir().expect("sibling dir");
    let overlay_root = overlay_dir.path().to_string_lossy().into_owned();
    let sibling_root = sibling_dir.path().to_string_lossy().into_owned();
    let machine_id = fx.machine_id.clone();
    let machine_uuid = db::id_param(&machine_id).expect("machine uuid");
    let parent_uuid = db::id_param(&parent).expect("parent uuid");
    let sibling_id: String = fx
        .conn
        .query_one(
            "SELECT gobby_agent_auth.code_index_project_id($1)::text",
            &[&sibling_root],
        )
        .expect("derive sibling overlay id")
        .get(0);
    fx.register(&sibling_id);
    // The sibling's live checkout row names its own worktree; the git tree comes from the repo.
    let sibling_uuid = db::id_param(&sibling_id).expect("sibling overlay uuid");
    fx.conn
        .execute(
            "INSERT INTO projects (id, name) VALUES ($1, $2) ON CONFLICT (id) DO NOTHING",
            &[&sibling_uuid, &format!("gcode-test-{sibling_id}")],
        )
        .expect("seed sibling registry row");
    fx.conn
        .execute(
            "INSERT INTO project_checkouts (machine_id, project_id, root_path)
             VALUES ($1, $2, $3)",
            &[&machine_uuid, &sibling_uuid, &sibling_root],
        )
        .expect("seed sibling checkout row");
    fx.index_selector(
        &sibling_id,
        repo.root(),
        "src/sibling.rs",
        "hash-sibling",
        Some(&repo.blob("src/sibling.rs")),
    );

    let session_id = uuid::Uuid::new_v4();
    let execution_id = uuid::Uuid::new_v4();
    let password = format!("gobby-pin-scoped-{}", uuid::Uuid::new_v4().simple());
    fx.conn
        .execute(
            "INSERT INTO sessions(id, external_id, machine_id, source, project_id)
             VALUES ($1, $2, $3, 'test', $4)",
            &[
                &session_id,
                &format!("pin-scoped-{session_id}"),
                &machine_uuid,
                &parent_uuid,
            ],
        )
        .expect("seed pin principal session");
    fx.conn
        .execute(
            "INSERT INTO worktrees(
                 id, project_id, machine_id, branch_name, worktree_path, agent_session_id, status
             ) VALUES ($1, $2, $3, 'pin-overlay', $4, $5, 'active')",
            &[
                &uuid::Uuid::new_v4(),
                &parent_uuid,
                &machine_uuid,
                &overlay_root,
                &session_id,
            ],
        )
        .expect("seed bound overlay worktree");
    fx.conn
        .execute(
            "INSERT INTO worktrees(
                 id, project_id, machine_id, branch_name, worktree_path, status
             ) VALUES ($1, $2, $3, 'pin-sibling', $4, 'active')",
            &[
                &uuid::Uuid::new_v4(),
                &parent_uuid,
                &machine_uuid,
                &sibling_root,
            ],
        )
        .expect("seed sibling worktree");
    let _cleanup = PrincipalCleanup {
        database_url: database_url.clone(),
        execution_id,
        session_id,
        parent_id: parent.clone(),
    };
    let role_name: String = fx
        .conn
        .query_one(
            "SELECT role_name::TEXT FROM gobby_agent_auth.issue_tool_principal(
                 $1, $2, $3, clock_timestamp() + INTERVAL '10 minutes', $4
             )",
            &[&execution_id, &session_id, &machine_uuid, &password],
        )
        .expect("issue scoped pin principal")
        .get(0);
    let bound_overlay: String = fx
        .conn
        .query_one(
            "SELECT code_overlay_project_id::text
             FROM gobby_agent_auth.principal_bindings
             WHERE managed_execution_id = $1",
            &[&execution_id],
        )
        .expect("read bound overlay")
        .get(0);
    let mut config = database_url
        .parse::<postgres::Config>()
        .expect("parse test database URL");
    config.user(&role_name).password(&password);
    let mut scoped = config
        .connect(postgres::NoTls)
        .expect("connect as the issued pin principal");

    let warm = apply_pin(
        &mut scoped,
        &machine_id,
        &bound_overlay,
        Some(PinSource {
            project_id: &parent,
            root: repo.root(),
            base_commit: &repo.commit,
        }),
    )
    .expect("scoped pin from the parent");
    assert!(warm.warm, "expected a warm pin, got {warm:?}");
    assert_eq!(warm.pinned, vec!["src/clean.rs".to_string()]);
    let pinned_rows = principal_pin_rows(&mut scoped, &bound_overlay);
    assert_eq!(
        pinned_rows,
        vec![(
            "src/clean.rs".to_string(),
            parent.clone(),
            "hash-clean".to_string(),
            Some(repo.blob("src/clean.rs")),
        )]
    );

    // Row-level security is the backstop: a pin row naming the sibling as its source is refused,
    // and the warm pin it would have replaced stays intact.
    let bound_overlay_uuid = db::id_param(&bound_overlay).expect("bound overlay uuid");
    let error = scoped
        .execute(
            "INSERT INTO code_overlay_pins (
                machine_id, overlay_project_id, file_path, source_project_id,
                content_hash, git_blob_id
             ) VALUES ($1, $2, $3, $4, $5, $6)",
            &[
                &machine_uuid,
                &bound_overlay_uuid,
                &"src/sibling.rs",
                &sibling_uuid,
                &"hash-sibling",
                &repo.blob("src/sibling.rs"),
            ],
        )
        .expect_err("a sibling overlay is not a pin source for this principal");
    assert_eq!(
        error.code(),
        Some(&SqlState::INSUFFICIENT_PRIVILEGE),
        "a sibling pin row must fail row-level security, got {error}",
    );
    assert_eq!(
        principal_pin_rows(&mut scoped, &bound_overlay),
        pinned_rows,
        "the refused insert leaves the warm pin intact",
    );

    // The sibling checkout row is hidden from this principal, so apply_pin reads the source as
    // not live and derives a cold pin before any write: no inherited rows, every tracked path a gap.
    let cold = apply_pin(
        &mut scoped,
        &machine_id,
        &bound_overlay,
        Some(PinSource {
            project_id: &sibling_id,
            root: repo.root(),
            base_commit: &repo.commit,
        }),
    )
    .expect("a sibling source degrades to a cold pin under row-level security");
    assert!(
        !cold.warm,
        "a sibling overlay is never a warm pin source: {cold:?}"
    );
    assert_eq!(cold.reason, Some("no_live_checkout"));
    assert!(cold.pinned.is_empty(), "{cold:?}");
    assert_eq!(
        cold.gaps,
        vec!["src/clean.rs".to_string(), "src/sibling.rs".to_string()],
    );
    assert!(
        principal_pin_rows(&mut scoped, &bound_overlay).is_empty(),
        "the cold replacement clears the inherited rows",
    );
}
