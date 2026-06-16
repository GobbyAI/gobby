use std::collections::HashSet;
use std::io::{BufRead, BufReader, Write};
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::mpsc::{self, Receiver, RecvTimeoutError, Sender};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

use anyhow::{Context, anyhow, bail};
use serde_json::{Value, json};

const CLANGD_RESPONSE_TIMEOUT: Duration = Duration::from_secs(30);

#[derive(Debug, Clone)]
pub(crate) struct SemanticCallRequest<'a> {
    pub(crate) language: &'a str,
    pub(crate) file_path: &'a Path,
    pub(crate) root_path: &'a Path,
    pub(crate) source: &'a [u8],
    pub(crate) callee_name: &'a str,
    pub(crate) line: usize,
    pub(crate) column: usize,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct SemanticCallTarget {
    pub(crate) callee_name: String,
    pub(crate) kind: SemanticTargetKind,
}

/// Where clangd's `textDocument/definition` resolved a C/C++ call's definition.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) enum SemanticTargetKind {
    /// Definition lives OUTSIDE the project root — a dependency. The string is
    /// the absolute declaration path, recorded as the call's external module.
    External(String),
    /// Definition lives in a project-relative file INSIDE the root — a
    /// cross-file local call. The string is that project-relative file; the
    /// post-write DB pass (`index::indexer::local_imports`) narrows it to a real
    /// `code_symbols` id (or degrades to unresolved), exactly like the
    /// import-binding local-call path.
    LocalCandidate(String),
}

pub(crate) trait SemanticCallResolver {
    fn resolve(
        &mut self,
        request: &SemanticCallRequest<'_>,
    ) -> anyhow::Result<Option<SemanticCallTarget>>;
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct DefinitionLocation {
    pub(crate) path: PathBuf,
}

pub(crate) fn create_cpp_semantic_resolver(
    root_path: &Path,
    require_cpp_semantics: bool,
) -> anyhow::Result<Option<Box<dyn SemanticCallResolver>>> {
    let strict = require_cpp_semantics || env_flag("GCODE_REQUIRE_CPP_SEMANTICS");
    let compile_commands_dir = discover_compile_commands_dir(root_path);
    let Some(compile_commands_dir) = compile_commands_dir else {
        if strict {
            bail!(
                "C/C++ semantic indexing requires compile_commands.json; set GCODE_COMPILE_COMMANDS_DIR or generate one"
            );
        }
        return Ok(None);
    };

    let clangd = resolve_clangd_command();
    let Some(clangd) = clangd else {
        if strict {
            bail!("C/C++ semantic indexing requires clangd; set GCODE_CLANGD or install clangd");
        }
        return Ok(None);
    };

    match ClangdResolver::start(root_path, &compile_commands_dir, &clangd) {
        Ok(resolver) => Ok(Some(Box::new(resolver))),
        Err(err) if strict => Err(err),
        Err(_) => Ok(None),
    }
}

pub(crate) fn discover_compile_commands_dir(root_path: &Path) -> Option<PathBuf> {
    if let Ok(override_dir) = std::env::var("GCODE_COMPILE_COMMANDS_DIR") {
        let dir = PathBuf::from(override_dir);
        if dir.join("compile_commands.json").is_file() {
            return Some(dir);
        }
        return None;
    }

    [
        root_path.to_path_buf(),
        root_path.join("build"),
        root_path.join("cmake-build-debug"),
        root_path.join("cmake-build-release"),
        root_path.join("out").join("build"),
    ]
    .into_iter()
    .find(|dir| dir.join("compile_commands.json").is_file())
}

pub(crate) fn classify_definition(
    root_path: &Path,
    source: &[u8],
    callee_name: &str,
    locations: &[DefinitionLocation],
) -> Option<SemanticCallTarget> {
    if locations.len() != 1 || source_defines_macro(source, callee_name) {
        return None;
    }
    let declaration_path = &locations[0].path;
    let kind = match local_candidate_file(declaration_path, root_path) {
        // Definition inside the project root: a cross-file local call. Carry the
        // project-relative file so the post-write DB pass can resolve it to a
        // canonical symbol id.
        Some(candidate_file) => SemanticTargetKind::LocalCandidate(candidate_file),
        // Definition outside the root: an external dependency module.
        None => SemanticTargetKind::External(declaration_path.to_string_lossy().to_string()),
    };
    Some(SemanticCallTarget {
        callee_name: callee_name.to_string(),
        kind,
    })
}

pub(crate) fn locations_from_lsp_response(response: &Value) -> Vec<DefinitionLocation> {
    let Some(result) = response.get("result") else {
        return Vec::new();
    };
    if result.is_null() {
        return Vec::new();
    }
    if let Some(items) = result.as_array() {
        return items.iter().filter_map(location_from_lsp_value).collect();
    }
    location_from_lsp_value(result).into_iter().collect()
}

fn location_from_lsp_value(value: &Value) -> Option<DefinitionLocation> {
    let uri = value
        .get("uri")
        .or_else(|| value.get("targetUri"))
        .and_then(|value| value.as_str())?;
    Some(DefinitionLocation {
        path: file_uri_to_path(uri)?,
    })
}

fn source_defines_macro(source: &[u8], callee_name: &str) -> bool {
    let text = String::from_utf8_lossy(source);
    logical_source_lines(&text)
        .iter()
        .filter_map(|line| macro_definition_name(line))
        .any(|macro_name| macro_name == callee_name)
}

fn logical_source_lines(text: &str) -> Vec<String> {
    let mut logical_lines = Vec::new();
    let mut current = String::new();

    for line in text.lines() {
        let trimmed = line.trim_end();
        if let Some(continued) = trimmed.strip_suffix('\\') {
            current.push_str(continued);
            continue;
        }

        current.push_str(line);
        logical_lines.push(std::mem::take(&mut current));
    }

    if !current.is_empty() {
        logical_lines.push(current);
    }

    logical_lines
}

fn macro_definition_name(line: &str) -> Option<&str> {
    let rest = line.trim_start().strip_prefix('#')?.trim_start();
    let rest = rest.strip_prefix("define")?;
    if !rest.chars().next().is_some_and(char::is_whitespace) {
        return None;
    }

    let rest = rest.trim_start();
    let mut chars = rest.char_indices();
    let (_, first) = chars.next()?;
    if !(first == '_' || first.is_ascii_alphabetic()) {
        return None;
    }

    let mut end = first.len_utf8();
    for (idx, ch) in chars {
        if ch == '_' || ch.is_ascii_alphanumeric() {
            end = idx + ch.len_utf8();
        } else {
            break;
        }
    }

    let after_name = &rest[end..];
    if after_name
        .chars()
        .next()
        .is_none_or(|ch| ch == '(' || ch.is_whitespace())
    {
        Some(&rest[..end])
    } else {
        None
    }
}

/// Computes the project-relative path for a definition that lives inside
/// `root_path`, matching exactly how the indexer derives `code_symbols.file_path`
/// (canonicalize file + root, strip prefix, lossy string — see
/// `parser::parse_file`) so the post-write DB pass can match the candidate
/// against the stored symbol row. Returns `None` when the definition is outside
/// the root, cannot be canonicalized, or resolves to the root itself — all of
/// which the caller treats as "not a local definition".
fn local_candidate_file(path: &Path, root_path: &Path) -> Option<String> {
    let canonical_path = path.canonicalize().ok()?;
    let canonical_root = root_path.canonicalize().ok()?;
    let relative = canonical_path.strip_prefix(&canonical_root).ok()?;
    let candidate = relative.to_string_lossy().to_string();
    if candidate.is_empty() {
        None
    } else {
        Some(candidate)
    }
}

fn resolve_clangd_command() -> Option<String> {
    if let Ok(command) = std::env::var("GCODE_CLANGD")
        && !command.trim().is_empty()
    {
        return Some(command);
    }
    find_executable_in_path("clangd").map(|path| path.to_string_lossy().to_string())
}

fn parse_clangd_command(command: &str) -> anyhow::Result<Vec<String>> {
    let parts = shlex::split(command).ok_or_else(|| anyhow!("empty clangd command"))?;
    if parts.is_empty() {
        bail!("empty clangd command");
    }
    Ok(parts)
}

#[cfg(not(windows))]
fn find_executable_in_path(name: &str) -> Option<PathBuf> {
    let path = std::env::var_os("PATH")?;
    std::env::split_paths(&path)
        .map(|dir| dir.join(name))
        .find(|path| path.is_file())
}

#[cfg(windows)]
fn find_executable_in_path(name: &str) -> Option<PathBuf> {
    let path = std::env::var_os("PATH")?;
    let candidates = executable_name_candidates(name);
    for dir in std::env::split_paths(&path) {
        for candidate in &candidates {
            let path = dir.join(candidate);
            if path.is_file() {
                return Some(path);
            }
        }
    }
    None
}

#[cfg(windows)]
fn executable_name_candidates(name: &str) -> Vec<PathBuf> {
    if Path::new(name).extension().is_some() {
        return vec![PathBuf::from(name)];
    }

    let mut candidates = vec![PathBuf::from(name)];
    if let Some(pathext) = std::env::var_os("PATHEXT") {
        for ext in pathext.to_string_lossy().split(';') {
            let ext = ext.trim();
            if ext.is_empty() {
                continue;
            }
            let ext = if ext.starts_with('.') {
                ext.to_string()
            } else {
                format!(".{ext}")
            };
            candidates.push(PathBuf::from(format!("{name}{ext}")));
        }
    }
    candidates
}

fn env_flag(name: &str) -> bool {
    std::env::var(name)
        .ok()
        .map(|value| matches!(value.as_str(), "1" | "true" | "TRUE" | "yes" | "on"))
        .unwrap_or(false)
}

fn path_to_uri(path: &Path) -> String {
    let path = path.to_string_lossy();
    #[cfg(windows)]
    let path = path.replace('\\', "/");
    #[cfg(not(windows))]
    let path = path.into_owned();

    let encoded = path
        .split('/')
        .enumerate()
        .map(|(idx, part)| {
            if idx == 0 && is_windows_drive_prefix(part) {
                part.to_string()
            } else {
                urlencoding::encode(part).into_owned()
            }
        })
        .collect::<Vec<_>>()
        .join("/");
    if encoded.starts_with("//") {
        format!("file:{encoded}")
    } else if is_windows_drive_path(&encoded) {
        format!("file:///{encoded}")
    } else {
        format!("file://{encoded}")
    }
}

fn is_windows_drive_prefix(part: &str) -> bool {
    let bytes = part.as_bytes();
    bytes.len() == 2 && bytes[0].is_ascii_alphabetic() && bytes[1] == b':'
}

fn is_windows_drive_path(path: &str) -> bool {
    path.get(..2).is_some_and(is_windows_drive_prefix)
}

fn file_uri_to_path(uri: &str) -> Option<PathBuf> {
    let rest = uri.strip_prefix("file://")?;
    let decoded = urlencoding::decode(rest).ok()?;
    let mut path = decoded.into_owned();
    if cfg!(windows) {
        let bytes = path.as_bytes();
        if bytes.len() >= 3
            && bytes[0] == b'/'
            && bytes[1].is_ascii_alphabetic()
            && bytes[2] == b':'
        {
            path.remove(0);
        }
    }
    Some(PathBuf::from(path))
}

struct ClangdResolver {
    child: Child,
    stdin: ChildStdin,
    response_rx: Receiver<anyhow::Result<Value>>,
    reader_handle: Option<JoinHandle<()>>,
    next_id: u64,
    root_path: PathBuf,
    open_files: HashSet<PathBuf>,
}

impl ClangdResolver {
    fn start(root_path: &Path, compile_commands_dir: &Path, clangd: &str) -> anyhow::Result<Self> {
        let parts = parse_clangd_command(clangd)?;
        let (program, args) = parts
            .split_first()
            .ok_or_else(|| anyhow!("empty clangd command"))?;
        let mut command = Command::new(program);
        command.args(args);
        command.arg(format!(
            "--compile-commands-dir={}",
            compile_commands_dir.display()
        ));
        command.arg("--background-index=false");
        command.stdin(Stdio::piped());
        command.stdout(Stdio::piped());
        command.stderr(Stdio::null());
        let mut child = command.spawn().context("start clangd")?;
        let stdin = child.stdin.take().context("open clangd stdin")?;
        let stdout = child.stdout.take().context("open clangd stdout")?;
        let (response_rx, reader_handle) = spawn_clangd_stdout_reader(stdout);
        let mut resolver = Self {
            child,
            stdin,
            response_rx,
            reader_handle: Some(reader_handle),
            next_id: 1,
            root_path: root_path.to_path_buf(),
            open_files: HashSet::new(),
        };
        resolver.initialize()?;
        Ok(resolver)
    }

    fn initialize(&mut self) -> anyhow::Result<()> {
        let id = self.send_request(
            "initialize",
            json!({
                "processId": Value::Null,
                "rootUri": path_to_uri(&self.root_path),
                "capabilities": {}
            }),
        )?;
        self.read_response(id)?;
        self.send_notification("initialized", json!({}))?;
        Ok(())
    }

    fn ensure_open(&mut self, request: &SemanticCallRequest<'_>) -> anyhow::Result<()> {
        if self.open_files.contains(request.file_path) {
            return Ok(());
        }
        let text = String::from_utf8_lossy(request.source).to_string();
        self.send_notification(
            "textDocument/didOpen",
            json!({
                "textDocument": {
                    "uri": path_to_uri(request.file_path),
                    "languageId": request.language,
                    "version": 1,
                    "text": text
                }
            }),
        )?;
        self.open_files.insert(request.file_path.to_path_buf());
        Ok(())
    }

    fn close_open_files(&mut self) -> anyhow::Result<()> {
        let paths: Vec<PathBuf> = self.open_files.iter().cloned().collect();
        let mut first_error = None;

        for path in paths {
            let result = self.send_notification(
                "textDocument/didClose",
                json!({
                    "textDocument": {
                        "uri": path_to_uri(&path)
                    }
                }),
            );
            match result {
                Ok(()) => {
                    self.open_files.remove(&path);
                }
                Err(err) if first_error.is_none() => {
                    first_error = Some(err);
                }
                Err(_) => {}
            }
        }

        match first_error {
            Some(err) => Err(err),
            None => Ok(()),
        }
    }

    fn send_request(&mut self, method: &str, params: Value) -> anyhow::Result<u64> {
        let id = self.next_id;
        self.next_id += 1;
        self.write_message(json!({
            "jsonrpc": "2.0",
            "id": id,
            "method": method,
            "params": params
        }))?;
        Ok(id)
    }

    fn send_notification(&mut self, method: &str, params: Value) -> anyhow::Result<()> {
        self.write_message(json!({
            "jsonrpc": "2.0",
            "method": method,
            "params": params
        }))
    }

    fn write_message(&mut self, value: Value) -> anyhow::Result<()> {
        let body = value.to_string();
        write!(self.stdin, "Content-Length: {}\r\n\r\n{}", body.len(), body)?;
        self.stdin.flush()?;
        Ok(())
    }

    fn read_response(&mut self, id: u64) -> anyhow::Result<Value> {
        read_response_from_channel(&self.response_rx, id, CLANGD_RESPONSE_TIMEOUT)
    }
}

impl Drop for ClangdResolver {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
        if let Some(handle) = self.reader_handle.take() {
            let _ = handle.join();
        }
    }
}

impl SemanticCallResolver for ClangdResolver {
    fn resolve(
        &mut self,
        request: &SemanticCallRequest<'_>,
    ) -> anyhow::Result<Option<SemanticCallTarget>> {
        if !matches!(request.language, "c" | "cpp") {
            return Ok(None);
        }
        let result = (|| -> anyhow::Result<Option<SemanticCallTarget>> {
            self.ensure_open(request).context("open clangd document")?;
            let id = self
                .send_request(
                    "textDocument/definition",
                    json!({
                        "textDocument": { "uri": path_to_uri(request.file_path) },
                        "position": {
                            "line": request.line.saturating_sub(1),
                            "character": request.column,
                        }
                    }),
                )
                .context("send clangd definition request")?;
            let response = self
                .read_response(id)
                .context("read clangd definition response")?;
            let locations = locations_from_lsp_response(&response);
            Ok(classify_definition(
                request.root_path,
                request.source,
                request.callee_name,
                &locations,
            ))
        })();
        let resolved = result?;
        self.close_open_files().context("close clangd open files")?;
        Ok(resolved)
    }
}

fn spawn_clangd_stdout_reader(
    stdout: std::process::ChildStdout,
) -> (Receiver<anyhow::Result<Value>>, JoinHandle<()>) {
    let (tx, rx) = mpsc::channel();
    let handle = thread::spawn(move || read_clangd_stdout(BufReader::new(stdout), tx));
    (rx, handle)
}

fn read_clangd_stdout(mut reader: impl BufRead, tx: Sender<anyhow::Result<Value>>) {
    loop {
        match read_json_rpc_message(&mut reader) {
            Ok(Some(response)) => {
                if tx.send(Ok(response)).is_err() {
                    break;
                }
            }
            Ok(None) => {
                let _ = tx.send(Err(anyhow!("clangd closed stdout")));
                break;
            }
            Err(err) => {
                let _ = tx.send(Err(err));
                break;
            }
        }
    }
}

fn read_json_rpc_message(reader: &mut impl BufRead) -> anyhow::Result<Option<Value>> {
    let mut content_length = None;
    loop {
        let mut header = String::new();
        let read = reader.read_line(&mut header)?;
        if read == 0 {
            return Ok(None);
        }
        let header = header.trim_end_matches(['\r', '\n']);
        if header.is_empty() {
            break;
        }
        if let Some(value) = header.strip_prefix("Content-Length:") {
            content_length = Some(value.trim().parse::<usize>()?);
        }
    }

    let len = content_length.context("missing clangd Content-Length header")?;
    let mut body = vec![0u8; len];
    reader.read_exact(&mut body)?;
    let response = serde_json::from_slice(&body)?;
    Ok(Some(response))
}

fn read_response_from_channel(
    rx: &Receiver<anyhow::Result<Value>>,
    id: u64,
    timeout: Duration,
) -> anyhow::Result<Value> {
    let started = Instant::now();
    let deadline = started + timeout;

    loop {
        let now = Instant::now();
        if now >= deadline {
            bail!(
                "clangd response timeout after {}",
                format_clangd_timeout(timeout)
            );
        }
        match rx.recv_timeout(deadline.saturating_duration_since(now)) {
            Ok(Ok(response)) => {
                if response.get("id").and_then(|value| value.as_u64()) == Some(id) {
                    return Ok(response);
                }
            }
            Ok(Err(err)) => return Err(err),
            Err(RecvTimeoutError::Timeout) => {
                bail!(
                    "clangd response timeout after {}",
                    format_clangd_timeout(timeout)
                );
            }
            Err(RecvTimeoutError::Disconnected) => bail!("clangd closed stdout"),
        }
    }
}

fn format_clangd_timeout(timeout: Duration) -> String {
    if timeout.as_nanos().is_multiple_of(1_000_000_000) {
        format!("{}s", timeout.as_secs())
    } else if timeout.as_nanos().is_multiple_of(1_000_000) {
        format!("{}ms", timeout.as_millis())
    } else {
        format!("{timeout:?}")
    }
}

#[cfg(test)]
mod tests {
    use std::fs;

    use tempfile::TempDir;

    use super::*;

    #[test]
    fn discovers_compile_commands_in_root_and_build_dirs() {
        let tempdir = TempDir::new().expect("tempdir");
        assert!(discover_compile_commands_dir(tempdir.path()).is_none());
        let build = tempdir.path().join("build");
        fs::create_dir_all(&build).expect("build dir");
        fs::write(build.join("compile_commands.json"), "[]").expect("compile db");
        assert_eq!(discover_compile_commands_dir(tempdir.path()), Some(build));
    }

    #[test]
    fn parses_lsp_location_and_location_link_results() {
        let response = json!({
            "id": 1,
            "result": [
                { "uri": "file:///usr/include/stdio.h", "range": {} },
                { "targetUri": "file:///opt/pkg/include/foo.hpp", "targetRange": {} }
            ]
        });
        let locations = locations_from_lsp_response(&response);
        assert_eq!(locations.len(), 2);
        assert_eq!(locations[0].path, PathBuf::from("/usr/include/stdio.h"));
        assert_eq!(locations[1].path, PathBuf::from("/opt/pkg/include/foo.hpp"));
    }

    #[test]
    fn parses_quoted_clangd_command_arguments() {
        let parts =
            parse_clangd_command(r#""/tmp/tool dir/clangd" --query-driver="/usr/bin/cc *""#)
                .expect("clangd argv");

        assert_eq!(
            parts,
            vec!["/tmp/tool dir/clangd", "--query-driver=/usr/bin/cc *"]
        );
    }

    #[test]
    fn rejects_empty_and_invalid_clangd_commands() {
        for command in ["", "   ", "clangd \"unterminated"] {
            let err = parse_clangd_command(command).expect_err("invalid clangd command");
            assert_eq!(err.to_string(), "empty clangd command");
        }
    }

    #[test]
    fn channel_response_wait_times_out() {
        let (_tx, rx) = std::sync::mpsc::channel();
        let err = read_response_from_channel(&rx, 42, Duration::from_millis(1))
            .expect_err("clangd response timeout");

        assert_eq!(err.to_string(), "clangd response timeout after 1ms");
    }

    #[test]
    fn classifies_single_definition_outside_project_as_external() {
        let tempdir = TempDir::new().expect("tempdir");
        let external = TempDir::new().expect("external tempdir");
        let header = external.path().join("vendor.h");
        fs::write(&header, "void vendor_call();").expect("header");
        let target = classify_definition(
            tempdir.path(),
            b"void run() { vendor_call(); }",
            "vendor_call",
            &[DefinitionLocation { path: header }],
        )
        .expect("external target");

        assert_eq!(target.callee_name, "vendor_call");
        let SemanticTargetKind::External(module) = target.kind else {
            panic!("expected external target");
        };
        assert!(module.ends_with("vendor.h"));
    }

    #[test]
    fn classifies_single_definition_inside_project_as_local_candidate() {
        let tempdir = TempDir::new().expect("tempdir");
        let local = tempdir.path().join("local.h");
        fs::write(&local, "void local_call();").expect("local header");

        let target = classify_definition(
            tempdir.path(),
            b"void run() { local_call(); }",
            "local_call",
            &[DefinitionLocation { path: local }],
        )
        .expect("local candidate target");

        // The project-relative path matches how the indexer stores
        // `code_symbols.file_path`, so the post-write DB pass can narrow it.
        assert_eq!(target.callee_name, "local_call");
        assert_eq!(
            target.kind,
            SemanticTargetKind::LocalCandidate("local.h".to_string())
        );
    }

    #[test]
    fn leaves_empty_multiple_and_macro_definitions_unresolved() {
        let tempdir = TempDir::new().expect("tempdir");
        let external = TempDir::new()
            .expect("external tempdir")
            .path()
            .join("vendor.h");

        assert!(classify_definition(tempdir.path(), b"", "missing", &[]).is_none());
        assert!(
            classify_definition(
                tempdir.path(),
                b"",
                "ambiguous",
                &[
                    DefinitionLocation {
                        path: PathBuf::from("/usr/include/a.h")
                    },
                    DefinitionLocation {
                        path: PathBuf::from("/usr/include/b.h")
                    }
                ]
            )
            .is_none()
        );
        assert!(
            classify_definition(
                tempdir.path(),
                b"#define printf my_printf\nvoid run() { printf(\"x\"); }",
                "printf",
                &[DefinitionLocation { path: external }]
            )
            .is_none()
        );
    }

    #[test]
    fn detects_function_like_and_backslash_continued_macros() {
        assert!(source_defines_macro(
            b"#define trace(value) log(value)\nvoid run() { trace(1); }",
            "trace"
        ));
        assert!(source_defines_macro(
            b"#define \\\ntrace(value) \\\nlog(value)\nvoid run() { trace(1); }",
            "trace"
        ));
        assert!(source_defines_macro(
            b"# define spaced(value) log(value)\nvoid run() { spaced(1); }",
            "spaced"
        ));
        assert!(!source_defines_macro(
            b"#define trace_wrapper(value) trace(value)",
            "trace"
        ));
        assert!(!source_defines_macro(b"# defined trace(value)", "trace"));
    }

    #[test]
    #[cfg(not(windows))]
    fn path_to_uri_encodes_absolute_path_components() {
        let uri = path_to_uri(Path::new("/tmp/gobby uri/a b/c#d.rs"));

        assert_eq!(uri, "file:///tmp/gobby%20uri/a%20b/c%23d.rs");
    }

    #[test]
    #[cfg(windows)]
    fn path_to_uri_preserves_windows_drive_prefix() {
        let uri = path_to_uri(Path::new(r"C:\Users\Josh\gobby uri\a#b.rs"));

        assert_eq!(uri, "file:///C:/Users/Josh/gobby%20uri/a%23b.rs");
    }

    #[test]
    #[cfg(windows)]
    fn file_uri_to_path_strips_windows_drive_leading_slash() {
        let path =
            file_uri_to_path("file:///C:/Users/Josh/gobby%20uri/a%23b.rs").expect("file uri path");

        assert_eq!(path, PathBuf::from(r"C:/Users/Josh/gobby uri/a#b.rs"));
    }

    #[test]
    #[cfg(not(windows))]
    fn file_uri_to_path_keeps_decoded_path_on_non_windows() {
        let path =
            file_uri_to_path("file:///C:/Users/Josh/gobby%20uri/a%23b.rs").expect("file uri path");

        assert_eq!(path, PathBuf::from("/C:/Users/Josh/gobby uri/a#b.rs"));
    }

    #[test]
    #[cfg(windows)]
    #[serial_test::serial]
    fn find_executable_in_path_honors_pathext_on_windows() {
        let tempdir = TempDir::new().expect("tempdir");
        let exe = tempdir.path().join("clangd.CMD");
        fs::write(&exe, "").expect("fake executable");
        let old_path = std::env::var_os("PATH");
        let old_pathext = std::env::var_os("PATHEXT");

        unsafe {
            std::env::set_var("PATH", tempdir.path());
            std::env::set_var("PATHEXT", ".COM;.EXE;.CMD");
        }
        let found = find_executable_in_path("clangd");
        unsafe {
            match old_path {
                Some(value) => std::env::set_var("PATH", value),
                None => std::env::remove_var("PATH"),
            }
            match old_pathext {
                Some(value) => std::env::set_var("PATHEXT", value),
                None => std::env::remove_var("PATHEXT"),
            }
        }

        assert_eq!(found.as_deref(), Some(exe.as_path()));
    }

    #[test]
    fn optional_clangd_integration_resolves_external_definition() {
        if std::env::var("GCODE_TEST_CLANGD").ok().as_deref() != Some("1") {
            return;
        }
        let Some(clangd) = resolve_clangd_command() else {
            panic!("GCODE_TEST_CLANGD=1 requires clangd");
        };
        let tempdir = TempDir::new().expect("tempdir");
        let source_dir = tempdir.path().join("src");
        fs::create_dir_all(&source_dir).expect("source dir");
        let source_path = source_dir.join("main.c");
        let source = b"#include <stdio.h>\nvoid run(void) {\n    printf(\"x\");\n}\n";
        fs::write(&source_path, source).expect("source");
        let compile_db = format!(
            r#"[{{"directory":"{}","command":"cc -c {}","file":"{}"}}]"#,
            tempdir.path().display(),
            source_path.display(),
            source_path.display()
        );
        fs::write(tempdir.path().join("compile_commands.json"), compile_db).expect("compile db");

        let mut resolver =
            ClangdResolver::start(tempdir.path(), tempdir.path(), &clangd).expect("clangd");
        let target = resolver
            .resolve(&SemanticCallRequest {
                language: "c",
                file_path: &source_path,
                root_path: tempdir.path(),
                source,
                callee_name: "printf",
                line: 3,
                column: 4,
            })
            .expect("resolve external definition");
        assert!(target.is_some());
    }
}
