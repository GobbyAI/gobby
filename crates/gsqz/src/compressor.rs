use regex::Regex;

use crate::command_split;
use crate::config::{Config, Step};
use crate::primitives::{dedup, filter, group, match_output, prose, replace, truncate};

pub struct CompressionResult {
    pub compressed: String,
    pub original_chars: usize,
    pub compressed_chars: usize,
    pub strategy_name: String,
}

impl CompressionResult {
    pub fn savings_pct(&self) -> f64 {
        if self.original_chars == 0 {
            return 0.0;
        }
        (1.0 - self.compressed_chars as f64 / self.original_chars as f64) * 100.0
    }

    /// True when no useful compression occurred — original output should be
    /// surfaced verbatim, with no `[Output compressed by gsqz — …]` header
    /// and no daemon savings report. Covers:
    ///   - `passthrough` — output too short or fallback couldn't help.
    ///   - `excluded` — command matched an exclusion regex.
    ///   - `*/no-op` — a pipeline matched but adding the low-savings marker
    ///     would itself have grown the output, so we kept the original.
    pub fn is_passthrough(&self) -> bool {
        self.strategy_name == "passthrough"
            || self.strategy_name == "excluded"
            || self.strategy_name.ends_with("/no-op")
    }
}

struct CompiledPipeline {
    name: String,
    regex: Regex,
    steps: Vec<Step>,
    on_empty: Option<String>,
}

pub struct Compressor {
    pipelines: Vec<CompiledPipeline>,
    fallback_steps: Vec<Step>,
    excluded: Vec<Regex>,
    min_length: usize,
    max_lines: usize,
    global_on_empty: Option<String>,
}

impl Compressor {
    pub fn new(config: &Config) -> Self {
        let pipelines = config
            .pipelines
            .iter()
            .filter_map(|(name, p)| {
                Regex::new(&p.match_pattern)
                    .ok()
                    .map(|regex| CompiledPipeline {
                        name: name.clone(),
                        regex,
                        steps: p.steps.clone(),
                        on_empty: p.on_empty.clone(),
                    })
            })
            .collect();

        let excluded = config
            .excluded_commands
            .iter()
            .filter_map(|p| Regex::new(p).ok())
            .collect();

        Self {
            pipelines,
            fallback_steps: config.fallback.steps.clone(),
            excluded,
            min_length: config.settings.min_output_length,
            max_lines: config.settings.max_compressed_lines,
            global_on_empty: config.settings.on_empty.clone(),
        }
    }

    pub fn compress(&self, command: &str, output: &str) -> CompressionResult {
        let original_chars = output.len();

        // Skip if too short
        if original_chars < self.min_length {
            return CompressionResult {
                compressed: output.to_string(),
                original_chars,
                compressed_chars: original_chars,
                strategy_name: "passthrough".into(),
            };
        }

        // Skip excluded commands
        if self.excluded.iter().any(|r| r.is_match(command)) {
            return CompressionResult {
                compressed: output.to_string(),
                original_chars,
                compressed_chars: original_chars,
                strategy_name: "excluded".into(),
            };
        }

        // Find matching pipeline — try compound command segments in reverse
        // (last command's output is most relevant)
        let mut lines: Vec<String> = output.lines().map(|l| format!("{}\n", l)).collect();
        let mut strategy_name = "fallback".to_string();
        let mut pipeline_on_empty: Option<&str> = None;
        let segments = command_split::split_compound(command);

        let mut matched = false;
        'outer: for segment in segments.iter().rev() {
            for pipeline in &self.pipelines {
                if pipeline.regex.is_match(segment) {
                    strategy_name = pipeline.name.clone();
                    lines = apply_steps(lines, &pipeline.steps);
                    pipeline_on_empty = pipeline.on_empty.as_deref();
                    matched = true;
                    break 'outer;
                }
            }
        }

        if !matched {
            lines = apply_steps(lines, &self.fallback_steps);
            lines.insert(0, "[gsqz:passthrough]\n".to_string());
        }

        // Apply max_lines cap
        if self.max_lines > 0 && lines.len() > self.max_lines {
            let cap_head = (self.max_lines * 3) / 5;
            let cap_tail = self.max_lines - cap_head;
            lines = truncate::truncate(lines, cap_head, cap_tail, 0, "");
        }

        let compressed = lines.join("");
        let compressed_chars = compressed.len();

        // If compression produced empty output, try on_empty fallback
        if compressed.trim().is_empty() {
            let on_empty_msg = pipeline_on_empty.or(self.global_on_empty.as_deref());
            if let Some(msg) = on_empty_msg {
                return CompressionResult {
                    compressed_chars: msg.len(),
                    compressed: msg.to_string(),
                    original_chars,
                    strategy_name: format!("{}/on_empty", strategy_name),
                };
            }
            return CompressionResult {
                compressed: output.to_string(),
                original_chars,
                compressed_chars: original_chars,
                strategy_name: "passthrough".into(),
            };
        }

        // Named pipeline with low savings gets [gsqz:low-savings] marker.
        // Fallback path already has [gsqz:passthrough] marker, so just falls through.
        // Skip the marker if adding it would make the output larger than the original.
        if compressed_chars >= (original_chars * 95) / 100 && matched {
            let marked = format!("[gsqz:low-savings]\n{}", compressed);
            if marked.len() < original_chars {
                return CompressionResult {
                    compressed: marked.clone(),
                    original_chars,
                    compressed_chars: marked.len(),
                    strategy_name: format!("{}/low-savings", strategy_name),
                };
            }
            return CompressionResult {
                compressed: output.to_string(),
                original_chars,
                compressed_chars: original_chars,
                strategy_name: format!("{}/no-op", strategy_name),
            };
        }

        CompressionResult {
            compressed,
            original_chars,
            compressed_chars,
            strategy_name,
        }
    }
}

fn apply_steps(mut lines: Vec<String>, steps: &[Step]) -> Vec<String> {
    for step in steps {
        match step {
            Step::MatchOutput(args) => {
                if let Some(msg) = match_output::check(&lines, &args.rules) {
                    return vec![format!("{}\n", msg)];
                }
            }
            Step::FilterLines(args) => lines = filter::filter_lines(lines, &args.patterns),
            Step::GroupLines(args) => lines = group::group_lines(lines, &args.mode),
            Step::Truncate(args) => {
                lines = truncate::truncate(
                    lines,
                    args.head,
                    args.tail,
                    args.per_file_lines,
                    &args.file_marker,
                )
            }
            Step::Dedup(_) => lines = dedup::dedup(lines),
            Step::Replace(args) => lines = replace::replace(lines, &args.rules),
            Step::CompressProse(args) => {
                // Text-level step: join lines, compress, split back
                let level = prose::Level::from_str(&args.level).unwrap_or(prose::Level::Standard);
                let text = lines.join("");
                let compressed = prose::compress_prose(&text, level);
                lines = compressed.lines().map(|l| format!("{}\n", l)).collect();
            }
        }
    }
    lines
}

#[cfg(test)]
mod tests {
    use super::*;

    fn test_config() -> Config {
        Config::builtin()
    }

    #[test]
    fn test_passthrough_short_output() {
        let compressor = Compressor::new(&test_config());
        let result = compressor.compress("uv run pytest", "ok");
        assert_eq!(result.strategy_name, "passthrough");
        assert_eq!(result.compressed, "ok");
    }

    #[test]
    fn test_pipeline_match() {
        let compressor = Compressor::new(&test_config());
        let output = (0..200)
            .map(|i| format!("tests/test_{}.py PASSED\n", i))
            .collect::<String>();
        let result = compressor.compress("uv run pytest tests/", &output);
        assert_eq!(result.strategy_name, "pytest");
        assert!(result.compressed_chars < result.original_chars);
    }

    #[test]
    fn test_savings_pct_zero_original() {
        let result = CompressionResult {
            compressed: String::new(),
            original_chars: 0,
            compressed_chars: 0,
            strategy_name: "test".into(),
        };
        assert_eq!(result.savings_pct(), 0.0);
    }

    #[test]
    fn test_savings_pct_calculation() {
        let result = CompressionResult {
            compressed: "short".into(),
            original_chars: 1000,
            compressed_chars: 250,
            strategy_name: "test".into(),
        };
        assert!((result.savings_pct() - 75.0).abs() < 0.01);
    }

    #[test]
    fn test_fallback_used_when_no_pipeline_matches() {
        let compressor = Compressor::new(&test_config());
        // Use a command that won't match any pipeline
        let output = (0..200)
            .map(|i| format!("some random line number {}\n", i))
            .collect::<String>();
        let result = compressor.compress("some-unknown-command --flag", &output);
        assert_eq!(result.strategy_name, "fallback");
        assert!(result.compressed.starts_with("[gsqz:passthrough]\n"));
    }

    #[test]
    fn test_max_lines_cap_applied() {
        let config = test_config();
        let compressor = Compressor::new(&config);
        // Generate output that'll survive pipeline steps but exceed max_lines
        let output = (0..500)
            .map(|i| format!("unique line {} with distinct content abc{}\n", i, i * 37))
            .collect::<String>();
        let result = compressor.compress("some-unknown-command", &output);
        let line_count = result.compressed.lines().count();
        assert!(
            line_count <= config.settings.max_compressed_lines + 1, // +1 for omission marker
            "got {} lines, max is {}",
            line_count,
            config.settings.max_compressed_lines
        );
    }

    #[test]
    fn test_low_savings_fallback_keeps_passthrough_marker() {
        let compressor = Compressor::new(&test_config());
        // 25 unique long lines — fallback head=20+tail=20 won't truncate,
        // so savings < 5%. Fallback path keeps [gsqz:passthrough] marker.
        let output = (0..25)
            .map(|i| format!("unique line {} {}\n", i, "x".repeat(50)))
            .collect::<String>();
        let result = compressor.compress("some-unknown-command", &output);
        assert_eq!(result.strategy_name, "fallback");
        assert!(result.compressed.starts_with("[gsqz:passthrough]\n"));
    }

    #[test]
    fn test_git_status_pipeline() {
        let compressor = Compressor::new(&test_config());
        let mut lines = Vec::new();
        for i in 0..100 {
            lines.push(format!(" M src/file_{}.rs\n", i));
        }
        for i in 0..50 {
            lines.push(format!("?? new_{}.txt\n", i));
        }
        let output = lines.join("");
        let result = compressor.compress("git status", &output);
        assert_eq!(result.strategy_name, "git-status");
        assert!(result.compressed.contains("Modified"));
        assert!(result.compressed.contains("Untracked"));
    }

    #[test]
    fn test_cargo_test_pipeline() {
        let compressor = Compressor::new(&test_config());
        let mut lines: Vec<String> = (0..100)
            .map(|i| format!("test test_{} ... ok\n", i))
            .collect();
        lines.push("test result: ok. 100 passed; 0 failed\n".into());
        let output = lines.join("");
        let result = compressor.compress("cargo test", &output);
        assert_eq!(result.strategy_name, "cargo-test");
    }

    #[test]
    fn test_match_output_short_circuits() {
        let compressor = Compressor::new(&test_config());
        // cargo-test pipeline has match_output that fires on "test result: ok."
        let mut lines: Vec<String> = (0..100)
            .map(|i| format!("test test_{} ... ok\n", i))
            .collect();
        lines.push("test result: ok. 100 passed; 0 failed\n".into());
        let output = lines.join("");
        let result = compressor.compress("cargo test", &output);
        assert_eq!(result.strategy_name, "cargo-test");
        assert!(result.compressed.contains("All tests passed."));
    }

    #[test]
    fn test_match_output_unless_prevents_short_circuit() {
        let compressor = Compressor::new(&test_config());
        // If output contains FAILED, unless pattern blocks the match_output
        let mut lines: Vec<String> = (0..100)
            .map(|i| format!("test test_{} ... ok\n", i))
            .collect();
        lines.push("test test_bad ... FAILED\n".into());
        lines.push("test result: ok. 99 passed; 1 FAILED\n".into());
        let output = lines.join("");
        let result = compressor.compress("cargo test", &output);
        assert_eq!(result.strategy_name, "cargo-test");
        // Should NOT have short-circuited because of FAILED in output
        assert!(!result.compressed.contains("All tests passed."));
    }

    #[test]
    fn test_on_empty_global_fallback() {
        // When pipeline steps produce empty output, global on_empty kicks in
        let mut config = test_config();
        config.settings.on_empty = Some("Nothing left.".into());
        config.settings.min_output_length = 0; // ensure we don't passthrough early
        config.pipelines.insert(
            "filter-all".into(),
            crate::config::Pipeline {
                match_pattern: r"\bfilter-all\b".into(),
                steps: vec![crate::config::Step::FilterLines(
                    crate::config::FilterLinesArgs {
                        patterns: vec![".*".into()],
                    },
                )],
                on_empty: None,
            },
        );
        let compressor = Compressor::new(&config);
        let output = (0..50).map(|i| format!("line {}\n", i)).collect::<String>();
        let result = compressor.compress("filter-all", &output);
        assert_eq!(result.compressed, "Nothing left.");
        assert!(result.strategy_name.contains("on_empty"));
    }

    #[test]
    fn test_on_empty_pipeline_overrides_global() {
        let mut config = test_config();
        config.settings.on_empty = Some("Global fallback.".into());
        config.settings.min_output_length = 0;
        config.pipelines.insert(
            "filter-all".into(),
            crate::config::Pipeline {
                match_pattern: r"\bfilter-all\b".into(),
                steps: vec![crate::config::Step::FilterLines(
                    crate::config::FilterLinesArgs {
                        patterns: vec![".*".into()],
                    },
                )],
                on_empty: Some("Pipeline-specific.".into()),
            },
        );
        let compressor = Compressor::new(&config);
        let output = (0..50).map(|i| format!("line {}\n", i)).collect::<String>();
        let result = compressor.compress("filter-all", &output);
        assert_eq!(result.compressed, "Pipeline-specific.");
    }

    #[test]
    fn test_on_empty_not_used_when_output_nonempty() {
        let mut config = test_config();
        config.settings.on_empty = Some("Should not appear.".into());
        let compressor = Compressor::new(&config);
        let output = (0..200)
            .map(|i| format!("some random line number {}\n", i))
            .collect::<String>();
        let result = compressor.compress("some-unknown-command", &output);
        assert!(!result.compressed.contains("Should not appear."));
    }

    #[test]
    fn test_low_savings_pipeline_gets_marker() {
        // A named pipeline that compresses just a little (still above the 95% threshold)
        // gets the [gsqz:low-savings] marker as long as the marker fits under the original.
        let mut config = test_config();
        config.settings.min_output_length = 0;
        config.pipelines.insert(
            "minimal-pipeline".into(),
            crate::config::Pipeline {
                match_pattern: r"\bminimal\b".into(),
                steps: vec![crate::config::Step::FilterLines(
                    crate::config::FilterLinesArgs {
                        patterns: vec!["^drop ".into()],
                    },
                )],
                on_empty: None,
            },
        );
        let compressor = Compressor::new(&config);
        // ~100 keep lines + 3 drop lines — filter removes ~3% of bytes, under the
        // 5% savings threshold, but the savings are enough that the ~20-byte marker
        // still fits under the original size.
        let mut lines: Vec<String> = (0..100)
            .map(|i| format!("keep line number {}\n", i))
            .collect();
        lines.push("drop this entire line\n".into());
        lines.push("drop this entire line\n".into());
        lines.push("drop this entire line\n".into());
        let output = lines.join("");
        let result = compressor.compress("minimal", &output);
        assert!(result.strategy_name.contains("low-savings"));
        assert!(result.compressed.starts_with("[gsqz:low-savings]\n"));
        assert!(result.compressed_chars < result.original_chars);
    }

    #[test]
    fn test_low_savings_suppressed_when_marker_would_grow_output() {
        // A named pipeline that produces zero savings should NOT get the marker —
        // adding it would make the output larger than the original. Return the
        // original unchanged with a {pipeline}/no-op strategy name instead.
        let mut config = test_config();
        config.settings.min_output_length = 0;
        config.pipelines.insert(
            "noop-pipeline".into(),
            crate::config::Pipeline {
                match_pattern: r"\bnoop\b".into(),
                steps: vec![], // no steps = no compression
                on_empty: None,
            },
        );
        let compressor = Compressor::new(&config);
        let output = (0..20)
            .map(|i| format!("unique line {}\n", i))
            .collect::<String>();
        let result = compressor.compress("noop", &output);
        assert_eq!(result.strategy_name, "noop-pipeline/no-op");
        assert!(!result.compressed.contains("[gsqz:low-savings]"));
        assert_eq!(result.compressed, output);
        assert_eq!(result.compressed_chars, result.original_chars);
        // /no-op is a passthrough — main.rs uses this to skip the outer header.
        assert!(result.is_passthrough());
    }

    #[test]
    fn test_is_passthrough_classification() {
        let mk = |name: &str| CompressionResult {
            compressed: String::new(),
            original_chars: 0,
            compressed_chars: 0,
            strategy_name: name.into(),
        };
        // Pure passthrough cases — main.rs surfaces output verbatim.
        assert!(mk("passthrough").is_passthrough());
        assert!(mk("excluded").is_passthrough());
        assert!(mk("git-mutation/no-op").is_passthrough());
        assert!(mk("cargo-test/no-op").is_passthrough());
        // Real compression — main.rs prepends the header and reports to daemon.
        assert!(!mk("git-status").is_passthrough());
        assert!(!mk("cargo-test/low-savings").is_passthrough());
        assert!(!mk("pytest/on_empty").is_passthrough());
        assert!(!mk("fallback").is_passthrough());
    }

    #[test]
    fn test_good_compression_has_no_marker() {
        let compressor = Compressor::new(&test_config());
        let mut lines: Vec<String> = (0..100)
            .map(|i| format!("test test_{} ... ok\n", i))
            .collect();
        lines.push("test result: ok. 100 passed; 0 failed\n".into());
        let output = lines.join("");
        let result = compressor.compress("cargo test", &output);
        assert!(!result.compressed.contains("[gsqz:"));
    }

    #[test]
    fn test_compound_command_matches_last_segment() {
        let compressor = Compressor::new(&test_config());
        // "cargo build && cargo test" — last segment "cargo test" matches cargo-test pipeline
        let mut lines: Vec<String> = (0..100)
            .map(|i| format!("test test_{} ... ok\n", i))
            .collect();
        lines.push("test result: ok. 100 passed; 0 failed\n".into());
        let output = lines.join("");
        let result = compressor.compress("cargo build && cargo test", &output);
        assert_eq!(result.strategy_name, "cargo-test");
    }

    #[test]
    fn test_compound_single_command_unchanged() {
        let compressor = Compressor::new(&test_config());
        let output = (0..200)
            .map(|i| format!("some random line number {}\n", i))
            .collect::<String>();
        // Single command behaves exactly as before
        let result = compressor.compress("some-unknown-command", &output);
        assert_eq!(result.strategy_name, "fallback");
    }

    #[test]
    fn test_compound_falls_back_to_earlier_segment() {
        let compressor = Compressor::new(&test_config());
        // Last segment doesn't match any pipeline, first does
        let output = (0..200)
            .map(|i| format!(" M src/file_{}.rs\n", i))
            .collect::<String>();
        let result = compressor.compress("git status && unknown-cmd", &output);
        assert_eq!(result.strategy_name, "git-status");
    }
}
