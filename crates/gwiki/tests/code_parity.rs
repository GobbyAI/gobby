//! Parity checks for the relocated CodeWiki engine. The script-driven harness
//! (`deterministic_output_matches_baseline`, `production_vault_untouched`,
//! `gwiki_mode_capture_discipline`) needs a migrated PostgreSQL hub, git, and a
//! nested `cargo build --locked`; it runs only when `CODEWIKI_PARITY_HARNESS=1`
//! is exported. The remaining tests are self-contained and always run.

use std::collections::{BTreeMap, BTreeSet};
use std::ffi::OsStr;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::{Command, Output};
use std::sync::OnceLock;

use tempfile::TempDir;

const PARITY_PROJECT_ID: &str = "019fe500-0000-7000-8000-000000019814";

struct ParityRun {
    _temp: TempDir,
    root: PathBuf,
    output: Output,
    baseline: String,
    generated: String,
    capture: String,
    fixture_before: String,
    fixture_after: String,
    production_before: BTreeMap<String, Vec<u8>>,
    production_after: BTreeMap<String, Vec<u8>>,
}

static PARITY_RUN: OnceLock<Result<ParityRun, String>> = OnceLock::new();

fn repo_root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../..")
        .canonicalize()
        .expect("repository root resolves")
}

fn command_output(mut command: Command) -> Result<Output, String> {
    command
        .output()
        .map_err(|error| format!("failed to run {command:?}: {error}"))
}

fn git_fixture_status(repo: &Path) -> Result<String, String> {
    let output = command_output({
        let mut command = Command::new("git");
        command.arg("-C").arg(repo).args([
            "status",
            "--porcelain",
            "--",
            "crates/gwiki/tests/fixtures/codewiki_parity",
        ]);
        command
    })?;
    if !output.status.success() {
        return Err(String::from_utf8_lossy(&output.stderr).into_owned());
    }
    String::from_utf8(output.stdout).map_err(|error| error.to_string())
}

fn tree_bytes(root: &Path) -> Result<BTreeMap<String, Vec<u8>>, String> {
    fn visit(
        root: &Path,
        current: &Path,
        snapshot: &mut BTreeMap<String, Vec<u8>>,
    ) -> Result<(), String> {
        if !current.exists() {
            return Ok(());
        }
        for entry in fs::read_dir(current).map_err(|error| error.to_string())? {
            let entry = entry.map_err(|error| error.to_string())?;
            let path = entry.path();
            let relative = path
                .strip_prefix(root)
                .map_err(|error| error.to_string())?
                .to_string_lossy()
                .replace('\\', "/");
            let file_type = entry.file_type().map_err(|error| error.to_string())?;
            if file_type.is_dir() {
                snapshot.insert(format!("{relative}/"), Vec::new());
                visit(root, &path, snapshot)?;
            } else if file_type.is_symlink() {
                snapshot.insert(
                    relative,
                    fs::read_link(&path)
                        .map_err(|error| error.to_string())?
                        .to_string_lossy()
                        .into_owned()
                        .into_bytes(),
                );
            } else {
                snapshot.insert(
                    relative,
                    fs::read(&path).map_err(|error| error.to_string())?,
                );
            }
        }
        Ok(())
    }

    let mut snapshot = BTreeMap::new();
    visit(root, root, &mut snapshot)?;
    Ok(snapshot)
}

fn harness_enabled() -> bool {
    std::env::var_os("CODEWIKI_PARITY_HARNESS").is_some_and(|value| value == "1")
}

fn execute_parity_run() -> Result<ParityRun, String> {
    let repo = repo_root();
    let root = tempfile::tempdir().map_err(|error| error.to_string())?;
    let production_vault = repo.join("wiki");
    let fixture_before = git_fixture_status(&repo)?;
    let production_before = tree_bytes(&production_vault)?;
    let output = command_output({
        let mut command = Command::new(repo.join("scripts/codewiki_parity_baseline.sh"));
        command
            .args(["--engine", "gwiki"])
            .env("CODEWIKI_PARITY_RUN_ROOT", root.path())
            .env("CODEWIKI_PARITY_PRODUCTION_VAULT", &production_vault)
            // A dedicated target dir keeps the script's nested cargo build off
            // the lock this `cargo test` invocation already holds.
            .env(
                "CODEWIKI_PARITY_TARGET_DIR",
                repo.join("target/codewiki-parity"),
            );
        command
    })?;
    let fixture_after = git_fixture_status(&repo)?;
    let production_after = tree_bytes(&production_vault)?;
    let baseline = fs::read_to_string(
        repo.join("crates/gwiki/tests/fixtures/codewiki_parity/baseline.sha256"),
    )
    .map_err(|error| error.to_string())?;
    let generated = fs::read_to_string(root.path().join("run-1/manifest.sha256"))
        .map_err(|error| error.to_string())?;
    let capture =
        fs::read_to_string(root.path().join("capture.txt")).map_err(|error| error.to_string())?;

    Ok(ParityRun {
        root: root.path().to_path_buf(),
        _temp: root,
        output,
        baseline,
        generated,
        capture,
        fixture_before,
        fixture_after,
        production_before,
        production_after,
    })
}

fn parity_run() -> &'static ParityRun {
    match PARITY_RUN.get_or_init(execute_parity_run) {
        Ok(run) => run,
        Err(error) => panic!("parity harness setup failed: {error}"),
    }
}

fn assert_run_succeeded(run: &ParityRun) {
    assert!(
        run.output.status.success(),
        "parity script failed\nstdout:\n{}\nstderr:\n{}",
        String::from_utf8_lossy(&run.output.stdout),
        String::from_utf8_lossy(&run.output.stderr),
    );
}

fn manifest_entries(manifest: &str) -> BTreeMap<&str, &str> {
    manifest
        .lines()
        .map(|line| {
            let (digest, path) = line
                .split_once("  ")
                .unwrap_or_else(|| panic!("invalid manifest line: {line}"));
            (path, digest)
        })
        .collect()
}

fn manifest_diff(expected: &str, actual: &str) -> String {
    let expected = manifest_entries(expected);
    let actual = manifest_entries(actual);
    expected
        .keys()
        .chain(actual.keys())
        .copied()
        .collect::<BTreeSet<_>>()
        .into_iter()
        .filter(|path| expected.get(path) != actual.get(path))
        .map(|path| format!("{path}: {:?} != {:?}", expected.get(path), actual.get(path)))
        .collect::<Vec<_>>()
        .join("\n")
}

#[test]
fn deterministic_output_matches_baseline() {
    if !harness_enabled() {
        eprintln!("skipping: CODEWIKI_PARITY_HARNESS is not set to 1");
        return;
    }
    let run = parity_run();
    assert_run_succeeded(run);
    assert_eq!(
        run.baseline,
        run.generated,
        "normalized parity paths differ:\n{}",
        manifest_diff(&run.baseline, &run.generated),
    );
}

#[test]
fn production_vault_untouched() {
    if !harness_enabled() {
        eprintln!("skipping: CODEWIKI_PARITY_HARNESS is not set to 1");
        return;
    }
    let run = parity_run();
    assert_run_succeeded(run);
    assert_eq!(run.production_before, run.production_after);
}

#[test]
fn gwiki_mode_capture_discipline() {
    if !harness_enabled() {
        eprintln!("skipping: CODEWIKI_PARITY_HARNESS is not set to 1");
        return;
    }
    let run = parity_run();
    assert_run_succeeded(run);
    assert!(run.fixture_before.is_empty(), "{}", run.fixture_before);
    assert_eq!(
        run.fixture_before, run.fixture_after,
        "{}",
        run.fixture_after
    );

    let readme = fs::read_to_string(
        repo_root().join("crates/gwiki/tests/fixtures/codewiki_parity/README.md"),
    )
    .expect("fixture README is readable");
    let pinned_digest = readme
        .split("Pinned fixture input digest: `")
        .nth(1)
        .and_then(|tail| tail.split('`').next())
        .expect("README pins the fixture digest");
    assert!(run.capture.contains("engine: gwiki\n"));
    assert!(
        run.capture
            .contains(&format!("input_digest: {pinned_digest}\n"))
    );
    assert!(
        run.capture
            .contains(&format!("project_id: {PARITY_PROJECT_ID}\n"))
    );
    assert!(run.capture.contains("build: cargo build --locked\n"));

    let identity: serde_json::Value = serde_json::from_slice(
        &fs::read(run.root.join("project/.gobby/gcode.json")).expect("identity is readable"),
    )
    .expect("identity is valid JSON");
    assert_eq!(identity["id"], PARITY_PROJECT_ID);

    let mut root_files = fs::read_dir(&run.root)
        .expect("run root is readable")
        .filter_map(Result::ok)
        .filter(|entry| entry.file_type().is_ok_and(|kind| kind.is_file()))
        .map(|entry| entry.file_name())
        .collect::<Vec<_>>();
    root_files.sort();
    assert_eq!(root_files, [OsStr::new("capture.txt")]);
    assert!(
        include_str!("../../../scripts/codewiki_parity_baseline.sh")
            .contains("cargo build --locked")
    );
}

struct ModeArtifact {
    mode: &'static str,
    test_name: &'static str,
    source: &'static str,
}

#[test]
fn legacy_mode_matrix() {
    let artifacts = [
        ModeArtifact {
            mode: "--compare-to",
            test_name: "compare_to_matches_path_sorted_json_goldens_without_writing_pages",
            source: include_str!("../src/commands/code/tests/incremental.rs"),
        },
        ModeArtifact {
            mode: "--purge --force",
            test_name: "purge_removes_generated_docs_and_metadata_only",
            source: include_str!("../src/commands/code/tests/purge.rs"),
        },
        ModeArtifact {
            mode: "--repair-citations",
            test_name: "repair_reanchors_moved_citation_and_counts_unresolved",
            source: include_str!("../src/commands/code/tests/repair.rs"),
        },
        ModeArtifact {
            mode: "--scope",
            test_name: "scoped_incremental_write_preserves_out_of_scope_docs_and_meta",
            source: include_str!("../src/commands/code/tests/incremental.rs"),
        },
        ModeArtifact {
            mode: "--since",
            test_name: "since_scopes_regeneration_to_changed_files_and_preserves_the_rest",
            source: include_str!("../src/commands/code/tests/invalidation.rs"),
        },
    ];
    let mut distinct = BTreeSet::new();
    for artifact in &artifacts {
        assert!(
            artifact
                .source
                .contains(&format!("fn {}(", artifact.test_name)),
            "{} must remain pinned to {}",
            artifact.mode,
            artifact.test_name,
        );
        distinct.insert(artifact.test_name);
    }
    assert_eq!(distinct.len(), artifacts.len());
}

#[test]
fn lock_contention_matches_legacy() {
    let tests = include_str!("../src/commands/code/tests/lock.rs");
    let implementation = include_str!("../src/commands/code/lock.rs");
    assert!(
        tests.contains("fn second_sink_on_same_out_dir_is_refused_while_the_first_holds_the_lock(")
    );
    assert!(tests.contains("another `gwiki code` run is already writing"));
    assert!(implementation.contains("CODE_WRITER_LOCK_TIMEOUT"));
    assert!(implementation.contains("Duration::from_secs(2)"));
    assert!(implementation.contains("started.elapsed() >= timeout"));
}

fn initialize_failure_project() -> TempDir {
    let temp = tempfile::tempdir().expect("failure fixture tempdir");
    let root = temp.path();
    fs::create_dir_all(root.join(".gobby")).expect("identity directory created");
    fs::create_dir_all(root.join("wiki/_meta")).expect("metadata directory created");
    fs::write(
        root.join(".gobby/gcode.json"),
        format!("{{\"id\":\"{PARITY_PROJECT_ID}\",\"name\":\"parity-failures\"}}\n"),
    )
    .expect("identity written");
    fs::write(
        root.join("wiki/_meta/codewiki.json"),
        "{\"docs\":{},\"generated_docs\":[],\"commit\":null,\"commit_dirty\":false,\"ai_mode\":\"\"}\n",
    )
    .expect("current metadata written");
    let init = Command::new("git")
        .arg("-C")
        .arg(root)
        .arg("init")
        .arg("--quiet")
        .output()
        .expect("git init runs");
    assert!(
        init.status.success(),
        "{}",
        String::from_utf8_lossy(&init.stderr)
    );
    temp
}

fn gwiki(project: &Path, args: &[&str]) -> Output {
    Command::new(env!("CARGO_BIN_EXE_gwiki"))
        .arg("--project")
        .arg(project)
        .arg("code")
        .args(args)
        .output()
        .expect("gwiki runs")
}

#[test]
fn failure_paths_match_legacy() {
    let project = initialize_failure_project();
    let current_vault = project.path().join("wiki");
    let before_compare = tree_bytes(&current_vault).expect("current vault snapshots");
    let compare = gwiki(
        project.path(),
        &["--out", "wiki", "--compare-to", "does-not-exist"],
    );
    assert!(!compare.status.success());
    let compare_error = String::from_utf8_lossy(&compare.stderr);
    assert!(
        compare_error
            .contains("codewiki compare ref 'does-not-exist' does not resolve to a commit"),
        "{compare_error}"
    );
    assert_eq!(
        before_compare,
        tree_bytes(&current_vault).expect("current vault snapshots after compare")
    );

    let complete_out = project.path().join("invalid-complete-scope");
    let complete = gwiki(
        project.path(),
        &[
            "--out",
            "invalid-complete-scope",
            "--complete-scope",
            "--no-ai",
            "--allow-stale",
        ],
    );
    assert!(!complete.status.success());
    let complete_error = String::from_utf8_lossy(&complete.stderr);
    assert!(
        complete_error.contains("--complete-scope requires at least one --scope path"),
        "{complete_error}"
    );
    assert!(
        !complete_out.exists(),
        "failure must not create partial output"
    );
}
