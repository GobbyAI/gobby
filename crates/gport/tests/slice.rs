//! Contracts for `gport check-slice` and `gport check-slice --report`, run black-box through the
//! built binary against temp git repositories.
//!
//! Each test builds a base commit on `0.9.0` (a map, a ledger, a record and the Cargo manifests
//! the registration rule reads), branches `slice` from it and adds commits. A refusal exits 1 with
//! `REFUSE` lines on stdout. No test expects exit 2.

use std::net::TcpListener;

use serde_json::{Value, json};

use crate::support::{Repo, Run, gport, gport_env};

const CHUNK: &str = "SLICE-A";
const OTHER: &str = "SLICE-B";
const MAP: &str = "docs/port/map-v2.json";
const LEDGER: &str = "docs/port/ledger.md";
const RECORD: &str = "docs/port/record.md";
const STATUS: &str = "docs/port/status/SLICE-A.json";
const FROZEN: &str = "crates/gseam/src/frozen.rs";
const LIB: &str = "crates/gseam/src/lib.rs";
const PY: &str = "src/gobby/foo.py";
const NOTE: &str = "fixture note";
/// The vocabulary a status refusal prints, as the spec names it.
const VOCABULARY: &str =
    "todo specced done partial stub needs-local deferred-local blocked( delegated dropped";
const ROOT_CARGO: &str = include_str!("fixtures/slice/root.toml");
const GCODE_CARGO: &str = include_str!("fixtures/slice/gcode.toml");
const GDAEMON_CARGO: &str = include_str!("fixtures/slice/gdaemon.toml");

// --- Fixture builders ---

/// A map chunk entry with the fields check-slice reads. `prefix` anchors its ledger selector.
fn chunk(id: &str, kind: &str, python: &str, prefix: &str) -> Value {
    json!({
        "id": id, "title": id, "kind": kind, "depends_on": [],
        "ledger_selectors": [format!("^{prefix}")], "python_edits": python,
    })
}

/// The map: `a` as given, and OTHER owning the SURF-B- rows.
fn map_json(a: Value) -> String {
    json!({"chunks": [a, chunk(OTHER, "family", "tests/ only.", "SURF-B-")]}).to_string()
}

/// Row keys `SURF-A-1..=a` and `SURF-B-1..=b`.
fn keys(a: usize, b: usize) -> Vec<String> {
    let a = (1..=a).map(|i| format!("SURF-A-{i}"));
    let b = (1..=b).map(|i| format!("SURF-B-{i}"));
    a.chain(b).collect()
}

/// A ledger document with one table row per key.
fn ledger(rows: &[String]) -> String {
    let mut text = String::from("# Parity ledger\n\n| Row | Status |\n| --- | --- |\n");
    for row in rows {
        text.push_str(&format!("| `{row}` | proxied | fixture row |\n"));
    }
    text
}

/// A record in the port grammar: the frozen seam block, then one Found Work block per chunk.
fn record(frozen: &[&str], found: &[String]) -> String {
    let mut text = String::from("# Port record\n\n## Frozen seam contract\n\n");
    text.push_str("<!-- gport:kept seam-contract -->\n### Frozen files\n\n");
    for path in frozen {
        text.push_str(&format!("- `{path}`\n"));
    }
    text.push_str("<!-- gport:end seam-contract -->\n\n## Found Work\n\n");
    for id in [CHUNK, OTHER] {
        text.push_str(&format!("### {id}\n\n<!-- gport:kept found:{id} -->\n"));
        for line in found.iter().filter(|_| id == CHUNK) {
            text.push_str(&format!("{line}\n"));
        }
        text.push_str(&format!("<!-- gport:end found:{id} -->\n\n"));
    }
    text
}

/// A Found Work line that names `path` in backticks.
fn fixed(path: &str) -> String {
    format!("- `{path}` is fixed in this slice.")
}

/// A base commit on `0.9.0` with the map, ledger, optional record and manifests; `slice` is
/// checked out from it.
fn base(a: Value, rows: &[String], text: Option<String>) -> Repo {
    let repo = Repo::new();
    repo.write(MAP, &map_json(a));
    repo.write(LEDGER, &ledger(rows));
    if let Some(text) = text {
        repo.write(RECORD, &text);
    }
    repo.write("Cargo.toml", ROOT_CARGO);
    repo.write("crates/gcode/Cargo.toml", GCODE_CARGO);
    repo.write("crates/gdaemon/Cargo.toml", GDAEMON_CARGO);
    repo.write(FROZEN, "pub const FROZEN: bool = true;\n");
    repo.write(LIB, "pub fn seam() {}\n");
    repo.commit("[gobby-#1] chore: slice fixture base");
    repo.git(&["checkout", "-q", "-b", "slice"]);
    repo
}

/// CHUNK (family) owns SURF-A-1 and SURF-A-2, so a clean run needs its status file.
fn owning(python: &str) -> Repo {
    base(
        chunk(CHUNK, "family", python, "SURF-A-"),
        &keys(2, 2),
        Some(record(&[FROZEN], &[])),
    )
}

/// CHUNK owns no ledger rows, so a clean run needs no status file.
fn rowless(kind: &str, python: &str) -> Repo {
    base(
        chunk(CHUNK, kind, python, "SURF-A-"),
        &keys(0, 2),
        Some(record(&[FROZEN], &[])),
    )
}

/// CHUNK owns six SURF-A rows and OTHER owns four SURF-B rows.
fn report_base() -> Repo {
    base(
        chunk(CHUNK, "family", "None.", "SURF-A-"),
        &keys(6, 4),
        Some(record(&[FROZEN], &[])),
    )
}

// --- Commits, runs and status files ---

/// Writes one file and commits it. Returns the full SHA.
fn add(repo: &Repo, path: &str, text: &str, subject: &str) -> String {
    repo.write(path, text);
    repo.commit(subject)
}

/// Sets the LIB seam to `n` and commits it.
fn touch(repo: &Repo, n: u8, subject: &str) -> String {
    add(
        repo,
        LIB,
        &format!("pub fn seam() -> u8 {{ {n} }}\n"),
        subject,
    )
}

/// Commits a status file at HEAD.
fn commit_status(repo: &Repo, text: &str) -> String {
    add(repo, STATUS, text, "[gobby-#12] docs: slice status")
}

/// Two commits on `slice`, red then green. Returns their full SHAs.
fn red_and_green(repo: &Repo) -> (String, String) {
    let red = touch(repo, 0, "[gobby-#12] test: failing slice case");
    let green = touch(repo, 1, "[gobby-#12] feat: slice implementation");
    (red, green)
}

/// The done-row evidence line the spec names.
fn done_evidence(red: &str, green: &str) -> String {
    format!("red {red} green {green} test cargo test -p gobby-port --test suite slice")
}

/// Red and green commits, then a status file whose SURF-A-1 row is `done` with evidence built
/// from the base, red and green SHAs, and whose SURF-A-2 row is partial.
fn done_run(evidence: fn(&str, &str, &str) -> String) -> Run {
    let repo = owning("None.");
    let base_sha = repo.git(&["rev-parse", "0.9.0"]);
    let (red, green) = red_and_green(&repo);
    let text = evidence(&base_sha, &red, &green);
    let rows = [
        ("SURF-A-1", "done", text.as_str()),
        ("SURF-A-2", "partial", NOTE),
    ];
    commit_status(&repo, &status(CHUNK, &rows));
    check(&repo, CHUNK, &["12"])
}

/// A status file for `id` with the given (row, status, evidence) rows.
fn status(id: &str, rows: &[(&str, &str, &str)]) -> String {
    let rows: Vec<Value> = rows
        .iter()
        .map(|(row, state, evidence)| json!({"row": row, "status": state, "evidence": evidence}))
        .collect();
    json!({"chunk": id, "rows": rows}).to_string()
}

/// Writes a status file into the working tree without committing it.
fn write_status(repo: &Repo, id: &str, rows: &[(&str, &str, &str)]) {
    repo.write(&format!("docs/port/status/{id}.json"), &status(id, rows));
}

/// Runs `gport check-slice <id> --task <ref>...` in the repository.
fn check(repo: &Repo, id: &str, tasks: &[&str]) -> Run {
    let mut args = vec!["check-slice", id];
    for task in tasks {
        args.extend(["--task", *task]);
    }
    gport(repo.path(), &args)
}

/// Runs `gport check-slice --report <id>...` in the repository.
fn report(repo: &Repo, ids: &[&str]) -> Run {
    let mut args = vec!["check-slice", "--report"];
    args.extend(ids.iter().copied());
    gport(repo.path(), &args)
}

/// The report fixture with both status files uncommitted in the working tree. CHUNK's fifth row
/// is `fifth`; the other rows are done, partial, needs-local and stub, and OTHER's are excluded
/// or done.
fn report_repo(fifth: &str) -> Repo {
    let repo = report_base();
    let a = [
        ("SURF-A-1", "done", NOTE),
        ("SURF-A-2", "done", NOTE),
        ("SURF-A-3", "partial", NOTE),
        ("SURF-A-4", "needs-local", NOTE),
        ("SURF-A-5", fifth, NOTE),
        ("SURF-A-6", "stub", NOTE),
    ];
    write_status(&repo, CHUNK, &a);
    let b = [
        ("SURF-B-1", "done", NOTE),
        ("SURF-B-2", "delegated", NOTE),
        ("SURF-B-3", "deferred-local", NOTE),
        ("SURF-B-4", "dropped", NOTE),
    ];
    write_status(&repo, OTHER, &b);
    repo
}

// --- Assertions ---

/// Every stdout line that starts with REFUSE.
fn refuse_lines(run: &Run) -> Vec<&str> {
    run.stdout
        .lines()
        .filter(|line| line.starts_with("REFUSE"))
        .collect()
}

/// The exit code's streams, for a failure message.
fn show(run: &Run) -> String {
    format!("stdout:\n{}\nstderr:\n{}", run.stdout, run.stderr)
}

/// Exit 1, and some REFUSE line contains one of `needles`.
fn assert_refused(run: &Run, needles: &[&str]) {
    assert_eq!(run.code, 1, "{}", show(run));
    let named = refuse_lines(run)
        .iter()
        .any(|line| needles.iter().any(|needle| line.contains(*needle)));
    assert!(
        named,
        "no REFUSE line names any of {needles:?}\n{}",
        show(run)
    );
}

/// Exit 0, and no REFUSE line.
fn assert_clean(run: &Run) {
    assert_eq!(run.code, 0, "{}", show(run));
    assert!(
        refuse_lines(run).is_empty(),
        "unexpected refusals\n{}",
        show(run)
    );
}

/// The seven-character short form of a full SHA, as a refusal line may print it.
fn short(sha: &str) -> &str {
    &sha[..7]
}

// --- Path rules: python_edits, Found Work and the frozen list ---

/// A `.py` path under src/gobby/ on a chunk whose python_edits starts with `python_edits` is
/// refused, and passes once a Found Work line under the chunk names it in backticks at HEAD.
fn python_refused_until_found_work(python_edits: &str) {
    let repo = rowless("family", python_edits);
    add(&repo, PY, "VALUE = 1\n", "[gobby-#12] fix: python change");
    assert_refused(&check(&repo, CHUNK, &["12"]), &[PY]);
    let found = record(&[FROZEN], &[fixed(PY)]);
    add(&repo, RECORD, &found, "[gobby-#12] docs: found work");
    assert_clean(&check(&repo, CHUNK, &["12"]));
}

/// Contract: a `.py` path on a `None` chunk is refused until Found Work names it.
#[test]
fn python_on_none_chunk_is_refused_until_found_work_names_it() {
    python_refused_until_found_work("None. The port adds no Python to this chunk.");
}

/// Contract: a `.py` path on a `tests/ only` chunk is refused until Found Work names it.
#[test]
fn python_on_tests_only_chunk_is_refused_until_found_work_names_it() {
    python_refused_until_found_work("tests/ only. Recorder and corpus.");
}

/// Contract: a `.py` path on a `tests/ and scripts/ only` chunk is refused until Found Work
/// names it.
#[test]
fn python_on_tests_and_scripts_only_chunk_is_refused_until_found_work_names_it() {
    python_refused_until_found_work("tests/ and scripts/ only. Fixtures and port scripts.");
}

/// Contract: a frozen-listed file is refused without a Found Work line and passes with one.
#[test]
fn frozen_file_is_refused_until_found_work_names_it() {
    let repo = rowless("family", "None.");
    add(
        &repo,
        FROZEN,
        "pub const FROZEN: bool = false;\n",
        "[gobby-#12] fix: frozen edit",
    );
    assert_refused(&check(&repo, CHUNK, &["12"]), &[FROZEN]);
    let found = record(&[FROZEN], &[fixed(FROZEN)]);
    add(&repo, RECORD, &found, "[gobby-#12] docs: found work");
    assert_clean(&check(&repo, CHUNK, &["12"]));
}

/// Contract: src/gobby/install/version_pins.py passes on a `None` chunk.
#[test]
fn version_pins_python_passes_on_none_chunk() {
    let repo = rowless("family", "None.");
    let path = "src/gobby/install/version_pins.py";
    add(&repo, path, "PINS = {}\n", "[gobby-#12] chore: pins");
    assert_clean(&check(&repo, CHUNK, &["12"]));
}

/// Contract: a non-Python template under src/gobby/install/shared/ passes on a `None` chunk.
#[test]
fn shared_template_passes_on_none_chunk() {
    let repo = rowless("family", "None.");
    let path = "src/gobby/install/shared/templates/slice-sample.yaml";
    add(
        &repo,
        path,
        "version: \"1.0.0\"\n",
        "[gobby-#12] chore: template",
    );
    assert_clean(&check(&repo, CHUNK, &["12"]));
}

/// Contract: a `.py` path on a chunk whose python_edits starts with `tests/, plus` is not refused.
#[test]
fn python_on_tests_plus_chunk_is_not_refused() {
    let repo = rowless("family", "tests/, plus D64's bug-fix commit.");
    add(&repo, PY, "VALUE = 1\n", "[gobby-#12] fix: python change");
    assert_clean(&check(&repo, CHUNK, &["12"]));
}

// --- Commit subjects ---

/// Contract: a commit whose subject lacks `[gobby-#N]` for a passed `--task` is refused.
#[test]
fn commit_without_task_tag_is_refused() {
    let repo = rowless("family", "None.");
    let subject = "fix: change with no task tag";
    let sha = touch(&repo, 2, subject);
    assert_refused(&check(&repo, CHUNK, &["12"]), &[short(&sha), subject]);
}

/// Contract: a commit naming another task than the passed `--task` is refused. The longer ref
/// `[gobby-#123]` does not satisfy `--task 12`.
#[test]
fn commit_naming_another_task_is_refused() {
    let repo = rowless("family", "None.");
    let subject = "[gobby-#123] feat: longer ref";
    let sha = touch(&repo, 2, subject);
    assert_refused(&check(&repo, CHUNK, &["12"]), &[short(&sha), subject]);
}

/// Contract: two `--task` refs admit commits tagged with either.
#[test]
fn two_task_refs_admit_commits_of_either() {
    let repo = rowless("family", "None.");
    touch(&repo, 2, "[gobby-#12] feat: first");
    touch(&repo, 3, "[gobby-#13] feat: second");
    assert_clean(&check(&repo, CHUNK, &["12", "13"]));
}

/// Contract: `--task 12` and `--task #12` behave the same. Both admit `[gobby-#12]`, and both
/// refuse `[gobby-#13]` with the same number of REFUSE lines.
#[test]
fn task_ref_with_or_without_hash_behaves_the_same() {
    let repo = rowless("family", "None.");
    touch(&repo, 2, "[gobby-#12] feat: tagged change");
    assert_clean(&check(&repo, CHUNK, &["12"]));
    assert_clean(&check(&repo, CHUNK, &["#12"]));
    let subject = "[gobby-#13] feat: other change";
    let sha = touch(&repo, 3, subject);
    let plain = check(&repo, CHUNK, &["12"]);
    let hashed = check(&repo, CHUNK, &["#12"]);
    assert_refused(&plain, &[short(&sha), subject]);
    assert_refused(&hashed, &[short(&sha), subject]);
    assert_eq!(refuse_lines(&plain).len(), refuse_lines(&hashed).len());
}

/// Contract: a merge commit is not checked for a subject, so an untagged merge in the range
/// passes. Its tagged parent commits are still checked.
#[test]
fn untagged_merge_commit_is_not_checked_for_subject() {
    let repo = rowless("family", "None.");
    touch(&repo, 2, "[gobby-#12] feat: slice change");
    repo.git(&["checkout", "-q", "-b", "side", "0.9.0"]);
    add(
        &repo,
        "crates/gseam/src/side.rs",
        "pub fn side() {}\n",
        "[gobby-#12] feat: side",
    );
    repo.git(&["checkout", "-q", "slice"]);
    repo.git(&["merge", "-q", "--no-ff", "-m", "Merge side work", "side"]);
    assert!(
        !repo
            .git(&["rev-list", "--merges", "0.9.0..HEAD"])
            .is_empty(),
        "no merge"
    );
    assert_clean(&check(&repo, CHUNK, &["12"]));
}

// --- Done-row evidence ---

/// Contract: a `done` row whose evidence has no red SHA is refused.
#[test]
fn done_row_without_red_is_refused() {
    let run = done_run(|_, _, green| format!("green {green} test cargo test slice"));
    assert_refused(&run, &[STATUS, "SURF-A-1"]);
}

/// Contract: a `done` row whose evidence has no green SHA is refused.
#[test]
fn done_row_without_green_is_refused() {
    let run = done_run(|_, red, _| format!("red {red} test cargo test slice"));
    assert_refused(&run, &[STATUS, "SURF-A-1"]);
}

/// Contract: a `done` row whose SHAs are outside the range is refused. The base commit `0.9.0`
/// is outside `0.9.0..HEAD`.
#[test]
fn done_row_sha_outside_range_is_refused() {
    let run = done_run(|base, _, green| format!("red {base} green {green} test cargo test slice"));
    assert_refused(&run, &[STATUS, "SURF-A-1"]);
}

/// Contract: a `done` row whose red commit is not an ancestor of its green commit is refused.
/// Both SHAs are in the range; the evidence swaps them.
#[test]
fn done_row_red_not_ancestor_of_green_is_refused() {
    let run = done_run(|_, red, green| format!("red {green} green {red} test cargo test slice"));
    assert_refused(&run, &[STATUS, "SURF-A-1"]);
}

/// Contract: `red <sha> green <sha> test <command>`, with both SHAs in the range and red an
/// ancestor of green, passes.
#[test]
fn done_row_with_red_and_green_in_range_passes() {
    assert_clean(&done_run(|_, red, green| done_evidence(red, green)));
}

/// Contract: a row at `todo` or `specced` is refused.
#[test]
fn todo_or_specced_row_is_refused() {
    let repo = owning("None.");
    touch(&repo, 2, "[gobby-#12] feat: slice change");
    for word in ["todo", "specced"] {
        let rows = [("SURF-A-1", word, NOTE), ("SURF-A-2", "partial", NOTE)];
        commit_status(&repo, &status(CHUNK, &rows));
        assert_refused(&check(&repo, CHUNK, &["12"]), &[STATUS, "SURF-A-1"]);
    }
}

// --- Status vocabulary and row ownership ---

/// Contract: an invalid status is refused. The refusal names the status file path and prints the
/// vocabulary.
#[test]
fn invalid_status_names_the_file_and_prints_the_vocabulary() {
    let repo = owning("None.");
    touch(&repo, 2, "[gobby-#12] feat: slice change");
    let rows = [
        ("SURF-A-1", "finished", NOTE),
        ("SURF-A-2", "partial", NOTE),
    ];
    commit_status(&repo, &status(CHUNK, &rows));
    let run = check(&repo, CHUNK, &["12"]);
    assert_refused(&run, &[STATUS]);
    for word in VOCABULARY.split(' ') {
        assert!(
            run.stdout.contains(word),
            "vocabulary term {word:?} is not printed\n{}",
            show(&run)
        );
    }
}

/// Contract: `blocked(<why>)`, `partial`, `stub`, `needs-local`, `deferred-local`, `delegated`
/// and `dropped` are accepted. One owned row gets each, so the slice passes.
#[test]
fn accepted_statuses_pass() {
    let text = Some(record(&[FROZEN], &[]));
    let repo = base(
        chunk(CHUNK, "family", "None.", "SURF-A-"),
        &keys(7, 0),
        text,
    );
    touch(&repo, 2, "[gobby-#12] feat: slice change");
    let rows = [
        ("SURF-A-1", "blocked(waiting on the seam)", NOTE),
        ("SURF-A-2", "partial", NOTE),
        ("SURF-A-3", "stub", NOTE),
        ("SURF-A-4", "needs-local", NOTE),
        ("SURF-A-5", "deferred-local", NOTE),
        ("SURF-A-6", "delegated", NOTE),
        ("SURF-A-7", "dropped", NOTE),
    ];
    commit_status(&repo, &status(CHUNK, &rows));
    assert_clean(&check(&repo, CHUNK, &["12"]));
}

/// Contract: a status file missing an owned row is refused, and the refusal is about that file.
#[test]
fn status_missing_an_owned_row_is_refused() {
    let repo = owning("None.");
    touch(&repo, 2, "[gobby-#12] feat: slice change");
    commit_status(&repo, &status(CHUNK, &[("SURF-A-1", "partial", NOTE)]));
    assert_refused(&check(&repo, CHUNK, &["12"]), &[STATUS, "SURF-A-2"]);
}

/// Contract: a status file that repeats an owned row is refused.
#[test]
fn status_duplicating_a_row_is_refused() {
    let repo = owning("None.");
    touch(&repo, 2, "[gobby-#12] feat: slice change");
    let rows = [
        ("SURF-A-1", "partial", NOTE),
        ("SURF-A-1", "partial", NOTE),
        ("SURF-A-2", "partial", NOTE),
    ];
    commit_status(&repo, &status(CHUNK, &rows));
    assert_refused(&check(&repo, CHUNK, &["12"]), &[STATUS, "SURF-A-1"]);
}

/// Contract: a status file with a row that another chunk owns is refused.
#[test]
fn status_with_a_foreign_row_is_refused() {
    let repo = owning("None.");
    touch(&repo, 2, "[gobby-#12] feat: slice change");
    let rows = [
        ("SURF-A-1", "partial", NOTE),
        ("SURF-A-2", "partial", NOTE),
        ("SURF-B-1", "partial", NOTE),
    ];
    commit_status(&repo, &status(CHUNK, &rows));
    assert_refused(&check(&repo, CHUNK, &["12"]), &[STATUS, "SURF-B-1"]);
}

/// Contract: an unparseable status file is refused.
#[test]
fn unparseable_status_file_is_refused() {
    let repo = owning("None.");
    touch(&repo, 2, "[gobby-#12] feat: slice change");
    commit_status(&repo, "{\"chunk\": ");
    assert_refused(&check(&repo, CHUNK, &["12"]), &[STATUS]);
}

/// Contract: a chunk that owns rows and has no status file at HEAD is refused.
#[test]
fn owned_rows_without_status_file_are_refused() {
    let repo = owning("None.");
    touch(&repo, 2, "[gobby-#12] feat: slice change");
    assert_refused(&check(&repo, CHUNK, &["12"]), &[STATUS]);
}

/// Contract: a chunk with no ledger rows needs no status file, so a slice with no owned rows
/// passes.
#[test]
fn chunk_without_rows_needs_no_status_file() {
    let repo = rowless("family", "None.");
    touch(&repo, 2, "[gobby-#12] feat: slice change");
    assert_clean(&check(&repo, CHUNK, &["12"]));
}

// --- Authority is read at the merge base ---

/// Contract: the frozen list is read at the merge base. Removing an entry in the range does not
/// free the file, so editing it is still refused.
#[test]
fn frozen_entry_removed_in_range_still_refuses_the_edit() {
    let repo = rowless("family", "None.");
    let unfrozen = record(&[], &[]);
    add(&repo, RECORD, &unfrozen, "[gobby-#12] docs: unfreeze");
    add(
        &repo,
        FROZEN,
        "pub const FROZEN: bool = false;\n",
        "[gobby-#12] fix: frozen edit",
    );
    assert_refused(&check(&repo, CHUNK, &["12"]), &[FROZEN]);
}

/// Contract: python_edits is read at the merge base. Editing it in the range to admit Python
/// does not admit a `.py` path added after the edit.
#[test]
fn python_edits_edited_in_range_still_refuse_python() {
    let repo = rowless("family", "None.");
    let edited = map_json(chunk(
        CHUNK,
        "family",
        "tests/, plus D64's commit.",
        "SURF-A-",
    ));
    add(&repo, MAP, &edited, "[gobby-#12] docs: map edit");
    add(&repo, PY, "VALUE = 1\n", "[gobby-#12] fix: python change");
    assert_refused(&check(&repo, CHUNK, &["12"]), &[PY]);
}

/// Contract: a record missing at the merge base is an empty frozen list. A range that edits a file
/// the HEAD record lists as frozen is not refused, because the base holds no record.
#[test]
fn record_absent_at_base_is_an_empty_frozen_list() {
    let repo = base(
        chunk(CHUNK, "family", "None.", "SURF-A-"),
        &keys(0, 2),
        None,
    );
    add(
        &repo,
        FROZEN,
        "pub const FROZEN: bool = false;\n",
        "[gobby-#12] fix: frozen edit",
    );
    let listed = record(&[FROZEN], &[]);
    add(&repo, RECORD, &listed, "[gobby-#12] docs: add record");
    assert_clean(&check(&repo, CHUNK, &["12"]));
}

/// Contract: a chunk branch that merged `0.9.0` after another chunk landed there is checked for its
/// own commits and paths only. The other chunk's `.py` path and untagged subject are ignored.
#[test]
fn merge_of_landed_chunk_checks_only_own_commits_and_paths() {
    let repo = rowless("family", "None.");
    touch(&repo, 2, "[gobby-#12] feat: slice change");
    repo.git(&["checkout", "-q", "0.9.0"]);
    add(&repo, PY, "VALUE = 2\n", "fix: other chunk lands on 0.9.0");
    repo.git(&["checkout", "-q", "slice"]);
    repo.git(&["merge", "-q", "--no-edit", "0.9.0"]);
    let base_sha = repo.git(&["merge-base", "0.9.0", "HEAD"]);
    assert_eq!(
        repo.git(&["merge-base", "--all", "0.9.0", "HEAD"]),
        base_sha,
        "one merge base"
    );
    let changed = repo.git(&["diff", "--name-only", base_sha.as_str(), "HEAD"]);
    assert!(
        changed.contains(LIB) && !changed.contains(PY),
        "topology: {changed}"
    );
    assert_clean(&check(&repo, CHUNK, &["12"]));
}

// --- Registration protocol ---

/// Contract: removing a workspace member from the root Cargo.toml is refused.
#[test]
fn removing_a_workspace_member_is_refused() {
    let repo = rowless("family", "None.");
    let text = ROOT_CARGO.replace("\"crates/gcode\", ", "");
    add(
        &repo,
        "Cargo.toml",
        &text,
        "[gobby-#12] chore: drop a member",
    );
    assert_refused(&check(&repo, CHUNK, &["12"]), &["crates/gcode"]);
}

/// Contract: removing a key from crates/gdaemon/Cargo.toml [dependencies] is refused.
#[test]
fn removing_a_gdaemon_dependency_is_refused() {
    let repo = rowless("family", "None.");
    let text = GDAEMON_CARGO.replace("walkdir = \"2\"\n", "");
    add(
        &repo,
        "crates/gdaemon/Cargo.toml",
        &text,
        "[gobby-#12] chore: drop walkdir",
    );
    assert_refused(
        &check(&repo, CHUNK, &["12"]),
        &["walkdir", "crates/gdaemon"],
    );
}

/// Contract: a changed `[package]` version in crates/gcode/Cargo.toml is refused for a chunk whose
/// kind is not `cutover`.
#[test]
fn gcode_version_change_is_refused_for_a_family_chunk() {
    let repo = rowless("family", "None.");
    let text = GCODE_CARGO.replace("1.9.125", "1.9.126");
    add(
        &repo,
        "crates/gcode/Cargo.toml",
        &text,
        "[gobby-#12] chore: bump gcode",
    );
    assert_refused(&check(&repo, CHUNK, &["12"]), &["crates/gcode"]);
}

/// Contract: the same crates/gcode version change is allowed for a `cutover` chunk.
#[test]
fn gcode_version_change_is_allowed_for_a_cutover_chunk() {
    let repo = rowless("cutover", "None.");
    let text = GCODE_CARGO.replace("1.9.125", "1.9.126");
    add(
        &repo,
        "crates/gcode/Cargo.toml",
        &text,
        "[gobby-#12] chore: bump gcode",
    );
    assert_clean(&check(&repo, CHUNK, &["12"]));
}

/// Contract: adding a workspace member passes.
#[test]
fn adding_a_workspace_member_passes() {
    let repo = rowless("family", "None.");
    let text = ROOT_CARGO.replace("\"crates/gport\"]", "\"crates/gport\", \"crates/gnew\"]");
    add(
        &repo,
        "Cargo.toml",
        &text,
        "[gobby-#12] chore: add a member",
    );
    assert_clean(&check(&repo, CHUNK, &["12"]));
}

// --- Output and daemon independence ---

/// Contract: every refusal in one run is printed on its own REFUSE line, and the exit code is 1.
/// The run plants an untagged commit with a `.py` path on a `None` chunk and a frozen edit, then
/// removes a workspace member.
#[test]
fn all_refusals_in_one_run_are_printed_with_exit_1() {
    let repo = rowless("family", "None.");
    let subject = "fix: untagged python and frozen change";
    repo.write(PY, "VALUE = 1\n");
    repo.write(FROZEN, "pub const FROZEN: bool = false;\n");
    let sha = repo.commit(subject);
    let text = ROOT_CARGO.replace("\"crates/gcode\", ", "");
    add(
        &repo,
        "Cargo.toml",
        &text,
        "[gobby-#12] chore: drop a member",
    );
    let run = check(&repo, CHUNK, &["12"]);
    let lines = refuse_lines(&run);
    assert_eq!(run.code, 1, "{}", show(&run));
    assert!(
        lines.len() >= 4,
        "expected at least 4 REFUSE lines\n{}",
        show(&run)
    );
    let commit_named = lines
        .iter()
        .any(|line| line.contains(short(&sha)) || line.contains(subject));
    assert!(
        commit_named,
        "no REFUSE line names the untagged commit\n{}",
        show(&run)
    );
    for needle in [PY, FROZEN, "crates/gcode"] {
        assert!(
            lines.iter().any(|line| line.contains(needle)),
            "no REFUSE line names {needle}"
        );
    }
}

/// Contract: a clean slice prints no REFUSE line and exits 0.
#[test]
fn clean_slice_prints_no_refuse_and_exits_0() {
    let repo = rowless("family", "None.");
    touch(&repo, 2, "[gobby-#12] feat: slice change");
    assert_clean(&check(&repo, CHUNK, &["12"]));
}

/// Contract: check-slice makes no daemon call. With GOBBY_DAEMON_URL pointing at a closed port, a
/// clean slice still exits 0 with no REFUSE line.
#[test]
fn check_slice_makes_no_daemon_call() {
    let repo = rowless("family", "None.");
    touch(&repo, 2, "[gobby-#12] feat: slice change");
    let listener = TcpListener::bind("127.0.0.1:0").expect("bind a port");
    let port = listener.local_addr().expect("local address").port();
    drop(listener);
    let url = format!("http://127.0.0.1:{port}");
    let env = [("GOBBY_DAEMON_URL", url.as_str())];
    assert_clean(&gport_env(
        repo.path(),
        &["check-slice", CHUNK, "--task", "12"],
        &env,
    ));
}

// --- Report ---

/// Contract: `--report` prints (partial+stub)/(done+partial+stub) over the named chunks, leaving out
/// needs-local, deferred-local, blocked, delegated and dropped rows. Here partial+stub is 2 and
/// done+partial+stub is 5, so the report prints 2/5 and exits 0. It reads the working tree, since
/// both status files are uncommitted.
#[test]
fn report_prints_partial_and_stub_over_attempted_rows() {
    let repo = report_repo("blocked(waiting on the seam)");
    let run = report(&repo, &[CHUNK, OTHER]);
    assert_eq!(run.code, 0, "{}", show(&run));
    assert!(run.stdout.contains("2/5"), "expected 2/5\n{}", show(&run));
}

/// Contract: `--report` exits 1 when any row is `todo` or `specced`.
#[test]
fn report_exits_1_when_a_row_is_todo_or_specced() {
    for word in ["todo", "specced"] {
        let run = report(&report_repo(word), &[CHUNK, OTHER]);
        assert_eq!(run.code, 1, "{word}: {}", show(&run));
    }
}

/// Contract: `--report` exits 1 and prints `no attempted items` when the denominator is 0, which
/// happens when every row is one of the five excluded statuses.
#[test]
fn report_refuses_no_attempted_items_with_exit_1() {
    let repo = report_base();
    let a = [
        ("SURF-A-1", "blocked(x)", NOTE),
        ("SURF-A-2", "needs-local", NOTE),
        ("SURF-A-3", "delegated", NOTE),
        ("SURF-A-4", "dropped", NOTE),
        ("SURF-A-5", "blocked(y)", NOTE),
        ("SURF-A-6", "deferred-local", NOTE),
    ];
    write_status(&repo, CHUNK, &a);
    let b = [
        ("SURF-B-1", "dropped", NOTE),
        ("SURF-B-2", "delegated", NOTE),
        ("SURF-B-3", "needs-local", NOTE),
        ("SURF-B-4", "blocked(z)", NOTE),
    ];
    write_status(&repo, OTHER, &b);
    let run = report(&repo, &[CHUNK, OTHER]);
    assert_eq!(run.code, 1, "{}", show(&run));
    assert!(run.stdout.contains("no attempted items"), "{}", show(&run));
}

/// Contract: `--report` exits 1 when a status file's rows differ from the rows its chunk owns. The
/// control is the passing report fixture; this run drops a row, then adds a foreign one.
#[test]
fn report_exits_1_when_status_rows_differ_from_owned_rows() {
    let repo = report_repo("blocked(waiting on the seam)");
    let five = [
        ("SURF-A-1", "done", NOTE),
        ("SURF-A-2", "done", NOTE),
        ("SURF-A-3", "partial", NOTE),
        ("SURF-A-4", "needs-local", NOTE),
        ("SURF-A-5", "blocked(waiting on the seam)", NOTE),
    ];
    write_status(&repo, CHUNK, &five);
    assert_eq!(
        report(&repo, &[CHUNK, OTHER]).code,
        1,
        "a missing row must refuse"
    );
    let mut foreign = five.to_vec();
    foreign.push(("SURF-B-1", "stub", NOTE));
    write_status(&repo, CHUNK, &foreign);
    assert_eq!(
        report(&repo, &[CHUNK, OTHER]).code,
        1,
        "a foreign row must refuse"
    );
}
