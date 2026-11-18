//! Contract tests for `gport record`, which regenerates docs/port/record.md.
//!
//! Each test builds a synthetic repository root in a temp directory, runs the
//! built `gport --root <dir> record`, and asserts on the exit code and the
//! record it writes. Exit 0 writes the record, exit 1 refuses, exit 2 is a
//! usage or input error. Nothing here touches the real repository or daemon.

use std::fs;
use std::path::{Path, PathBuf};

use serde_json::{Value, json};
use tempfile::TempDir;

use crate::support::{Run, gport};

const CHUNK_IDS: [&str; 3] = ["ALPHA", "BETA", "CHECK-GATE"];

const RULINGS: &str = r##"# Synthetic operator rulings

RULING-BODY-ONE: the first ruling body line.

## RULING-SECTION synthetic heading

RULING-BODY-TWO: a second body line with emphasis.
"##;

const LEDGER: &str = r##"# Parity ledger

| Public surface | Rust status | Notes |
| --- | --- | --- |
| `CLI gobby agents` | proxied | Synthetic note one. |
| `CLI gobby agents check` | proxied | Synthetic note two. |
| `CLI gobby tasks` | proxied | Synthetic note three. |
| `MCP gobby-tasks:create_task` | delegated | Synthetic note four. |
"##;

const DISPOSITIONS_ALPHA: &str = r##"{"dispositions": [{"ref": "#90001", "disposition": "chunk", "target": "ALPHA", "rationale": "DISP-RAT-ALPHA synthetic rationale."}]}"##;

const DISPOSITIONS_BETA: &str = r##"{"dispositions": [{"ref": "#90002", "disposition": "dropped", "target": "decision D2", "rationale": "DISP-RAT-BETA synthetic rationale."}]}"##;

/// One chunk of the synthetic map, with every key the real map carries.
fn chunk(
    id: &str,
    title: &str,
    kind: &str,
    selectors: &[&str],
    depends_on: &[&str],
    python_edits: &str,
) -> Value {
    json!({
        "id": id,
        "title": title,
        "kind": kind,
        "goal": format!("Synthetic goal for {id}."),
        "ledger_selectors": selectors,
        "engines": [],
        "depends_on": depends_on,
        "oracle": "Synthetic oracle.",
        "size": "S",
        "decision_refs": [],
        "seam_gaps": [],
        "tree_nodes": [],
        "python_edits": python_edits,
        "registration": "Synthetic registration text.",
        "risks": "Synthetic risks."
    })
}

fn decision(id: &str, question: &str, default: &str, rationale: &str, resolved_by: &str) -> Value {
    json!({
        "id": id,
        "question": question,
        "recommended_default": default,
        "rationale": rationale,
        "resolved_by": resolved_by
    })
}

/// The synthetic map. ALPHA owns three ledger rows, BETA owns none, and the
/// checkpoint CHECK-GATE owns the MCP row, so every row has one owner.
fn base_map() -> Value {
    json!({
        "report_path": "docs/port/report.md",
        "json_path": "docs/port/map-v2.json",
        "angle": "ANGLE-SENTINEL synthetic angle.",
        "summary": "SUMMARY-SENTINEL synthetic summary. It was brought up to the cut SHA, 0.5.0 at 0256724f1f, and carries three chunks.",
        "assumptions": ["ASSUMPTION-SENTINEL synthetic."],
        "cutover_model": "CUTOVER-SENTINEL synthetic cutover model.",
        "python_strategy": "PYSTRAT-SENTINEL synthetic python strategy.",
        "branching": "BRANCH-SENTINEL synthetic branching.",
        "live_checkout_constraints": {
            "intro": "LIVE-INTRO-SENTINEL synthetic intro.",
            "items": ["LIVE-ITEM-ONE-SENTINEL synthetic.", "LIVE-ITEM-TWO-SENTINEL synthetic."]
        },
        "rollback_and_packaging": "ROLLBACK-SENTINEL synthetic rollback.",
        "chunk_protocol": {
            "steps": [
                "STEP-ONE-SENTINEL synthetic.",
                "STEP-TWO-SENTINEL synthetic.",
                "STEP-THREE-SENTINEL synthetic."
            ],
            "note": "NOTE-SENTINEL synthetic note."
        },
        "start_gate": ["START-GATE-SENTINEL synthetic."],
        "chunks": [
            chunk("ALPHA", "Alpha title", "family", &["^CLI gobby agents", "^CLI gobby tasks$"], &[], "PY-EDITS-ALPHA synthetic."),
            chunk("BETA", "Beta title", "engine", &[], &["ALPHA"], "PY-EDITS-BETA synthetic."),
            chunk("CHECK-GATE", "Gate title", "checkpoint", &["^MCP gobby-tasks:"], &["ALPHA", "BETA"], "None.")
        ],
        "decisions": [
            decision("D1", "Q-ONE synthetic question?", "DEF-ONE synthetic default.", "RAT-ONE synthetic rationale.", "RES-ONE synthetic resolver."),
            decision("D2", "Q-TWO synthetic question?", "DEF-TWO synthetic default.", "RAT-TWO synthetic rationale.", "RES-TWO synthetic resolver.")
        ],
        "answers_25341": [],
        "open_problems": [],
        "coverage_selfcheck": "COVERAGE-SENTINEL synthetic."
    })
}

/// A synthetic repository root holding the record's inputs.
struct Fixture {
    dir: TempDir,
}

impl Fixture {
    fn new() -> Self {
        let fixture = Self {
            dir: TempDir::new().expect("temp dir"),
        };
        fixture.write_map(&base_map());
        fixture.write("docs/port/rulings.md", RULINGS);
        fixture.write("docs/port/dispositions/alpha.json", DISPOSITIONS_ALPHA);
        fixture.write("docs/port/dispositions/beta.json", DISPOSITIONS_BETA);
        fixture.write("docs/port/ledger.md", LEDGER);
        fixture
    }

    fn root(&self) -> &Path {
        self.dir.path()
    }

    fn write(&self, relative: &str, content: &str) {
        let path = self.root().join(relative);
        fs::create_dir_all(path.parent().expect("parent dir")).expect("create dirs");
        fs::write(path, content).expect("write fixture file");
    }

    fn write_map(&self, map: &Value) {
        let text = serde_json::to_string_pretty(map).expect("map serializes");
        self.write("docs/port/map-v2.json", &text);
    }

    fn record_path(&self) -> PathBuf {
        self.root().join("docs/port/record.md")
    }

    fn record(&self) -> String {
        fs::read_to_string(self.record_path()).expect("docs/port/record.md exists")
    }

    fn write_record(&self, text: &str) {
        fs::write(self.record_path(), text).expect("write record");
    }

    fn run(&self) -> Run {
        let root = self.root().to_str().expect("utf-8 temp path");
        gport(self.root(), &["--root", root, "record"])
    }
}

fn expect_code(run: &Run, code: i32) {
    assert_eq!(
        run.code, code,
        "gport record exit code\nstdout:\n{}\nstderr:\n{}",
        run.stdout, run.stderr
    );
}

/// Run the record and require exit 0; returns the written record.
fn generate(fixture: &Fixture) -> String {
    let run = fixture.run();
    expect_code(&run, 0);
    fixture.record()
}

fn expected_kept_names() -> Vec<String> {
    let mut names: Vec<String> = [
        "registration",
        "memory-rulings",
        "seam-contract",
        "known-bad",
    ]
    .iter()
    .map(|name| name.to_string())
    .collect();
    for id in CHUNK_IDS {
        names.push(format!("debt:{id}"));
        names.push(format!("found:{id}"));
    }
    names.sort();
    names
}

fn lines(text: &str) -> Vec<String> {
    text.lines().map(str::to_owned).collect()
}

/// The markdown heading level of a line, or None when it is not a heading.
fn level(line: &str) -> Option<usize> {
    let hashes = line.chars().take_while(|c| *c == '#').count();
    (hashes > 0 && line[hashes..].starts_with(' ')).then_some(hashes)
}

/// The lines under `heading` up to the next heading of the same or a higher level.
fn block(section: &[String], heading: &str) -> Vec<String> {
    let start = section
        .iter()
        .position(|line| line == heading)
        .unwrap_or_else(|| panic!("heading {heading:?} is missing"));
    let depth = level(heading).expect("heading line");
    section[start + 1..]
        .iter()
        .take_while(|line| level(line.as_str()).is_none_or(|n| n > depth))
        .cloned()
        .collect()
}

fn position_of(section: &[String], line: &str) -> usize {
    section
        .iter()
        .position(|candidate| candidate == line)
        .unwrap_or_else(|| panic!("line {line:?} is missing"))
}

/// Each heading appears, and in the order given.
fn assert_ordered(section: &[String], headings: &[&str]) {
    let positions: Vec<usize> = headings
        .iter()
        .map(|heading| position_of(section, heading))
        .collect();
    assert!(
        positions.windows(2).all(|pair| pair[0] < pair[1]),
        "headings are out of order: {headings:?}"
    );
}

/// Each sentinel appears in `text`, and in the order given.
fn assert_in_order(text: &str, sentinels: &[&str], within: &str) {
    let mut last = 0;
    for sentinel in sentinels {
        let at = text
            .find(*sentinel)
            .unwrap_or_else(|| panic!("{within} lacks {sentinel:?}:\n{text}"));
        assert!(at >= last, "{within}: {sentinel:?} is out of order");
        last = at;
    }
}

/// The chunk section for `id`, found by its `### <id>: <title>` heading.
fn chunk_block(all: &[String], id: &str, title: &str) -> Vec<String> {
    let chunks = block(all, "## Chunks");
    block(&chunks, &format!("### {id}: {title}"))
}

/// The keys listed as "- `key`" lines under a chunk's Owned rows heading.
fn owned_rows(chunk: &[String]) -> Vec<String> {
    block(chunk, "#### Owned rows")
        .iter()
        .filter_map(|line| {
            line.strip_prefix("- `")?
                .strip_suffix('`')
                .map(str::to_owned)
        })
        .collect()
}

fn open_marker(name: &str) -> String {
    format!("<!-- gport:kept {name} -->")
}

fn end_marker(name: &str) -> String {
    format!("<!-- gport:end {name} -->")
}

/// The indices of a kept block's open and end marker lines.
fn kept_span(all: &[String], name: &str) -> (usize, usize) {
    let open = open_marker(name);
    let end = end_marker(name);
    let start = all
        .iter()
        .position(|line| *line == open)
        .unwrap_or_else(|| panic!("no line {open:?}"));
    let stop = all[start + 1..]
        .iter()
        .position(|line| *line == end)
        .unwrap_or_else(|| panic!("no line {end:?} after {open:?}"))
        + start
        + 1;
    (start, stop)
}

/// The lines between a kept block's markers.
fn kept_body(text: &str, name: &str) -> Vec<String> {
    let all = lines(text);
    let (start, stop) = kept_span(&all, name);
    all[start + 1..stop].to_vec()
}

/// A kept block with its markers, as the text a run would copy.
fn kept_block_text(text: &str, name: &str) -> String {
    let all = lines(text);
    let (start, stop) = kept_span(&all, name);
    all[start..=stop]
        .iter()
        .map(|line| format!("{line}\n"))
        .collect()
}

/// Every kept block name in the record, in file order.
fn kept_names(text: &str) -> Vec<String> {
    lines(text)
        .iter()
        .filter_map(|line| {
            line.strip_prefix("<!-- gport:kept ")?
                .strip_suffix(" -->")
                .map(str::to_owned)
        })
        .collect()
}

/// Put `inserted` on the line after the open marker of `name`, as a hand edit.
fn insert_after_open(text: &str, name: &str, inserted: &str) -> String {
    let marker = format!("{}\n", open_marker(name));
    assert_eq!(
        text.matches(marker.as_str()).count(),
        1,
        "exactly one open marker for {name}"
    );
    text.replacen(marker.as_str(), &format!("{marker}{inserted}\n"), 1)
}

fn assert_kept_markers(section: &[String], name: &str, within: &str) {
    let open = open_marker(name);
    let end = end_marker(name);
    assert!(
        section.contains(&open),
        "{within} lacks {open:?}:\n{}",
        section.join("\n")
    );
    assert!(
        section.contains(&end),
        "{within} lacks {end:?}:\n{}",
        section.join("\n")
    );
}

#[test]
/// Contract: the record is written to docs/port/record.md under the root, and it starts with its title.
fn writes_record_under_root() {
    let fixture = Fixture::new();
    let text = generate(&fixture);
    assert!(
        text.starts_with("# Port record\n"),
        "the record does not start with its title:\n{text}"
    );
}

#[test]
/// Contract: a generated paragraph under the title names the command.
fn intro_paragraph_names_the_command() {
    let text = generate(&Fixture::new());
    let all = lines(&text);
    let intro = &all[..position_of(&all, "## Direction")];
    assert!(
        intro
            .iter()
            .any(|line| !line.starts_with('#') && line.contains("gport record")),
        "no intro paragraph names gport record:\n{}",
        intro.join("\n")
    );
}

#[test]
/// Contract: every section of the record grammar appears, in grammar order.
fn sections_appear_in_grammar_order() {
    let all = lines(&generate(&Fixture::new()));
    assert_ordered(
        &all,
        &[
            "# Port record",
            "## Direction",
            "## Reference base",
            "## Chunk protocol",
            "## Registration protocol",
            "## Decisions",
            "## Rulings",
            "## Rulings from memory",
            "## Open-node dispositions",
            "## Chunks",
            "## Frozen seam contract",
            "## Known-bad index",
            "## Found Work",
        ],
    );
}

#[test]
/// Contract: Direction carries the map's summary, angle, cutover model, python strategy, branching, live checkout constraints and rollback text, in that order.
fn direction_holds_the_map_fields_in_order() {
    let all = lines(&generate(&Fixture::new()));
    let direction = block(&all, "## Direction").join("\n");
    assert_in_order(
        &direction,
        &[
            "SUMMARY-SENTINEL",
            "ANGLE-SENTINEL",
            "CUTOVER-SENTINEL",
            "PYSTRAT-SENTINEL",
            "BRANCH-SENTINEL",
            "LIVE-INTRO-SENTINEL",
            "LIVE-ITEM-ONE-SENTINEL",
            "LIVE-ITEM-TWO-SENTINEL",
            "ROLLBACK-SENTINEL",
        ],
        "## Direction",
    );
}

#[test]
/// Contract: Reference base names the cut SHA from the map summary.
fn reference_base_names_the_cut_sha() {
    let all = lines(&generate(&Fixture::new()));
    let base = block(&all, "## Reference base").join("\n");
    assert!(
        base.contains("0256724f1f"),
        "Reference base lacks the cut SHA:\n{base}"
    );
}

#[test]
/// Contract: chunk protocol steps are numbered 1, 2, 3 in order, and the note follows the last step.
fn chunk_protocol_numbers_steps_then_the_note() {
    let all = lines(&generate(&Fixture::new()));
    let protocol = block(&all, "## Chunk protocol");
    let steps = [
        "STEP-ONE-SENTINEL",
        "STEP-TWO-SENTINEL",
        "STEP-THREE-SENTINEL",
    ];
    let mut positions = Vec::new();
    for (index, sentinel) in steps.iter().enumerate() {
        let prefix = format!("{}. ", index + 1);
        let at = protocol
            .iter()
            .position(|line| line.starts_with(prefix.as_str()) && line.contains(*sentinel))
            .unwrap_or_else(|| {
                panic!(
                    "step {sentinel:?} is not numbered {prefix:?}:\n{}",
                    protocol.join("\n")
                )
            });
        positions.push(at);
    }
    assert!(
        positions.windows(2).all(|pair| pair[0] < pair[1]),
        "steps are out of order: {positions:?}"
    );
    let note = protocol
        .iter()
        .position(|line| line.contains("NOTE-SENTINEL"))
        .expect("the note is in the chunk protocol");
    assert!(
        note > positions[2],
        "the note does not follow the last step"
    );
}

#[test]
/// Contract: the registration kept block sits under the Registration protocol heading.
fn registration_protocol_holds_its_kept_block() {
    let all = lines(&generate(&Fixture::new()));
    assert_kept_markers(
        &block(&all, "## Registration protocol"),
        "registration",
        "## Registration protocol",
    );
}

#[test]
/// Contract: each decision is `### <id>: <question>` followed by its default, rationale and resolved_by.
fn decisions_render_heading_default_rationale_and_resolver() {
    let all = lines(&generate(&Fixture::new()));
    let decisions = block(&all, "## Decisions");
    let first = block(&decisions, "### D1: Q-ONE synthetic question?").join("\n");
    let second = block(&decisions, "### D2: Q-TWO synthetic question?").join("\n");
    for sentinel in ["DEF-ONE", "RAT-ONE", "RES-ONE"] {
        assert!(first.contains(sentinel), "D1 lacks {sentinel}:\n{first}");
    }
    for sentinel in ["DEF-TWO", "RAT-TWO", "RES-TWO"] {
        assert!(second.contains(sentinel), "D2 lacks {sentinel}:\n{second}");
    }
    assert!(
        !first.contains("TWO") && !second.contains("ONE"),
        "a decision's section holds another decision's text"
    );
}

#[test]
/// Contract: rulings.md appears under Rulings with headings demoted two levels, and its body lines verbatim.
fn rulings_copy_headings_demoted_two_levels() {
    let all = lines(&generate(&Fixture::new()));
    let rulings = block(&all, "## Rulings");
    for expected in [
        "### Synthetic operator rulings",
        "#### RULING-SECTION synthetic heading",
        "RULING-BODY-ONE: the first ruling body line.",
        "RULING-BODY-TWO: a second body line with emphasis.",
    ] {
        assert!(
            rulings.iter().any(|line| line == expected),
            "Rulings lacks {expected:?}:\n{}",
            rulings.join("\n")
        );
    }
    assert!(
        !all.iter().any(|line| line == "# Synthetic operator rulings"
            || line == "## RULING-SECTION synthetic heading"),
        "a rulings heading was copied without demotion"
    );
}

#[test]
/// Contract: the Rulings from memory section holds the memory-rulings kept block.
fn rulings_from_memory_holds_its_kept_block() {
    let all = lines(&generate(&Fixture::new()));
    assert_kept_markers(
        &block(&all, "## Rulings from memory"),
        "memory-rulings",
        "## Rulings from memory",
    );
}

#[test]
/// Contract: every disposition from every dispositions file is one table row carrying its ref, disposition, target and rationale.
fn open_node_dispositions_are_table_rows() {
    let all = lines(&generate(&Fixture::new()));
    let rows = block(&all, "## Open-node dispositions");
    let expected = [
        (
            "#90001",
            "chunk",
            "ALPHA",
            "DISP-RAT-ALPHA synthetic rationale.",
        ),
        (
            "#90002",
            "dropped",
            "decision D2",
            "DISP-RAT-BETA synthetic rationale.",
        ),
    ];
    for (reference, disposition, target, rationale) in expected {
        let found = rows.iter().any(|row| {
            row.starts_with('|')
                && [reference, disposition, target, rationale]
                    .iter()
                    .all(|cell| row.contains(*cell))
        });
        assert!(found, "no table row for {reference}:\n{}", rows.join("\n"));
    }
}

#[test]
/// Contract: each chunk section is `### <id>: <title>`, in map order, with its kind, depends_on and python_edits.
fn chunk_sections_hold_heading_kind_depends_on_and_python_edits() {
    let all = lines(&generate(&Fixture::new()));
    assert_ordered(
        &block(&all, "## Chunks"),
        &[
            "### ALPHA: Alpha title",
            "### BETA: Beta title",
            "### CHECK-GATE: Gate title",
        ],
    );
    let alpha = chunk_block(&all, "ALPHA", "Alpha title").join("\n");
    let beta = chunk_block(&all, "BETA", "Beta title").join("\n");
    let gate = chunk_block(&all, "CHECK-GATE", "Gate title").join("\n");
    assert!(
        alpha.contains("family") && alpha.contains("PY-EDITS-ALPHA synthetic."),
        "ALPHA section lacks its kind or python_edits:\n{alpha}"
    );
    assert!(
        beta.contains("engine")
            && beta.contains("ALPHA")
            && beta.contains("PY-EDITS-BETA synthetic."),
        "BETA section lacks its kind, depends_on or python_edits:\n{beta}"
    );
    assert!(
        gate.contains("checkpoint")
            && gate.contains("ALPHA")
            && gate.contains("BETA")
            && gate.contains("None."),
        "CHECK-GATE section lacks its kind, depends_on or python_edits:\n{gate}"
    );
}

#[test]
/// Contract: a chunk's owned rows are one "- `key`" line for each ledger row its selectors match, and no other row.
fn owned_rows_list_exactly_the_keys_the_selectors_own() {
    let all = lines(&generate(&Fixture::new()));
    let mut alpha = owned_rows(&chunk_block(&all, "ALPHA", "Alpha title"));
    alpha.sort();
    assert_eq!(
        alpha,
        vec![
            "CLI gobby agents",
            "CLI gobby agents check",
            "CLI gobby tasks"
        ]
    );
    let gate = owned_rows(&chunk_block(&all, "CHECK-GATE", "Gate title"));
    assert_eq!(gate, vec!["MCP gobby-tasks:create_task"]);
}

#[test]
/// Contract: a chunk whose selectors are empty has an Owned rows heading with an empty list.
fn chunk_without_selectors_has_empty_owned_rows() {
    let all = lines(&generate(&Fixture::new()));
    let beta = chunk_block(&all, "BETA", "Beta title");
    // block() panics when the heading is missing, so this also checks the heading exists.
    let _ = block(&beta, "#### Owned rows");
    assert_eq!(owned_rows(&beta), Vec::<String>::new());
}

#[test]
/// Contract: a selector that fails at run time is an input error: exit 2, the message names the chunk and the selector, and no record is written.
fn selector_that_fails_to_run_is_an_input_error_and_writes_nothing() {
    let fixture = Fixture::new();
    let key = format!("MCP gobby-tasks:{}?", "a".repeat(40));
    fixture.write(
        "docs/port/ledger.md",
        &format!("{LEDGER}| `{key}` | proxied | Synthetic note five. |\n"),
    );
    let mut map = base_map();
    map["chunks"][1]["ledger_selectors"] = json!(["^MCP gobby-tasks:(a+)+(?!z)!"]);
    fixture.write_map(&map);
    let run = fixture.run();
    expect_code(&run, 2);
    assert!(
        run.stderr.contains("BETA") && run.stderr.contains("^MCP gobby-tasks:(a+)+"),
        "the message does not name BETA and its selector:\nstderr:\n{}",
        run.stderr
    );
    assert!(
        !fixture.record_path().exists(),
        "an input error wrote the record"
    );
}

#[test]
/// Contract: a falsy `ledger_selectors` means no selectors, as Python's `or []` reads it. The map
/// parses, BETA owns no rows, and the record is written with an empty Owned rows list for BETA.
fn record_falsy_ledger_selectors_parse_and_own_nothing() {
    for value in [json!(false), json!(0), json!(""), json!({})] {
        let fixture = Fixture::new();
        let mut map = base_map();
        map["chunks"][1]["ledger_selectors"] = value.clone();
        fixture.write_map(&map);
        let all = lines(&generate(&fixture));
        let beta = chunk_block(&all, "BETA", "Beta title");
        assert_eq!(owned_rows(&beta), Vec::<String>::new(), "{value}");
    }
}

#[test]
/// Contract: a `ledger_selectors` that is truthy but not a list, or a list with an item that is not
/// a string, is an input error: exit 2, stderr names the chunk, and no record is written.
fn record_malformed_ledger_selectors_is_an_input_error_naming_the_chunk() {
    for value in [json!("x"), json!(true), json!(1), json!([1])] {
        let fixture = Fixture::new();
        let mut map = base_map();
        map["chunks"][1]["ledger_selectors"] = value.clone();
        fixture.write_map(&map);
        let run = fixture.run();
        expect_code(&run, 2);
        assert!(
            run.stderr.contains("BETA"),
            "{value}: stderr does not name BETA\nstderr:\n{}",
            run.stderr
        );
        assert!(
            !fixture.record_path().exists(),
            "an input error wrote the record"
        );
    }
}

#[test]
/// Contract: each chunk section holds its debt kept block under "#### Debt not to port".
fn chunk_sections_hold_their_debt_kept_blocks() {
    let all = lines(&generate(&Fixture::new()));
    for (id, title) in [
        ("ALPHA", "Alpha title"),
        ("BETA", "Beta title"),
        ("CHECK-GATE", "Gate title"),
    ] {
        let debt = block(&chunk_block(&all, id, title), "#### Debt not to port");
        assert_kept_markers(&debt, &format!("debt:{id}"), "#### Debt not to port");
    }
}

#[test]
/// Contract: Found Work holds a `### <chunk id>` heading per chunk in map order, each with its found kept block.
fn found_work_holds_one_kept_block_per_chunk_in_map_order() {
    let all = lines(&generate(&Fixture::new()));
    let found = block(&all, "## Found Work");
    assert_ordered(&found, &["### ALPHA", "### BETA", "### CHECK-GATE"]);
    for id in CHUNK_IDS {
        let heading = format!("### {id}");
        let section = block(&found, &heading);
        assert_kept_markers(&section, &format!("found:{id}"), &heading);
    }
}

#[test]
/// Contract: the seam-contract kept block sits under Frozen seam contract, and known-bad under Known-bad index.
fn seam_contract_and_known_bad_hold_their_kept_blocks() {
    let all = lines(&generate(&Fixture::new()));
    assert_kept_markers(
        &block(&all, "## Frozen seam contract"),
        "seam-contract",
        "## Frozen seam contract",
    );
    assert_kept_markers(
        &block(&all, "## Known-bad index"),
        "known-bad",
        "## Known-bad index",
    );
}

#[test]
/// Contract: a freshly generated record holds an empty body in every kept block.
fn fresh_kept_blocks_start_empty() {
    let text = generate(&Fixture::new());
    for name in expected_kept_names() {
        let body = kept_body(&text, &name);
        assert!(
            body.iter().all(|line| line.trim().is_empty()),
            "kept block {name} starts with text: {body:?}"
        );
    }
}

#[test]
/// Contract: the record holds each kept block name exactly once, and no kept block outside the expected set.
fn kept_block_names_are_unique_and_complete() {
    let text = generate(&Fixture::new());
    let mut names = kept_names(&text);
    names.sort();
    assert_eq!(names, expected_kept_names());
}

#[test]
/// Contract: running twice with no input change writes identical bytes.
fn second_run_with_no_input_change_is_byte_identical() {
    let fixture = Fixture::new();
    let first = generate(&fixture);
    let second = generate(&fixture);
    assert_eq!(first, second, "a second run with no input change differs");
}

#[test]
/// Contract: hand text inside kept blocks (registration, the frozen file list, debt, a Found Work line) survives a second run byte for byte.
fn hand_written_kept_text_survives_a_second_run_byte_for_byte() {
    let fixture = Fixture::new();
    let generated = generate(&fixture);
    let edited = insert_after_open(
        &generated,
        "registration",
        "HAND-REGISTRATION-SENTINEL written by hand.",
    );
    let edited = insert_after_open(
        &edited,
        "seam-contract",
        "### Frozen files\n- `crates/gport/src/record.rs`",
    );
    let edited = insert_after_open(&edited, "debt:ALPHA", "- `HAND-DEBT-SENTINEL`");
    let edited = insert_after_open(
        &edited,
        "found:ALPHA",
        "- `crates/gport/src/record.rs` HAND-FOUND-SENTINEL",
    );
    fixture.write_record(&edited);
    let rerun = generate(&fixture);
    assert_eq!(
        rerun, edited,
        "a second run changed hand-written kept block text"
    );
}

#[test]
/// Contract: generated sections change when the map changes, and the rerun follows the new map.
fn generated_sections_follow_the_map() {
    let fixture = Fixture::new();
    let before = generate(&fixture);
    assert!(
        before.contains("RAT-ONE synthetic rationale."),
        "the first record lacks RAT-ONE"
    );
    assert!(
        before.contains("PY-EDITS-BETA synthetic."),
        "the first record lacks PY-EDITS-BETA"
    );
    let mut map = base_map();
    map["decisions"][0]["rationale"] = json!("RAT-REWRITTEN-SENTINEL synthetic rationale.");
    map["chunks"][1]["python_edits"] = json!("PY-EDITS-REWRITTEN-SENTINEL synthetic.");
    fixture.write_map(&map);
    let after = generate(&fixture);
    assert!(
        after.contains("RAT-REWRITTEN-SENTINEL synthetic rationale."),
        "the rerun lacks the new decision rationale"
    );
    assert!(
        after.contains("PY-EDITS-REWRITTEN-SENTINEL synthetic."),
        "the rerun lacks the new python_edits"
    );
    assert!(
        !after.contains("RAT-ONE synthetic rationale."),
        "the rerun keeps the old decision rationale"
    );
    assert!(
        !after.contains("PY-EDITS-BETA synthetic."),
        "the rerun keeps the old python_edits"
    );
}

#[test]
/// Contract: a chunk added to the map gets empty debt and found kept blocks, and its heading appears in map order.
fn added_chunk_gets_empty_kept_blocks() {
    let fixture = Fixture::new();
    let _ = generate(&fixture);
    let mut map = base_map();
    map["chunks"]
        .as_array_mut()
        .expect("chunks is an array")
        .push(chunk(
            "DELTA",
            "Delta title",
            "family",
            &[],
            &["BETA"],
            "PY-EDITS-DELTA synthetic.",
        ));
    fixture.write_map(&map);
    let text = generate(&fixture);
    let all = lines(&text);
    assert_ordered(
        &block(&all, "## Chunks"),
        &[
            "### ALPHA: Alpha title",
            "### BETA: Beta title",
            "### CHECK-GATE: Gate title",
            "### DELTA: Delta title",
        ],
    );
    let delta = chunk_block(&all, "DELTA", "Delta title");
    assert_kept_markers(
        &block(&delta, "#### Debt not to port"),
        "debt:DELTA",
        "#### Debt not to port",
    );
    let found = block(&all, "## Found Work");
    assert_ordered(
        &found,
        &["### ALPHA", "### BETA", "### CHECK-GATE", "### DELTA"],
    );
    assert_kept_markers(&block(&found, "### DELTA"), "found:DELTA", "### DELTA");
    for name in ["debt:DELTA", "found:DELTA"] {
        let body = kept_body(&text, name);
        assert!(
            body.iter().all(|line| line.trim().is_empty()),
            "new kept block {name} is not empty: {body:?}"
        );
    }
}

#[test]
/// Contract: an existing kept block whose name has no place in the new layout makes the run exit 1, names the block, and leaves the record unchanged.
fn unplaced_kept_block_refuses_and_leaves_record_unchanged() {
    let fixture = Fixture::new();
    let mut map = base_map();
    map["chunks"]
        .as_array_mut()
        .expect("chunks is an array")
        .push(chunk(
            "GONE",
            "Gone title",
            "family",
            &[],
            &[],
            "PY-EDITS-GONE synthetic.",
        ));
    fixture.write_map(&map);
    let _ = generate(&fixture);
    let edited = insert_after_open(
        &fixture.record(),
        "found:GONE",
        "- `FOUND-GONE-SENTINEL` hand line",
    );
    fixture.write_record(&edited);
    fixture.write_map(&base_map());
    let run = fixture.run();
    expect_code(&run, 1);
    let report = format!("{}{}", run.stdout, run.stderr);
    assert!(
        report.contains("found:GONE"),
        "the refusal does not name found:GONE:\n{report}"
    );
    assert_eq!(fixture.record(), edited, "a refused run changed the record");
}

#[test]
/// Contract: a record holding two kept blocks of one name is never rewritten with two; the run either refuses naming the block or writes one of each.
fn duplicated_kept_block_is_never_written_twice() {
    let fixture = Fixture::new();
    let base = generate(&fixture);
    let duplicated = format!("{base}{}", kept_block_text(&base, "found:ALPHA"));
    fixture.write_record(&duplicated);
    let run = fixture.run();
    assert!(
        run.code == 0 || run.code == 1,
        "a duplicated kept block is a refusal (1) or resolved (0), got {}\nstdout:\n{}\nstderr:\n{}",
        run.code,
        run.stdout,
        run.stderr
    );
    if run.code == 0 {
        let mut names = kept_names(&fixture.record());
        let total = names.len();
        names.sort();
        names.dedup();
        assert_eq!(
            names.len(),
            total,
            "the written record repeats a kept block name"
        );
    } else {
        let report = format!("{}{}", run.stdout, run.stderr);
        assert!(
            report.contains("found:ALPHA"),
            "the refusal does not name the duplicated block:\n{report}"
        );
    }
}

#[test]
/// Contract: a kept block's body survives a run byte for byte, a carriage return at the end of a line included.
fn kept_body_keeps_carriage_returns_byte_for_byte() {
    let fixture = Fixture::new();
    let generated = generate(&fixture);
    let edited = insert_after_open(&generated, "registration", "- crlf body line\r");
    fixture.write_record(&edited);
    let rerun = generate(&fixture);
    assert!(
        rerun.contains("- crlf body line\r\n"),
        "a kept body line lost its carriage return"
    );
    assert_eq!(rerun, edited, "a second run changed a CRLF kept block");
}

#[test]
/// Guard: a record written with CRLF line endings still reads its kept blocks, so their text survives a run.
fn crlf_record_keeps_its_kept_blocks() {
    let fixture = Fixture::new();
    let generated = generate(&fixture);
    let open = open_marker("found:ALPHA");
    let crlf = generated.replace('\n', "\r\n").replacen(
        &format!("{open}\r\n"),
        &format!("{open}\r\n- `crates/x.rs` CRLF-SENTINEL\r\n"),
        1,
    );
    fixture.write_record(&crlf);
    let rerun = generate(&fixture);
    assert!(
        rerun.contains("CRLF-SENTINEL"),
        "a CRLF record lost its kept text"
    );
    assert_eq!(
        kept_body(&rerun, "found:ALPHA"),
        ["- `crates/x.rs` CRLF-SENTINEL"]
    );
}

#[test]
/// Contract: a run replaces the record through a rename, so a hard link to the old record keeps the bytes it had.
fn rerun_replaces_the_record_instead_of_rewriting_it_in_place() {
    let fixture = Fixture::new();
    let before = generate(&fixture);
    let link = fixture.root().join("docs/port/record-before.md");
    fs::hard_link(fixture.record_path(), &link).expect("hard link to the record");
    let mut map = base_map();
    map["summary"] = json!(
        "SUMMARY-REWRITTEN-SENTINEL synthetic summary. It was brought up to the cut SHA, 0.5.0 at 0256724f1f and carries three chunks."
    );
    fixture.write_map(&map);
    let rewritten = generate(&fixture);
    assert!(
        rewritten.contains("SUMMARY-REWRITTEN-SENTINEL"),
        "the rerun lacks the new summary"
    );
    assert_eq!(
        fs::read_to_string(&link).expect("read the hard link"),
        before,
        "the run rewrote the record in place"
    );
}

/// The names of the entries directly under docs/port, sorted.
fn port_entries(fixture: &Fixture) -> Vec<String> {
    let mut names: Vec<String> = fs::read_dir(fixture.root().join("docs/port"))
        .expect("docs/port exists")
        .map(|entry| {
            entry
                .expect("directory entry")
                .file_name()
                .to_string_lossy()
                .into_owned()
        })
        .collect();
    names.sort();
    names
}

#[test]
/// Contract: a run leaves no file beside the record, whether it writes or refuses.
fn run_leaves_no_file_beside_the_record() {
    let fixture = Fixture::new();
    let expected = [
        "dispositions",
        "ledger.md",
        "map-v2.json",
        "record.md",
        "rulings.md",
    ];
    let before = generate(&fixture);
    assert_eq!(port_entries(&fixture), expected);
    fixture.write_record(&format!("{before}<!-- gport:kept stray -->\n"));
    expect_code(&fixture.run(), 1);
    assert_eq!(port_entries(&fixture), expected);
}

/// Run over changed inputs: exit 2, the report names the source and the marker, and the record is unchanged.
fn assert_marker_refusal(fixture: &Fixture, before: &str, source: &str, marker: &str) {
    let run = fixture.run();
    expect_code(&run, 2);
    let report = format!("{}{}", run.stdout, run.stderr);
    assert!(
        report.contains(source),
        "the refusal does not name {source}:\n{report}"
    );
    assert!(
        report.contains(marker),
        "the refusal does not name the marker {marker}:\n{report}"
    );
    assert_eq!(fixture.record(), before, "a refused run changed the record");
}

#[test]
/// Contract: a rulings line shaped like a kept-block marker is an input error that names docs/port/rulings.md, and writes nothing.
fn marker_shaped_rulings_line_is_an_input_error() {
    let fixture = Fixture::new();
    let before = generate(&fixture);
    let marker = open_marker("memory-rulings");
    fixture.write(
        "docs/port/rulings.md",
        &format!("{RULINGS}\n{marker}\n{}\n", end_marker("memory-rulings")),
    );
    assert_marker_refusal(&fixture, &before, "docs/port/rulings.md", &marker);
}

#[test]
/// Contract: a map string holding a marker line is an input error that names the map, and writes nothing.
fn marker_shaped_map_string_is_an_input_error() {
    let fixture = Fixture::new();
    let before = generate(&fixture);
    let marker = open_marker("known-bad");
    let mut map = base_map();
    map["summary"] = json!(format!(
        "SUMMARY-SENTINEL synthetic summary. It was brought up to the cut SHA, 0.5.0 at 0256724f1f.\n{marker}"
    ));
    fixture.write_map(&map);
    assert_marker_refusal(&fixture, &before, "docs/port/map-v2.json", &marker);
}

#[test]
/// Contract: a list item holding a marker line on a later line is an input error that names the map.
fn marker_shaped_list_item_is_an_input_error() {
    let fixture = Fixture::new();
    let before = generate(&fixture);
    let marker = end_marker("seam-contract");
    let mut map = base_map();
    map["chunk_protocol"]["steps"][1] = json!(format!("STEP-TWO-SENTINEL synthetic.\n{marker}"));
    fixture.write_map(&map);
    assert_marker_refusal(&fixture, &before, "docs/port/map-v2.json", &marker);
}

#[test]
/// Contract: a disposition holding a marker line is an input error that names its dispositions file, and writes nothing.
fn marker_shaped_disposition_is_an_input_error() {
    let fixture = Fixture::new();
    let before = generate(&fixture);
    let marker = open_marker("registration");
    let disposition = json!({
        "dispositions": [{
            "ref": "#90001",
            "disposition": "chunk",
            "target": "ALPHA",
            "rationale": format!("DISP-RAT-ALPHA synthetic rationale.\n{marker}")
        }]
    });
    fixture.write(
        "docs/port/dispositions/alpha.json",
        &disposition.to_string(),
    );
    assert_marker_refusal(
        &fixture,
        &before,
        "docs/port/dispositions/alpha.json",
        &marker,
    );
}
