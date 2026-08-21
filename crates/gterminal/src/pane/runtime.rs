//! PTY-backed pane runtime with no embedded agent detection.

use std::cell::Cell;
use std::sync::{
    atomic::{AtomicBool, AtomicU16, AtomicU32, Ordering},
    Arc, Mutex,
};

use bytes::Bytes;
use portable_pty::CommandBuilder;
use tokio::sync::{mpsc, Notify};
use tracing::{debug, error, warn};

use crate::layout::PaneId;
use crate::pty::actor::{PtyIoActor, PtyIoActorConfig, PtyIoActorHandle, PtyReadResult};

use super::shell::{
    apply_pane_launch_env, apply_pane_terminal_env, pane_shell_command_builder, PaneLaunchEnv,
    PaneShellConfig,
};
use super::shutdown::shutdown_pane_processes;
use super::terminal::{GhosttyPaneTerminal, PaneTerminal};

/// PTY runtime for a pane. Owns the terminal, I/O channels, and background tasks.
/// Dropping this shuts down background tasks and closes the PTY.
pub struct PaneRuntime {
    pane_id: PaneId,
    terminal: Arc<PaneTerminal>,
    io: PaneRuntimeIo,
    current_size: Cell<(u16, u16, u32, u32)>,
    child_pid: Arc<AtomicU32>,
    reported_cwd: Arc<Mutex<Option<std::path::PathBuf>>>,
    child_wait_completed: Option<Arc<AtomicBool>>,
    kitty_keyboard_flags: Arc<AtomicU16>,
    preserve_processes_on_drop: bool,
    render_notify: Arc<Notify>,
}

enum PaneRuntimeIo {
    Actor(PtyIoActorHandle),
    #[cfg(test)]
    TestChannel {
        sender: mpsc::Sender<Bytes>,
        resize_tx: tokio::sync::watch::Sender<(u16, u16, u32, u32)>,
    },
}

impl PaneRuntimeIo {
    fn shutdown(&self) {
        match self {
            PaneRuntimeIo::Actor(actor) => actor.shutdown(),
            #[cfg(test)]
            PaneRuntimeIo::TestChannel { .. } => {}
        }
    }

    #[cfg(unix)]
    fn duplicate_handoff_fd(&self) -> std::io::Result<std::os::fd::RawFd> {
        match self {
            PaneRuntimeIo::Actor(actor) => actor.duplicate_for_handoff(),
            #[cfg(test)]
            PaneRuntimeIo::TestChannel { .. } => {
                Err(std::io::Error::other("test runtime has no PTY master fd"))
            }
        }
    }

    #[cfg(unix)]
    fn foreground_process_group_id(&self) -> Option<u32> {
        match self {
            PaneRuntimeIo::Actor(actor) => actor.foreground_process_group_id(),
            #[cfg(test)]
            PaneRuntimeIo::TestChannel { .. } => None,
        }
    }

    #[cfg(unix)]
    fn begin_handoff(&self, timeout: std::time::Duration) -> std::io::Result<()> {
        match self {
            PaneRuntimeIo::Actor(actor) => actor.begin_handoff(timeout),
            #[cfg(test)]
            PaneRuntimeIo::TestChannel { .. } => Ok(()),
        }
    }

    #[cfg(unix)]
    fn set_handoff_paused(&self, paused: bool) -> std::io::Result<()> {
        match self {
            PaneRuntimeIo::Actor(actor) => {
                if paused {
                    actor.begin_handoff(std::time::Duration::from_secs(1))
                } else {
                    actor.rollback_handoff()
                }
            }
            #[cfg(test)]
            PaneRuntimeIo::TestChannel { .. } => Ok(()),
        }
    }

    #[cfg(unix)]
    fn release_after_commit(&self) -> std::io::Result<()> {
        match self {
            PaneRuntimeIo::Actor(actor) => actor.release_after_commit(),
            #[cfg(test)]
            PaneRuntimeIo::TestChannel { .. } => Ok(()),
        }
    }

    fn resize(
        &self,
        rows: u16,
        cols: u16,
        cell_width_px: u32,
        cell_height_px: u32,
        terminal_responses: Vec<Bytes>,
    ) {
        match self {
            PaneRuntimeIo::Actor(actor) => {
                actor.resize(
                    rows,
                    cols,
                    cell_width_px,
                    cell_height_px,
                    terminal_responses,
                );
            }
            #[cfg(test)]
            PaneRuntimeIo::TestChannel { resize_tx, .. } => {
                let _ = resize_tx.send((rows, cols, cell_width_px, cell_height_px));
            }
        }
    }

    #[cfg(unix)]
    fn nudge_child_redraw_after_handoff(
        &self,
        rows: u16,
        cols: u16,
        cell_width_px: u32,
        cell_height_px: u32,
    ) {
        match self {
            PaneRuntimeIo::Actor(actor) => {
                actor.nudge_child_redraw_after_handoff(rows, cols, cell_width_px, cell_height_px);
            }
            #[cfg(test)]
            PaneRuntimeIo::TestChannel { .. } => {}
        }
    }

    async fn send_bytes(&self, bytes: Bytes) -> Result<(), mpsc::error::SendError<Bytes>> {
        match self {
            PaneRuntimeIo::Actor(actor) => actor.write_user_input(bytes).await,
            #[cfg(test)]
            PaneRuntimeIo::TestChannel { sender, .. } => sender.send(bytes).await,
        }
    }

    fn try_send_bytes(&self, bytes: Bytes) -> Result<(), mpsc::error::TrySendError<Bytes>> {
        match self {
            PaneRuntimeIo::Actor(actor) => actor.try_write_user_input(bytes),
            #[cfg(test)]
            PaneRuntimeIo::TestChannel { sender, .. } => sender.try_send(bytes),
        }
    }

    fn write_terminal_response(&self, response: impl FnOnce() -> Option<Bytes>) {
        match self {
            PaneRuntimeIo::Actor(actor) => actor.write_terminal_response(response),
            #[cfg(test)]
            PaneRuntimeIo::TestChannel { sender, .. } => {
                if let Some(bytes) = response() {
                    let _ = sender.try_send(bytes);
                }
            }
        }
    }

    fn send_bytes_after(&self, bytes: Bytes, delay: std::time::Duration) {
        match self {
            PaneRuntimeIo::Actor(actor) => {
                let actor = actor.clone();
                tokio::spawn(async move {
                    tokio::time::sleep(delay).await;
                    if let Err(err) = actor.write_user_input(bytes).await {
                        warn!(error = %err, "failed to send delayed PTY input");
                    }
                });
            }
            #[cfg(test)]
            PaneRuntimeIo::TestChannel { sender, .. } => {
                let sender = sender.clone();
                tokio::spawn(async move {
                    tokio::time::sleep(delay).await;
                    let _ = sender.send(bytes).await;
                });
            }
        }
    }
}

impl Drop for PaneRuntime {
    fn drop(&mut self) {
        self.io.shutdown();
        if !self.preserve_processes_on_drop {
            shutdown_pane_processes(
                self.pane_id,
                self.child_pid.load(Ordering::Acquire),
                self.child_wait_completed.as_deref(),
            );
        }
    }
}

impl PaneRuntime {
    pub fn shutdown(mut self) {
        self.io.shutdown();
        shutdown_pane_processes(
            self.pane_id,
            self.child_pid.load(Ordering::Acquire),
            self.child_wait_completed.as_deref(),
        );
        self.preserve_processes_on_drop = true;
    }

    pub fn kill(self) {
        self.shutdown();
    }

    #[allow(clippy::too_many_arguments)]
    pub fn spawn(
        rows: u16,
        cols: u16,
        cwd: std::path::PathBuf,
        scrollback_limit_bytes: usize,
        host_terminal_theme: crate::terminal_theme::TerminalTheme,
        host_terminal_appearance: Option<crate::terminal_theme::HostAppearance>,
        shell_config: PaneShellConfig<'_>,
    ) -> std::io::Result<Self> {
        Self::spawn_with_launch_env(
            rows,
            cols,
            cwd,
            scrollback_limit_bytes,
            host_terminal_theme,
            host_terminal_appearance,
            shell_config,
            &PaneLaunchEnv::default(),
            None,
        )
    }

    #[allow(clippy::too_many_arguments)]
    pub fn spawn_with_launch_env(
        rows: u16,
        cols: u16,
        cwd: std::path::PathBuf,
        scrollback_limit_bytes: usize,
        host_terminal_theme: crate::terminal_theme::TerminalTheme,
        host_terminal_appearance: Option<crate::terminal_theme::HostAppearance>,
        shell_config: PaneShellConfig<'_>,
        launch_env: &PaneLaunchEnv,
        initial_history_ansi: Option<&str>,
    ) -> std::io::Result<Self> {
        let windows_powershell_prompt_cwd_reporting =
            super::shell::uses_windows_powershell_pane_shell(shell_config);
        let mut cmd = pane_shell_command_builder(shell_config)?;
        cmd.cwd(cwd);
        apply_pane_terminal_env(&mut cmd);
        apply_pane_launch_env(&mut cmd, launch_env);
        Self::spawn_command_builder(
            rows,
            cols,
            scrollback_limit_bytes,
            host_terminal_theme,
            host_terminal_appearance,
            cmd,
            "failed to spawn shell",
            initial_history_ansi,
            windows_powershell_prompt_cwd_reporting,
        )
    }

    #[allow(clippy::too_many_arguments)]
    pub fn spawn_argv_command(
        rows: u16,
        cols: u16,
        cwd: std::path::PathBuf,
        argv: &[String],
        launch_env: &PaneLaunchEnv,
        scrollback_limit_bytes: usize,
        host_terminal_theme: crate::terminal_theme::TerminalTheme,
        host_terminal_appearance: Option<crate::terminal_theme::HostAppearance>,
    ) -> std::io::Result<Self> {
        let Some((program, args)) = argv.split_first() else {
            return Err(std::io::Error::new(
                std::io::ErrorKind::InvalidInput,
                "argv must not be empty",
            ));
        };
        let mut cmd = CommandBuilder::new(program);
        for arg in args {
            cmd.arg(arg);
        }
        cmd.cwd(cwd);
        apply_pane_terminal_env(&mut cmd);
        apply_pane_launch_env(&mut cmd, launch_env);
        Self::spawn_command_builder(
            rows,
            cols,
            scrollback_limit_bytes,
            host_terminal_theme,
            host_terminal_appearance,
            cmd,
            "failed to spawn argv command pane",
            None,
            false,
        )
    }

    #[allow(clippy::too_many_arguments)]
    fn spawn_command_builder(
        rows: u16,
        cols: u16,
        scrollback_limit_bytes: usize,
        host_terminal_theme: crate::terminal_theme::TerminalTheme,
        host_terminal_appearance: Option<crate::terminal_theme::HostAppearance>,
        cmd: CommandBuilder,
        spawn_error_message: &'static str,
        initial_history_ansi: Option<&str>,
        windows_powershell_prompt_cwd_reporting: bool,
    ) -> std::io::Result<Self> {
        let pane_id = PaneId::alloc();
        let (response_tx, _response_rx) = mpsc::channel::<Bytes>(1);
        let mut terminal = crate::ghostty::Terminal::new(cols, rows, scrollback_limit_bytes)
            .map_err(|e| std::io::Error::other(e.to_string()))?;
        if crate::kitty_graphics::is_enabled() {
            terminal
                .enable_kitty_graphics()
                .map_err(|e| std::io::Error::other(e.to_string()))?;
        }
        let pane_terminal = GhosttyPaneTerminal::new(terminal, response_tx.clone())?;
        pane_terminal.apply_host_terminal_theme(host_terminal_theme);
        let _ = pane_terminal.apply_host_terminal_appearance(host_terminal_appearance);
        pane_terminal
            .set_windows_powershell_prompt_cwd_reporting(windows_powershell_prompt_cwd_reporting);
        if let Some(ansi) = initial_history_ansi {
            pane_terminal.seed_history_ansi(ansi);
        }
        let terminal = Arc::new(PaneTerminal::new(pane_terminal));
        let kitty_keyboard_flags = Arc::new(AtomicU16::new(0));
        let render_notify = Arc::new(Notify::new());

        let spawned = crate::pty::backend::spawn_with_portable_pty(rows, cols, cmd)
            .inspect_err(|err| error!(pane = pane_id.raw(), err = %err, "{spawn_error_message}"))?;

        let child_pid = Arc::new(AtomicU32::new(0));
        let reported_cwd = Arc::new(Mutex::new(None));
        let child_wait_completed = Arc::new(AtomicBool::new(false));
        {
            let child_pid = child_pid.clone();
            let child_wait_completed = child_wait_completed.clone();
            let mut child = spawned.child;
            if let Some(pid) = child.process_id() {
                child_pid.store(pid, Ordering::Release);
                debug!(pane = pane_id.raw(), pid, "spawned pane PTY");
            }
            tokio::task::spawn_blocking(move || {
                match child.wait() {
                    Ok(status) => debug!(pane = pane_id.raw(), ?status, "pane child exited"),
                    Err(err) => error!(pane = pane_id.raw(), err = %err, "pane child wait failed"),
                }
                child_wait_completed.store(true, Ordering::Release);
            });
        }

        let io = {
            let terminal = terminal.clone();
            let response_writer = response_tx.clone();
            let render_notify = render_notify.clone();
            let child_pid = child_pid.clone();
            let reported_cwd = reported_cwd.clone();
            let rt = tokio::runtime::Handle::current();
            let on_read = Box::new(move |bytes: &[u8]| {
                let shell_pid = child_pid.load(Ordering::Acquire);
                let result =
                    terminal.process_pty_bytes(pane_id, shell_pid, bytes, &response_writer);
                if result.request_render {
                    render_notify.notify_one();
                }
                if let Some(delay) = result.render_delay {
                    let render_notify = render_notify.clone();
                    rt.spawn(async move {
                        tokio::time::sleep(delay).await;
                        render_notify.notify_one();
                    });
                }
                if let Some(cwd) = result.reported_cwd.clone() {
                    if let Ok(mut reported) = reported_cwd.lock() {
                        *reported = Some(cwd);
                    }
                }
                for content in result.clipboard_writes {
                    let _ = crate::platform::write_clipboard(&content);
                }
                PtyReadResult {
                    terminal_responses: result.terminal_responses,
                }
            });
            PaneRuntimeIo::Actor(PtyIoActor::spawn(PtyIoActorConfig {
                pane_id: pane_id.raw(),
                #[cfg(unix)]
                master_fd: spawned.master_fd,
                #[cfg(windows)]
                master: spawned.master,
                initially_quiesced: false,
                on_read,
                on_reader_exit: None,
            })?)
        };

        Ok(Self {
            pane_id,
            terminal,
            io,
            current_size: Cell::new((rows, cols, 0, 0)),
            child_pid,
            reported_cwd,
            child_wait_completed: Some(child_wait_completed),
            kitty_keyboard_flags,
            preserve_processes_on_drop: false,
            render_notify,
        })
    }

    /// Attach Ghostty + the PTY actor to an already-forked master fd (exec barrier).
    #[cfg(unix)]
    pub fn from_master_fd(
        rows: u16,
        cols: u16,
        scrollback_limit_bytes: usize,
        host_terminal_theme: crate::terminal_theme::TerminalTheme,
        host_terminal_appearance: Option<crate::terminal_theme::HostAppearance>,
        master_fd: std::os::fd::OwnedFd,
        child_pid_value: u32,
    ) -> std::io::Result<Self> {
        let pane_id = PaneId::alloc();
        let (response_tx, _response_rx) = mpsc::channel::<Bytes>(1);
        let mut terminal = crate::ghostty::Terminal::new(cols, rows, scrollback_limit_bytes)
            .map_err(|e| std::io::Error::other(e.to_string()))?;
        if crate::kitty_graphics::is_enabled() {
            terminal
                .enable_kitty_graphics()
                .map_err(|e| std::io::Error::other(e.to_string()))?;
        }
        let pane_terminal = GhosttyPaneTerminal::new(terminal, response_tx.clone())?;
        pane_terminal.apply_host_terminal_theme(host_terminal_theme);
        let _ = pane_terminal.apply_host_terminal_appearance(host_terminal_appearance);
        let terminal = Arc::new(PaneTerminal::new(pane_terminal));
        let kitty_keyboard_flags = Arc::new(AtomicU16::new(0));
        let render_notify = Arc::new(Notify::new());
        let child_pid = Arc::new(AtomicU32::new(child_pid_value));
        let reported_cwd = Arc::new(Mutex::new(None));
        let child_wait_completed = Arc::new(AtomicBool::new(false));
        {
            let child_wait_completed = child_wait_completed.clone();
            let pid = child_pid_value as i32;
            tokio::task::spawn_blocking(move || {
                let mut status = 0;
                unsafe {
                    libc::waitpid(pid, &mut status, 0);
                }
                child_wait_completed.store(true, Ordering::Release);
            });
        }
        let io = {
            let terminal = terminal.clone();
            let response_writer = response_tx.clone();
            let render_notify = render_notify.clone();
            let child_pid = child_pid.clone();
            let reported_cwd = reported_cwd.clone();
            let rt = tokio::runtime::Handle::current();
            let on_read = Box::new(move |bytes: &[u8]| {
                let shell_pid = child_pid.load(Ordering::Acquire);
                let result =
                    terminal.process_pty_bytes(pane_id, shell_pid, bytes, &response_writer);
                if result.request_render {
                    render_notify.notify_one();
                }
                if let Some(delay) = result.render_delay {
                    let render_notify = render_notify.clone();
                    rt.spawn(async move {
                        tokio::time::sleep(delay).await;
                        render_notify.notify_one();
                    });
                }
                if let Some(cwd) = result.reported_cwd.clone() {
                    if let Ok(mut reported) = reported_cwd.lock() {
                        *reported = Some(cwd);
                    }
                }
                for content in result.clipboard_writes {
                    let _ = crate::platform::write_clipboard(&content);
                }
                PtyReadResult {
                    terminal_responses: result.terminal_responses,
                }
            });
            PaneRuntimeIo::Actor(PtyIoActor::spawn(PtyIoActorConfig {
                pane_id: pane_id.raw(),
                master_fd,
                initially_quiesced: false,
                on_read,
                on_reader_exit: None,
            })?)
        };
        Ok(Self {
            pane_id,
            terminal,
            io,
            current_size: Cell::new((rows, cols, 0, 0)),
            child_pid,
            reported_cwd,
            child_wait_completed: Some(child_wait_completed),
            kitty_keyboard_flags,
            preserve_processes_on_drop: false,
            render_notify,
        })
    }
}

include!("runtime_ops.rs");

#[cfg(test)]
impl PaneRuntime {
    pub(crate) fn test_with_channel(cols: u16, rows: u16) -> (Self, mpsc::Receiver<Bytes>) {
        Self::test_with_channel_and_scrollback_bytes(cols, rows, 0, &[], 4)
    }

    pub(crate) fn test_with_channel_and_scrollback_bytes(
        cols: u16,
        rows: u16,
        scrollback_limit_bytes: usize,
        bytes: &[u8],
        channel_capacity: usize,
    ) -> (Self, mpsc::Receiver<Bytes>) {
        let (tx, rx) = mpsc::channel(channel_capacity);
        let (resize_tx, _resize_rx) = tokio::sync::watch::channel((rows, cols, 0, 0));
        let mut terminal =
            crate::ghostty::Terminal::new(cols, rows, scrollback_limit_bytes).unwrap();
        terminal.write(bytes);

        (
            Self {
                pane_id: PaneId::from_raw(0),
                terminal: Arc::new(PaneTerminal::new(
                    GhosttyPaneTerminal::new(terminal, tx.clone()).unwrap(),
                )),
                io: PaneRuntimeIo::TestChannel {
                    sender: tx,
                    resize_tx,
                },
                current_size: Cell::new((rows, cols, 0, 0)),
                child_pid: Arc::new(AtomicU32::new(0)),
                reported_cwd: Arc::new(Mutex::new(None)),
                child_wait_completed: None,
                kitty_keyboard_flags: Arc::new(AtomicU16::new(0)),
                preserve_processes_on_drop: true,
                render_notify: Arc::new(Notify::new()),
            },
            rx,
        )
    }
}
