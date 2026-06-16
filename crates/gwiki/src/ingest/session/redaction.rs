use std::sync::OnceLock;

use regex::Regex;

pub(crate) fn redact_session_markdown(markdown: &str) -> String {
    redact_session_text(markdown)
}

pub(crate) fn redact_session_text(text: &str) -> String {
    let markdown = home_dir_regex().replace_all(text, "[REDACTED_HOME]");
    let markdown = api_key_regex().replace_all(&markdown, "[REDACTED_API_KEY]");
    email_regex()
        .replace_all(&markdown, "[REDACTED_EMAIL]")
        .into_owned()
}

fn home_dir_regex() -> &'static Regex {
    static REGEX: OnceLock<Regex> = OnceLock::new();
    REGEX.get_or_init(|| {
        Regex::new(r#"/Users/[^/\s`'"<>{}\[\](),:;]+"#)
            .expect("session home-dir redaction regex must compile")
    })
}

fn api_key_regex() -> &'static Regex {
    static REGEX: OnceLock<Regex> = OnceLock::new();
    REGEX.get_or_init(|| {
        Regex::new(
            r"\b(?:(?:sk|pk|rk)-[A-Za-z0-9_-]{16,}|xox[abprs]-[A-Za-z0-9-]{16,}|gh[opsu]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,}|AIza[A-Za-z0-9_-]{20,}|ya29\.[A-Za-z0-9_-]{20,})\b",
        )
        .expect("session API-key redaction regex must compile")
    })
}

fn email_regex() -> &'static Regex {
    static REGEX: OnceLock<Regex> = OnceLock::new();
    REGEX.get_or_init(|| {
        Regex::new(r"(?i)\b[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}\b")
            .expect("session email redaction regex must compile")
    })
}

#[cfg(test)]
mod tests {
    use std::fmt::Write as _;
    use std::path::PathBuf;

    use super::super::{SessionFileSnapshot, ingest_session_file_without_index};
    use super::redact_session_markdown;
    use crate::ingest::index_after_ingest;
    use crate::store::MemoryWikiStore;

    #[test]
    fn redacts_session_secret_patterns() {
        let openai_key = format!("{}{}", "sk-proj-", "abcdefghijklmnopqrstuvwxyz123456");
        let slack_token = format!("{}{}", "xoxb-", "123456789012-abcdefghijklmnop");
        let raw = format!(
            "email: Casey.User+demo@example.com\npath: /Users/casey/project/config.toml\nopenai: {openai_key}\nslack: {slack_token}\n"
        );

        let redacted = redact_session_markdown(&raw);

        assert!(redacted.contains("[REDACTED_EMAIL]"));
        assert!(redacted.contains("[REDACTED_HOME]/project/config.toml"));
        assert!(redacted.contains("[REDACTED_API_KEY]"));
        assert!(!redacted.contains("Casey.User+demo@example.com"));
        assert!(!redacted.contains("/Users/casey"));
        assert!(!redacted.contains(&openai_key));
        assert!(!redacted.contains(&slack_token));
    }

    #[test]
    fn session_ingest_writes_redacted_markdown() {
        let temp = tempfile::tempdir().expect("tempdir");
        let archive_path = PathBuf::from("/Users/casey/session.jsonl");
        let openai_key = format!("{}{}", "sk-proj-", "abcdefghijklmnopqrstuvwxyz123456");
        let github_token = format!("{}{}", "ghp_", "abcdefghijklmnopqrstuvwxyz1234567890");
        let bytes = format!(
            r#"{{"type":"session","timestamp":"2026-06-16T20:00:00Z","payload":{{"title":"Import for casey@example.com","messages":[{{"role":"user","timestamp":"2026-06-16T20:00:01Z","content":"Read /Users/casey/work/app.rs with {openai_key} and email ops@example.com."}},{{"role":"assistant","timestamp":"2026-06-16T20:00:02Z","content":"GitHub token {github_token} was present."}}]}}}}"#
        )
        .into_bytes();

        let result = ingest_session_file_without_index(
            temp.path(),
            SessionFileSnapshot {
                location: "/Users/casey/session.jsonl".to_string(),
                file_name: "session.jsonl".to_string(),
                fetched_at: "2026-06-16T20:05:00Z".to_string(),
                path: archive_path,
                bytes,
            },
        )
        .expect("ingest session");
        let raw_markdown =
            std::fs::read_to_string(temp.path().join(result.raw_path)).expect("raw markdown");

        assert!(raw_markdown.contains("[REDACTED_HOME]/session.jsonl"));
        assert!(raw_markdown.contains("[REDACTED_HOME]/work/app.rs"));
        assert!(raw_markdown.contains("[REDACTED_EMAIL]"));
        assert!(raw_markdown.contains("[REDACTED_API_KEY]"));
        assert!(!raw_markdown.contains("/Users/casey"));
        assert!(!raw_markdown.contains("casey@example.com"));
        assert!(!raw_markdown.contains("ops@example.com"));
        assert!(!raw_markdown.contains(&openai_key));
        assert!(!raw_markdown.contains(&github_token));

        let mut store = MemoryWikiStore::default();
        index_after_ingest(temp.path(), &mut store).expect("index redacted session markdown");
        let indexed_text = indexed_store_text(&store);
        assert!(!indexed_text.contains("/Users/casey"));
        assert!(!indexed_text.contains("casey@example.com"));
        assert!(!indexed_text.contains("ops@example.com"));
        assert!(!indexed_text.contains(&openai_key));
        assert!(!indexed_text.contains(&github_token));
    }

    fn indexed_store_text(store: &MemoryWikiStore) -> String {
        let mut text = String::new();
        for document in store.documents.values() {
            if let Some(title) = &document.title {
                let _ = writeln!(text, "{title}");
            }
            let _ = writeln!(text, "{}", document.body);
        }
        for chunks in store.chunks.values() {
            for chunk in chunks {
                if let Some(heading) = &chunk.heading {
                    let _ = writeln!(text, "{heading}");
                }
                let _ = writeln!(text, "{}", chunk.content);
            }
        }
        text
    }
}
