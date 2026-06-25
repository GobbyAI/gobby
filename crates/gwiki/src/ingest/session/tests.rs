use super::*;

#[test]
fn render_session_markdown_emits_deterministic_session_frontmatter() {
    let snapshot = SessionFileSnapshot {
        location: "/tmp/session.jsonl".to_string(),
        file_name: "session.jsonl".to_string(),
        fetched_at: "2026-06-16T20:00:00Z".to_string(),
        path: PathBuf::from("/tmp/session.jsonl"),
        bytes: Vec::new(),
    };
    let parsed = ParsedSession {
        title: "Fixture session".to_string(),
        session_type: "claude-code".to_string(),
        started_at: Some("2026-06-16T20:00:00Z".to_string()),
        metadata: ParsedSessionMetadata {
            model: Some("claude-opus-4-8".to_string()),
            token_totals: std::collections::BTreeMap::from([
                ("input_tokens".to_string(), 10),
                ("output_tokens".to_string(), 5),
            ]),
            git_branch: Some("dev".to_string()),
            is_subagent: true,
        },
        messages: vec![
            ParsedSessionMessage {
                role: "assistant".to_string(),
                timestamp: Some("2026-06-16T20:00:00Z".to_string()),
                content: "I will inspect.".to_string(),
                tool_names: vec!["Read".to_string(), "Bash".to_string()],
            },
            ParsedSessionMessage {
                role: "assistant".to_string(),
                timestamp: Some("2026-06-16T21:01:01Z".to_string()),
                content: "Done.".to_string(),
                tool_names: vec!["Read".to_string()],
            },
        ],
    };

    let markdown = render_session_markdown(&snapshot, &parsed, &parsed.title, "hash");

    assert!(markdown.contains("model: claude-opus-4-8\n"));
    assert!(markdown.contains("tool_counts: {\"Bash\":1,\"Read\":2}\n"));
    assert!(markdown.contains("token_totals: {\"input_tokens\":10,\"output_tokens\":5}\n"));
    assert!(markdown.contains("duration_seconds: 3661\n"));
    assert!(
        markdown
            .contains("hour_buckets: {\"2026-06-16T20:00:00Z\":1,\"2026-06-16T21:00:00Z\":1}\n")
    );
    assert!(markdown.contains("is_subagent: true\n"));
    assert!(markdown.contains("gitBranch: dev\n"));
}

#[test]
fn session_wiki_ingest_strips_daemon_frontmatter_and_redacts() {
    let temp = tempfile::tempdir().expect("tempdir");
    let openai_key = format!("{}{}", "sk-proj-", "abcdefghijklmnopqrstuvwxyz123456");
    let daemon_md = format!(
        concat!(
            "---\n",
            "title: \"Session: abcd1234 — 2026-06-24\"\n",
            "type: source\n",
            "tags: [rust, sessions]\n",
            "date: 2026-06-24\n",
            "model: claude-opus-4-8\n",
            "project: proj-1\n",
            "session_id: sess-1\n",
            "source: claude\n",
            "---\n",
            "\n",
            "## Summary\n\nWired the synthesis ingest.\n\n",
            "## Key Quotes\n\n> Token {key} lives at /Users/casey/secret.env\n\n",
            "## Connections\n\n- [[session-transcript-wiki-fix]]\n"
        ),
        key = openai_key
    );
    let snapshot = SessionWikiFileSnapshot {
        external_id: "abcd1234-0000-4000-8000-000000000000".to_string(),
        path: PathBuf::from(
            "/Users/josh/.gobby/session_wiki/abcd1234-0000-4000-8000-000000000000.md",
        ),
        fetched_at: "2026-06-24T00:00:00Z".to_string(),
        bytes: daemon_md.into_bytes(),
    };

    let result =
        ingest_session_wiki_file_without_index(temp.path(), snapshot).expect("ingest wiki");

    assert_eq!(
        result.record.location,
        "session:abcd1234-0000-4000-8000-000000000000"
    );

    let derived = std::fs::read_to_string(
        temp.path()
            .join("knowledge")
            .join("sources")
            .join(format!("{}.md", result.record.id)),
    )
    .expect("derived markdown");

    // Exactly one frontmatter block: the daemon block was stripped.
    let fence_lines = derived
        .lines()
        .filter(|line| line.trim_end() == "---")
        .count();
    assert_eq!(
        fence_lines, 2,
        "expected one frontmatter block, got:\n{derived}"
    );
    assert!(!derived.contains("type: source"));
    assert!(!derived.contains("session_id: sess-1"));
    // The daemon `project:` line is stripped, but its value is preserved as a
    // gwiki-owned `session_project` field. Match on exact lines: a substring
    // check is ambiguous because `session_project: proj-1` contains the
    // `project: proj-1` substring.
    assert!(
        derived
            .lines()
            .any(|line| line.trim() == "session_project: proj-1"),
        "expected preserved session_project provenance, got:\n{derived}"
    );
    assert!(
        !derived
            .lines()
            .any(|line| line.trim_start().starts_with("project:")),
        "expected raw daemon project: line to be stripped, got:\n{derived}"
    );

    // gwiki-owned frontmatter.
    assert!(derived.contains("source_kind: session"));
    assert!(derived.contains("session:abcd1234-0000-4000-8000-000000000000"));
    assert!(derived.contains("source_archive:"));
    assert!(derived.contains("session_type: claude"));
    assert!(derived.contains("model: claude-opus-4-8"));
    assert!(derived.contains("tags:"));
    assert!(derived.contains("rust"));

    // Body kept: H1 title + sections + wikilink, no `## Messages` dump.
    assert!(derived.contains("# Session: abcd1234"));
    assert!(derived.contains("## Summary"));
    assert!(derived.contains("## Connections"));
    assert!(derived.contains("[[session-transcript-wiki-fix]]"));
    assert!(!derived.contains("## Messages"));

    // Defensive re-redaction over the synthesized body and the mirror path.
    assert!(derived.contains("[REDACTED_API_KEY]"));
    assert!(!derived.contains(&openai_key));
    assert!(!derived.contains("/Users/casey"));
    assert!(!derived.contains("/Users/josh"));
    assert!(derived.contains("[REDACTED_HOME]"));
}

#[test]
fn common_session_adapter_accepts_fixture_payload_messages() {
    let envelopes = vec![SessionArchiveEnvelope {
        envelope_type: "session".to_string(),
        timestamp: Some("2026-06-16T20:00:00Z".to_string()),
        payload: serde_json::json!({
            "title": "Fixture import",
            "messages": [
                {"role": "user", "content": "Can you summarize this?"},
                {"role": "assistant", "timestamp": "2026-06-16T20:00:05Z", "content": "Yes."}
            ]
        }),
    }];

    let adapters = default_session_adapters();
    let parsed = parse_session_archive(&envelopes, &adapters).expect("parse session fixture");

    assert_eq!(parsed.title, "Fixture import");
    assert_eq!(parsed.session_type, "session");
    assert_eq!(parsed.started_at.as_deref(), Some("2026-06-16T20:00:00Z"));
    assert_eq!(parsed.messages.len(), 2);
    assert_eq!(parsed.messages[0].role, "user");
    assert_eq!(parsed.messages[0].content, "Can you summarize this?");
    assert_eq!(
        parsed.messages[1].timestamp.as_deref(),
        Some("2026-06-16T20:00:05Z")
    );
}

#[test]
fn read_session_archive_accepts_raw_claude_code_records() {
    let bytes = br#"{"type":"user","sessionId":"session-1","message":{"role":"user","content":"Hello"}}
{"type":"assistant","timestamp":"2026-06-16T20:00:01Z","message":{"role":"assistant","content":[{"type":"text","text":"Hi."}]}}
"#;

    let envelopes =
        read_session_archive(Path::new("claude.jsonl"), bytes).expect("read raw archive");

    assert_eq!(envelopes.len(), 2);
    assert_eq!(envelopes[0].envelope_type, "user");
    assert_eq!(envelopes[0].timestamp, None);
    assert!(envelopes[0].payload.get("message").is_some());
    assert_eq!(
        envelopes[1].timestamp.as_deref(),
        Some("2026-06-16T20:00:01Z")
    );
}

#[test]
fn claude_code_adapter_parses_messages_tools_and_sidechains() {
    let envelopes = vec![
        SessionArchiveEnvelope {
            envelope_type: "ai-title".to_string(),
            timestamp: Some("2026-06-16T20:00:00Z".to_string()),
            payload: serde_json::json!({
                "type": "ai-title",
                "timestamp": "2026-06-16T20:00:00Z",
                "aiTitle": "Claude Fixture"
            }),
        },
        SessionArchiveEnvelope {
            envelope_type: "user".to_string(),
            timestamp: Some("2026-06-16T20:00:01Z".to_string()),
            payload: serde_json::json!({
                "type": "user",
                "timestamp": "2026-06-16T20:00:01Z",
                "message": {"role": "user", "content": "Inspect this repo."}
            }),
        },
        SessionArchiveEnvelope {
            envelope_type: "assistant".to_string(),
            timestamp: Some("2026-06-16T20:00:02Z".to_string()),
            payload: serde_json::json!({
                "type": "assistant",
                "timestamp": "2026-06-16T20:00:02Z",
                "gitBranch": "dev",
                "message": {
                    "id": "msg_1",
                    "model": "claude-opus-4-8",
                    "role": "assistant",
                    "usage": {
                        "input_tokens": 10,
                        "output_tokens": 5
                    },
                    "content": [
                        {"type": "thinking", "thinking": "internal"},
                        {"type": "text", "text": "I will inspect it."},
                        {"type": "tool_use", "name": "Read", "input": {"file_path": "Cargo.toml"}}
                    ]
                }
            }),
        },
        SessionArchiveEnvelope {
            envelope_type: "user".to_string(),
            timestamp: Some("2026-06-16T20:00:03Z".to_string()),
            payload: serde_json::json!({
                "type": "user",
                "timestamp": "2026-06-16T20:00:03Z",
                "toolUseResult": {"file": "Cargo.toml"},
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_1",
                            "content": "workspace manifest",
                            "is_error": false
                        }
                    ]
                }
            }),
        },
        SessionArchiveEnvelope {
            envelope_type: "assistant".to_string(),
            timestamp: Some("2026-06-16T20:00:04Z".to_string()),
            payload: serde_json::json!({
                "type": "assistant",
                "timestamp": "2026-06-16T20:00:04Z",
                "isSidechain": true,
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "Sidechain note."}]
                }
            }),
        },
    ];

    let adapters = default_session_adapters();
    let parsed = parse_session_archive(&envelopes, &adapters).expect("parse Claude Code fixture");

    assert_eq!(parsed.title, "Claude Fixture");
    assert_eq!(parsed.session_type, "claude-code");
    assert_eq!(parsed.started_at.as_deref(), Some("2026-06-16T20:00:00Z"));
    assert_eq!(parsed.metadata.model.as_deref(), Some("claude-opus-4-8"));
    assert_eq!(parsed.metadata.git_branch.as_deref(), Some("dev"));
    assert!(parsed.metadata.is_subagent);
    assert_eq!(parsed.metadata.token_totals.get("input_tokens"), Some(&10));
    assert_eq!(parsed.metadata.token_totals.get("output_tokens"), Some(&5));
    assert_eq!(parsed.messages.len(), 4);
    assert_eq!(parsed.messages[0].role, "user");
    assert_eq!(parsed.messages[0].content, "Inspect this repo.");
    assert_eq!(parsed.messages[1].role, "assistant");
    assert_eq!(parsed.messages[1].tool_names, vec!["Read"]);
    assert!(parsed.messages[1].content.contains("I will inspect it."));
    assert!(parsed.messages[1].content.contains("Tool use: Read"));
    assert!(!parsed.messages[1].content.contains("internal"));
    assert_eq!(parsed.messages[2].role, "tool result");
    assert!(parsed.messages[2].content.contains("Tool result: toolu_1"));
    assert!(parsed.messages[2].content.contains("workspace manifest"));
    assert_eq!(parsed.messages[3].role, "assistant (sidechain)");
    assert_eq!(parsed.messages[3].content, "Sidechain note.");
}

#[test]
fn claude_code_adapter_parses_real_archive_when_fixture_is_set() {
    let Ok(path) = std::env::var("GWIKI_CLAUDE_CODE_ARCHIVE_FIXTURE") else {
        return;
    };
    let bytes = std::fs::read(&path).expect("read real Claude Code archive fixture");
    let envelopes =
        read_session_archive(Path::new(&path), &bytes).expect("read real Claude Code archive");
    let adapters = default_session_adapters();

    let parsed =
        parse_session_archive(&envelopes, &adapters).expect("parse real Claude Code archive");

    assert_eq!(parsed.session_type, "claude-code");
    assert!(
        parsed.messages.len() > 1,
        "expected more than one parsed Claude Code message"
    );
    assert!(parsed.messages.iter().any(|message| message.role == "user"));
    assert!(
        parsed.messages.iter().any(|message| {
            message.role == "assistant" || message.role == "assistant (sidechain)"
        })
    );
    assert!(
        parsed
            .messages
            .iter()
            .any(|message| message.role.starts_with("tool result")),
        "expected real archive to include at least one tool result"
    );
    assert!(parsed.metadata.model.is_some(), "expected model metadata");
    assert!(
        parsed.metadata.git_branch.is_some(),
        "expected gitBranch metadata"
    );
    assert!(
        !parsed.metadata.token_totals.is_empty(),
        "expected token totals metadata"
    );
    assert!(
        parsed
            .messages
            .iter()
            .any(|message| !message.tool_names.is_empty()),
        "expected tool name metadata"
    );

    let snapshot = SessionFileSnapshot {
        location: path.clone(),
        file_name: "claude-code-real.jsonl".to_string(),
        fetched_at: "2026-06-16T00:00:00Z".to_string(),
        path: PathBuf::from(&path),
        bytes,
    };
    let markdown = render_session_markdown(&snapshot, &parsed, &parsed.title, "fixture-hash");

    assert!(markdown.contains("model: "));
    assert!(markdown.contains("tool_counts: {"));
    assert!(markdown.contains("token_totals: {"));
    assert!(markdown.contains("duration_seconds: "));
    assert!(markdown.contains("hour_buckets: {"));
    assert!(markdown.contains("is_subagent: false\n"));
    assert!(markdown.contains("gitBranch: "));
}
