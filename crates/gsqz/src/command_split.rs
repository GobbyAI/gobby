/// Split a compound command at `&&`, `||`, and `;` operators,
/// respecting single/double quotes and parentheses.
/// Pipe chains (`|`) are NOT split.
/// Returns trimmed segments; single commands return a single-element vec.
pub fn split_compound(cmd: &str) -> Vec<&str> {
    let bytes = cmd.as_bytes();
    let len = bytes.len();
    let mut segments = Vec::new();
    let mut start = 0;
    let mut i = 0;
    let mut in_single_quote = false;
    let mut in_double_quote = false;
    let mut paren_depth: usize = 0;

    while i < len {
        let c = bytes[i];

        if in_single_quote {
            if c == b'\'' {
                in_single_quote = false;
            }
            i += 1;
            continue;
        }

        if in_double_quote {
            if c == b'"' {
                in_double_quote = false;
            } else if c == b'\\' && i + 1 < len {
                i += 1; // skip escaped char
            }
            i += 1;
            continue;
        }

        match c {
            b'\'' => in_single_quote = true,
            b'"' => in_double_quote = true,
            b'(' => paren_depth += 1,
            b')' => paren_depth = paren_depth.saturating_sub(1),
            b'&' if paren_depth == 0 && i + 1 < len && bytes[i + 1] == b'&' => {
                let seg = cmd[start..i].trim();
                if !seg.is_empty() {
                    segments.push(seg);
                }
                i += 2;
                start = i;
                continue;
            }
            b'|' if paren_depth == 0 && i + 1 < len && bytes[i + 1] == b'|' => {
                let seg = cmd[start..i].trim();
                if !seg.is_empty() {
                    segments.push(seg);
                }
                i += 2;
                start = i;
                continue;
            }
            b';' if paren_depth == 0 => {
                let seg = cmd[start..i].trim();
                if !seg.is_empty() {
                    segments.push(seg);
                }
                i += 1;
                start = i;
                continue;
            }
            _ => {}
        }

        i += 1;
    }

    // Final segment
    let seg = cmd[start..].trim();
    if !seg.is_empty() {
        segments.push(seg);
    }

    if segments.is_empty() {
        vec![cmd]
    } else {
        segments
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_single_command() {
        assert_eq!(split_compound("cargo test"), vec!["cargo test"]);
    }

    #[test]
    fn test_and_split() {
        assert_eq!(
            split_compound("cargo build && cargo test"),
            vec!["cargo build", "cargo test"]
        );
    }

    #[test]
    fn test_or_split() {
        assert_eq!(split_compound("cmd1 || cmd2"), vec!["cmd1", "cmd2"]);
    }

    #[test]
    fn test_semicolon_split() {
        assert_eq!(split_compound("cmd1; cmd2"), vec!["cmd1", "cmd2"]);
    }

    #[test]
    fn test_mixed_operators() {
        assert_eq!(
            split_compound("cmd1 && cmd2 || cmd3; cmd4"),
            vec!["cmd1", "cmd2", "cmd3", "cmd4"]
        );
    }

    #[test]
    fn test_pipe_not_split() {
        assert_eq!(
            split_compound("cmd1 | cmd2 && cmd3"),
            vec!["cmd1 | cmd2", "cmd3"]
        );
    }

    #[test]
    fn test_single_quoted_operators_preserved() {
        assert_eq!(
            split_compound("echo 'a && b' && cmd2"),
            vec!["echo 'a && b'", "cmd2"]
        );
    }

    #[test]
    fn test_double_quoted_operators_preserved() {
        assert_eq!(
            split_compound(r#"echo "a && b" && cmd2"#),
            vec![r#"echo "a && b""#, "cmd2"]
        );
    }

    #[test]
    fn test_parenthesized_operators_preserved() {
        assert_eq!(
            split_compound("(cd foo && make) && cmd2"),
            vec!["(cd foo && make)", "cmd2"]
        );
    }

    #[test]
    fn test_empty_string() {
        assert_eq!(split_compound(""), vec![""]);
    }

    #[test]
    fn test_trailing_operator() {
        assert_eq!(split_compound("cmd1 &&"), vec!["cmd1"]);
    }

    #[test]
    fn test_whitespace_trimming() {
        assert_eq!(split_compound("  cmd1  &&  cmd2  "), vec!["cmd1", "cmd2"]);
    }
}
