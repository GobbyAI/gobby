//! Client terminal themes reach native panes (#22851): the spawn request's
//! theme, live re-theme over the frame stream, newest-declaration ownership
//! across a pane's streams, and the unset fallback. Each child prints its own OSC 10/11 answers, so every
//! assertion reads what a program inside the pane would see.

#![cfg(all(unix, feature = "vt-engine"))]

mod host_support;

use gobby_terminal::protocol::{
    read_message, write_message, ClientMessage, RenderEncoding, ServerMessage, MAX_FRAME_SIZE,
    PROTOCOL_VERSION,
};
use gobby_terminal::terminal_theme::{RgbColor, ThemeDeclaration};
use host_support::{
    connect, hello_control, recv_json, rpc, send_json, spawn_host, temp_socket_dir, wait_socket,
    wait_until_host, write_token, HostProc, CONTROL_SOCKET, FRAMES_SOCKET,
};
use serde_json::{json, Value};
use std::io::Write;
use std::os::unix::net::UnixStream;

const LOCAL: &str = "local-token";

/// Queries OSC 10 and OSC 11 every 300 ms and prints the raw answers, so the
/// screen always ends with the pane's current default colours.
const QUERY_LOOP: &str = "stty raw -echo min 0 time 2; \
    while :; do printf '\\033]10;?\\033\\\\\\033]11;?\\033\\\\'; sleep 0.1; \
    printf 'A<'; dd bs=256 count=1 2>/dev/null | cat -v; printf '>\\r\\n'; sleep 0.2; done";

const LIGHT: ThemeColors = ThemeColors {
    fg: rgb(0x20, 0x21, 0x22),
    bg: rgb(0xfa, 0xfb, 0xfc),
    fg_answer: "rgb:2020/2121/2222",
    bg_answer: "rgb:fafa/fbfb/fcfc",
};
const DARK: ThemeColors = ThemeColors {
    fg: rgb(0xe0, 0xe1, 0xe2),
    bg: rgb(0x10, 0x11, 0x12),
    fg_answer: "rgb:e0e0/e1e1/e2e2",
    bg_answer: "rgb:1010/1111/1212",
};

struct ThemeColors {
    fg: RgbColor,
    bg: RgbColor,
    fg_answer: &'static str,
    bg_answer: &'static str,
}

impl ThemeColors {
    fn declaration(&self) -> ThemeDeclaration {
        ThemeDeclaration {
            foreground: Some(self.fg),
            background: Some(self.bg),
            palette: vec![(1, rgb(0xc0, 0x10, 0x10))],
        }
    }

    fn shown_by(&self, screen: &str) -> bool {
        let last = screen.lines().rev().find(|line| line.contains("A<"));
        last.is_some_and(|line| line.contains(self.fg_answer) && line.contains(self.bg_answer))
    }
}

const fn rgb(r: u8, g: u8, b: u8) -> RgbColor {
    RgbColor { r, g, b }
}

struct Host {
    _dir: tempfile::TempDir,
    proc: HostProc,
    ctrl: UnixStream,
    frames_path: std::path::PathBuf,
    next: u32,
}

impl Host {
    fn start(token: &str) -> Self {
        let dir = temp_socket_dir();
        write_token(dir.path(), token);
        std::fs::write(
            dir.path().join("bootstrap.yaml"),
            format!("api_key: {LOCAL}\n"),
        )
        .unwrap();
        let proc = spawn_host(dir.path());
        wait_socket(&dir.path().join(CONTROL_SOCKET));
        wait_socket(&dir.path().join(FRAMES_SOCKET));
        let mut ctrl = connect(&dir.path().join(CONTROL_SOCKET));
        let hello = hello_control(&mut ctrl, token);
        assert_eq!(hello["ok"], true, "{hello}");
        let frames_path = dir.path().join(FRAMES_SOCKET);
        Self {
            _dir: dir,
            proc,
            ctrl,
            frames_path,
            next: 0,
        }
    }

    /// Spawns and commits the query loop; returns its host terminal id or the
    /// spawn error code.
    fn spawn(&mut self, theme: Option<Value>) -> Result<String, String> {
        self.spawn_with("terminal_theme", theme.unwrap_or(Value::Null))
    }

    /// Spawn with one extra request field, such as `theme_from`.
    fn spawn_with(&mut self, field: &str, value: Value) -> Result<String, String> {
        self.next += 1;
        let terminal_id = format!("term-{}", self.next);
        let reserved = rpc(
            &mut self.ctrl,
            "reserve_observer",
            json!({"terminal_id": terminal_id, "reserve_key": "rk"}),
        );
        let mut request = json!({
            "method": "spawn",
            "operation_seq": self.next,
            "terminal_id": terminal_id,
            "spawn_key": "sk",
            "reservation_id": reserved["reservation_id"],
            "reserve_key": "rk",
            "argv": ["/bin/sh", "-c", QUERY_LOOP],
            "cwd": "/",
            "rows": 24,
            "cols": 100,
            "commit_deadline_ms": 8000,
        });
        if !value.is_null() {
            request[field] = value;
        }
        send_json(&mut self.ctrl, &request);
        let prepared = recv_json(&mut self.ctrl);
        if prepared["ok"] != true {
            return Err(prepared["error"].as_str().unwrap_or_default().to_string());
        }
        self.proc
            .track_pgid(prepared["pgid"].as_i64().unwrap() as i32);
        let committed = rpc(
            &mut self.ctrl,
            "spawn_commit",
            json!({"terminal_id": terminal_id, "spawn_key": "sk"}),
        );
        assert_eq!(committed["ok"], true, "{committed}");
        Ok(prepared["host_terminal_id"].as_str().unwrap().to_string())
    }

    fn screen(&mut self, host_terminal_id: &str) -> String {
        let snap = rpc(
            &mut self.ctrl,
            "snapshot",
            json!({
                "host_terminal_id": host_terminal_id,
                "mode": "text",
                "max_bytes": 8192,
                "max_lines": 50,
            }),
        );
        assert_eq!(snap["ok"], true, "{snap}");
        snap["text"].as_str().unwrap_or_default().to_string()
    }

    /// Polls until `ready` accepts the pane's screen and returns that screen.
    fn wait_for(
        &mut self,
        host_terminal_id: &str,
        what: &str,
        ready: impl Fn(&str) -> bool,
    ) -> String {
        let mut screen = String::new();
        let dir = self._dir.path().to_path_buf();
        wait_until_host(&dir, what, || {
            screen = self.screen(host_terminal_id);
            ready(&screen)
        });
        screen
    }

    fn attach(&self, host_terminal_id: &str) -> (UnixStream, u64) {
        let mut stream = connect(&self.frames_path);
        write_msg(
            &mut stream,
            &ClientMessage::Hello {
                version: PROTOCOL_VERSION,
                encoding: RenderEncoding::SemanticFrame,
                local_token: LOCAL.into(),
                cols: 100,
                rows: 24,
                tmux_identity: None,
            },
        );
        assert!(matches!(
            read_msg(&mut stream),
            ServerMessage::Welcome { .. }
        ));
        write_msg(
            &mut stream,
            &ClientMessage::AttachTerminal {
                host_terminal_id: host_terminal_id.into(),
                reservation_id: None,
                locator: None,
            },
        );
        loop {
            match read_msg(&mut stream) {
                ServerMessage::Attached {
                    host_attachment_id, ..
                } => return (stream, host_attachment_id),
                ServerMessage::Error { code, .. } => panic!("attach refused: {code}"),
                _ => {}
            }
        }
    }
}

fn write_msg(stream: &mut UnixStream, msg: &ClientMessage) {
    let mut buf = Vec::new();
    write_message(&mut buf, msg).unwrap();
    stream.write_all(&buf).unwrap();
    stream.flush().unwrap();
}

fn read_msg(stream: &mut UnixStream) -> ServerMessage {
    stream
        .set_read_timeout(Some(std::time::Duration::from_secs(5)))
        .unwrap();
    read_message(stream, MAX_FRAME_SIZE).expect("frame message")
}

fn declare(stream: &mut UnixStream, colors: &ThemeColors) {
    write_msg(
        stream,
        &ClientMessage::SetTerminalTheme {
            theme: colors.declaration(),
        },
    );
}

fn bind(stream: &mut UnixStream, attachment_id: &str) {
    write_msg(
        stream,
        &ClientMessage::BindAttachment {
            attachment_id: attachment_id.into(),
        },
    );
}

fn theme_json(colors: &ThemeColors) -> Value {
    serde_json::to_value(colors.declaration()).unwrap()
}

#[test]
fn control_hello_advertises_terminal_theme() {
    let dir = temp_socket_dir();
    write_token(dir.path(), "theme-hello");
    let _proc = spawn_host(dir.path());
    wait_socket(&dir.path().join(CONTROL_SOCKET));
    let mut ctrl = connect(&dir.path().join(CONTROL_SOCKET));
    let hello = hello_control(&mut ctrl, "theme-hello");
    let capabilities = hello["capabilities"].as_array().expect("capabilities list");
    assert!(capabilities.contains(&json!("terminal_theme")), "{hello}");
}

#[test]
fn spawn_theme_answers_the_first_osc_queries() {
    let mut host = Host::start("theme-spawn");
    let light = host.spawn(Some(theme_json(&LIGHT))).unwrap();
    let dark = host.spawn(Some(theme_json(&DARK))).unwrap();
    host.wait_for(&light, "light spawn theme", |s| LIGHT.shown_by(s));
    host.wait_for(&dark, "dark spawn theme", |s| DARK.shown_by(s));
    // The child is gated until commit, so no answer predates the theme.
    let first = host.screen(&light);
    let first_answer = first.lines().find(|line| line.contains("A<")).unwrap();
    assert!(first_answer.contains(LIGHT.bg_answer), "{first}");
}

#[test]
fn unset_theme_is_the_fallback_until_a_client_declares() {
    let mut host = Host::start("theme-fallback");
    let pane = host.spawn(None).unwrap();
    host.wait_for(&pane, "a fallback answer", |s| {
        s.lines()
            .any(|line| line.contains("A<") && line.contains('>'))
    });
    // With no theme known, the pane leaves OSC 10/11 unanswered, so programs
    // keep their own defaults instead of trusting an invented colour.
    let screen = host.screen(&pane);
    let answer = screen
        .lines()
        .find(|line| line.contains("A<") && line.contains('>'))
        .unwrap();
    assert_eq!(answer.trim(), "A<>", "{screen}");

    // A pane with no declaration yet takes the first declaring stream's theme live.
    let (mut stream, _) = host.attach(&pane);
    declare(&mut stream, &LIGHT);
    host.wait_for(&pane, "declared theme", |s| LIGHT.shown_by(s));
}

#[test]
fn live_retheme_changes_answers_without_respawn() {
    let mut host = Host::start("theme-live");
    let pane = host.spawn(Some(theme_json(&DARK))).unwrap();
    host.wait_for(&pane, "dark spawn theme", |s| DARK.shown_by(s));
    let (mut stream, _) = host.attach(&pane);
    bind(&mut stream, "att-gclient");
    declare(&mut stream, &LIGHT);
    host.wait_for(&pane, "light after toggle", |s| LIGHT.shown_by(s));
    declare(&mut stream, &DARK);
    let screen = host.wait_for(&pane, "dark after toggle back", |s| DARK.shown_by(s));
    // The same child printed both themes' answers: no respawn happened.
    assert!(screen.contains(LIGHT.bg_answer), "{screen}");
}

#[test]
fn the_newest_declaration_themes_a_pane_shared_by_its_streams() {
    let mut host = Host::start("theme-newest");
    let pane = host.spawn(None).unwrap();
    let (mut first, _) = host.attach(&pane);
    bind(&mut first, "att-first");
    declare(&mut first, &LIGHT);
    host.wait_for(&pane, "first stream theme", |s| LIGHT.shown_by(s));

    // The second stream's declaration applies on arrival.
    let (mut second, _) = host.attach(&pane);
    bind(&mut second, "att-second");
    declare(&mut second, &DARK);
    host.wait_for(&pane, "newest declaration theme", |s| DARK.shown_by(s));

    // Re-declaring from the first stream makes its theme the newest.
    declare(&mut first, &LIGHT);
    let screen = host.wait_for(&pane, "re-declared theme", |s| LIGHT.shown_by(s));
    assert!(LIGHT.shown_by(&screen), "{screen}");
}

#[test]
fn a_bind_reapplies_the_newest_declaration_not_the_latest_stream() {
    let mut host = Host::start("theme-rebind");
    let pane = host.spawn(None).unwrap();
    let (mut first, _) = host.attach(&pane);
    bind(&mut first, "att-first");
    declare(&mut first, &DARK);
    host.wait_for(&pane, "first declaration theme", |s| DARK.shown_by(s));

    let (mut second, _) = host.attach(&pane);
    bind(&mut second, "att-second");
    declare(&mut second, &LIGHT);
    host.wait_for(&pane, "second declaration theme", |s| LIGHT.shown_by(s));

    // The first stream declares again, so dark is the newest declaration.
    declare(&mut first, &DARK);
    host.wait_for(&pane, "re-declared dark theme", |s| DARK.shown_by(s));

    // A third stream binds without a declaration. The pane keeps the newest one,
    // not the theme of the most recently attached stream.
    let (mut third, _) = host.attach(&pane);
    bind(&mut third, "att-third");
    let screen = host.wait_for(&pane, "newest declaration after a bind", |s| {
        DARK.shown_by(s)
    });
    assert!(DARK.shown_by(&screen), "{screen}");
}

/// A declaration themes only its own attachment's pane (#23286). A laptop
/// client on Light once seeded every later spawn, so a dark desktop's new
/// Codex read a light OSC 11 answer and drew light bands for its lifetime.
#[test]
fn a_declaration_never_seeds_another_clients_spawn() {
    let mut host = Host::start("theme-no-seed");
    let laptop_pane = host.spawn(None).unwrap();
    let (mut laptop, _) = host.attach(&laptop_pane);
    declare(&mut laptop, &LIGHT);
    host.wait_for(&laptop_pane, "the laptop's theme", |s| LIGHT.shown_by(s));

    // A spawn that brings no theme leaves OSC 10/11 unanswered.
    let unthemed = host.spawn(None).unwrap();
    let screen = host.wait_for(&unthemed, "a first answer", |s| {
        s.lines()
            .any(|line| line.contains("A<") && line.contains('>'))
    });
    let answer = screen
        .lines()
        .find(|line| line.contains("A<") && line.contains('>'))
        .unwrap();
    assert_eq!(answer.trim(), "A<>", "{screen}");

    // A spawn that brings its client's theme answers with that theme alone.
    let desktop = host.spawn(Some(theme_json(&DARK))).unwrap();
    let screen = host.wait_for(&desktop, "the desktop's theme", |s| DARK.shown_by(s));
    let first_answer = screen.lines().find(|line| line.contains("A<")).unwrap();
    assert!(first_answer.contains(DARK.bg_answer), "{screen}");
}

/// A daemon spawn names its spawner's pane in `theme_from` and answers with
/// that pane's current ground, never with a newer declaration on another
/// client's pane (#23286).
#[test]
fn theme_from_seeds_a_spawn_with_its_parents_ground_alone() {
    let mut host = Host::start("theme-from");
    let parent = host.spawn(None).unwrap();
    let (mut desktop, _) = host.attach(&parent);
    declare(&mut desktop, &DARK);
    host.wait_for(&parent, "the desktop's theme", |s| DARK.shown_by(s));
    let other = host.spawn(None).unwrap();
    let (mut laptop, _) = host.attach(&other);
    declare(&mut laptop, &LIGHT);
    host.wait_for(&other, "the laptop's theme", |s| LIGHT.shown_by(s));

    let child = host.spawn_with("theme_from", json!(parent)).unwrap();
    let screen = host.wait_for(&child, "a first answer", |s| {
        s.lines()
            .any(|line| line.contains("A<") && line.contains('>'))
    });
    let first_answer = screen.lines().find(|line| line.contains("A<")).unwrap();
    assert!(first_answer.contains(DARK.bg_answer), "{screen}");
    assert!(first_answer.contains(DARK.fg_answer), "{screen}");
}

/// A `theme_from` naming no live pane leaves the spawn unset.
#[test]
fn theme_from_an_unknown_pane_leaves_the_spawn_unset() {
    let mut host = Host::start("theme-from-unknown");
    let child = host
        .spawn_with("theme_from", json!("no-such-pane"))
        .unwrap();
    let screen = host.wait_for(&child, "a first answer", |s| {
        s.lines()
            .any(|line| line.contains("A<") && line.contains('>'))
    });
    let answer = screen
        .lines()
        .find(|line| line.contains("A<") && line.contains('>'))
        .unwrap();
    assert_eq!(answer.trim(), "A<>", "{screen}");
}

#[test]
fn malformed_spawn_theme_is_refused() {
    let mut host = Host::start("theme-invalid");
    let refused = host.spawn(Some(json!({"background": "white"})));
    assert_eq!(refused, Err("invalid_terminal_theme".to_string()));
}

#[test]
fn declaring_before_attach_is_refused() {
    let host = Host::start("theme-unattached");
    let mut stream = connect(&host.frames_path);
    write_msg(
        &mut stream,
        &ClientMessage::Hello {
            version: PROTOCOL_VERSION,
            encoding: RenderEncoding::SemanticFrame,
            local_token: LOCAL.into(),
            cols: 80,
            rows: 24,
            tmux_identity: None,
        },
    );
    assert!(matches!(
        read_msg(&mut stream),
        ServerMessage::Welcome { .. }
    ));
    declare(&mut stream, &LIGHT);
    match read_msg(&mut stream) {
        ServerMessage::Error { code, .. } => assert_eq!(code, "attach_required"),
        other => panic!("{other:?}"),
    }
}
