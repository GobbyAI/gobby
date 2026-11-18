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

/// A pipe in a frozen row key is written as `\|`. Ownership matches the key as written, as
/// scripts/port/check_coverage.py does, so a selector that spells the backslash owns the row, and
/// `--owners` prints the key as written.
#[test]
fn escaped_pipe_in_a_row_key_is_matched_as_written() {
    let temp = synthetic(&["MCP x:a\\|b"], &[("alpha", &[r"^MCP x:a\\\|b$"])]);
    let run = run_ledger(temp.path(), &["--owners"]);
    expect_exit(&run, 0);
    assert!(
        run.stdout == "MCP x:a\\|b\talpha\n",
        "owners differ\ncode: {}\nstdout:\n{}\nstderr:\n{}",
        run.code,
        run.stdout,
        run.stderr
    );
}

/// A selector for the unescaped pipe does not match the key as written, so the escaped row is an
/// orphan that prints with its backslash.
#[test]
fn selector_for_the_unescaped_pipe_does_not_own_an_escaped_key() {
    let temp = synthetic(&["MCP x:a\\|b"], &[("alpha", &[r"^MCP x:a\|b$"])]);
    let run = run_ledger(temp.path(), &[]);
    expect_exit(&run, 1);
    expect_line(&run, "ORPHAN MCP x:a\\|b");
    expect_line(
        &run,
        "rows=1 owned_once=0 orphans=1 double_owned=0 bad_selectors=0",
    );
}

/// `--owners` prints one line per ledger row, so a key listed twice prints twice. The summary
/// still counts each distinct key once in `owned_once`.
#[test]
fn owners_print_one_line_per_row_including_repeats() {
    let temp = synthetic(
        &["CLI gobby a", "CLI gobby a", "CLI gobby b"],
        &[("alpha", &["^CLI gobby a$"]), ("beta", &["^CLI gobby b$"])],
    );
    let run = run_ledger(temp.path(), &["--owners"]);
    expect_exit(&run, 0);
    assert!(
        run.stdout == "CLI gobby a\talpha\nCLI gobby a\talpha\nCLI gobby b\tbeta\n",
        "owners differ\ncode: {}\nstdout:\n{}\nstderr:\n{}",
        run.code,
        run.stdout,
        run.stderr
    );
    let summary = run_ledger(temp.path(), &[]);
    expect_line(
        &summary,
        "rows=3 owned_once=2 orphans=0 double_owned=0 bad_selectors=0",
    );
}

/// The `--owners` lines for `rows` under one chunk, `alpha`, that holds `selector`.
fn owners_under(selector: &str, rows: &[&str]) -> String {
    let temp = synthetic(rows, &[("alpha", &[selector])]);
    run_ledger(temp.path(), &["--owners"]).stdout
}

/// The run must print a `BAD alpha: '<selector>': ` line, with the selector as a Python repr, and
/// exit 1.
fn expect_bad_selector(selector: &str) {
    let temp = synthetic(&["MCP x:a"], &[("alpha", &[selector])]);
    let run = run_ledger(temp.path(), &[]);
    let prefix = format!("BAD alpha: '{}': ", selector.replace('\\', "\\\\"));
    expect_exit(&run, 1);
    assert!(
        run.stdout.lines().any(|line| line.starts_with(&prefix)),
        "missing {prefix:?}\nstdout:\n{}\nstderr:\n{}",
        run.stdout,
        run.stderr
    );
}

/// Each selector is one that Python's `re.compile` refuses, each raising `re.error`. Verdicts were
/// read under `python3.13 -I` (Python 3.13 is the repository's floor, `requires-python >= 3.13`);
/// `\z` is the one entry Python 3.14.3 accepts instead. gport must refuse each as a BAD line.
#[test]
fn selectors_python_refuses_are_bad_lines() {
    for selector in [
        r"\p{L}",
        r"\P{L}",
        r"\x{41}",
        r"\h",
        r"\K",
        r"\X",
        r"\G",
        r"\e",
        r"\Q",
        r"\cA",
        r"\z",
        r"(?<name>x)",
        r"(?L)x",
        r"(?<=a|bc)x",
        r"(?<!a|bc)x",
        r"(?<=a*)x",
        r"(?<=a?)x",
        r"\1",
        r"a{2,1}",
        r"a**",
        r"a{1}{2}",
        r"[a--b]",
        r"[\Z]",
        r"[\A]",
        r"[\N]",
    ] {
        expect_bad_selector(selector);
    }
}

/// Each selector Python compiles, but whose set syntax means something else in fancy-regex: a
/// nested `[` (including POSIX `[[:alpha:]]`) and the class operators `&&`, `||`, `~~` and `--`.
/// Python's verdicts are OK: a nested set and a set operator are literal characters there, with a
/// FutureWarning. Decision (c) reports them BAD rather than compile them, so gport reports BAD.
#[test]
fn python_compiled_set_constructs_are_bad_lines_by_decision() {
    for selector in [
        "[[:alpha:]]+",
        "[a-z&&[^b]]",
        "[a&&b]",
        "[a||b]",
        "[a~~b]",
        "[[]",
        "[+--]",
    ] {
        expect_bad_selector(selector);
    }
}

/// `\Z` matches only at the end of the key, as Python's does. Verdict: Python `re.search` owns
/// `end` and not `endx` (`python3 -I`, Python 3.14.3).
#[test]
fn python_end_of_string_escape_owns_only_the_key_it_ends() {
    assert_eq!(
        owners_under(r"^end\Z", &["end", "endx"]),
        "end\talpha\nendx\t\n"
    );
}

/// `{,2}` is a quantifier from zero to two, and a `{` that does not start a quantifier is a
/// literal. Verdicts: Python owns `x` and not `xxx`; `x{` is a literal (`python3 -I`).
#[test]
fn python_brace_quantifiers_and_literal_braces_keep_python_meaning() {
    assert_eq!(owners_under(r"^x{,2}$", &["x", "xxx"]), "x\talpha\nxxx\t\n");
    assert_eq!(owners_under(r"^x{$", &["x{", "x"]), "x\t\nx{\talpha\n");
}

/// `A` is the letter `A`. `\S` excludes U+001F, which Python counts as whitespace, so the key
/// with a unit separator is an orphan. Verdicts from `python3 -I`: `^A$` owns `A` only;
/// `^\S+$` owns `abc` only.
#[test]
fn python_unicode_escape_and_space_class_keep_python_meaning() {
    assert_eq!(owners_under(r"^A$", &["A", "a"]), "A\talpha\na\t\n");
    assert_eq!(
        owners_under(r"^\S+$", &["abc", "a\u{1f}b"]),
        "a\u{1f}b\t\nabc\talpha\n"
    );
}

/// Outside a set, `&&` is two literal characters in Python and in fancy-regex alike. Verdict:
/// Python owns `a&&b` (`python3 -I`).
#[test]
fn set_operator_outside_a_set_is_literal() {
    assert_eq!(owners_under(r"^a&&b$", &["a&&b"]), "a&&b\talpha\n");
}

/// A look-behind of fixed width is valid in Python 3.13 and owns the rows it allows: `(?<=ab)x`
/// owns `abx` and not a bare `x` (`python3.13 -I`).
#[test]
fn fixed_width_look_behind_keeps_python_meaning() {
    assert_eq!(
        owners_under(r"(?<=ab)x", &["abx", "x"]),
        "abx\talpha\nx\t\n"
    );
}

/// A selector that runs out of backtracking on a key fails the run: the ledger exits 2 and names
/// the chunk, rather than reading the selector as no match.
#[test]
fn selector_that_fails_to_run_fails_the_ledger_run() {
    let key = format!("MCP x:{}?", "a".repeat(40));
    let temp = synthetic(&[key.as_str()], &[("alpha", &[r"^MCP x:(a+)+(?!z)!"])]);
    let run = run_ledger(temp.path(), &[]);
    expect_error(&run, "selector of alpha failed on");
}

/// A backreference is refused in every form: `\1` and `(?P=name)`, a reference to a group still
/// open as in `(a\1)`, and a case-insensitive one. A differential run found gport matching these
/// differently from Python, so each is reported BAD rather than matched.
#[test]
fn backreference_selectors_are_bad_lines() {
    for selector in [r"(a)\1", r"(a\1)", r"(?i)(a)\1", r"(?P=n)"] {
        expect_bad_selector(selector);
    }
}

/// A named group is refused, whatever its name and however often the name repeats. Python
/// compiles `(?P<n>x)` and rejects the duplicate name in `(?P<n>a)(?P<n>b)`; gport reports both
/// BAD.
#[test]
fn named_group_selectors_are_bad_lines() {
    for selector in [r"(?P<n>x)", r"(?P<n>a)(?P<n>b)"] {
        expect_bad_selector(selector);
    }
}

/// The verbose flag is refused in every inline-flag form, turned on or off: `(?x)a{2} {3}`,
/// `(?ix)a`, `(?x:a b)` and `(?-x:a)`. Python ignores the whitespace in verbose mode, and
/// fancy-regex does not match it the same way.
#[test]
fn verbose_flag_selectors_are_bad_lines() {
    for selector in [r"(?x)a{2} {3}", r"(?ix)a", r"(?x:a b)", r"(?-x:a)"] {
        expect_bad_selector(selector);
    }
}

/// The ignore-case flag is refused with a non-ASCII character in the selector, whether written as
/// a character or as an escape such as `ı` or `\xff`. A differential run found gport matching
/// U+0131 (dotless i) and the other non-ASCII letters under `(?i)` differently from Python.
#[test]
fn ignore_case_with_non_ascii_selectors_are_bad_lines() {
    for selector in [
        "(?i)\u{131}",
        "(?i)[\u{131}]",
        "(?i:é)",
        r"(?i)ı",
        r"(?i)\xff",
    ] {
        expect_bad_selector(selector);
    }
}

/// A `ledger_selectors` list with an item that is not a string is an input error that names the
/// chunk, exit 2, as the record and check-slice verbs read the same field.
#[test]
fn list_ledger_selector_that_is_not_a_string_is_an_input_error() {
    let temp = synthetic(&["MCP x:a"], &[]);
    write(
        temp.path(),
        "docs/port/map-v2.json",
        &json!({"chunks": [{"id": "alpha", "ledger_selectors": [1]}]}).to_string(),
    );
    let run = run_ledger(temp.path(), &[]);
    expect_error(&run, "chunk alpha");
}

/// A chunk whose `ledger_selectors` is falsy owns nothing: the row is an orphan and the run exits 1.
/// Python's `chunk.get("ledger_selectors") or []` reads a falsy value as no selectors: null, a
/// missing key, false, 0, 0.0, "", [] and {}.
#[test]
fn falsy_ledger_selectors_own_nothing() {
    let mut maps = vec![json!({"chunks": [{"id": "alpha"}]})];
    for value in [
        json!(null),
        json!(false),
        json!(0),
        json!(0.0),
        json!(""),
        json!([]),
        json!({}),
    ] {
        maps.push(json!({"chunks": [{"id": "alpha", "ledger_selectors": value}]}));
    }
    for map in maps {
        let temp = synthetic(&["MCP x:a"], &[]);
        write(temp.path(), "docs/port/map-v2.json", &map.to_string());
        let run = run_ledger(temp.path(), &[]);
        expect_exit(&run, 1);
        expect_line(&run, "ORPHAN MCP x:a");
    }
}

/// A chunk whose `ledger_selectors` is truthy but not a list is an input error that names the
/// chunk, exit 2. Python would iterate its characters or fail, so gport does not read it as no
/// selectors.
#[test]
fn truthy_non_list_ledger_selectors_is_an_input_error() {
    for value in [json!("^MCP x:a$"), json!(true), json!(1), json!({"a": 1})] {
        let temp = synthetic(&["MCP x:a"], &[]);
        write(
            temp.path(),
            "docs/port/map-v2.json",
            &json!({"chunks": [{"id": "alpha", "ledger_selectors": value}]}).to_string(),
        );
        let run = run_ledger(temp.path(), &[]);
        expect_error(&run, "chunk alpha");
    }
}

/// A group name must be a Python identifier, as `re` reads `(?P<name>`. Verdicts from
/// `python3.13 -I`: `1a`, `a-b` and `a²` raise `bad character in group name`, `(?P<>x)` raises
/// `missing group name`, and `(?P=1a)` raises `bad character in group name` too.
#[test]
fn group_names_that_are_not_identifiers_are_bad_lines() {
    for selector in [
        r"(?P<1a>x)",
        r"(?P<a-b>x)",
        r"(?P<>x)",
        r"(?P<a²>x)",
        r"(?P<٣>x)",
        r"(?P=1a)",
    ] {
        expect_bad_selector(selector);
    }
}

/// A named group is refused whatever its name, Unicode identifiers included. Python compiles
/// `(?P<é>x)`, `(?P<ǅ>x)` and `(?P<_a1>x)`, each matching `x` and not `y`; gport reports each BAD
/// rather than match it, because a named group is one of the constructs `pyre` refuses.
/// Changed from an owns-what-it-matches test: the names were Python-valid, the constructs are now
/// refused.
#[test]
fn unicode_and_underscore_group_names_are_bad_lines() {
    for selector in [r"(?P<é>x)", r"(?P<ǅ>x)", r"(?P<_a1>x)"] {
        expect_bad_selector(selector);
    }
}

/// A named backreference is refused. Python compiles `(?P<a>x)(?P=a)` and matches `xx`, not `x`;
/// gport reports it BAD, since a backreference by name is one of the constructs `pyre` refuses.
/// Changed from an owns-what-it-matches test for the same reason.
#[test]
fn named_backreference_to_an_identifier_is_a_bad_line() {
    expect_bad_selector(r"(?P<a>x)(?P=a)");
}

/// A `-` in inline flags needs at least one flag after it, then a `:`. Verdicts from
/// `python3.13 -I`: `(?i-)a` and `(?-)a` give `missing flag`, `(?-i)a`, `(?i-m)a` and `(?i--m:a)`
/// give `missing :` or `missing flag`, and `(?i-:a)`, `(?-:a)` give `missing flag`.
#[test]
fn minus_in_inline_flags_needs_flags_and_a_colon_is_a_bad_line() {
    for selector in [
        r"(?i-)a",
        r"a(?i-)b",
        r"(?-)a",
        r"(?-i)a",
        r"(?i-m)a",
        r"(?x-s)a",
        r"(?i-:a)",
        r"(?-:a)",
        r"(?i--m:a)",
    ] {
        expect_bad_selector(selector);
    }
}

/// A flag that cannot be turned off, or a flag turned on and off in one group, is refused. Verdicts
/// from `python3.13 -I`: `(?-u:a)` gives `cannot turn off flags 'a', 'u' and 'L'`, and `(?i-i:a)`
/// gives `flag turned on and off`.
#[test]
fn scoped_flags_turned_off_that_python_refuses_are_bad_lines() {
    for selector in [r"(?-u:a)", r"(?-a:a)", r"(?i-i:a)"] {
        expect_bad_selector(selector);
    }
}

/// A minus with flags after it and a colon keeps Python's meaning. Verdicts from `python3.13 -I`
/// with `re.search`: `(?i-m:A)` matches `a` and `A`; `(?-i:a)` and `(?u-i:a)` match `a` and not `A`.
#[test]
fn scoped_flags_with_a_minus_keep_python_meaning() {
    assert_eq!(
        owners_under(r"(?i-m:A)", &["a", "A"]),
        "A\talpha\na\talpha\n"
    );
    assert_eq!(owners_under(r"(?-i:a)", &["a", "A"]), "A\t\na\talpha\n");
    assert_eq!(owners_under(r"(?u-i:a)", &["a", "A"]), "A\t\na\talpha\n");
}

/// The frozen ledger is split as Python's `splitlines` splits it, so a key holding one of its
/// separators is cut in two and is not a row. Each separator is checked on its own.
#[test]
fn row_keys_are_split_on_python_line_separators() {
    for separator in [
        "\r", "\u{b}", "\u{c}", "\u{1c}", "\u{1d}", "\u{1e}", "\u{85}", "\u{2028}", "\u{2029}",
    ] {
        let key = format!("MCP x:b{separator}c");
        let temp = synthetic(&["MCP x:a", &key], &[("alpha", &["^MCP x:a$"])]);
        let run = run_ledger(temp.path(), &[]);
        expect_exit(&run, 0);
        expect_line(
            &run,
            "rows=1 owned_once=1 orphans=0 double_owned=0 bad_selectors=0",
        );
    }
}

/// Python's `str.strip()` removes U+001C to U+001F, which Rust's `trim()` keeps. A waiver whose
/// behaviour is only such a character is an empty field and is refused with exit 2.
#[test]
fn waiver_behaviour_of_only_python_whitespace_is_refused() {
    let temp = with_waiver(json!({
        "id": "gobby.notes.unit-sep",
        "surface": "MCP x:a",
        "behaviour": "\u{1c}",
        "decision": "#5",
        "replacement": "Replacement text."
    }));
    let run = run_ledger(temp.path(), &["--stdout"]);
    expect_error(&run, "Waiver field behaviour must be a non-empty string");
}

/// A rationale of only U+001C is empty after Python's `strip()`, so the disposition is reported
/// `INVALID <ref>: no rationale`.
#[test]
fn disposition_rationale_of_only_python_whitespace_is_missing() {
    let temp = disposition_root(
        "disp-unit-sep.json",
        json!({"dispositions": [
            {"ref": "#1", "disposition": "chunk", "target": "alpha", "rationale": "ok"},
            {"ref": "#2", "disposition": "chunk", "target": "alpha", "rationale": "\u{1c}"},
            {"ref": "#3", "disposition": "chunk", "target": "alpha", "rationale": "ok"}
        ]}),
    );
    let run = run_ledger(temp.path(), &[]);
    expect_exit(&run, 1);
    expect_line(&run, "INVALID #2: no rationale");
}

/// The contract ledger is written through a sibling temp file and renamed over the target, so a
/// write that cannot finish leaves the existing ledger as it was. A read-only directory refuses the
/// temp file: the run exits 2 and the old bytes stay. Skipped where the directory stays writable,
/// such as when the tests run as root.
#[cfg(unix)]
#[test]
fn contract_ledger_write_goes_through_a_sibling_temp_file() {
    use std::os::unix::fs::PermissionsExt;

    let temp = synthetic(&["MCP x:a"], &[("alpha", &["^MCP x:a$"])]);
    let contracts = temp.path().join("docs/contracts");
    let target = contracts.join("parity-ledger.md");
    write(
        temp.path(),
        "docs/contracts/parity-ledger.md",
        "old ledger\n",
    );
    fs::set_permissions(&contracts, fs::Permissions::from_mode(0o555)).expect("chmod");
    if fs::write(contracts.join("probe"), "").is_ok() {
        fs::set_permissions(&contracts, fs::Permissions::from_mode(0o755)).expect("chmod");
        eprintln!("skipped: the directory stays writable for this user");
        return;
    }
    let run = run_ledger(temp.path(), &[]);
    let old = fs::read_to_string(&target).expect("old ledger still present");
    fs::set_permissions(&contracts, fs::Permissions::from_mode(0o755)).expect("chmod");
    expect_error(&run, "cannot write");
    assert!(old == "old ledger\n", "the old ledger was changed: {old:?}");
}

/// A successful run leaves no temp file beside the contract ledger.
#[test]
fn contract_ledger_run_leaves_no_temp_file_behind() {
    let temp = synthetic(&["MCP x:a"], &[("alpha", &["^MCP x:a$"])]);
    let run = run_ledger(temp.path(), &[]);
    expect_exit(&run, 0);
    let mut names: Vec<String> = fs::read_dir(temp.path().join("docs/contracts"))
        .expect("read contracts dir")
        .map(|entry| {
            entry
                .expect("dir entry")
                .file_name()
                .to_string_lossy()
                .into_owned()
        })
        .collect();
    names.sort();
    assert!(
        names
            == [
                "parity-ledger-status.d",
                "parity-ledger.md",
                "parity-waivers.json"
            ],
        "unexpected files beside the ledger: {names:?}"
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
