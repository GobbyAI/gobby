use super::*;

fn chunk(path: &str, line_start: usize, content: &str) -> IndexedContentChunk {
    IndexedContentChunk {
        file_path: path.to_string(),
        line_start,
        content: content.to_string(),
    }
}

fn options(pattern: &str) -> GrepOptions<'_> {
    GrepOptions {
        pattern,
        paths: &[],
        globs: &[],
        fixed_strings: false,
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
    }
}

#[test]
fn text_renders_grouped_grep_shape() {
    let chunks = vec![chunk("src/lib.rs", 1, "one\nneedle\nthree")];
    let result = grep_chunks(&chunks, &options("needle")).expect("grep chunks");

    assert_eq!(format_text_matches(&result.matches), "src/lib.rs\n2:needle");
}

#[test]
fn text_groups_multiple_files() {
    let chunks = vec![
        chunk("src/a.rs", 1, "needle a"),
        chunk("tests/b.rs", 10, "needle b"),
    ];
    let result = grep_chunks(&chunks, &options("needle")).expect("grep chunks");

    assert_eq!(
        format_text_matches(&result.matches),
        "src/a.rs\n1:needle a\ntests/b.rs\n10:needle b"
    );
}

#[test]
fn ordering_is_path_then_line() {
    let chunks = vec![
        chunk("b.rs", 10, "needle later"),
        chunk("a.rs", 3, "needle first"),
        chunk("a.rs", 1, "needle earliest"),
    ];
    let result = grep_chunks(&chunks, &options("needle")).expect("grep chunks");

    let keys: Vec<_> = result
        .matches
        .iter()
        .map(|m| (m.path.as_str(), m.line))
        .collect();
    assert_eq!(keys, vec![("a.rs", 1), ("a.rs", 3), ("b.rs", 10)]);
}

#[test]
fn ignore_case_matches_case_insensitively() {
    let chunks = vec![chunk("src/lib.rs", 1, "Needle")];
    let mut opts = options("needle");
    opts.ignore_case = true;
    let result = grep_chunks(&chunks, &opts).expect("grep chunks");

    assert_eq!(result.matches.len(), 1);
}

#[test]
fn fixed_strings_treat_regex_metacharacters_literally() {
    let chunks = vec![chunk("src/lib.rs", 1, "a.b\naxb")];
    let mut opts = options("a.b");
    opts.fixed_strings = true;
    let result = grep_chunks(&chunks, &opts).expect("grep chunks");

    assert_eq!(result.matches.len(), 1);
    assert_eq!(result.matches[0].line, 1);
}

#[test]
fn sql_prefix_prefilter_requires_convertible_globs() {
    let paths = vec!["src/foo_bar".to_string(), "src/foo_bar/**".to_string()];
    assert_eq!(
        sql_like_prefixes(&paths).expect("path prefixes"),
        vec!["src/foo\\_bar%", "src/foo\\_bar/%"]
    );

    let globs = vec!["*.rs".to_string(), "src/*.rs".to_string()];
    assert_eq!(
        sql_like_prefixes(&globs).expect("glob prefixes"),
        vec!["src/%"]
    );

    assert_eq!(sql_like_prefixes(&[]), None);
    assert_eq!(sql_like_prefixes(&["*.rs".to_string()]), None);
}

#[test]
fn context_flags_include_bounded_neighbors() {
    let chunks = vec![chunk("src/lib.rs", 1, "one\ntwo\nneedle\nfour\nfive")];
    let mut opts = options("needle");
    opts.before_context = Some(1);
    opts.after_context = Some(2);
    let result = grep_chunks(&chunks, &opts).expect("grep chunks");
    let item = &result.matches[0];

    assert_eq!(
        item.before,
        vec![GrepContextLine {
            line: 2,
            text: "two".to_string()
        }]
    );
    assert_eq!(
        item.after,
        vec![
            GrepContextLine {
                line: 4,
                text: "four".to_string()
            },
            GrepContextLine {
                line: 5,
                text: "five".to_string()
            }
        ]
    );
    assert_eq!(
        format_text_matches(&result.matches),
        "src/lib.rs\n2-two\n3:needle\n4-four\n5-five"
    );
}

#[test]
fn token_paging_keeps_match_context_blocks_complete() {
    let chunks = vec![chunk(
        "src/lib.rs",
        1,
        "before one\nneedle one\nafter one\nbefore two\nneedle two\nafter two",
    )];
    let mut opts = options("needle");
    opts.context = Some(1);
    let result = grep_chunks(&chunks, &opts).expect("grep chunks");
    let render = |rows: &[GrepMatch], next_offset, _| {
        format!("{}\nnext={next_offset:?}", format_text_matches(rows))
    };
    let budget = token_budget::estimate_tokens(&render(&result.matches[..1], Some(1), false));
    let page = token_budget::paginate_results(result.matches, 0, false, Some(budget), render);

    assert_eq!(page.results.len(), 1);
    assert_eq!(page.results[0].text, "needle one");
    assert_eq!(page.results[0].before[0].text, "before one");
    assert_eq!(page.results[0].after[0].text, "after one");
    assert_eq!(page.next_offset, Some(1));
}

#[test]
fn text_output_trims_leading_whitespace_without_changing_matches() {
    let chunks = vec![chunk(
        "src/lib.rs",
        1,
        "    before\n        needle\n\t\tafter",
    )];
    let mut opts = options("needle");
    opts.context = Some(1);
    let result = grep_chunks(&chunks, &opts).expect("grep chunks");
    let item = &result.matches[0];

    assert_eq!(item.text, "        needle");
    assert_eq!(item.before[0].text, "    before");
    assert_eq!(item.after[0].text, "\t\tafter");
    assert_eq!(
        format_text_matches(&result.matches),
        "src/lib.rs\n1-before\n2:needle\n3-after"
    );
}

#[test]
fn text_suppresses_duplicate_context_lines() {
    let chunks = vec![chunk(
        "src/lib.rs",
        1,
        "one\nneedle one\nmiddle\nneedle two\nfive",
    )];
    let mut opts = options("needle");
    opts.context = Some(1);
    let result = grep_chunks(&chunks, &opts).expect("grep chunks");

    assert_eq!(
        format_text_matches(&result.matches),
        "src/lib.rs\n1-one\n2:needle one\n3-middle\n4:needle two\n5-five"
    );
}

#[test]
fn max_count_caps_retained_matches_not_total_matching_lines() {
    let chunks = vec![chunk(
        "src/lib.rs",
        1,
        "before\nneedle one\nmiddle\nneedle two\nafter",
    )];
    let mut opts = options("needle");
    opts.context = Some(1);
    opts.max_count = Some(1);
    let result = grep_chunks(&chunks, &opts).expect("grep chunks");

    assert_eq!(result.matched_lines, 2);
    assert!(result.truncated);
    assert_eq!(result.matches[0].line, 2);
    assert_eq!(result.matches[0].before.len(), 1);
    assert_eq!(result.matches[0].after.len(), 1);
    assert_eq!(
        format_text_matches(&result.matches),
        "src/lib.rs\n1-before\n2:needle one\n3-middle"
    );
}

#[test]
fn json_match_contains_spans_and_context() {
    let chunks = vec![chunk("src/lib.rs", 1, "before\nneedle needle\nafter")];
    let mut opts = options("needle");
    opts.context = Some(1);
    let result = grep_chunks(&chunks, &opts).expect("grep chunks");
    let value = serde_json::to_value(&result.matches[0]).expect("serialize match");

    assert_eq!(value["path"], "src/lib.rs");
    assert_eq!(value["line"], 2);
    assert_eq!(value["text"], "needle needle");
    assert_eq!(value["spans"][0]["start"], 0);
    assert_eq!(value["spans"][0]["end"], 6);
    assert_eq!(value["spans"][1]["start"], 7);
    assert_eq!(value["before"][0]["line"], 1);
    assert_eq!(value["after"][0]["line"], 3);
}

#[test]
fn path_and_glob_filters_compose() {
    let chunks = vec![
        chunk("src/gobby/app.py", 1, "needle"),
        chunk("src/gobby/app.rs", 1, "needle"),
        chunk("tests/app.py", 1, "needle"),
    ];
    let paths = vec!["src/gobby".to_string()];
    let globs = vec!["*.py".to_string()];
    let opts = GrepOptions {
        paths: &paths,
        globs: &globs,
        ..options("needle")
    };
    let result = grep_chunks(&chunks, &opts).expect("grep chunks");

    assert_eq!(result.scanned_chunks, 1);
    assert_eq!(result.matches[0].path, "src/gobby/app.py");
}

#[test]
fn bare_globs_match_basenames_but_slash_globs_match_paths() {
    let chunks = vec![
        chunk("src/app.py", 1, "needle"),
        chunk("tests/app.py", 1, "needle"),
    ];
    let bare = vec!["*.py".to_string()];
    let slash = vec!["src/*.py".to_string()];

    let bare_result = grep_chunks(
        &chunks,
        &GrepOptions {
            globs: &bare,
            ..options("needle")
        },
    )
    .expect("bare glob grep");
    let slash_result = grep_chunks(
        &chunks,
        &GrepOptions {
            globs: &slash,
            ..options("needle")
        },
    )
    .expect("slash glob grep");

    assert_eq!(bare_result.matches.len(), 2);
    assert_eq!(slash_result.matches.len(), 1);
    assert_eq!(slash_result.matches[0].path, "src/app.py");
}

#[test]
fn overlapping_chunks_dedupe_by_file_and_line() {
    let chunks = vec![
        chunk("src/lib.rs", 1, "needle\nother"),
        chunk("src/lib.rs", 1, "needle\nother"),
    ];
    let result = grep_chunks(&chunks, &options("needle")).expect("grep chunks");

    assert_eq!(result.matches.len(), 1);
}

#[test]
fn files_with_matches_text_lists_sorted_unique_paths() {
    let chunks = vec![
        chunk("z.rs", 1, "needle"),
        chunk("a.rs", 1, "needle\nneedle"),
        chunk("m.rs", 1, "other"),
    ];
    let mut opts = options("needle");
    opts.files_with_matches = true;
    let result = grep_chunks(&chunks, &opts).expect("grep chunks");
    let (files, truncated) = matching_files(&result.matches, opts.max_count);

    assert_eq!(files, vec!["a.rs", "z.rs"]);
    assert!(!truncated);
    assert_eq!(format_matching_files(&files), "a.rs\nz.rs");
    assert_eq!(result.matched_lines, 3);
}

#[test]
fn files_with_matches_json_populates_files_and_empties_matches() {
    let chunks = vec![chunk("z.rs", 1, "needle"), chunk("a.rs", 1, "needle")];
    let mut opts = options("needle");
    opts.files_with_matches = true;
    let result = grep_chunks(&chunks, &opts).expect("grep chunks");
    let (files, _) = matching_files(&result.matches, None);
    let response = grep_response("proj", &opts, &result, &[], Some(&files), None, false);
    let value = serde_json::to_value(&response).expect("serialize response");

    assert_eq!(value["files"], serde_json::json!(["a.rs", "z.rs"]));
    assert_eq!(value["matches"], serde_json::json!([]));
    assert_eq!(value["matched_lines"], 2);
    assert_eq!(value["truncated"], false);
}

#[test]
fn files_with_matches_max_count_caps_files_not_lines() {
    let chunks = vec![
        chunk("a.rs", 1, "needle\nneedle"),
        chunk("b.rs", 1, "needle"),
        chunk("c.rs", 1, "needle"),
    ];
    let mut opts = options("needle");
    opts.files_with_matches = true;
    opts.max_count = Some(2);
    let result = grep_chunks(&chunks, &opts).expect("grep chunks");
    let (files, truncated) = matching_files(&result.matches, opts.max_count);
    let response = grep_response("proj", &opts, &result, &[], Some(&files), Some(2), false);

    assert_eq!(result.matched_lines, 4);
    assert_eq!(result.matches.len(), 4);
    assert_eq!(files, vec!["a.rs", "b.rs"]);
    assert!(truncated);
    assert!(response.truncated);
    assert_eq!(response.next_offset, Some(2));
    assert_eq!(response.files, Some(files.as_slice()));
    assert!(response.matches.is_empty());
}

#[test]
fn files_with_matches_ignores_context_flags() {
    let chunks = vec![chunk("src/lib.rs", 1, "one\ntwo\nneedle\nfour\nfive")];
    let mut opts = options("needle");
    opts.files_with_matches = true;
    opts.context = Some(2);
    opts.before_context = Some(1);
    opts.after_context = Some(1);
    let result = grep_chunks(&chunks, &opts).expect("grep chunks");

    assert_eq!(result.matches.len(), 1);
    assert!(result.matches[0].before.is_empty());
    assert!(result.matches[0].after.is_empty());
    assert_eq!(
        matching_files(&result.matches, None).0,
        vec!["src/lib.rs".to_string()]
    );
}
