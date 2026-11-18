//! Contract tests for `gport census`.
//!
//! Each test builds a synthetic tree in a temp directory and runs the built binary with `--root`
//! pointing at it. Runs also pass `--generations` and set `HOME` to a temp directory, so the real
//! home is never read. Only the real-tree test reads the repository. Every test fails today at its
//! first assertion, because the verb exits 2 until it is implemented.

use std::path::{Path, PathBuf};

use tempfile::TempDir;

use crate::support::{Run, gport_env, repo_root};

/// The eight section titles in spec order, matched case-insensitively in Markdown headings.
const SECTIONS: [&str; 8] = [
    "package",
    "caller",
    "writer",
    "cache",
    "registr",
    "tool",
    "surface",
    "generation",
];

/// The top-level packages under `src/gobby` in the synthetic tree.
const PACKAGES: [&str; 6] = ["alpha", "beta", "gamma", "delta", "servers", "install"];

/// Every fixture module as (package, file stem), for checks that a module is listed or not.
const MODULES: &[(&str, &str)] = &[
    ("alpha", "catalog"),
    ("alpha", "store"),
    ("alpha", "memo_lookup"),
    ("beta", "service"),
    ("beta", "reader"),
    ("beta", "row_snapshot"),
    ("gamma", "cleanup"),
    ("gamma", "route_bus"),
    ("gamma", "bare_calls"),
    ("delta", "session_notify"),
    ("delta", "session_rows"),
    ("delta", "handlers"),
    ("servers", "_app_ui"),
    ("servers", "dispatch"),
    ("servers", "other"),
    ("install", "installer"),
];

/// Synthetic files: a path under the tree root and the file's lines. Each file ends with a newline.
#[rustfmt::skip]
const FILES: &[(&str, &[&str])] = &[
    ("src/gobby/alpha/catalog.py", &[
        "\"\"\"Catalog of widget lookups.\"\"\"",
        "",
        "class SomethingRegistry:",
        "    \"\"\"Holds the widget kinds.\"\"\"",
        "",
        "    def __init__(self) -> None:",
        "        self.kinds: dict[str, str] = {}",
        "",
        "    def clone(self) -> \"SomethingRegistry\":",
        "        return SomethingRegistry()",
    ]),
    ("src/gobby/alpha/store.py", &[
        "\"\"\"Widget storage.\"\"\"",
        "from gobby.alpha import catalog",
        "",
        "",
        "def add_widget(db, widget_id: str) -> None:",
        "    db.execute(\"INSERT INTO widgets (id) VALUES (?)\", (widget_id,))",
        "",
        "",
        "def list_widgets(db) -> list:",
        "    kinds = catalog.SomethingRegistry()",
        "    return [kinds, db.execute(\"SELECT id FROM widgets\").fetchall()]",
    ]),
    ("src/gobby/alpha/memo_lookup.py", &[
        "\"\"\"Memoized kind resolution.\"\"\"",
        "import functools",
        "from gobby.alpha import catalog",
        "",
        "",
        "@functools.lru_cache(maxsize=None)",
        "def resolve_kind(name: str) -> str:",
        "    return name.lower()",
        "",
        "",
        "def reset_kinds() -> None:",
        "    resolve_kind.cache_clear()",
    ]),
    ("src/gobby/beta/service.py", &[
        "\"\"\"Gadget service.\"\"\"",
        "import gobby.alpha.store",
        "from gobby.beta import reader",
        "",
        "",
        "def rename_gadget(db, gadget_id: str, name: str) -> None:",
        "    db.execute(\"UPDATE gadgets SET name = ? WHERE id = ?\", (name, gadget_id))",
        "",
        "",
        "def touch(db, widget_id: str) -> None:",
        "    gobby.alpha.store.add_widget(db, widget_id)",
    ]),
    ("src/gobby/beta/reader.py", &[
        "\"\"\"Read-only gadget views.\"\"\"",
        "from ..alpha import catalog",
        "from ..alpha import store",
        "",
        "",
        "def gadget_names(db) -> list:",
        "    store.list_widgets(db)",
        "    kinds = catalog.SomethingRegistry()",
        "    return [kinds, db.execute(\"SELECT name FROM gadgets\").fetchall()]",
        "",
        "",
        "def gadget_kinds() -> object:",
        "    return isinstance(catalog.SomethingRegistry(), object)",
    ]),
    ("src/gobby/beta/row_snapshot.py", &[
        "\"\"\"Gadget row snapshots.\"\"\"",
        "",
        "",
        "class RowSnapshot:",
        "    def __init__(self) -> None:",
        "        self._row_cache: dict[str, object] = {}",
        "",
        "    def get(self, key: str) -> object:",
        "        return self._row_cache.get(key)",
    ]),
    ("src/gobby/gamma/cleanup.py", &[
        "\"\"\"Retention cleanup.\"\"\"",
        "from gobby.alpha import catalog",
        "from gobby.beta import service",
        "",
        "",
        "def purge_orders(db, order_id: str) -> None:",
        "    db.execute(\"DELETE FROM orders WHERE id = ?\", (order_id,))",
        "",
        "",
        "def kind_count() -> int:",
        "    return len(catalog.SomethingRegistry().kinds)",
    ]),
    ("src/gobby/gamma/route_bus.py", &[
        "\"\"\"Route table with a TTL cache.\"\"\"",
        "from cachetools import TTLCache",
        "",
        "",
        "class RouteBus:",
        "    def __init__(self) -> None:",
        "        self._route_cache = TTLCache(maxsize=64, ttl=30)",
        "",
        "    def invalidate_routes(self) -> None:",
        "        self._route_cache.clear()",
        "        self.db.execute(\"NOTIFY routes_changed\")",
    ]),
    ("src/gobby/gamma/bare_calls.py", &[
        "\"\"\"Calls by bare name.\"\"\"",
        "",
        "",
        "def sweep(db) -> None:",
        "    create_task(db)",
        "    recall_note(db)",
    ]),
    ("src/gobby/delta/session_notify.py", &[
        "\"\"\"Session change notices.\"\"\"",
        "from gobby.delta import session_rows",
        "",
        "",
        "def invalidate_session(db, session_id: str) -> None:",
        "    db.execute(\"NOTIFY session_changed\", ())",
    ]),
    ("src/gobby/delta/session_rows.py", &[
        "\"\"\"Session rows.\"\"\"",
        "",
        "",
        "def get_session(db, session_id: str) -> object:",
        "    return db.execute(\"SELECT * FROM sessions WHERE id = ?\", (session_id,)).fetchone()",
    ]),
    ("src/gobby/delta/handlers.py", &[
        "\"\"\"Memory tool handlers.\"\"\"",
        "MEMORY_TOOL = \"recall_note\"",
        "",
        "",
        "def handle(db) -> None:",
        "    return None",
    ]),
    ("src/gobby/servers/_app_ui.py", &[
        "\"\"\"Web UI routes.\"\"\"",
        "from fastapi import APIRouter",
        "",
        "",
        "app = APIRouter()",
        "",
        "",
        "@app.get(\"/api/census-widgets\")",
        "def widgets_view() -> list:",
        "    return []",
        "",
        "",
        "@app.websocket(\"/ws/census-feed\")",
        "async def feed(socket) -> None:",
        "    return None",
    ]),
    ("src/gobby/servers/dispatch.py", &[
        "\"\"\"Task tool dispatch.\"\"\"",
        "TASK_TOOL = \"create_task\"",
        "",
        "",
        "def dispatch(name: str) -> bool:",
        "    return name == TASK_TOOL",
    ]),
    ("src/gobby/servers/other.py", &[
        "\"\"\"Non-UI routes.\"\"\"",
        "from fastapi import APIRouter",
        "",
        "",
        "app = APIRouter()",
        "",
        "",
        "@app.get(\"/api/census-hidden\")",
        "def hidden() -> None:",
        "    return None",
    ]),
    ("src/gobby/install/installer.py", &[
        "\"\"\"Install helpers.\"\"\"",
        "",
        "",
        "def installed_names() -> list:",
        "    return []",
    ]),
    ("src/gobby/install/shared/rules_stub.py", &[
        "\"\"\"Template stub.\"\"\"",
        "NAME = \"stub\"",
    ]),
    ("src/gobby/install/shared/extra_stub.py", &[
        "\"\"\"Second template stub.\"\"\"",
        "NAME = \"extra\"",
        "VERSION = 1",
    ]),
    ("tests/test_placeholder.py", &[
        "def test_placeholder() -> None:",
        "    assert True",
    ]),
    ("tests/test_extra.py", &[
        "def test_extra() -> None:",
        "    assert 1 == 1",
        "    assert True",
    ]),
];

/// The frozen ledger: MCP rows in the shape the real `docs/port/ledger.md` uses.
const LEDGER: &[&str] = &[
    "# Parity ledger (census fixture)",
    "",
    "| Key | Status | Note |",
    "| --- | --- | --- |",
    "| `MCP gobby-tasks:create_task` | proxied | fixture |",
    "| `MCP gobby-memory:recall_note` | proxied | fixture |",
];

/// Writes `lines` to `path` with a trailing newline, creating parent directories.
fn write_lines(path: &Path, lines: &[&str]) {
    std::fs::create_dir_all(path.parent().expect("parent dir")).expect("create fixture dirs");
    let mut text = lines.join("\n");
    text.push('\n');
    std::fs::write(path, text).expect("write fixture file");
}

/// Builds the synthetic tree and its generation store in a fresh temp directory.
fn fixture() -> (TempDir, PathBuf, PathBuf) {
    let base = TempDir::new().expect("temp dir");
    let root = base.path().join("tree");
    for (path, lines) in FILES {
        write_lines(&root.join(path), lines);
    }
    write_lines(&root.join("docs/port/ledger.md"), LEDGER);
    let generations = base.path().join("generations");
    write_lines(
        &generations.join("gen-alpha/manifest.json"),
        &[r#"{"worker_protocol_version": "17.2", "built_from": "fixture"}"#],
    );
    write_lines(
        &generations.join("gen-beta/manifest.json"),
        &[r#"{"built_from": "fixture"}"#],
    );
    (base, root, generations)
}

fn path_str(path: &Path) -> &str {
    path.to_str().expect("utf-8 path")
}

/// Runs `gport --root <root> census --generations <generations> <flags>` in `root`, with `HOME` in a
/// temp directory under `base` and the extra environment on top.
fn census(
    base: &Path,
    root: &Path,
    generations: &Path,
    flags: &[&str],
    env: &[(&str, &str)],
) -> Run {
    let home = base.join("home");
    std::fs::create_dir_all(&home).expect("create fake home");
    let mut args = vec![
        "--root",
        path_str(root),
        "census",
        "--generations",
        path_str(generations),
    ];
    args.extend_from_slice(flags);
    let mut vars = vec![("HOME", path_str(&home))];
    vars.extend_from_slice(env);
    gport_env(root, &args, &vars)
}

/// The stdout of a run that must succeed, after checking the exit code and that a report printed.
fn expect_report(run: Run) -> String {
    assert_eq!(run.code, 0, "census should exit 0; stderr:\n{}", run.stderr);
    assert!(
        !run.stdout.trim().is_empty(),
        "census should print a report; stderr:\n{}",
        run.stderr
    );
    run.stdout
}

fn is_heading(line: &str) -> bool {
    line.trim_start().starts_with('#')
}

fn indent_of(line: &str) -> usize {
    line.len() - line.trim_start().len()
}

fn is_name_char(c: char) -> bool {
    c.is_ascii_alphanumeric() || c == '_' || c == '-'
}

/// Whether `word` appears in `line` as a whole name. It matches `alpha`, `| alpha |`, `alpha:` and
/// `gobby.alpha`, but not `beta/service.py`, `alpha.store` or `alphabet`.
fn has_word(line: &str, word: &str) -> bool {
    line.match_indices(word).any(|(at, _)| {
        let before = &line[..at];
        let after = &line[at + word.len()..];
        let before_ok = match before.chars().last() {
            None => true,
            Some(c) if is_name_char(c) => false,
            Some('.') | Some('/') => before[..before.len() - 1].ends_with("gobby"),
            Some(_) => true,
        };
        let after_ok = match after.chars().next() {
            None => true,
            Some(c) if is_name_char(c) => false,
            Some('/') => after[1..]
                .chars()
                .next()
                .is_none_or(|c| c.is_whitespace() || "`:".contains(c)),
            Some('.') => !after[1..].starts_with(|c: char| c.is_ascii_alphanumeric()),
            Some(_) => true,
        };
        before_ok && after_ok
    })
}

/// The line index of the first heading naming each section, in spec order.
fn heading_positions(lines: &[&str]) -> Vec<Option<usize>> {
    SECTIONS
        .iter()
        .copied()
        .map(|word| {
            lines
                .iter()
                .position(|line| is_heading(line) && line.to_lowercase().contains(word))
        })
        .collect()
}

/// The body of section `index`: its heading and the lines up to the next section's heading.
fn section(text: &str, index: usize) -> String {
    let lines: Vec<&str> = text.lines().collect();
    let positions = heading_positions(&lines);
    let start = positions[index]
        .unwrap_or_else(|| panic!("no Markdown heading for section {}", SECTIONS[index]));
    let end = positions
        .iter()
        .flatten()
        .copied()
        .filter(|&at| at > start)
        .min()
        .unwrap_or(lines.len());
    lines[start..end].join("\n")
}

/// The lines filed under `label`. A line naming a label word starts that label's group, a line with
/// no label word stays in the current group, and a heading with no label word ends the group.
fn filed_under(body: &str, labels: &[&str], label: &str) -> String {
    let mut group: Vec<&str> = Vec::new();
    let mut kept: Vec<&str> = Vec::new();
    for line in body.lines() {
        let named: Vec<&str> = labels
            .iter()
            .copied()
            .filter(|name| has_word(line, name))
            .collect();
        if !named.is_empty() {
            group = named;
        } else if is_heading(line) {
            group.clear();
        }
        if group.contains(&label) {
            kept.push(line);
        }
    }
    kept.join("\n")
}

/// The text filed under `key`: each line naming it, plus the lines beneath it. Beneath a heading is
/// everything up to the next heading; beneath a list item or row it is the more-indented lines.
fn entry(body: &str, key: &str) -> String {
    let lines: Vec<&str> = body.lines().collect();
    let mut kept: Vec<&str> = Vec::new();
    for (at, &line) in lines.iter().enumerate() {
        if !line.contains(key) {
            continue;
        }
        kept.push(line);
        let heading = is_heading(line);
        for &next in &lines[at + 1..] {
            if is_heading(next) {
                break;
            }
            if next.trim().is_empty() {
                if heading {
                    continue;
                }
                break;
            }
            if heading || indent_of(next) > indent_of(line) {
                kept.push(next);
            } else {
                break;
            }
        }
    }
    kept.join("\n")
}

/// The numbers printed beside each occurrence of `label`: the first digit run within the next 12
/// characters after it.
fn counts_beside(text: &str, label: &str) -> Vec<u64> {
    text.match_indices(label)
        .filter_map(|(at, _)| {
            let window: String = text[at + label.len()..].chars().take(12).collect();
            let start = window.find(|c: char| c.is_ascii_digit())?;
            window[start..]
                .chars()
                .take_while(|c| c.is_ascii_digit())
                .collect::<String>()
                .parse::<u64>()
                .ok()
        })
        .collect()
}

/// Whether `line` carries every number in `needles` as a separate digit run.
fn has_all(line: &str, needles: &[usize]) -> bool {
    let found: Vec<usize> = line
        .split(|c: char| !c.is_ascii_digit())
        .filter_map(|token| token.parse::<usize>().ok())
        .collect();
    needles.iter().all(|n| found.contains(n))
}

/// Whether `text` names `pkg/file` as a path, `pkg.file` as a dotted name, or `file.py`.
fn names(text: &str, pkg: &str, file: &str) -> bool {
    text.contains(&format!("{pkg}/{file}"))
        || text.contains(&format!("{pkg}.{file}"))
        || text.contains(&format!("{file}.py"))
}

/// Asserts that `block` names exactly the modules in `expected` among every fixture module.
fn assert_exactly(block: &str, expected: &[(&str, &str)], what: &str) {
    for &(pkg, file) in MODULES {
        let listed = names(block, pkg, file);
        let wanted = expected.contains(&(pkg, file));
        assert_eq!(
            listed, wanted,
            "{what}: {pkg}/{file} listed = {listed}, expected = {wanted}\n--- block ---\n{block}"
        );
    }
}

/// The package a fixture path belongs to, when it sits under `src/gobby/<package>/`.
fn package_of(path: &str) -> Option<&str> {
    path.strip_prefix("src/gobby/")?.split('/').next()
}

/// The (file count, line count) of the fixture files whose path `keep` accepts.
fn totals(keep: impl Fn(&str) -> bool) -> (usize, usize) {
    FILES
        .iter()
        .filter(|(path, _)| keep(path))
        .fold((0usize, 0usize), |(files, lines), (_, body)| {
            (files + 1, lines + body.len())
        })
}

/// The report from the synthetic tree with the Python inventory skipped.
fn tree_report() -> (TempDir, String) {
    let (base, root, generations) = fixture();
    let text = expect_report(census(
        base.path(),
        &root,
        &generations,
        &["--no-python"],
        &[],
    ));
    (base, text)
}

#[test]
/// Contract: the report is eight Markdown headings, one per section, in the spec's order.
fn sections_are_eight_markdown_headings_in_spec_order() {
    let (_base, text) = tree_report();
    let lines: Vec<&str> = text.lines().collect();
    let positions: Vec<Option<usize>> = heading_positions(&lines);
    for (index, position) in positions.iter().enumerate() {
        assert!(
            position.is_some(),
            "section {} ({}) has no Markdown heading in:\n{text}",
            index + 1,
            SECTIONS[index]
        );
    }
    let found: Vec<usize> = positions.into_iter().flatten().collect();
    assert!(
        found.windows(2).all(|pair| pair[0] < pair[1]),
        "section headings are out of spec order: {found:?}"
    );
}

#[test]
/// Contract: package sizes list each package under src/gobby with its file and line counts, plus the
/// totals for src, tests and src/gobby/install/shared. Expected counts come from the fixture.
fn package_sizes_list_each_package_with_files_lines_and_three_totals() {
    let (_base, text) = tree_report();
    let body = section(&text, 0);
    let lines: Vec<&str> = body.lines().collect();
    for pkg in PACKAGES {
        let (files, total) = totals(|path| package_of(path) == Some(pkg));
        assert!(
            lines
                .iter()
                .any(|line| line.contains(pkg) && has_all(line, &[files, total])),
            "package {pkg} should show {files} files and {total} lines in:\n{body}"
        );
    }
    let (files, total) = totals(|path| path.starts_with("src/"));
    assert!(
        lines.iter().any(|line| has_all(line, &[files, total])),
        "the src total should show {files} files and {total} lines in:\n{body}"
    );
    let (files, total) = totals(|path| path.starts_with("tests/"));
    assert!(
        lines
            .iter()
            .any(|line| line.contains("tests") && has_all(line, &[files, total])),
        "the tests total should show {files} files and {total} lines in:\n{body}"
    );
    let (files, total) = totals(|path| path.starts_with("src/gobby/install/shared/"));
    assert!(
        lines
            .iter()
            .any(|line| line.contains("shared") && has_all(line, &[files, total])),
        "the install/shared total should show {files} files and {total} lines in:\n{body}"
    );
}

#[test]
/// Contract: a package's callers are exactly the outside modules that import it, by absolute or
/// relative import. A module importing its own package is not a caller.
fn callers_list_exactly_the_outside_modules_that_import_each_package() {
    let (_base, text) = tree_report();
    let body = section(&text, 1);
    for pkg in PACKAGES {
        let expected: &[(&str, &str)] = match pkg {
            "alpha" => &[
                ("beta", "service"),
                ("beta", "reader"),
                ("gamma", "cleanup"),
            ],
            "beta" => &[("gamma", "cleanup")],
            _ => &[],
        };
        let block = filed_under(&body, &PACKAGES, pkg);
        assert_exactly(&block, expected, &format!("callers of {pkg}"));
    }
}

#[test]
/// Contract: writers by table list, for each table, exactly the modules with INSERT INTO, UPDATE ...
/// SET or DELETE FROM for it. A module that only reads a table is not a writer.
fn writers_list_the_modules_per_table_for_all_three_statement_forms() {
    let (_base, text) = tree_report();
    let body = section(&text, 2);
    let tables = ["widgets", "gadgets", "orders"];
    let cases: [(&str, &[(&str, &str)]); 3] = [
        ("widgets", &[("alpha", "store")]),
        ("gadgets", &[("beta", "service")]),
        ("orders", &[("gamma", "cleanup")]),
    ];
    for (table, expected) in cases {
        let block = filed_under(&body, &tables, table);
        assert_exactly(&block, expected, &format!("writers of {table}"));
    }
}

#[test]
/// Contract: caches lists each defining module with its invalidation calls, or "none found". A module
/// that invalidates without defining a cache is absent.
fn caches_list_each_defining_module_with_invalidation_calls_or_none_found() {
    let (_base, text) = tree_report();
    let body = section(&text, 3);

    let memo = entry(&body, "memo_lookup").to_lowercase();
    assert!(
        memo.contains("cache_clear(") && !memo.contains("none found"),
        "memo_lookup defines an lru_cache and calls cache_clear(:\n{body}"
    );

    let route = entry(&body, "route_bus").to_lowercase();
    assert!(
        route.contains("invalidate") && route.contains("notify") && !route.contains("none found"),
        "route_bus defines a TTLCache with invalidate_routes and NOTIFY:\n{body}"
    );

    let row = entry(&body, "row_snapshot").to_lowercase();
    assert!(
        row.contains("none found")
            && !row.contains("cache_clear(")
            && !row.contains("invalidate")
            && !row.contains("notify"),
        "row_snapshot defines _row_cache with no invalidation call, so 'none found':\n{body}"
    );

    assert!(
        entry(&body, "session_notify").is_empty(),
        "session_notify defines no cache, so it is not listed:\n{body}"
    );
}

#[test]
/// Contract: each class ending in Registry is listed with reference counts by top-level package,
/// counting only references outside its defining module.
fn registries_count_references_per_outside_package() {
    let (_base, text) = tree_report();
    let body = section(&text, 4);
    let registry = entry(&body, "SomethingRegistry");
    assert!(
        !registry.is_empty(),
        "SomethingRegistry should be listed in:\n{body}"
    );
    assert!(
        counts_beside(&registry, "alpha").contains(&1),
        "alpha has one reference outside catalog.py (store.py), so 1:\n{body}"
    );
    assert!(
        counts_beside(&registry, "beta").contains(&2),
        "beta has two references (reader.py), so 2:\n{body}"
    );
    assert!(
        counts_beside(&registry, "gamma").contains(&1),
        "gamma has one reference (cleanup.py), so 1:\n{body}"
    );
}

#[test]
/// Contract: the tool-name sweep lists, per MCP ledger tool, the modules holding the name as a string
/// literal. A bare identifier that is not a string literal does not count.
fn tool_sweep_lists_modules_holding_the_literal_and_ignores_bare_names() {
    let (_base, text) = tree_report();
    let body = section(&text, 5);
    let cases: [(&str, &[(&str, &str)]); 2] = [
        ("create_task", &[("servers", "dispatch")]),
        ("recall_note", &[("delta", "handlers")]),
    ];
    for (tool, expected) in cases {
        let listed = entry(&body, tool);
        assert!(
            !listed.is_empty(),
            "{tool} should have a sweep entry in:\n{body}"
        );
        assert_exactly(
            &listed,
            expected,
            &format!("modules holding {tool} as a literal"),
        );
    }
    assert!(
        !names(&body, "gamma", "bare_calls"),
        "bare_calls uses tool names without string literals, so it is not listed:\n{body}"
    );
}

#[test]
/// Contract: the surfaces section lists the route decorators of src/gobby/servers/_app_ui.py, and only
/// those routes.
fn surfaces_list_app_ui_routes_only() {
    let (_base, text) = tree_report();
    let body = section(&text, 6);
    assert!(
        body.contains("/api/census-widgets"),
        "the _app_ui.py GET route should be listed in:\n{body}"
    );
    assert!(
        body.contains("/ws/census-feed"),
        "the _app_ui.py websocket route should be listed in:\n{body}"
    );
    assert!(
        !body.contains("/api/census-hidden"),
        "a route outside _app_ui.py must not be listed:\n{body}"
    );
}

#[test]
/// Contract: the generations section lists each generation directory with its manifest's worker
/// protocol version, or "not recorded" when the manifest has none.
fn generations_list_each_version_or_not_recorded() {
    let (_base, text) = tree_report();
    let body = section(&text, 7);
    let alpha = entry(&body, "gen-alpha");
    assert!(
        alpha.contains("17.2") && !alpha.to_lowercase().contains("not recorded"),
        "gen-alpha records worker_protocol_version 17.2, so it should show that:\n{body}"
    );
    let beta = entry(&body, "gen-beta");
    assert!(
        beta.to_lowercase().contains("not recorded") && !beta.contains("17.2"),
        "gen-beta has no recorded version, so it should say 'not recorded':\n{body}"
    );
}

#[test]
/// Contract: a generations directory that does not exist is recorded in the output, and the exit is 0.
fn missing_generations_directory_is_recorded_and_exit_is_zero() {
    let (base, root, _generations) = fixture();
    let missing = base.path().join("no-such-generations");
    let text = expect_report(census(base.path(), &root, &missing, &["--no-python"], &[]));
    let body = section(&text, 7);
    let lower = body.to_lowercase();
    assert!(
        body.contains("no-such-generations")
            || lower.contains("missing")
            || lower.contains("not found")
            || lower.contains("does not exist"),
        "the missing generations root should be recorded in the generations section:\n{body}"
    );
}

#[test]
/// Contract: with --no-python, the surfaces section says the Python inventory was skipped.
fn no_python_flag_says_the_python_inventory_was_skipped() {
    let (_base, text) = tree_report();
    let body = section(&text, 6).to_lowercase();
    assert!(
        body.contains("skipped") && body.contains("python"),
        "with --no-python the surfaces section should say the Python inventory was skipped:\n{body}"
    );
}

#[test]
/// Contract: without --no-python, a generator that cannot run is recorded in the surfaces section and
/// the exit is 0. PATH is an empty directory, so the generator cannot be spawned.
fn failed_python_generator_is_recorded_and_exit_is_zero() {
    let (base, root, generations) = fixture();
    let empty_bin = base.path().join("empty-bin");
    std::fs::create_dir_all(&empty_bin).expect("create empty PATH dir");
    let text = expect_report(census(
        base.path(),
        &root,
        &generations,
        &[],
        &[("PATH", path_str(&empty_bin))],
    ));
    let lines: Vec<&str> = text.lines().collect();
    assert!(
        heading_positions(&lines).iter().all(Option::is_some),
        "all eight sections should still print when the generator fails:\n{text}"
    );
    let body = section(&text, 6).to_lowercase();
    assert!(
        body.contains("parity_ledger") || body.contains("generator"),
        "the failed generator should be recorded in the surfaces section:\n{body}"
    );
}

#[test]
/// Contract: two runs over the same tree and generation store print identical bytes.
fn output_is_byte_identical_across_runs() {
    let (base, root, generations) = fixture();
    let first = expect_report(census(
        base.path(),
        &root,
        &generations,
        &["--no-python"],
        &[],
    ));
    let second = expect_report(census(
        base.path(),
        &root,
        &generations,
        &["--no-python"],
        &[],
    ));
    assert_eq!(first, second, "two runs should print identical bytes");
}

#[test]
/// Contract: against the real repository, census exits 0 with --no-python and an empty generation
/// store, and the registries section names a class ending in Registry.
fn real_tree_census_exits_zero_and_names_a_registry_class() {
    let base = TempDir::new().expect("temp dir");
    let empty = base.path().join("empty-generations");
    std::fs::create_dir_all(&empty).expect("create empty generation store");
    let root = repo_root();
    let text = expect_report(census(base.path(), &root, &empty, &["--no-python"], &[]));
    let body = section(&text, 4);
    let named = body
        .split(|c: char| !(c.is_ascii_alphanumeric() || c == '_'))
        .any(|token| token.len() > "Registry".len() && token.ends_with("Registry"));
    assert!(
        named,
        "the registries section should name a class ending in Registry:\n{body}"
    );
}

#[test]
/// Contract: a route is listed as in the frozen ledger only when a row's key equals its ledger key,
/// `HTTP <METHOD> <path>` for an HTTP route and `WS transport <path>` for a websocket route. A longer
/// row that contains the path, or the same path under another method, does not match.
fn surface_is_listed_only_when_the_ledger_has_its_exact_key() {
    let (base, root, generations) = fixture();
    write_lines(
        &root.join("src/gobby/servers/_app_ui.py"),
        &[
            "\"\"\"Web UI routes.\"\"\"",
            "from fastapi import APIRouter",
            "",
            "app = APIRouter()",
            "",
            "",
            "@app.get(\"/api/census-widgets\")",
            "def widgets_view() -> list:",
            "    return []",
            "",
            "",
            "@app.get(\"/api/census-gadgets\")",
            "def gadgets_view() -> list:",
            "    return []",
            "",
            "",
            "@app.delete(\"/api/census-gadgets\")",
            "def drop_gadget() -> None:",
            "    return None",
            "",
            "",
            "@app.get('')",
            "def root_view() -> None:",
            "    return None",
            "",
            "",
            "@app.websocket(\"/ws/census-feed\")",
            "async def feed(websocket) -> None:",
            "    return None",
        ],
    );
    write_lines(
        &root.join("docs/port/ledger.md"),
        &[
            "# Parity ledger (census fixture)",
            "",
            "| Key | Status | Note |",
            "| --- | --- | --- |",
            "| `HTTP GET /api/census-widgets/extra` | proxied | fixture |",
            "| `HTTP GET /api/census-gadgets` | proxied | fixture |",
            "| `WS transport /ws/census-feed` | proxied | fixture |",
        ],
    );
    let text = expect_report(census(
        base.path(),
        &root,
        &generations,
        &["--no-python"],
        &[],
    ));
    let body = section(&text, 6);
    assert_eq!(
        listed(&body, "GET", "/api/census-widgets"),
        "no",
        "a longer ledger key that contains the route's path is not its row:\n{body}"
    );
    assert_eq!(
        listed(&body, "GET", "/api/census-gadgets"),
        "yes",
        "the exact HTTP key is listed:\n{body}"
    );
    assert_eq!(
        listed(&body, "DELETE", "/api/census-gadgets"),
        "no",
        "the same path under another method is not listed:\n{body}"
    );
    assert_eq!(
        listed(&body, "GET", ""),
        "no",
        "an empty path is not listed by any ledger row:\n{body}"
    );
    assert_eq!(
        listed(&body, "WEBSOCKET", "/ws/census-feed"),
        "yes",
        "the exact WS transport key is listed:\n{body}"
    );
}

#[test]
#[cfg(unix)]
/// Contract: a file under src/gobby that cannot be read is listed under "Skipped files" with its path
/// and reason, and the exit is 0. A process that can read a mode 000 file (root) cannot show the
/// failure, so the test returns early there.
fn unreadable_file_is_listed_under_skipped_files_and_exit_is_zero() {
    use std::os::unix::fs::PermissionsExt;

    let (base, root, generations) = fixture();
    let locked = root.join("src/gobby/alpha/locked.py");
    write_lines(&locked, &["VALUE = 1"]);
    std::fs::set_permissions(&locked, std::fs::Permissions::from_mode(0o000))
        .expect("chmod the fixture file");
    if std::fs::read(&locked).is_ok() {
        eprintln!("skipped: this process can read a mode 000 file, so the failure cannot be shown");
        return;
    }
    let text = expect_report(census(
        base.path(),
        &root,
        &generations,
        &["--no-python"],
        &[],
    ));
    let body = skipped_section(&text);
    assert!(
        body.contains("src/gobby/alpha/locked.py"),
        "the unreadable file should be listed under Skipped files:\n{body}"
    );
    assert!(
        body.contains("1 file"),
        "the skipped file should be counted:\n{body}"
    );
}

#[test]
/// Contract: a .py file under src/gobby that is not UTF-8 is listed under "Skipped files" and counted.
/// Other non-UTF-8 files, such as binary assets, are not listed. The exit is 0.
fn non_utf8_python_file_is_listed_under_skipped_files_and_binary_is_not() {
    let (base, root, generations) = fixture();
    std::fs::write(root.join("src/gobby/alpha/bad.py"), b"\xff\xfe = 1\n")
        .expect("write a non-UTF-8 module");
    std::fs::write(root.join("src/gobby/alpha/logo.png"), b"\x89PNG\xff\x00")
        .expect("write a binary asset");
    let text = expect_report(census(
        base.path(),
        &root,
        &generations,
        &["--no-python"],
        &[],
    ));
    let body = skipped_section(&text);
    assert!(
        body.contains("src/gobby/alpha/bad.py"),
        "the non-UTF-8 module should be listed under Skipped files:\n{body}"
    );
    assert!(
        body.contains("1 file"),
        "the skipped module should be counted:\n{body}"
    );
    assert!(
        !body.contains("logo.png"),
        "a binary asset that is not a .py file is not listed:\n{body}"
    );
}

#[test]
/// Contract: a dotted call whose name ends in _cache, such as self.get_cache() or
/// torch.cuda.empty_cache(), is not a cache definition. An attribute assigned under such a name still is.
fn dotted_cache_call_is_not_a_cache_definition() {
    let (base, root, generations) = fixture();
    write_lines(
        &root.join("src/gobby/alpha/caller.py"),
        &[
            "import torch",
            "",
            "",
            "class Caller:",
            "    def __init__(self) -> None:",
            "        self.lookup_cache = {}",
            "",
            "    def run(self) -> None:",
            "        self.get_cache()",
            "        torch.cuda.empty_cache()",
        ],
    );
    let text = expect_report(census(
        base.path(),
        &root,
        &generations,
        &["--no-python"],
        &[],
    ));
    let body = section(&text, 3);
    let row = entry(&body, "alpha/caller.py");
    assert!(
        row.contains("attribute lookup_cache"),
        "the assigned _cache attribute still defines a cache:\n{body}"
    );
    assert!(
        !row.contains("get_cache") && !row.contains("empty_cache"),
        "dotted calls are not cache definitions:\n{body}"
    );
}

#[test]
#[cfg(unix)]
/// Contract: a directory under src/gobby that cannot be listed is recorded under "Skipped files" with
/// its path and reason, and the exit is 0. The directory's mode is restored when the test ends. A
/// process that can list a mode 000 directory (root) cannot show the failure, so the test returns
/// early there.
fn unlistable_directory_is_listed_under_skipped_files_and_exit_is_zero() {
    use std::os::unix::fs::PermissionsExt;

    /// Restores a directory's mode when dropped, so the temp directory can be removed.
    struct Restore(PathBuf);
    impl Drop for Restore {
        fn drop(&mut self) {
            let _ = std::fs::set_permissions(&self.0, std::fs::Permissions::from_mode(0o755));
        }
    }

    let (base, root, generations) = fixture();
    let sealed = root.join("src/gobby/alpha/sealed");
    write_lines(&sealed.join("inside.py"), &["VALUE = 1"]);
    std::fs::set_permissions(&sealed, std::fs::Permissions::from_mode(0o000))
        .expect("chmod the fixture directory");
    let _restore = Restore(sealed.clone());
    if std::fs::read_dir(&sealed).is_ok() {
        eprintln!(
            "skipped: this process can list a mode 000 directory, so the failure cannot be shown"
        );
        return;
    }
    let text = expect_report(census(
        base.path(),
        &root,
        &generations,
        &["--no-python"],
        &[],
    ));
    let body = skipped_section(&text);
    assert!(
        body.contains("src/gobby/alpha/sealed"),
        "the unlistable directory should be listed under Skipped files:\n{body}"
    );
}

#[test]
#[cfg(unix)]
/// Contract: a UTF-8 text file whose name is not UTF-8 is recorded under "Skipped files" with the
/// reason "path is not UTF-8", and the exit is 0. The test returns early when the filesystem refuses
/// such a name.
fn text_file_with_a_non_utf8_name_is_listed_under_skipped_files() {
    use std::ffi::OsStr;
    use std::os::unix::ffi::OsStrExt;

    let (base, root, generations) = fixture();
    let name = OsStr::from_bytes(b"odd\xff.txt");
    if std::fs::write(root.join("src/gobby/alpha").join(name), "VALUE = 2\n").is_err() {
        eprintln!("skipped: this filesystem refuses a non-UTF-8 file name");
        return;
    }
    let text = expect_report(census(
        base.path(),
        &root,
        &generations,
        &["--no-python"],
        &[],
    ));
    let body = skipped_section(&text);
    assert!(
        body.contains("odd") && body.contains("path is not UTF-8"),
        "the text file with a non-UTF-8 name should be listed under Skipped files:\n{body}"
    );
}

/// The lines from the "Skipped files" heading to the end of the report.
fn skipped_section(text: &str) -> String {
    let lines: Vec<&str> = text.lines().collect();
    let start = lines
        .iter()
        .position(|line| is_heading(line) && line.contains("Skipped files"))
        .unwrap_or_else(|| panic!("no Skipped files heading in:\n{text}"));
    lines[start..].join("\n")
}

/// The "In frozen ledger" cell of the surfaces table row whose method and path are given.
fn listed(body: &str, method: &str, path: &str) -> String {
    body.lines()
        .map(|line| line.split('|').map(str::trim).collect::<Vec<&str>>())
        .find(|cells| cells.len() > 4 && cells[1] == method && cells[2] == path)
        .map(|cells| cells[4].to_owned())
        .unwrap_or_else(|| panic!("no {method} {path:?} route row in:\n{body}"))
}

/// The caches section for a synthetic module `rel` (below the tree root) that holds `lines`.
fn caches_at(rel: &str, lines: &[&str]) -> String {
    let (base, root, generations) = fixture();
    write_lines(&root.join(rel), lines);
    let text = expect_report(census(
        base.path(),
        &root,
        &generations,
        &["--no-python"],
        &[],
    ));
    section(&text, 3)
}

/// The caches section for a synthetic module `src/gobby/alpha/probe.py` that holds `lines`.
fn probe_caches(lines: &[&str]) -> String {
    caches_at("src/gobby/alpha/probe.py", lines)
}

/// Contract: a dotted name ending in _cache is a cache definition only when it is an assignment
/// target. In a module that assigns `_hits_cache`, the `name` used on `use_line` is not listed.
fn assert_use_is_not_a_cache_definition(use_line: &str, name: &str) {
    let body = probe_caches(&[
        "class Probe:",
        "    def __init__(self) -> None:",
        "        self._hits_cache = {}",
        "",
        "    def run(self, key: str) -> None:",
        use_line,
    ]);
    let row = entry(&body, "probe.py");
    assert!(
        row.contains("attribute _hits_cache"),
        "the assigned cache should still define a cache:\n{body}"
    );
    assert!(
        !row.contains(&format!("attribute {name}")),
        "`{name}` is only used on `{use_line}`, so it is not a cache definition:\n{body}"
    );
}

#[test]
/// Contract: a method reference passed as an argument is a use, not a cache definition.
fn cache_passed_as_a_method_reference_is_not_a_cache_definition() {
    assert_use_is_not_a_cache_definition(
        "        offload(self._load_rules_into_cache, key)",
        "_load_rules_into_cache",
    );
}

#[test]
/// Contract: a cache whose only use is a .clear() call is not a cache definition.
fn cache_cleared_by_a_call_is_not_a_cache_definition() {
    assert_use_is_not_a_cache_definition("        self._drop_cache.clear()", "_drop_cache");
}

#[test]
/// Contract: a subscript read of a cache attribute is a use, not a cache definition.
fn cache_read_by_subscript_is_not_a_cache_definition() {
    assert_use_is_not_a_cache_definition("        return self._ledger_cache[key]", "_ledger_cache");
}

#[test]
/// Contract: a subscript store into a cache attribute is a use, not a cache definition.
fn cache_item_stored_by_subscript_is_not_a_cache_definition() {
    assert_use_is_not_a_cache_definition("        self._index_cache[key] = 1", "_index_cache");
}

#[test]
/// Contract: a cache attribute compared with == is a use, not a cache definition.
fn cache_compared_with_double_equals_is_not_a_cache_definition() {
    assert_use_is_not_a_cache_definition(
        "        return self._digest_cache == key",
        "_digest_cache",
    );
}

#[test]
/// Contract: an augmented assignment to a cache attribute is a use, not a cache definition.
fn cache_augmented_assignment_is_not_a_cache_definition() {
    assert_use_is_not_a_cache_definition("        self._pending_cache += 1", "_pending_cache");
}

#[test]
/// Contract: a cache attribute tested for membership is a use, not a cache definition.
fn cache_tested_for_membership_is_not_a_cache_definition() {
    assert_use_is_not_a_cache_definition("        if key in self._seen_cache:", "_seen_cache");
}

#[test]
/// Contract: a cache attribute that starts a single-line if is a use, even when an `=` follows the
/// colon.
fn cache_tested_on_a_single_line_if_is_not_a_cache_definition() {
    assert_use_is_not_a_cache_definition("        if self._guard_cache: count = 1", "_guard_cache");
}

#[test]
/// Contract: a cache attribute inside a slice is a use, not a cache definition.
fn cache_used_as_a_slice_bound_is_not_a_cache_definition() {
    assert_use_is_not_a_cache_definition(
        "        tail = batch[self._window_cache:]",
        "_window_cache",
    );
}

#[test]
/// Contract: a cache attribute used as a dict key is a use, not a cache definition.
fn cache_used_as_a_dict_key_is_not_a_cache_definition() {
    assert_use_is_not_a_cache_definition("        lookup = {self._key_cache: key}", "_key_cache");
}

#[test]
/// Contract: a plain assignment and annotated assignments to _cache attributes still define a
/// cache.
fn cache_plain_and_annotated_assignments_still_define_a_cache() {
    let body = probe_caches(&[
        "class Store:",
        "    def __init__(self) -> None:",
        "        self._rules_cache = {}",
        "        self._hits_cache: dict = {}",
        "        self._index_cache: dict[str, int] = {}",
    ]);
    let row = entry(&body, "probe.py");
    assert!(
        row.contains("attribute _rules_cache"),
        "self._rules_cache = {{}} should still define a cache:\n{body}"
    );
    assert!(
        row.contains("attribute _hits_cache"),
        "self._hits_cache: dict = {{}} should still define a cache:\n{body}"
    );
    assert!(
        row.contains("attribute _index_cache"),
        "self._index_cache: dict[str, int] = {{}} should still define a cache:\n{body}"
    );
}

#[test]
#[cfg(unix)]
/// Contract: a skipped file whose name holds a pipe is listed with the pipe escaped, so its row
/// keeps two cells. The name is creatable on APFS and ext4; the test returns early where the
/// filesystem refuses it.
fn skipped_path_with_a_pipe_is_escaped_so_its_row_keeps_two_cells() {
    let (base, root, generations) = fixture();
    if std::fs::write(root.join("src/gobby/alpha/pipe|name.py"), b"\xff\xfe = 1\n").is_err() {
        eprintln!("skipped: this filesystem refuses a pipe in a file name");
        return;
    }
    let text = expect_report(census(
        base.path(),
        &root,
        &generations,
        &["--no-python"],
        &[],
    ));
    let body = skipped_section(&text);
    assert!(
        body.lines()
            .any(|line| line == "| src/gobby/alpha/pipe\\|name.py | not UTF-8 |"),
        "the skipped row should escape the pipe and keep two cells:\n{body}"
    );
}

#[test]
#[cfg(unix)]
/// Contract: a line break in a skipped file's name is printed as a space, so its row stays on one
/// line. The test returns early where the filesystem refuses such a name.
fn skipped_path_with_a_line_break_keeps_its_row_on_one_line() {
    let (base, root, generations) = fixture();
    if std::fs::write(root.join("src/gobby/alpha/bad\nname.py"), b"\xff\xfe = 1\n").is_err() {
        eprintln!("skipped: this filesystem refuses a line break in a file name");
        return;
    }
    let text = expect_report(census(
        base.path(),
        &root,
        &generations,
        &["--no-python"],
        &[],
    ));
    let body = skipped_section(&text);
    assert!(
        body.lines()
            .any(|line| line == "| src/gobby/alpha/bad name.py | not UTF-8 |"),
        "the skipped row should replace the line break with a space:\n{body}"
    );
}

#[test]
/// Contract: every name in a tuple target list that ends in a single `=` is a cache definition. In
/// `self._pa_cache, self._pb_cache = {}, {}` both attributes are assigned.
fn cache_tuple_targets_each_define_a_cache() {
    let body = probe_caches(&[
        "class Pair:",
        "    def __init__(self) -> None:",
        "        self._pa_cache, self._pb_cache = {}, {}",
    ]);
    let row = entry(&body, "probe.py");
    assert!(
        row.contains("attribute _pa_cache"),
        "the first name of a tuple target list should define a cache:\n{body}"
    );
    assert!(
        row.contains("attribute _pb_cache"),
        "the second name of a tuple target list should define a cache:\n{body}"
    );
}

#[test]
/// Contract: a cache passed before a keyword argument is a use, not a cache definition. The `=` of
/// `retries=2` belongs to the call, not to a target list.
fn cache_passed_before_a_keyword_argument_is_not_a_cache_definition() {
    assert_use_is_not_a_cache_definition(
        "        offload(self._args_cache, retries=2)",
        "_args_cache",
    );
}

#[test]
/// Contract: a line-initial key of a dict display whose value is a call with a keyword argument is
/// not a cache definition. The `=` sits inside the call's brackets, and the key is inside `{`.
fn cache_key_of_a_line_initial_dict_display_is_not_a_cache_definition() {
    let body = probe_caches(&[
        "class Probe:",
        "    def __init__(self) -> None:",
        "        self._hits_cache = {}",
        "        self.table = {",
        "            self._k2_cache: make(size=3),",
        "        }",
    ]);
    let row = entry(&body, "probe.py");
    assert!(
        row.contains("attribute _hits_cache"),
        "the assigned cache should still define a cache:\n{body}"
    );
    assert!(
        !row.contains("attribute _k2_cache"),
        "a dict key with a keyword-argument value is not a cache definition:\n{body}"
    );
}

#[test]
/// Contract: a cache name in the debug form of an f-string, `{self._dbg2_cache=}`, is a use. The
/// `=` is inside a string literal and is followed by `}`.
fn cache_name_in_an_f_string_debug_form_is_not_a_cache_definition() {
    assert_use_is_not_a_cache_definition("        return f\"{self._dbg2_cache=}\"", "_dbg2_cache");
}

#[test]
/// Contract: an annotated assignment whose annotation spans lines, as in `self._ml_cache: dict[` on
/// one line and `str, int] = {}` on the next, still defines a cache.
fn cache_annotation_spanning_lines_still_defines_a_cache() {
    let body = probe_caches(&[
        "class Store:",
        "    def __init__(self) -> None:",
        "        self._ml_cache: dict[",
        "            str, int] = {}",
    ]);
    let row = entry(&body, "probe.py");
    assert!(
        row.contains("attribute _ml_cache"),
        "an annotation that spans lines should still define a cache:\n{body}"
    );
}

#[test]
/// Contract: a decorator whose dotted name ends in `_cache`, called with arguments, is a cache
/// definition: `@cachetools.func.ttl_cache(maxsize=4)` is listed as a decorator.
fn ttl_cache_decorator_with_arguments_is_a_cache_definition() {
    let body = probe_caches(&[
        "class Fetcher:",
        "    @cachetools.func.ttl_cache(maxsize=4)",
        "    def fetch(self, key: str) -> str:",
        "        return key",
    ]);
    let row = entry(&body, "probe.py");
    assert!(
        row.contains("decorator cachetools.func.ttl_cache"),
        "a called *_cache decorator should define a cache:\n{body}"
    );
}

#[test]
/// Contract: a bare `@cache` decorator, the name `cache` with no qualifier, is a cache definition.
fn bare_cache_decorator_is_a_cache_definition() {
    let body = probe_caches(&[
        "from functools import cache",
        "",
        "",
        "@cache",
        "def load(key: str) -> str:",
        "    return key",
    ]);
    let row = entry(&body, "probe.py");
    assert!(
        row.contains("decorator cache"),
        "a bare @cache decorator should define a cache:\n{body}"
    );
}

#[test]
/// Contract: a decorator whose last name component is neither `cache` nor ends in `_cache`, such as
/// `@cache_page(60)`, is not a cache definition.
fn decorator_that_is_not_a_cache_is_not_listed() {
    let body = probe_caches(&[
        "class Pages:",
        "    @cache_page(60)",
        "    def index(self) -> str:",
        "        return ''",
    ]);
    assert!(
        entry(&body, "probe.py").is_empty(),
        "cache_page is not a cache decorator, so the module is not listed:\n{body}"
    );
}

#[test]
#[cfg(unix)]
/// Contract: a module path with a pipe is escaped in the caches table, so its row keeps three cells.
/// The name is creatable on APFS and ext4.
fn cache_module_path_with_a_pipe_keeps_its_row_whole() {
    let body = caches_at(
        "src/gobby/alpha/a|b.py",
        &[
            "class Store:",
            "    def __init__(self) -> None:",
            "        self._x_cache = {}",
        ],
    );
    assert!(
        body.lines()
            .any(|line| line == "| alpha/a\\|b.py | attribute _x_cache | none found |"),
        "the module path should be escaped and the row should keep three cells:\n{body}"
    );
}

#[test]
#[cfg(unix)]
/// Contract: a module path with a pipe is escaped in the tool-name sweep too, so its row keeps two
/// cells.
fn tool_sweep_module_path_with_a_pipe_keeps_its_row_whole() {
    let (base, root, generations) = fixture();
    write_lines(
        &root.join("src/gobby/alpha/pipe|sweep.py"),
        &["NAME = \"gobby-tasks:create_task\""],
    );
    let text = expect_report(census(
        base.path(),
        &root,
        &generations,
        &["--no-python"],
        &[],
    ));
    let body = section(&text, 5);
    assert!(
        body.lines().any(|line| {
            line == "| `MCP gobby-tasks:create_task` | alpha/pipe\\|sweep.py, servers/dispatch.py |"
        }),
        "the sweep row should escape the module path's pipe and keep two cells:\n{body}"
    );
}

#[test]
#[cfg(unix)]
/// Contract: a ledger key that already carries its `\|` escape is shown with one escape in the sweep,
/// and it matches a literal holding the pipe it stands for.
fn tool_sweep_ledger_key_with_an_escaped_pipe_is_shown_with_one_escape() {
    let (base, root, generations) = fixture();
    write_lines(
        &root.join("docs/port/ledger.md"),
        &[
            "# Parity ledger (census fixture)",
            "",
            "| Key | Status | Note |",
            "| --- | --- | --- |",
            "| `MCP gobby-x:a\\|b` | proxied | fixture |",
        ],
    );
    write_lines(
        &root.join("src/gobby/alpha/names.py"),
        &["NAME = \"gobby-x:a|b\""],
    );
    let text = expect_report(census(
        base.path(),
        &root,
        &generations,
        &["--no-python"],
        &[],
    ));
    let body = section(&text, 5);
    assert!(
        body.lines()
            .any(|line| line == "| `MCP gobby-x:a\\|b` | alpha/names.py |"),
        "the key should keep one escape and list the module that holds its name:\n{body}"
    );
}
