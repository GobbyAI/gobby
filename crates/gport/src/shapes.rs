//! `gport shapes`: per-tool call counts and argument-key sets, with no values.
//!
//! Transcript records are scanned for tool calls. A proxy call is counted under its target
//! `<server>:<tool>`, and each tool lists the sorted union of its argument keys. Argument values
//! are decoded only to find keys and are never printed. The daemon's per-tool metrics add a second
//! count per tool. A source that is missing, unreadable or unreachable is listed under
//! `Skipped sources`; it never fails the run. The one exception is a redirect from the daemon: it
//! is not followed, so the run fails with exit 2.

use std::collections::{BTreeMap, BTreeSet};
use std::fs::File;
use std::io::{BufRead, BufReader, ErrorKind, Write};
use std::path::{Path, PathBuf};
use std::time::Duration;

use gobby_core::grant::reject_remote_endpoint;
use serde_json::Value;
use walkdir::WalkDir;

/// The MCP proxy tool. Its calls are counted under their target, never under its own name.
const PROXY_TOOL: &str = "mcp__gobby__call_tool";
/// The daemon route that lists tools with their metrics.
const METRICS_ROUTE: &str = "/api/mcp/tools";
/// How long the daemon may take before its metrics route is listed as skipped.
const DAEMON_TIMEOUT: Duration = Duration::from_secs(5);
/// Column headings of the table.
const HEADINGS: [&str; 4] = ["Tool", "Transcript", "Daemon", "Argument keys"];
/// The label that every tool name or daemon label not in name shape counts under.
const UNNAMED: &str = "(unnamed)";

#[derive(Debug, Clone, Default)]
pub struct Options {
    pub transcripts: Vec<PathBuf>,
    pub no_daemon: bool,
}

/// One tool's counts: its transcript calls and the argument keys they used, and the daemon's
/// count. `None` means that source did not report the tool. Keys that are not key-shaped go to
/// `other_keys` only so they can be counted; their text is never printed.
#[derive(Debug, Default)]
struct Tally {
    transcript_calls: Option<u64>,
    keys: BTreeSet<String>,
    other_keys: BTreeSet<String>,
    daemon_calls: Option<u64>,
}

impl Tally {
    /// Counts one transcript call and keeps the names of its argument keys, never their values.
    fn record_call(&mut self, arguments: Option<&Value>) {
        let calls = self.transcript_calls.get_or_insert(0);
        *calls = calls.saturating_add(1);
        for key in argument_keys(arguments) {
            if is_key(&key) {
                self.keys.insert(key);
            } else {
                self.other_keys.insert(key);
            }
        }
    }
}

type Tallies = BTreeMap<String, Tally>;

pub fn run(options: Options, out: &mut dyn Write) -> anyhow::Result<bool> {
    let mut skipped = Vec::new();
    let roots = if options.transcripts.is_empty() {
        default_roots(&mut skipped)
    } else {
        options.transcripts
    };
    let mut tallies = Tallies::new();
    for root in &roots {
        scan_root(root, &mut tallies, &mut skipped);
    }
    let metrics = format!("daemon tool metrics (GET {METRICS_ROUTE})");
    if options.no_daemon {
        skipped.push(format!("{metrics}: skipped by --no-daemon"));
    } else {
        match daemon_metrics()? {
            Ok(fetched) => {
                for (label, count) in fetched.calls {
                    tallies.entry(label).or_default().daemon_calls = Some(count);
                }
                if fetched.malformed > 0 {
                    let noun = if fetched.malformed == 1 {
                        "entry"
                    } else {
                        "entries"
                    };
                    skipped.push(format!(
                        "{metrics}: {} malformed {noun} not counted; each needs a string name \
                         and a numeric call_count",
                        fetched.malformed
                    ));
                }
            }
            Err(reason) => skipped.push(format!("{metrics}: {reason}")),
        }
    }
    write_table(out, &tallies)?;
    write_skipped(out, &skipped)?;
    Ok(true)
}

/// The transcript roots that `--transcripts` does not name: the Claude Code and Codex stores.
fn default_roots(skipped: &mut Vec<String>) -> Vec<PathBuf> {
    match std::env::var_os("HOME") {
        Some(home) => {
            let home = PathBuf::from(home);
            vec![
                home.join(".claude").join("projects"),
                home.join(".codex").join("sessions"),
            ]
        }
        None => {
            skipped.push("default transcript roots: HOME is not set".to_owned());
            Vec::new()
        }
    }
}

/// Scans one transcript root: a directory (every `.jsonl` file under it) or a single file.
fn scan_root(root: &Path, tallies: &mut Tallies, skipped: &mut Vec<String>) {
    let shown = show(&root.display().to_string());
    match std::fs::metadata(root) {
        Ok(meta) if meta.is_dir() => {
            // Links below the root are not followed; the root itself always is. Each link below is
            // counted, and the count is listed once for this root.
            let mut links = 0usize;
            for entry in WalkDir::new(root).follow_links(false).sort_by_file_name() {
                match entry {
                    Ok(entry) if entry.depth() > 0 && entry.file_type().is_symlink() => {
                        links = links.saturating_add(1);
                    }
                    Ok(entry)
                        if entry.file_type().is_file()
                            && entry.path().extension().is_some_and(|ext| ext == "jsonl") =>
                    {
                        scan_or_skip(entry.path(), tallies, skipped);
                    }
                    Ok(_) => {}
                    Err(error) => {
                        let path = error.path().map_or_else(
                            || shown.clone(),
                            |path| show(&path.display().to_string()),
                        );
                        skipped.push(format!("{path}: unreadable"));
                    }
                }
            }
            if links > 0 {
                let noun = if links == 1 { "symlink" } else { "symlinks" };
                skipped.push(format!("{shown}: {links} {noun} not followed"));
            }
        }
        Ok(meta) if meta.is_file() => scan_or_skip(root, tallies, skipped),
        Ok(_) => skipped.push(format!("{shown}: not a file or directory")),
        Err(error) if error.kind() == ErrorKind::NotFound => {
            skipped.push(format!("{shown}: missing"));
        }
        Err(_) => skipped.push(format!("{shown}: unreadable")),
    }
}

fn scan_or_skip(path: &Path, tallies: &mut Tallies, skipped: &mut Vec<String>) {
    // Calls read before a failure stay counted; the file is listed as skipped all the same.
    if scan_file(path, tallies).is_err() {
        skipped.push(format!("{}: unreadable", show(&path.display().to_string())));
    }
}

/// Reads one JSON-lines file. A line that is not JSON, or holds no tool call, is skipped.
fn scan_file(path: &Path, tallies: &mut Tallies) -> std::io::Result<()> {
    let mut reader = BufReader::new(File::open(path)?);
    let mut line = Vec::new();
    loop {
        line.clear();
        if reader.read_until(b'\n', &mut line)? == 0 {
            return Ok(());
        }
        if let Ok(record) = serde_json::from_slice::<Value>(line.trim_ascii()) {
            observe_record(&record, tallies);
        }
    }
}

/// Counts the tool calls in one record. Claude Code keeps them as `tool_use` blocks in a message;
/// Codex keeps them as `function_call` payloads whose arguments are a JSON string.
fn observe_record(record: &Value, tallies: &mut Tallies) {
    if let Some(payload) = record.get("payload")
        && payload.get("type").and_then(Value::as_str) == Some("function_call")
        && let Some(name) = payload.get("name").and_then(Value::as_str)
    {
        let decoded = match payload.get("arguments") {
            Some(Value::String(text)) => serde_json::from_str::<Value>(text).ok(),
            _ => None,
        };
        let input = decoded.as_ref().or_else(|| payload.get("arguments"));
        observe_call(tallies, name, input);
    }
    let Some(blocks) = record.pointer("/message/content").and_then(Value::as_array) else {
        return;
    };
    for block in blocks {
        if block.get("type").and_then(Value::as_str) != Some("tool_use") {
            continue;
        }
        if let Some(name) = block.get("name").and_then(Value::as_str) {
            observe_call(tallies, name, block.get("input"));
        }
    }
}

/// Counts one call under its label. A proxy call takes its target as the label and its own
/// `arguments` (or the `args` alias) as the keys. A proxy call with no target is not counted.
fn observe_call(tallies: &mut Tallies, name: &str, input: Option<&Value>) {
    let (label, arguments) = if name == PROXY_TOOL {
        let Some(label) = input.and_then(proxy_target) else {
            return;
        };
        let arguments = input.and_then(|input| {
            input
                .get("arguments")
                .filter(|value| !value.is_null())
                .or_else(|| input.get("args"))
        });
        (label, arguments)
    } else {
        (name.to_owned(), input)
    };
    tallies
        .entry(tool_label(label))
        .or_default()
        .record_call(arguments);
}

/// The `<server>:<tool>` label a proxy call names, when both names are present.
fn proxy_target(input: &Value) -> Option<String> {
    let server = input.get("server_name")?.as_str()?;
    let tool = input.get("tool_name")?.as_str()?;
    (!server.is_empty() && !tool.is_empty()).then(|| format!("{server}:{tool}"))
}

/// The sorted key names of an arguments object. A JSON object encoded in a string is decoded
/// first. Anything else has no keys.
fn argument_keys(arguments: Option<&Value>) -> Vec<String> {
    let decoded = match arguments {
        Some(Value::String(text)) => serde_json::from_str::<Value>(text).ok(),
        _ => None,
    };
    match decoded.as_ref().or(arguments) {
        Some(Value::Object(map)) => map.keys().cloned().collect(),
        _ => Vec::new(),
    }
}

/// The metrics request for a daemon base. A remote endpoint is refused before the bearer header
/// is attached, so no request that carries it is built for one. Redirects are not followed, so the
/// bearer never leaves the endpoint it was sent to.
fn metrics_request(base: &str) -> Result<ureq::Request, String> {
    reject_remote_endpoint(base).map_err(|error| error.to_string())?;
    let url = format!("{base}{METRICS_ROUTE}?include_metrics=true");
    let agent = ureq::AgentBuilder::new().redirects(0).build();
    Ok(gobby_core::local_token::apply_bearer_header(
        agent.get(&url).timeout(DAEMON_TIMEOUT),
    ))
}

/// Fetches the daemon's per-tool call counts. The outer `Err` fails the run: a redirect is not
/// followed, so it is refused. The inner `Err` names why the route is skipped.
fn daemon_metrics() -> anyhow::Result<Result<DaemonMetrics, String>> {
    let base = match gobby_core::daemon_url::daemon_url() {
        Ok(base) => base,
        Err(_) => return Ok(Err("daemon URL unavailable".to_owned())),
    };
    let request = match metrics_request(&base) {
        Ok(request) => request,
        Err(reason) => return Ok(Err(reason)),
    };
    let response = match request.call() {
        Ok(response) => response,
        Err(ureq::Error::Status(code, _)) => return Ok(Err(format!("HTTP status {code}"))),
        Err(_) => return Ok(Err("unreachable".to_owned())),
    };
    let status = response.status();
    if (300..400).contains(&status) {
        anyhow::bail!(
            "the daemon answered GET {METRICS_ROUTE} with status {status}, a redirect that gport \
             does not follow"
        );
    }
    let Ok(body) = response.into_json::<Value>() else {
        return Ok(Err("unreadable response".to_owned()));
    };
    Ok(parse_metrics(&body).ok_or_else(|| "unreadable response".to_owned()))
}

/// The daemon's per-tool call counts, and how many of its entries were dropped as malformed.
#[derive(Debug, Default)]
struct DaemonMetrics {
    calls: BTreeMap<String, u64>,
    malformed: usize,
}

/// Reads `{"tools": {<server>: [{"name": .., "call_count": ..}, ..]}}` into `<server>:<name>`
/// counts. A `success` of false, or no `tools` object, makes the whole answer unreadable. An entry
/// with no string name or no numeric call_count, and a server whose value is not a list, is counted
/// as malformed and left out.
fn parse_metrics(body: &Value) -> Option<DaemonMetrics> {
    if body.get("success").and_then(Value::as_bool) == Some(false) {
        return None;
    }
    let servers = body.get("tools")?.as_object()?;
    let mut metrics = DaemonMetrics::default();
    for (server, entries) in servers {
        let Some(entries) = entries.as_array() else {
            metrics.malformed = metrics.malformed.saturating_add(1);
            continue;
        };
        for entry in entries {
            let (Some(name), Some(count)) = (
                entry.get("name").and_then(Value::as_str),
                entry.get("call_count").and_then(Value::as_u64),
            ) else {
                metrics.malformed = metrics.malformed.saturating_add(1);
                continue;
            };
            let label = tool_label(format!("{server}:{name}"));
            let total: &mut u64 = metrics.calls.entry(label).or_default();
            *total = total.saturating_add(count);
        }
    }
    Some(metrics)
}

/// Shape of a printed word: a letter or `_`, then up to 63 more bytes, each a letter, a digit, `_`
/// or one of `extra`. Anything else is content, so it is counted, never printed.
fn has_shape(text: &str, extra: &[u8]) -> bool {
    let bytes = text.as_bytes();
    bytes.len() <= 64
        && bytes
            .first()
            .is_some_and(|&first| first.is_ascii_alphabetic() || first == b'_')
        && bytes
            .iter()
            .all(|&byte| byte.is_ascii_alphanumeric() || byte == b'_' || extra.contains(&byte))
}

/// Key shape for a printed argument key: letters, digits and `_` only, so a dot or a hyphen
/// means the key is content and is only counted.
fn is_key(text: &str) -> bool {
    has_shape(text, b"")
}

/// Shape of one part of a printed tool label: a hyphen is allowed, as in the server name
/// `gobby-tasks`, and a dot is allowed after the first character, as in a dotted tool name.
fn is_name_part(text: &str) -> bool {
    has_shape(text, b"-.")
}

/// A label is one name part, or two joined by a single `:` (a proxy call's `<server>:<tool>`).
fn is_label(label: &str) -> bool {
    match label.split_once(':') {
        Some((server, tool)) => is_name_part(server) && is_name_part(tool),
        None => is_name_part(label),
    }
}

/// The label a call counts under: its own name when name-shaped, otherwise the shared unnamed line.
fn tool_label(label: String) -> String {
    if is_label(&label) {
        label
    } else {
        UNNAMED.to_owned()
    }
}

/// Text as printed. Control characters and quotes are escaped so a name cannot break the layout.
fn show(text: &str) -> String {
    text.escape_debug().to_string()
}

fn count_cell(count: Option<u64>) -> String {
    count.map_or_else(|| "-".to_owned(), |count| count.to_string())
}

/// `-` when no transcript call named the tool; `(none)` when its calls carried no arguments.
/// Key-shaped keys print by name; the other distinct keys print only as a count.
fn keys_cell(tally: &Tally) -> String {
    if tally.transcript_calls.is_none() {
        return "-".to_owned();
    }
    let mut cells: Vec<String> = tally.keys.iter().cloned().collect();
    if !tally.other_keys.is_empty() {
        cells.push(format!("+{} other keys", tally.other_keys.len()));
    }
    if cells.is_empty() {
        "(none)".to_owned()
    } else {
        cells.join(", ")
    }
}

fn write_table(out: &mut dyn Write, tallies: &Tallies) -> anyhow::Result<()> {
    if tallies.is_empty() {
        writeln!(out, "No tool calls found.")?;
        return Ok(());
    }
    let rows: Vec<[String; 4]> = tallies
        .iter()
        .map(|(label, tally)| {
            [
                label.clone(),
                count_cell(tally.transcript_calls),
                count_cell(tally.daemon_calls),
                keys_cell(tally),
            ]
        })
        .collect();
    let mut widths = [0usize; 3];
    for (column, width) in widths.iter_mut().enumerate() {
        *width = rows
            .iter()
            .map(|row| row[column].as_str())
            .chain(std::iter::once(HEADINGS[column]))
            .map(|cell| cell.chars().count())
            .max()
            .unwrap_or(0);
    }
    let line = |cells: [&str; 4]| {
        format!(
            "{:<w0$}  {:>w1$}  {:>w2$}  {}",
            cells[0],
            cells[1],
            cells[2],
            cells[3],
            w0 = widths[0],
            w1 = widths[1],
            w2 = widths[2],
        )
    };
    writeln!(out, "{}", line(HEADINGS))?;
    for row in &rows {
        writeln!(out, "{}", line(row.each_ref().map(String::as_str)))?;
    }
    Ok(())
}

fn write_skipped(out: &mut dyn Write, skipped: &[String]) -> anyhow::Result<()> {
    if skipped.is_empty() {
        return Ok(());
    }
    writeln!(out)?;
    writeln!(out, "Skipped sources")?;
    for entry in skipped {
        writeln!(out, "  {entry}")?;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::{metrics_request, parse_metrics};

    /// The reason a skipped metrics route carries for a remote endpoint.
    const REFUSAL: &str = "remote daemon endpoint refused";

    #[test]
    fn non_loopback_endpoint_builds_no_metrics_request() {
        let Err(reason) = metrics_request("http://192.0.2.1:9") else {
            panic!("a non-loopback endpoint must not get a request that carries the bearer");
        };
        assert_eq!(reason, REFUSAL);
    }

    #[test]
    fn malformed_endpoint_builds_no_metrics_request() {
        assert!(metrics_request("not a url").is_err());
    }

    #[test]
    fn parse_metrics_counts_malformed_entries_and_keeps_the_rest() {
        let body = json!({"success": true, "tools": {
            "srv": [
                {"name": "good", "call_count": 2},
                {"name": "no_count"},
                {"name": 7, "call_count": 1},
                "not an object"
            ],
            "bad": "not a list"
        }});
        let metrics = parse_metrics(&body).expect("a readable answer");
        assert_eq!(metrics.calls.get("srv:good"), Some(&2));
        assert_eq!(metrics.calls.len(), 1);
        assert_eq!(metrics.malformed, 4);
    }
}
