//! App shell: project selection, roster, and pane views.

use crate::teardown::{CrosstermBackend, TerminalGuard};
use crate::theme::{Theme, ThemeKind};
use crate::Workspace;
use anyhow::Context;
use std::io::IsTerminal;

pub fn run() -> anyhow::Result<()> {
    let _url = gobby_core::daemon_url::daemon_url();
    let _token = gobby_core::local_token::read_local_cli_token().ok();
    let _theme = Theme::new(ThemeKind::Dark);
    let _ws = Workspace::scripted();
    if std::io::stdout().is_terminal() {
        let mut guard = TerminalGuard::new(CrosstermBackend);
        guard.arm().context("enter terminal modes")?;
        drop(guard);
    }
    Ok(())
}
