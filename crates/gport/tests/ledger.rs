//! Contracts for `gport ledger`, run black-box through the built binary.
//!
//! Golden `tests/fixtures/ledger/owners.tsv` was generated once from the worktree root with the
//! command below. Its 1106 lines, one per frozen row, show that every row had exactly one owner
//! under scripts/port/check_coverage.py's rule:
//!
//! python3 -I -c 'import json,re; m=json.load(open("docs/port/map-v2.json",encoding="utf-8")); sel=[(c["id"],re.compile(s)) for c in m["chunks"] for s in (c.get("ledger_selectors") or [])]; keys=[l[3:l.find("` |")] for l in open("docs/port/ledger.md",encoding="utf-8").read().splitlines() if l.startswith("| `") and "` |" in l]; [print(k+"\t"+o[0]) for k in sorted(keys) for o in [sorted({i for i,p in sel if p.search(k)})] if len(o)==1]' > crates/gport/tests/fixtures/ledger/owners.tsv
//!
//! The render goldens `tests/fixtures/ledger/render-notes/expected-parity-ledger.md` and
//! `render-waivers/expected-parity-ledger.md` were generated once from the worktree root with the
//! real Python renderer and parity_waivers loader:
//!
//! uv run --no-sync python -c 'import sys; from pathlib import Path; from scripts.generate_parity_ledger import load_statuses, render_ledger; from scripts.parity_waivers import load_waivers; root = Path(sys.argv[1]); keys = {l[3:l.find("` |")] for l in (root / "docs/port/ledger.md").read_text().splitlines() if l.startswith("| `") and "` |" in l}; statuses = load_statuses(root / "docs/contracts/parity-ledger-status.d"); waivers = load_waivers(root / "docs/contracts/parity-waivers.json", surfaces=keys, additive_surfaces=frozenset()); sys.stdout.buffer.write(render_ledger(keys, statuses, waivers))' crates/gport/tests/fixtures/ledger/render-notes > crates/gport/tests/fixtures/ledger/render-notes/expected-parity-ledger.md
//!
//! (the same with crates/gport/tests/fixtures/ledger/render-waivers for the second golden).
//! The Python checker lines asserted below were read from scripts/port/check_coverage.py and
//! scripts/port/check_dispositions.py run once on the same synthetic inputs.

use std::fs;
use std::path::{Path, PathBuf};

use serde_json::{Value, json};
use tempfile::TempDir;

use crate::support::{Run, gport, repo_root};

const CONTRACT_LEDGER: &str = "docs/contracts/parity-ledger.md";
const SHARDS: &str = "docs/contracts/parity-ledger-status.d";
const WAIVERS: &str = "docs/contracts/parity-waivers.json";
const REAL_SUMMARY: &str = "rows=1106 owned_once=1106 orphans=0 double_owned=0 bad_selectors=0";
const NO_SELECTORS: &[&str] = &[];
const NINE_ROWS: [&str; 9] = [
    "MCP x:r1", "MCP x:r2", "MCP x:r3", "MCP x:r4", "MCP x:r5", "MCP x:r6", "MCP x:r7", "MCP x:r8",
    "MCP x:r9",
];

/// A path under `tests/fixtures/ledger`.
fn fixture(rel: &str) -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("tests/fixtures/ledger")
        .join(rel)
}

/// Write a file under `root`, creating its parent directories.
fn write(root: &Path, rel: &str, content: &str) {
    let path = root.join(rel);
    fs::create_dir_all(path.parent().expect("parent dir")).expect("create dirs");
    fs::write(path, content).expect("write file");
}

/// Replace one status shard in a synthetic root.
fn shard(root: &Path, name: &str, body: &str) {
    write(root, &format!("{SHARDS}/{name}"), body);
}

fn copy_tree(from: &Path, to: &Path) {
    fs::create_dir_all(to).expect("create dir");
    for entry in fs::read_dir(from).expect("read dir") {
        let entry = entry.expect("dir entry");
        let target = to.join(entry.file_name());
        if entry.file_type().expect("file type").is_dir() {
            copy_tree(&entry.path(), &target);
        } else {
            fs::copy(entry.path(), &target).expect("copy file");
        }
    }
}

/// A temp copy of the committed inputs `gport ledger` reads. The rendered ledger is not copied,
/// so a test can tell whether the run wrote it.
fn real_copy() -> TempDir {
    let temp = TempDir::new().expect("temp dir");
    let real = repo_root();
    for rel in [
        "docs/port/ledger.md",
        "docs/port/map-v2.json",
        "docs/port/open-nodes.json",
        WAIVERS,
    ] {
        let target = temp.path().join(rel);
        fs::create_dir_all(target.parent().expect("parent dir")).expect("create dirs");
        fs::copy(real.join(rel), target).expect("copy real input");
    }
    copy_tree(
        &real.join("docs/port/dispositions"),
        &temp.path().join("docs/port/dispositions"),
    );
    copy_tree(&real.join(SHARDS), &temp.path().join(SHARDS));
    temp
}

/// A synthetic root: frozen rows, `(chunk id, selectors)` pairs, an empty status shard, no
/// waivers, no open nodes and an empty dispositions file. Each test overwrites what it varies.
fn synthetic(rows: &[&str], chunks: &[(&str, &[&str])]) -> TempDir {
    let temp = TempDir::new().expect("temp dir");
    let mut ledger = String::from("# Parity ledger\n\n");
    for row in rows {
        ledger.push_str(&format!("| `{row}` | proxied | |\n"));
    }
    write(temp.path(), "docs/port/ledger.md", &ledger);
    let map_chunks: Vec<Value> = chunks
        .iter()
        .map(|(id, selectors)| json!({"id": id, "ledger_selectors": selectors}))
        .collect();
    write(
        temp.path(),
        "docs/port/map-v2.json",
        &json!({"chunks": map_chunks}).to_string(),
    );
    write(temp.path(), "docs/port/open-nodes.json", r#"{"nodes": []}"#);
    write(
        temp.path(),
        "docs/port/dispositions/empty.json",
        r#"{"dispositions": []}"#,
    );
    shard(temp.path(), "10-base.json", "{}");
    write(
        temp.path(),
        WAIVERS,
        r#"{"schema_version": 1, "waivers": []}"#,
    );
    temp
}

/// A synthetic root with one row, `MCP x:a`, owned by `alpha`, and the given waiver entry.
fn with_waiver(waiver: Value) -> TempDir {
    let temp = synthetic(&["MCP x:a"], &[("alpha", &["^MCP x:a$"])]);
    write(
        temp.path(),
        WAIVERS,
        &json!({"schema_version": 1, "waivers": [waiver]}).to_string(),
    );
    temp
}

/// A root with no rows, chunks `alpha` and `beta`, open nodes `#1` to `#3`, and one
/// dispositions file named `file` holding `body`.
fn disposition_root(file: &str, body: Value) -> TempDir {
    let temp = synthetic(&[], &[("alpha", NO_SELECTORS), ("beta", NO_SELECTORS)]);
    write(
        temp.path(),
        "docs/port/open-nodes.json",
        &json!({"nodes": [
            {"ref": "#1", "title": "first"},
            {"ref": "#2", "title": "second"},
            {"ref": "#3", "title": "third"}
        ]})
        .to_string(),
    );
    write(
        temp.path(),
        &format!("docs/port/dispositions/{file}"),
        &body.to_string(),
    );
    temp
}

/// A temp copy of `fixtures/ledger/<name>`, a complete root that renders cleanly.
fn fixture_copy(name: &str) -> TempDir {
    let temp = TempDir::new().expect("temp dir");
    copy_tree(&fixture(name), temp.path());
    temp
}

/// Run `gport --root <root> ledger <args>`.
fn run_ledger(root: &Path, args: &[&str]) -> Run {
    let root_arg = root.to_str().expect("utf-8 temp path");
    let mut full = vec!["--root", root_arg, "ledger"];
    full.extend_from_slice(args);
    gport(root, &full)
}

/// The run must exit with `code`. Both streams go into the message.
fn expect_exit(run: &Run, code: i32) {
    assert_eq!(
        run.code, code,
        "stdout:\n{}\nstderr:\n{}",
        run.stdout, run.stderr
    );
}

/// The run must exit 2 and print `needle` on stderr.
fn expect_error(run: &Run, needle: &str) {
    assert!(
        run.code == 2 && run.stderr.contains(needle),
        "want exit 2 with stderr containing {needle:?}\ncode: {}\nstdout:\n{}\nstderr:\n{}",
        run.code,
        run.stdout,
        run.stderr
    );
}

/// The run's stdout must contain `line` as a whole line.
fn expect_line(run: &Run, line: &str) {
    assert!(
        run.stdout.lines().any(|l| l == line),
        "missing line {line:?}\ncode: {}\nstdout:\n{}\nstderr:\n{}",
        run.code,
        run.stdout,
        run.stderr
    );
}

/// The run's stdout must equal the golden file at `golden`.
fn expect_golden(run: &Run, golden: &Path) {
    let expected = fs::read_to_string(golden).expect("golden fixture");
    let first = run
        .stdout
        .lines()
        .zip(expected.lines())
        .find(|(got, want)| got != want)
        .map(|(got, want)| format!("got {got:?}, want {want:?}"))
        .unwrap_or_else(|| {
            format!(
                "line counts {} vs {}",
                run.stdout.lines().count(),
                expected.lines().count()
            )
        });
    assert!(
        run.stdout == expected,
        "stdout differs from {}: {first}\ncode: {}\nstderr:\n{}",
        golden.display(),
        run.code,
        run.stderr
    );
}

/// Whether `stdout` holds `count` as a whole number anywhere.
fn has_number(stdout: &str, count: &str) -> bool {
    stdout
        .split(|c: char| !c.is_ascii_digit())
        .any(|token| token == count)
}

/// `--stdout` prints exactly the committed ledger bytes and writes no file.
#[test]
fn stdout_prints_the_committed_ledger_bytes_and_writes_nothing() {
    let temp = real_copy();
    let run = run_ledger(temp.path(), &["--stdout"]);
    expect_exit(&run, 0);
    let committed = fs::read(repo_root().join(CONTRACT_LEDGER)).expect("committed ledger");
    assert!(
        run.stdout.as_bytes() == committed.as_slice(),
        "--stdout bytes differ from {CONTRACT_LEDGER}: got {} bytes, want {}\nstderr:\n{}",
        run.stdout.len(),
        committed.len(),
        run.stderr
    );
    assert!(
        !temp.path().join(CONTRACT_LEDGER).exists(),
        "--stdout wrote {CONTRACT_LEDGER}"
    );
}

/// Without `--stdout`, the ledger file is written with the committed bytes, and the real
/// coverage summary prints, exiting 0.
#[test]
fn run_writes_the_contract_ledger_and_prints_the_real_summary() {
    let temp = real_copy();
    let target = temp.path().join(CONTRACT_LEDGER);
    assert!(
        !target.exists(),
        "the temp copy must start without the ledger"
    );
    let run = run_ledger(temp.path(), &[]);
    expect_exit(&run, 0);
    expect_line(&run, REAL_SUMMARY);
    let written = fs::read(&target).expect("ledger file written");
    let committed = fs::read(repo_root().join(CONTRACT_LEDGER)).expect("committed ledger");
    assert!(
        written == committed,
        "written {CONTRACT_LEDGER} differs from the committed bytes ({} vs {})",
        written.len(),
        committed.len()
    );
}

/// `--owners` on the real inputs prints `<row key>\t<chunk id>` for each of the 1106 rows, sorted
/// by key, and nothing else. The golden holds the Python ownership.
#[test]
fn owners_match_the_python_ownership_golden() {
    let temp = real_copy();
    let run = run_ledger(temp.path(), &["--owners"]);
    expect_exit(&run, 0);
    expect_golden(&run, &fixture("owners.tsv"));
}

/// A frozen row that no selector matches is an orphan: exit 1 and an `ORPHAN <key>` line.
#[test]
fn orphan_row_exits_one_with_an_orphan_line() {
    let temp = synthetic(&["MCP x:a", "MCP x:b"], &[("alpha", &["^MCP x:a$"])]);
    let run = run_ledger(temp.path(), &[]);
    expect_exit(&run, 1);
    expect_line(&run, "ORPHAN MCP x:b");
    expect_line(
        &run,
        "rows=2 owned_once=1 orphans=1 double_owned=0 bad_selectors=0",
    );
}

/// A row owned by two chunks is double-owned: exit 1 and a `DOUBLE <key> -> <ids>` line with the
/// chunk ids sorted and comma-joined.
#[test]
fn double_owned_row_exits_one_with_a_double_line() {
    let temp = synthetic(
        &["MCP x:a"],
        &[("alpha", &["^MCP x:a$"]), ("beta", &["^MCP x:a$"])],
    );
    let run = run_ledger(temp.path(), &[]);
    expect_exit(&run, 1);
    expect_line(&run, "DOUBLE MCP x:a -> alpha,beta");
    expect_line(
        &run,
        "rows=1 owned_once=0 orphans=0 double_owned=1 bad_selectors=0",
    );
}

/// A selector that does not compile prints a `BAD <chunk>: '<selector>': ...` line and exits 1.
/// The message after the quoted selector is the regex engine's own and is not compared.
#[test]
fn selector_that_does_not_compile_prints_a_bad_line_and_exits_one() {
    let temp = synthetic(&["MCP x:a"], &[("alpha", &["[unclosed", "^MCP x:a$"])]);
    let run = run_ledger(temp.path(), &[]);
    expect_exit(&run, 1);
    assert!(
        run.stdout
            .lines()
            .any(|l| l.starts_with("BAD alpha: '[unclosed': ")),
        "missing BAD line for the broken selector\ncode: {}\nstdout:\n{}\nstderr:\n{}",
        run.code,
        run.stdout,
        run.stderr
    );
    expect_line(
        &run,
        "rows=1 owned_once=1 orphans=0 double_owned=0 bad_selectors=1",
    );
}

/// A negative-lookahead selector compiles and owns only the rows it allows. `--owners` prints the
/// exact ownership, sorted by key.
#[test]
fn negative_lookahead_selector_owns_only_the_rows_it_allows() {
    let temp = synthetic(
        &["MCP x:create_task", "MCP x:list_tasks"],
        &[
            ("alpha", &["^MCP x:(?!create_task$)"]),
            ("beta", &["create_task$"]),
        ],
    );
    let run = run_ledger(temp.path(), &["--owners"]);
    expect_exit(&run, 0);
    assert!(
        run.stdout == "MCP x:create_task\tbeta\nMCP x:list_tasks\talpha\n",
        "owners differ\ncode: {}\nstdout:\n{}\nstderr:\n{}",
        run.code,
        run.stdout,
        run.stderr
    );
}

/// A clean run exits 0 and prints the summary plus one `PER_CHUNK` line with sorted keys and
/// `json.dumps` spacing.
#[test]
fn clean_run_exits_zero_and_prints_per_chunk_counts() {
    let temp = synthetic(
        &["MCP x:a", "MCP x:b"],
        &[("alpha", &["^MCP x:a$"]), ("beta", &["^MCP x:b$"])],
    );
    let run = run_ledger(temp.path(), &[]);
    expect_exit(&run, 0);
    expect_line(
        &run,
        "rows=2 owned_once=2 orphans=0 double_owned=0 bad_selectors=0",
    );
    expect_line(&run, r#"PER_CHUNK {"alpha": 1, "beta": 1}"#);
}

/// A pipe in a frozen row key is written as `\|`. The key is unescaped before selectors match it,
/// so the selector sees `MCP x:a|b`.
#[test]
fn escaped_pipe_in_a_row_key_is_unescaped_before_matching() {
    let temp = synthetic(&["MCP x:a\\|b"], &[("alpha", &["^MCP x:a\\|b$"])]);
    let run = run_ledger(temp.path(), &["--owners"]);
    expect_exit(&run, 0);
    assert!(
        run.stdout == "MCP x:a|b\talpha\n",
        "owners differ\ncode: {}\nstdout:\n{}\nstderr:\n{}",
        run.code,
        run.stdout,
        run.stderr
    );
}

/// A surface listed in two status shards is refused with exit 2, and the message names both
/// shards in filename order.
#[test]
fn surface_in_two_status_shards_is_refused_naming_both_shards() {
    let temp = synthetic(&["MCP x:a"], &[("alpha", &["^MCP x:a$"])]);
    shard(temp.path(), "10-first.json", r#"{"MCP x:a": "proxied"}"#);
    shard(temp.path(), "20-second.json", r#"{"MCP x:a": "proxied"}"#);
    let run = run_ledger(temp.path(), &["--stdout"]);
    expect_error(
        &run,
        "Duplicate parity surface 'MCP x:a' in 10-first.json and 20-second.json",
    );
}

/// A status outside proxied, delegated and `dropped by decision #N` is refused with exit 2.
#[test]
fn invalid_status_is_refused_with_exit_two() {
    let temp = synthetic(&["MCP x:a"], &[("alpha", &["^MCP x:a$"])]);
    shard(temp.path(), "10-base.json", r#"{"MCP x:a": "maybe"}"#);
    let run = run_ledger(temp.path(), &["--stdout"]);
    expect_error(&run, "Invalid parity status for MCP x:a: 'maybe'");
}

/// An object entry without exactly `status` and `note` is refused with exit 2.
#[test]
fn malformed_object_entry_is_refused_with_exit_two() {
    let temp = synthetic(&["MCP x:a"], &[("alpha", &["^MCP x:a$"])]);
    shard(
        temp.path(),
        "10-base.json",
        r#"{"MCP x:a": {"status": "proxied"}}"#,
    );
    let run = run_ledger(temp.path(), &["--stdout"]);
    expect_error(
        &run,
        "Invalid parity entry for MCP x:a in 10-base.json: expected a status string or an object with string status and note",
    );
}

/// With no status shards at all, the run is refused with exit 2 naming the shard directory.
#[test]
fn no_status_shards_is_refused_with_exit_two() {
    let temp = synthetic(&["MCP x:a"], &[("alpha", &["^MCP x:a$"])]);
    fs::remove_dir_all(temp.path().join(SHARDS)).expect("remove shards");
    let run = run_ledger(temp.path(), &["--stdout"]);
    expect_error(&run, "No parity status shards found in");
    assert!(
        run.stderr.contains("parity-ledger-status.d"),
        "stderr does not name the shard directory\nstderr:\n{}",
        run.stderr
    );
}

/// A mapping key for a surface absent from the frozen rows is refused with exit 2, unless its
/// status is a `dropped by decision` status.
#[test]
fn mapping_key_for_an_absent_surface_that_is_not_a_drop_is_refused() {
    let temp = synthetic(&["MCP x:a"], &[("alpha", &["^MCP x:a$"])]);
    shard(
        temp.path(),
        "10-base.json",
        r#"{"MCP x:a": "proxied", "MCP x:gone": "proxied"}"#,
    );
    let run = run_ledger(temp.path(), &["--stdout"]);
    expect_error(&run, "Mapping references absent surfaces: MCP x:gone");
}

/// A `dropped by decision #N` key with no frozen row is kept as a row in the render. The header
/// counts it.
#[test]
fn dropped_key_absent_from_the_frozen_rows_is_kept_as_a_row() {
    let temp = synthetic(&["MCP x:a"], &[("alpha", &["^MCP x:a$"])]);
    shard(
        temp.path(),
        "10-base.json",
        r#"{"MCP x:a": "proxied", "MCP old:gone": "dropped by decision #7"}"#,
    );
    let run = run_ledger(temp.path(), &["--stdout"]);
    expect_exit(&run, 0);
    expect_line(&run, "| `MCP old:gone` | dropped by decision #7 |  |");
    expect_line(
        &run,
        "Public surfaces and evidence gates: 2. Unclassified: 0.",
    );
}

/// The status notes of `render-notes` (a pipe, a backslash, LF, CRLF and a bare CR, plus a
/// dropped key absent from the rows) render byte for byte as the Python renderer does.
#[test]
fn status_notes_render_exactly_as_render_ledger() {
    let temp = fixture_copy("render-notes");
    let run = run_ledger(temp.path(), &["--stdout"]);
    expect_exit(&run, 0);
    expect_golden(&run, &fixture("render-notes/expected-parity-ledger.md"));
}

/// The behaviour waivers of `render-waivers` (two on one row, sorted by id, with both decision
/// forms, and a waiver on a row with its own status note) render byte for byte as Python does.
#[test]
fn waiver_entries_render_their_notes_exactly_as_render_ledger() {
    let temp = fixture_copy("render-waivers");
    let run = run_ledger(temp.path(), &["--stdout"]);
    expect_exit(&run, 0);
    expect_golden(&run, &fixture("render-waivers/expected-parity-ledger.md"));
}

/// A waiver whose surface is not an exact ledger key is refused with exit 2.
#[test]
fn waiver_naming_an_absent_surface_is_refused() {
    let temp = with_waiver(json!({
        "id": "gobby.notes.gone-waiver",
        "surface": "MCP x:missing",
        "behaviour": "Behaviour text.",
        "decision": "#5",
        "replacement": "Replacement text."
    }));
    let run = run_ledger(temp.path(), &["--stdout"]);
    expect_error(
        &run,
        "gobby.notes.gone-waiver: surface 'MCP x:missing' is not an exact ledger key",
    );
}

/// A waiver id that is not three lowercase slugs is refused with exit 2.
#[test]
fn waiver_with_a_malformed_id_is_refused() {
    let temp = with_waiver(json!({
        "id": "Gobby.Notes.bad",
        "surface": "MCP x:a",
        "behaviour": "Behaviour text.",
        "decision": "#5",
        "replacement": "Replacement text."
    }));
    let run = run_ledger(temp.path(), &["--stdout"]);
    expect_error(
        &run,
        "Waiver id 'Gobby.Notes.bad' must be <family>.<surface>.<behaviour> slugs",
    );
}

/// A native-extension entry needs a declared additive surface. The additive set is empty, so the
/// entry is refused with exit 2. Python refuses it the same way, so no row note can be produced.
#[test]
fn native_extension_without_a_declared_additive_surface_is_refused() {
    let temp = with_waiver(json!({
        "id": "gobby.notes.native-extension",
        "surface": "MCP x:native",
        "behaviour": "Behaviour text.",
        "decision": "#13",
        "replacement": "Replacement text."
    }));
    let run = run_ledger(temp.path(), &["--stdout"]);
    expect_error(
        &run,
        "gobby.notes.native-extension: 'MCP x:native' is not a declared additive surface",
    );
}

/// The exit-rule count, rows neither delegated nor dropped by decision, prints without the flag
/// and exits 0. With `--exit-rule` the same run exits 1. Of the nine rows, seven are blocking.
#[test]
fn exit_rule_count_prints_and_gates_only_with_the_flag() {
    let temp = synthetic(&NINE_ROWS, &[("alpha", &["^MCP x:r[0-9]$"])]);
    shard(
        temp.path(),
        "10-base.json",
        r#"{"MCP x:r1": "proxied", "MCP x:r2": "proxied", "MCP x:r3": "proxied",
            "MCP x:r4": "proxied", "MCP x:r5": "proxied", "MCP x:r6": "proxied",
            "MCP x:r8": "delegated", "MCP x:r9": "dropped by decision #4"}"#,
    );
    let plain = run_ledger(temp.path(), &[]);
    expect_exit(&plain, 0);
    assert!(
        has_number(&plain.stdout, "7"),
        "exit-rule count 7 not printed\ncode: {}\nstdout:\n{}\nstderr:\n{}",
        plain.code,
        plain.stdout,
        plain.stderr
    );
    let gated = run_ledger(temp.path(), &["--exit-rule"]);
    expect_exit(&gated, 1);
}

/// With every row delegated or dropped by decision, `--exit-rule` exits 0.
#[test]
fn exit_rule_passes_when_every_row_is_delegated_or_dropped() {
    let temp = synthetic(&["MCP x:a", "MCP x:b"], &[("alpha", &["^MCP x:(a|b)$"])]);
    shard(
        temp.path(),
        "10-base.json",
        r#"{"MCP x:a": "delegated", "MCP x:b": "dropped by decision #4"}"#,
    );
    let run = run_ledger(temp.path(), &["--exit-rule"]);
    expect_exit(&run, 0);
}

/// A node with no disposition is reported `MISSING <ref> <title>`, and the run exits 1. The
/// lines match check_dispositions.py's output for this file.
#[test]
fn node_without_a_disposition_is_reported_missing() {
    let temp = disposition_root(
        "missing.json",
        json!({"dispositions": [
            {"ref": "#1", "disposition": "chunk", "target": "alpha", "rationale": "ok"},
            {"ref": "#3", "disposition": "chunk", "target": "alpha", "rationale": "ok"}
        ]}),
    );
    let run = run_ledger(temp.path(), &[]);
    expect_exit(&run, 1);
    expect_line(&run, "MISSING #2 second");
    expect_line(
        &run,
        "expected=3 disposed=2 missing=1 unknown=0 doubled=0 invalid=0",
    );
    expect_line(&run, r#"PER_KIND {"chunk": 2}"#);
    expect_line(&run, r#"CHUNKS_WITHOUT_NODES ["beta"]"#);
}

/// A disposition kind outside the vocabulary is reported `INVALID <ref>: unknown kind '<kind>'
/// (<file name>)`, and the run exits 1.
#[test]
fn unknown_disposition_kind_is_reported_invalid_with_the_file_name() {
    let temp = disposition_root(
        "disp-kind.json",
        json!({"dispositions": [
            {"ref": "#1", "disposition": "chunk", "target": "alpha", "rationale": "ok"},
            {"ref": "#2", "disposition": "bogus", "target": "alpha", "rationale": "ok"},
            {"ref": "#3", "disposition": "chunk", "target": "alpha", "rationale": "ok"}
        ]}),
    );
    let run = run_ledger(temp.path(), &[]);
    expect_exit(&run, 1);
    expect_line(&run, "INVALID #2: unknown kind 'bogus' (disp-kind.json)");
    expect_line(
        &run,
        "expected=3 disposed=3 missing=0 unknown=0 doubled=0 invalid=1",
    );
    expect_line(&run, r#"PER_KIND {"bogus": 1, "chunk": 2}"#);
}

/// A disposition without a rationale is reported `INVALID <ref>: no rationale`, and the run
/// exits 1.
#[test]
fn disposition_without_a_rationale_is_reported_invalid() {
    let temp = disposition_root(
        "disp-rationale.json",
        json!({"dispositions": [
            {"ref": "#1", "disposition": "chunk", "target": "alpha", "rationale": "ok"},
            {"ref": "#2", "disposition": "chunk", "target": "alpha"},
            {"ref": "#3", "disposition": "chunk", "target": "alpha", "rationale": "ok"}
        ]}),
    );
    let run = run_ledger(temp.path(), &[]);
    expect_exit(&run, 1);
    expect_line(&run, "INVALID #2: no rationale");
    expect_line(
        &run,
        "expected=3 disposed=3 missing=0 unknown=0 doubled=0 invalid=1",
    );
}
