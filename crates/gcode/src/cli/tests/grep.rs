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
fn parse_grep_line_number_flag_is_accepted_noop() {
    let cli = Cli::try_parse_from(["gcode", "grep", "-n", "needle", "src"])
        .expect("-n should parse as an accepted no-op");
    match cli.command {
        Command::Grep {
            pattern,
            paths,
            line_number,
            ..
        } => {
            assert_eq!(pattern, "needle");
            assert_eq!(paths, vec!["src"]);
            assert!(line_number);
        }
        _ => panic!("expected grep command"),
    }
}

#[test]
fn parse_grep_files_with_matches() {
    let cli = Cli::try_parse_from(["gcode", "grep", "-l", "needle"]).expect("grep -l parses");
    match cli.command {
        Command::Grep {
            files_with_matches, ..
        } => assert!(files_with_matches),
        _ => panic!("expected grep command"),
    }

    let cli = Cli::try_parse_from(["gcode", "grep", "--files-with-matches", "needle", "src"])
        .expect("grep --files-with-matches parses");
    match cli.command {
        Command::Grep {
            files_with_matches,
            paths,
            ..
        } => {
            assert!(files_with_matches);
            assert_eq!(paths, vec!["src"]);
        }
        _ => panic!("expected grep command"),
    }
}

#[test]
fn parse_grep_extended_regexp_is_accepted_noop() {
    let cli = Cli::try_parse_from(["gcode", "grep", "-E", "a|b"])
        .expect("-E should parse as an accepted no-op");
    match cli.command {
        Command::Grep {
            pattern,
            extended_regexp,
            ..
        } => {
            assert_eq!(pattern, "a|b");
            assert!(extended_regexp);
        }
        _ => panic!("expected grep command"),
    }
}

#[test]
fn parse_grep_recursive_flags_are_accepted_noops() {
    let cli = Cli::try_parse_from(["gcode", "grep", "-r", "needle"])
        .expect("-r should parse as an accepted no-op");
    match cli.command {
        Command::Grep {
            recursive,
            recursive_dereference,
            ..
        } => {
            assert!(recursive);
            assert!(!recursive_dereference);
        }
        _ => panic!("expected grep command"),
    }

    let cli = Cli::try_parse_from(["gcode", "grep", "-R", "needle"])
        .expect("-R should parse as an accepted no-op");
    match cli.command {
        Command::Grep {
            recursive,
            recursive_dereference,
            ..
        } => {
            assert!(!recursive);
            assert!(recursive_dereference);
        }
        _ => panic!("expected grep command"),
    }
}

#[test]
fn parse_grep_files_with_matches_composed_with_max_count_and_paths() {
    let cli = Cli::try_parse_from(["gcode", "grep", "-l", "-m", "3", "needle", "src", "tests"])
        .expect("grep -l with -m and paths parses");
    match cli.command {
        Command::Grep {
            files_with_matches,
            max_count,
            pattern,
            paths,
            ..
        } => {
            assert!(files_with_matches);
            assert_eq!(max_count, Some(3));
            assert_eq!(pattern, "needle");
            assert_eq!(paths, vec!["src", "tests"]);
        }
        _ => panic!("expected grep command"),
    }
}

#[test]
fn grep_help_hides_accepted_noop_flags() {
    let help = Cli::command()
        .find_subcommand("grep")
        .expect("grep subcommand")
        .clone()
        .render_help()
        .to_string();
    assert!(
        !help.contains("--extended-regexp")
            && !help.contains("--line-number")
            && !help.contains("--recursive"),
        "accepted no-op flags must stay hidden from --help: {help}"
    );
    assert!(
        help.contains("-l") && help.contains("--files-with-matches"),
        "-l/--files-with-matches should be advertised: {help}"
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
