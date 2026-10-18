//! Write-only tmux adapter behind [`crate::write::Runtime`].
//!
//! Mirrors the write operations of `TmuxTerminalRuntime` in
//! `src/gobby/terminals/tmux_runtime.py`. Every operation addresses the row's
//! recorded tmux socket and refuses before sending a byte when the server
//! generation recorded in the locator no longer matches the live server.

use std::io;
use std::sync::Arc;
use std::time::Duration;

use serde_json::Value;
use tokio::process::Command;
use uuid::Uuid;

use crate::repository::{Ownership, Terminal};
use crate::write::{BoxFuture, Delivery, Kind, Runtime, Stage, Step, WriteFailure};

/// Bound on one tmux invocation, matching `TMUX_COMMAND_TIMEOUT_SECONDS`.
const COMMAND_TIMEOUT: Duration = Duration::from_secs(10);
/// Pause between a pasted body and Enter, matching `TMUX_TEXT_ENTER_DELAY_SECONDS`.
const ENTER_DELAY: Duration = Duration::from_secs(1);
/// Largest chunk handed to one `set-buffer`, matching `TMUX_BUFFER_CHUNK_BYTES`.
const BUFFER_CHUNK_BYTES: usize = 8192;
/// Largest raw input sent per `send-keys -H`, matching `write_input`.
const INPUT_CHUNK_BYTES: usize = 512;
const MAX_INPUT_BYTES: usize = 64 * 1024;
const MAX_PASTE_BYTES: usize = 1024 * 1024;
const MAX_DIMENSION: i32 = 1024;
/// `MAX_CELLS` from `src/gobby/terminals/dimensions.py`.
const MAX_CELLS: i64 = (2 * 1024 * 1024 - 64) / 96;

const GENERATION_FORMAT: &str = "#{pid}\t#{start_time}";
const FLAGS_FORMAT: &str = "#{cursor_keys_flag}|#{keypad_cursor_flag}|#{bracket_paste_flag}";

/// Stderr fragments meaning the pane or session is gone.
const VANISHED_FRAGMENTS: &[&str] = &[
    "can't find pane",
    "can't find session",
    "dead pane",
    "no server running",
    "no such pane",
    "no such session",
    "pane is dead",
    "pane_dead",
];
/// Stderr fragment meaning the pane is not accepting pane input.
const PANE_MODE_FRAGMENTS: &[&str] = &["not in a mode"];

/// Keys tmux spells by name, matching `TMUX_KEY_NAMES`.
const TMUX_KEY_NAMES: &[(&str, &str)] = &[
    ("enter", "Enter"),
    ("escape", "Escape"),
    ("tab", "Tab"),
    ("backspace", "BSpace"),
    ("delete", "DC"),
    ("ctrl_c", "C-c"),
    ("ctrl_k", "C-k"),
    ("ctrl_l", "C-l"),
    ("ctrl_u", "C-u"),
];

/// Keypad keys as (name, application-mode bytes, normal-mode bytes).
const KEYPAD: &[(&str, &str, &str)] = &[
    ("kp0", "\x1bOp", "0"),
    ("kp1", "\x1bOq", "1"),
    ("kp2", "\x1bOr", "2"),
    ("kp3", "\x1bOs", "3"),
    ("kp4", "\x1bOt", "4"),
    ("kp5", "\x1bOu", "5"),
    ("kp6", "\x1bOv", "6"),
    ("kp7", "\x1bOw", "7"),
    ("kp8", "\x1bOx", "8"),
    ("kp9", "\x1bOy", "9"),
    ("kpdecimal", "\x1bOn", "."),
    ("kpminus", "\x1bOm", "-"),
    ("kpplus", "\x1bOk", "+"),
    ("kpmul", "\x1bOj", "*"),
    ("kpdiv", "\x1bOo", "/"),
    ("kpenter", "\x1bOM", "\r"),
];

/// Captured result of one tmux invocation.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CommandOutput {
    pub status: i32,
    pub stdout: String,
    pub stderr: String,
}

/// Runs one tmux argv. The argv begins with `tmux -S <socket>`, as Python builds it.
pub trait TmuxCommands: Send + Sync {
    fn run<'a>(
        &'a self,
        argv: &'a [String],
        timeout: Duration,
    ) -> BoxFuture<'a, io::Result<CommandOutput>>;
}

/// Production runner: spawns the tmux binary with a timeout.
#[derive(Debug, Default)]
pub struct SystemTmux;

impl TmuxCommands for SystemTmux {
    fn run<'a>(
        &'a self,
        argv: &'a [String],
        timeout: Duration,
    ) -> BoxFuture<'a, io::Result<CommandOutput>> {
        Box::pin(async move {
            let Some((program, args)) = argv.split_first() else {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidInput,
                    "empty tmux argv",
                ));
            };
            let mut command = Command::new(program);
            command.args(args).kill_on_drop(true);
            let output = tokio::time::timeout(timeout, command.output())
                .await
                .map_err(|_| io::Error::new(io::ErrorKind::TimedOut, "tmux command timed out"))??;
            Ok(CommandOutput {
                status: output.status.code().unwrap_or(-1),
                stdout: String::from_utf8_lossy(&output.stdout).into_owned(),
                stderr: String::from_utf8_lossy(&output.stderr).into_owned(),
            })
        })
    }
}

/// The tmux write runtime. Holds no per-terminal state.
pub struct TmuxWrite {
    commands: Arc<dyn TmuxCommands>,
}

/// Why an invocation or check stopped, before a write stage is assigned.
enum Fault {
    /// The invocation timed out, so the write outcome is unknown.
    Timeout(String),
    /// The recorded server generation no longer matches the live server.
    Stale,
    /// tmux failed or could not be spawned.
    Failed { code: &'static str, detail: String },
}

/// The pane and the server generation recorded for an external tmux row.
struct Target {
    socket: String,
    pane: String,
    server_pid: i64,
    server_start_time: i64,
}

impl TmuxWrite {
    pub fn new(commands: Arc<dyn TmuxCommands>) -> Self {
        Self { commands }
    }

    /// Sets the window size to `rows` by `cols`. A generation mismatch returns `Ok`
    /// without sending anything, as Python's `resize` does.
    pub async fn resize(
        &self,
        terminal: &Terminal,
        rows: i32,
        cols: i32,
    ) -> Result<(), WriteFailure> {
        validate_dimensions(rows, cols)?;
        let Some(target) = self.live_target(terminal).await? else {
            return Ok(());
        };
        let rows = rows.to_string();
        let cols = cols.to_string();
        self.run_unchecked(
            &target,
            &[
                "set-option",
                "-w",
                "-t",
                target.pane.as_str(),
                "window-size",
                "manual",
            ],
        )
        .await?;
        self.run_unchecked(
            &target,
            &[
                "resize-window",
                "-t",
                target.pane.as_str(),
                "-x",
                cols.as_str(),
                "-y",
                rows.as_str(),
            ],
        )
        .await
    }

    /// Releases the manual window size. A generation mismatch returns `Ok` silently.
    pub async fn release_size(&self, terminal: &Terminal) -> Result<(), WriteFailure> {
        let Some(target) = self.live_target(terminal).await? else {
            return Ok(());
        };
        self.run_unchecked(
            &target,
            &[
                "set-option",
                "-wu",
                "-t",
                target.pane.as_str(),
                "window-size",
            ],
        )
        .await
    }

    /// The target when the row is an external pane whose recorded server still matches.
    async fn live_target(&self, terminal: &Terminal) -> Result<Option<Target>, WriteFailure> {
        let pane = pane_of(terminal)?;
        let Some(target) = target_of(terminal, pane) else {
            return Ok(None);
        };
        Ok(self.same_generation(&target).await.then_some(target))
    }

    async fn dispatch(&self, terminal: &Terminal, step: &Step) -> Result<Delivery, WriteFailure> {
        let pane = pane_of(terminal)?;
        let target = target_of(terminal, pane).ok_or_else(|| {
            refuse(
                "tmux_target_unavailable",
                "tmux row has no external socket to address",
            )
        })?;
        match step.kind {
            Kind::Text => self.write_text(&target, &step.payload, step.submit).await,
            Kind::Key => self.write_key(&target, &step.payload).await,
            Kind::Input => self.write_input(&target, step.payload.as_bytes()).await,
            Kind::Paste => self.write_paste(&target, &step.payload).await,
        }
    }

    async fn write_text(
        &self,
        target: &Target,
        text: &str,
        submit: bool,
    ) -> Result<Delivery, WriteFailure> {
        // Python strips trailing newlines from the body before pasting it.
        let body = text.trim_end_matches('\n');
        let mut landed = false;
        let result = self.text_steps(target, body, submit, &mut landed).await;
        settle(result, landed)
    }

    async fn text_steps(
        &self,
        target: &Target,
        body: &str,
        submit: bool,
        landed: &mut bool,
    ) -> Result<(), Fault> {
        self.require(target).await?;
        if !body.is_empty() {
            self.paste(target, body).await?;
            *landed = true;
        }
        if submit {
            if !body.is_empty() {
                tokio::time::sleep(ENTER_DELAY).await;
                self.require(target).await?;
            }
            self.send(target, &["send-keys", "-t", target.pane.as_str(), "Enter"])
                .await?;
        }
        Ok(())
    }

    async fn write_key(&self, target: &Target, key: &str) -> Result<Delivery, WriteFailure> {
        let result: Result<(), Fault> = async {
            self.require(target).await?;
            let (cursor_app, keypad_app, _) = self.flags(target).await?;
            if let Some(name) = tmux_key_name(key) {
                return self
                    .send(target, &["send-keys", "-t", target.pane.as_str(), name])
                    .await;
            }
            let bytes = encode_key(key, cursor_app, keypad_app).ok_or_else(|| Fault::Failed {
                code: "invalid_key",
                detail: format!("unknown key {key}"),
            })?;
            let hex = hex_bytes(&bytes);
            let mut args = vec!["send-keys", "-t", target.pane.as_str(), "-H"];
            args.extend(hex.iter().map(String::as_str));
            self.send(target, &args).await
        }
        .await;
        settle(result, false)
    }

    async fn write_input(&self, target: &Target, data: &[u8]) -> Result<Delivery, WriteFailure> {
        if data.len() > MAX_INPUT_BYTES {
            return Err(refuse("request_too_large", "input exceeds 64 KiB"));
        }
        let mut delivered = 0;
        let result: Result<(), Fault> = async {
            self.require(target).await?;
            for chunk in data.chunks(INPUT_CHUNK_BYTES) {
                let hex = hex_bytes(chunk);
                let mut args = vec!["send-keys", "-t", target.pane.as_str(), "-H"];
                args.extend(hex.iter().map(String::as_str));
                self.send(target, &args).await?;
                delivered += chunk.len();
            }
            Ok(())
        }
        .await;
        match result {
            Ok(()) => Ok(Delivery::Delivered),
            // Python raises a partial stage for every timeout on raw input.
            Err(fault @ Fault::Timeout(_)) => Err(failure(fault, true)),
            Err(fault) => Err(failure(fault, delivered > 0)),
        }
    }

    async fn write_paste(&self, target: &Target, text: &str) -> Result<Delivery, WriteFailure> {
        if text.len() > MAX_PASTE_BYTES {
            return Err(refuse("request_too_large", "paste exceeds 1 MiB UTF-8"));
        }
        let result: Result<(), Fault> = async {
            self.require(target).await?;
            let (_, _, bracketed) = self.flags(target).await?;
            if bracketed {
                self.paste(target, &format!("\x1b[200~{text}\x1b[201~"))
                    .await
            } else {
                self.paste(target, text).await
            }
        }
        .await;
        settle(result, false)
    }

    /// Loads `text` into a uniquely named tmux buffer, pastes it, and deletes the buffer.
    async fn paste(&self, target: &Target, text: &str) -> Result<(), Fault> {
        let chunks = buffer_chunks(text);
        let Some((&head, tail)) = chunks.split_first() else {
            return Ok(());
        };
        let buffer = format!("gobby-{}-{}", std::process::id(), Uuid::new_v4().simple());
        let loaded: Result<(), Fault> = async {
            self.send(target, &["set-buffer", "-b", buffer.as_str(), "--", head])
                .await?;
            for &chunk in tail {
                self.send(
                    target,
                    &["set-buffer", "-a", "-b", buffer.as_str(), "--", chunk],
                )
                .await?;
            }
            self.send(
                target,
                &[
                    "paste-buffer",
                    "-d",
                    "-p",
                    "-b",
                    buffer.as_str(),
                    "-t",
                    target.pane.as_str(),
                ],
            )
            .await
        }
        .await;
        // Python deletes the buffer in a finally block and ignores a failure there.
        let _ = self
            .send(target, &["delete-buffer", "-b", buffer.as_str()])
            .await;
        loaded
    }

    /// Whether the recorded server generation still matches, as Python's `_same_generation`.
    async fn same_generation(&self, target: &Target) -> bool {
        let recorded = format!("{}\t{}", target.server_pid, target.server_start_time);
        match self
            .exec(
                target,
                &[
                    "display-message",
                    "-p",
                    "-t",
                    target.pane.as_str(),
                    GENERATION_FORMAT,
                ],
            )
            .await
        {
            Ok(output) => output.status == 0 && output.stdout.trim() == recorded,
            Err(_) => false,
        }
    }

    async fn require(&self, target: &Target) -> Result<(), Fault> {
        if self.same_generation(target).await {
            Ok(())
        } else {
            Err(Fault::Stale)
        }
    }

    /// Reads the cursor, keypad and bracketed-paste modes, as Python's `_query_flags`.
    async fn flags(&self, target: &Target) -> Result<(bool, bool, bool), Fault> {
        let output = self
            .exec(
                target,
                &[
                    "display-message",
                    "-t",
                    target.pane.as_str(),
                    "-p",
                    FLAGS_FORMAT,
                ],
            )
            .await?;
        if output.status != 0 {
            return Ok((false, false, false));
        }
        let flags: Vec<bool> = output
            .stdout
            .trim()
            .split('|')
            .map(|part| part == "1")
            .collect();
        let flag = |index: usize| flags.get(index).copied().unwrap_or(false);
        Ok((flag(0), flag(1), flag(2)))
    }

    /// Runs one tmux command against the recorded socket, returning its captured output.
    async fn exec(&self, target: &Target, args: &[&str]) -> Result<CommandOutput, Fault> {
        let mut argv = vec!["tmux".to_owned(), "-S".to_owned(), target.socket.clone()];
        argv.extend(args.iter().map(|arg| (*arg).to_owned()));
        self.commands
            .run(&argv, COMMAND_TIMEOUT)
            .await
            .map_err(|error| match error.kind() {
                io::ErrorKind::TimedOut => Fault::Timeout("tmux command timed out".to_owned()),
                _ => Fault::Failed {
                    code: "tmux_spawn_failed",
                    detail: error.to_string(),
                },
            })
    }

    /// Runs a tmux command whose exit status Python does not check.
    async fn run_unchecked(&self, target: &Target, args: &[&str]) -> Result<(), WriteFailure> {
        self.exec(target, args)
            .await
            .map(|_| ())
            .map_err(|fault| failure(fault, false))
    }

    /// Runs a tmux command and maps a nonzero exit to a classified failure.
    async fn send(&self, target: &Target, args: &[&str]) -> Result<(), Fault> {
        let output = self.exec(target, args).await?;
        if output.status == 0 {
            return Ok(());
        }
        Err(command_fault(&output))
    }
}

impl Runtime for TmuxWrite {
    fn write<'a>(
        &'a self,
        terminal: &'a Terminal,
        step: &'a Step,
    ) -> BoxFuture<'a, Result<Delivery, WriteFailure>> {
        Box::pin(async move { self.dispatch(terminal, step).await })
    }
}

/// The pane a row addresses: its recorded pane id, else its session name.
fn pane_of(terminal: &Terminal) -> Result<String, WriteFailure> {
    let pane = terminal
        .locator
        .as_ref()
        .and_then(|locator| locator.get("pane_id"))
        .and_then(Value::as_str)
        .filter(|pane| !pane.is_empty());
    if let Some(pane) = pane {
        return Ok(pane.to_owned());
    }
    [
        terminal.session_name.as_deref(),
        terminal.spawn_key.as_deref(),
    ]
    .into_iter()
    .flatten()
    .find(|name| !name.is_empty())
    .map(|name| format!("={name}:"))
    .ok_or_else(|| {
        refuse(
            "tmux_target_unavailable",
            "tmux row has no pane or session to address",
        )
    })
}

/// The socket and recorded generation of an external row, or `None` when it has none.
fn target_of(terminal: &Terminal, pane: String) -> Option<Target> {
    if !matches!(terminal.ownership, Ownership::External) {
        return None;
    }
    let locator = terminal.locator.as_ref()?;
    let socket = locator
        .get("socket_path")?
        .as_str()
        .filter(|socket| !socket.is_empty())?;
    let server_pid = locator.get("server_pid")?.as_i64()?;
    let server_start_time = locator.get("server_start_time")?.as_i64()?;
    Some(Target {
        socket: socket.to_owned(),
        pane,
        server_pid,
        server_start_time,
    })
}

fn validate_dimensions(rows: i32, cols: i32) -> Result<(), WriteFailure> {
    let in_range = |value: i32| (1..=MAX_DIMENSION).contains(&value);
    let cells = i64::from(rows) * i64::from(cols);
    if in_range(rows) && in_range(cols) && cells <= MAX_CELLS {
        Ok(())
    } else {
        Err(refuse(
            "invalid_dimensions",
            &format!("rows {rows} and cols {cols} are outside the terminal limits"),
        ))
    }
}

fn tmux_key_name(key: &str) -> Option<&'static str> {
    TMUX_KEY_NAMES
        .iter()
        .find(|(name, _)| *name == key)
        .map(|(_, tmux)| *tmux)
}

/// Bytes a non-tmux-named key sends, honoring cursor and keypad application modes.
fn encode_key(key: &str, cursor_app: bool, keypad_app: bool) -> Option<Vec<u8>> {
    let letter = match key {
        "up" => 'A',
        "down" => 'B',
        "right" => 'C',
        "left" => 'D',
        _ => {
            return KEYPAD
                .iter()
                .find(|(name, _, _)| *name == key)
                .map(|&(_, app_seq, normal)| if keypad_app { app_seq } else { normal })
                .map(|seq| seq.as_bytes().to_vec());
        }
    };
    let prefix = if cursor_app { "\x1bO" } else { "\x1b[" };
    Some(format!("{prefix}{letter}").into_bytes())
}

fn hex_bytes(bytes: &[u8]) -> Vec<String> {
    bytes.iter().map(|byte| format!("{byte:02x}")).collect()
}

/// Splits `text` into chunks of at most `BUFFER_CHUNK_BYTES`, on code point boundaries.
fn buffer_chunks(text: &str) -> Vec<&str> {
    let mut chunks = Vec::new();
    let mut start = 0;
    while start < text.len() {
        let mut end = (start + BUFFER_CHUNK_BYTES).min(text.len());
        while !text.is_char_boundary(end) {
            end -= 1;
        }
        chunks.push(&text[start..end]);
        start = end;
    }
    chunks
}

/// Classifies a nonzero tmux exit, as Python's `classify_tmux_text_injection_error`.
fn command_fault(output: &CommandOutput) -> Fault {
    let stderr = output.stderr.trim();
    let detail = if stderr.is_empty() {
        format!("tmux exited with status {}", output.status)
    } else {
        stderr.to_owned()
    };
    let lowered = detail.to_lowercase();
    let code = if VANISHED_FRAGMENTS
        .iter()
        .any(|fragment| lowered.contains(*fragment))
    {
        "tmux_target_unavailable"
    } else if PANE_MODE_FRAGMENTS
        .iter()
        .any(|fragment| lowered.contains(*fragment))
    {
        "tmux_pane_mode_unavailable"
    } else {
        "tmux_command_failed"
    };
    Fault::Failed {
        code,
        detail: format!("tmux command failed: {detail}"),
    }
}

/// A refusal before any byte was sent.
fn refuse(code: &str, detail: &str) -> WriteFailure {
    WriteFailure {
        stage: Stage::None,
        code: code.to_owned(),
        detail: detail.to_owned(),
    }
}

/// Maps a fault to a failure. The stage is `partial` once any payload landed.
fn failure(fault: Fault, landed: bool) -> WriteFailure {
    let stage = if landed { Stage::Partial } else { Stage::None };
    let (code, detail) = match fault {
        Fault::Timeout(detail) => ("tmux_timeout", detail),
        Fault::Stale => (
            "tmux_generation_changed",
            "tmux server generation changed".to_owned(),
        ),
        Fault::Failed { code, detail } => (code, detail),
    };
    WriteFailure {
        stage,
        code: code.to_owned(),
        detail,
    }
}

/// A timeout leaves the outcome unknown; every other fault is a failure.
fn settle(result: Result<(), Fault>, landed: bool) -> Result<Delivery, WriteFailure> {
    match result {
        Ok(()) => Ok(Delivery::Delivered),
        Err(Fault::Timeout(detail)) => Ok(Delivery::Uncertain { detail }),
        Err(fault) => Err(failure(fault, landed)),
    }
}
