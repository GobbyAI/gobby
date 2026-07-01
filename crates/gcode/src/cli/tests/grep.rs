use super::*;

#[test]
fn parse_grep_basic() {
    let cli = Cli::try_parse_from(["gcode", "grep", "needle", "src"]).expect("grep basic parses");
    match cli.command {
        Command::Grep {
            pattern,
            paths,
            fixed_strings,
            ignore_case,
            word,
            ..
        } => {
            assert_eq!(pattern, "needle");
            assert_eq!(paths, vec!["src"]);
            assert!(!fixed_strings);
            assert!(!ignore_case);
            assert!(!word);
        }
        _ => panic!("expected grep command"),
    }
}

#[test]
fn parse_grep_ignore_case() {
    let cli = Cli::try_parse_from(["gcode", "grep", "needle", "--ignore-case"])
        .expect("grep ignore-case parses");
    match cli.command {
        Command::Grep { ignore_case, .. } => assert!(ignore_case),
        _ => panic!("expected grep command"),
    }
}

#[test]
fn parse_grep_word() {
    let cli = Cli::try_parse_from(["gcode", "grep", "-w", "note_path"]).expect("grep -w parses");
    match cli.command {
        Command::Grep { pattern, word, .. } => {
            assert_eq!(pattern, "note_path");
            assert!(word);
        }
        _ => panic!("expected grep command"),
    }
}

#[test]
fn parse_grep_word_long_with_fixed_json() {
    let cli = Cli::try_parse_from([
        "gcode",
        "grep",
        "--word",
        "-F",
        "note_path",
        "src",
        "-m",
        "50",
        "--format",
        "json",
    ])
    .expect("grep --word with fixed-string json parses");
    assert!(matches!(cli.format, Some(output::Format::Json)));
    match cli.command {
        Command::Grep {
            pattern,
            paths,
            fixed_strings,
            word,
            max_count,
            ..
        } => {
            assert_eq!(pattern, "note_path");
            assert_eq!(paths, vec!["src"]);
            assert!(fixed_strings);
            assert!(word);
            assert_eq!(max_count, Some(50));
        }
        _ => panic!("expected grep command"),
    }
}

#[test]
fn parse_grep_with_flags() {
    let cli = Cli::try_parse_from([
        "gcode",
        "grep",
        "needle",
        "-F",
        "-C",
        "2",
        "-g",
        "*.py",
        "src/gobby",
    ])
    .expect("grep with flags parses");
    match cli.command {
        Command::Grep {
            pattern,
            paths,
            fixed_strings,
            context,
            glob,
            ..
        } => {
            assert_eq!(pattern, "needle");
            assert_eq!(paths, vec!["src/gobby"]);
            assert!(fixed_strings);
            assert_eq!(context, Some(2));
            assert_eq!(glob, vec!["*.py"]);
        }
        _ => panic!("expected grep command"),
    }
}

#[test]
fn parse_grep_max_count() {
    let cli = Cli::try_parse_from(["gcode", "grep", "needle", "-m", "5", "src"])
        .expect("grep with -m parses");
    match cli.command {
        Command::Grep {
            paths, max_count, ..
        } => {
            assert_eq!(paths, vec!["src"]);
            assert_eq!(max_count, Some(5));
        }
        _ => panic!("expected grep command"),
    }

    let cli = Cli::try_parse_from(["gcode", "grep", "needle", "--max-count", "5", "src"])
        .expect("grep with --max-count parses");
    match cli.command {
        Command::Grep {
            paths, max_count, ..
        } => {
            assert_eq!(paths, vec!["src"]);
            assert_eq!(max_count, Some(5));
        }
        _ => panic!("expected grep command"),
    }

    let cli = Cli::try_parse_from(["gcode", "grep", "needle", "src", "-m", "5"])
        .expect("grep with -m after path parses");
    match cli.command {
        Command::Grep {
            paths, max_count, ..
        } => {
            assert_eq!(paths, vec!["src"]);
            assert_eq!(max_count, Some(5));
        }
        _ => panic!("expected grep command"),
    }

    let cli = Cli::try_parse_from(["gcode", "grep", "needle", "src", "--max-count", "5"])
        .expect("grep with --max-count after path parses");
    match cli.command {
        Command::Grep {
            paths, max_count, ..
        } => {
            assert_eq!(paths, vec!["src"]);
            assert_eq!(max_count, Some(5));
        }
        _ => panic!("expected grep command"),
    }

    let too_large = (MAX_GREP_MAX_COUNT + 1).to_string();
    let error = match Cli::try_parse_from(["gcode", "grep", "needle", "--max-count", &too_large]) {
        Ok(_) => panic!("oversized grep max-count must fail"),
        Err(error) => error,
    };
    assert!(error.to_string().contains("no more than 10000"));
}

#[test]
fn parse_grep_rejects_limit() {
    let err = match Cli::try_parse_from(["gcode", "grep", "needle", "src", "--limit", "5"]) {
        Ok(_) => panic!("--limit should be rejected by clap"),
        Err(err) => err,
    };
    assert!(
        err.to_string().contains("unexpected argument '--limit'"),
        "unexpected error: {err}"
    );
}

#[test]
fn parse_grep_rejects_line_number_flag() {
    let err = match Cli::try_parse_from(["gcode", "grep", "-n", "needle", "src"]) {
        Ok(_) => panic!("-n should be rejected by clap"),
        Err(err) => err,
    };
    assert!(
        err.to_string().contains("unexpected argument '-n'"),
        "unexpected error: {err}"
    );
}

#[test]
fn parse_grep_rejects_empty_pattern() {
    let err = match Cli::try_parse_from(["gcode", "grep", ""]) {
        Ok(_) => panic!("empty pattern should be rejected"),
        Err(err) => err,
    };
    assert!(
        err.to_string()
            .contains("gcode grep pattern cannot be empty"),
        "unexpected error: {err}"
    );
}

#[test]
fn parse_grep_unsupported_flag_fails_before_context_resolution() {
    let err = match Cli::try_parse_from(["gcode", "grep", "needle", "--files-with-matches"]) {
        Ok(_) => panic!("unsupported grep flag should fail before context resolution"),
        Err(err) => err,
    };

    assert!(
        err.to_string()
            .contains("unexpected argument '--files-with-matches'"),
        "unexpected error: {err}"
    );
}

#[test]
fn parse_grep_unsupported_flag_after_path_fails_in_clap() {
    let err = match Cli::try_parse_from(["gcode", "grep", "needle", "src", "--files-with-matches"])
    {
        Ok(_) => panic!("unsupported grep flag after path should fail"),
        Err(err) => err,
    };

    assert!(
        err.to_string()
            .contains("unexpected argument '--files-with-matches'"),
        "unexpected error: {err}"
    );
}

#[test]
fn parse_grep_with_global_format() {
    let cli = Cli::try_parse_from(["gcode", "--format", "text", "grep", "needle", "src"])
        .expect("grep with global format parses");
    assert!(matches!(cli.format, Some(output::Format::Text)));
    match cli.command {
        Command::Grep { pattern, .. } => assert_eq!(pattern, "needle"),
        _ => panic!("expected grep command"),
    }
}

#[test]
fn effective_format_defaults_grep_to_text() {
    let cli =
        Cli::try_parse_from(["gcode", "grep", "needle", "src", "-m", "50"]).expect("grep parses");

    assert!(cli.format.is_none());
    assert!(matches!(
        effective_format(cli.format, &cli.command),
        output::Format::Text
    ));
}

#[test]
fn effective_format_honors_explicit_grep_json() {
    let cli = Cli::try_parse_from([
        "gcode", "grep", "needle", "src", "-m", "50", "--format", "json",
    ])
    .expect("grep parses with explicit json format");

    assert!(matches!(cli.format, Some(output::Format::Json)));
    assert!(matches!(
        effective_format(cli.format, &cli.command),
        output::Format::Json
    ));
}

#[test]
fn effective_format_keeps_other_commands_json_by_default() {
    let cli = Cli::try_parse_from(["gcode", "search-content", "needle"]).expect("search parses");

    assert!(cli.format.is_none());
    assert!(matches!(
        effective_format(cli.format, &cli.command),
        output::Format::Json
    ));
}
