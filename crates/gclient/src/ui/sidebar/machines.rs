// upstream: none (Gobby's machine axis; herdr runs on one machine)
//! The machines section: the hub (this machine) first, the other machines
//! nested under it, each a state dot (the most urgent of its agents) and
//! the machine's name. The highlighted rows are the machines the agents
//! section lists; a click on a row moves the filter there (`Hit::Machine`),
//! and View › Sidebar › Machines sets it to this machine or every machine.

use std::sync::OnceLock;

use super::agents::agent_state;
use super::{render_band, render_section_rows, SidebarHits, ALL_MACHINES};
use crate::app::short_terminal_id;
use crate::app::sidebar_model::rollup;
use crate::ui::chrome::{Chrome, WorkspaceView};
use crate::ui::hit::SidebarSection;
use crate::ui::sidebar_rows::{RowKind, SidebarRow};
use ratatui::layout::Rect;
use ratatui::Frame;

/// Names this machine's row instead of the probed host name; the test
/// runs set it (`.cargo/config.toml`) so rendered frames stay the same on
/// every machine.
pub const HOSTNAME_ENV: &str = "GOBBY_CLIENT_HOSTNAME";

/// This machine's row label: `HOSTNAME_ENV` when set, else the host name
/// without its domain, probed once per process; `None` where the platform
/// gives none.
pub fn local_hostname() -> Option<&'static str> {
    static HOSTNAME: OnceLock<Option<String>> = OnceLock::new();
    HOSTNAME
        .get_or_init(|| {
            std::env::var(HOSTNAME_ENV)
                .ok()
                .map(|name| name.trim().to_string())
                .filter(|name| !name.is_empty())
                .or_else(probe_hostname)
        })
        .as_deref()
}

#[cfg(unix)]
fn probe_hostname() -> Option<String> {
    let mut buffer = [0_u8; 256];
    // SAFETY: `gethostname` writes at most `buffer.len()` bytes into the
    // buffer lent for the call and nothing else; a longer name is cut and
    // the terminator written below bounds the read either way.
    let status = unsafe { libc::gethostname(buffer.as_mut_ptr().cast(), buffer.len()) };
    if status != 0 {
        return None;
    }
    buffer[buffer.len() - 1] = 0;
    let name = std::ffi::CStr::from_bytes_until_nul(&buffer)
        .ok()?
        .to_str()
        .ok()?;
    let name = name.split('.').next().unwrap_or(name).trim();
    (!name.is_empty()).then(|| name.to_string())
}

#[cfg(not(unix))]
fn probe_hostname() -> Option<String> {
    None
}

/// The rows: this machine as the hub, then the others nested under it in
/// model order. Each carries the most urgent state of the agents running
/// on it, its name (the host name here, the short id elsewhere), and the
/// `local`/`all` mark of the filter; the rows the filter admits are
/// `active`.
pub fn machine_rows<W: WorkspaceView>(ws: &W, chrome: &Chrome) -> Vec<SidebarRow> {
    let model = ws.sidebar();
    let filter = chrome.sidebar.machine_filter.as_deref();
    let local = model.local_machine.as_str();
    let nodes: Vec<&str> = model
        .machines
        .iter()
        .map(String::as_str)
        .filter(|machine| *machine != local)
        .collect();
    let count = nodes.len();
    (!local.is_empty())
        .then_some((local, None))
        .into_iter()
        .chain(
            nodes.into_iter().enumerate().map(|(index, machine)| {
                (machine, (!local.is_empty()).then_some(index + 1 == count))
            }),
        )
        .map(|(machine, nested)| {
            let is_local = machine == local;
            let state = rollup(
                model
                    .agents
                    .iter()
                    .filter(|agent| agent.machine_id == machine)
                    .map(|agent| agent_state(ws, agent)),
            );
            let label = if is_local {
                local_hostname().unwrap_or_else(|| short_terminal_id(machine))
            } else {
                short_terminal_id(machine)
            };
            let active = match filter {
                Some(ALL_MACHINES) => true,
                Some(selected) => selected == machine,
                None => local.is_empty() || is_local,
            };
            SidebarRow {
                id: machine.to_string(),
                label: label.to_string(),
                kind: RowKind::Machine,
                state,
                nested: nested.is_some(),
                last_child: nested.unwrap_or(false),
                active,
                ..SidebarRow::default()
            }
        })
        .collect()
}

/// Draw the section into `area` (the content rect, without the separator
/// column) and record its hits.
pub(super) fn render_machines(
    frame: &mut Frame,
    area: Rect,
    rows: &[SidebarRow],
    chrome: &Chrome,
    hits: &mut SidebarHits,
) {
    let section = SidebarSection::Machines;
    render_band(frame, area, section.title(), &chrome.palette);
    render_section_rows(frame, area, section, rows, chrome, hits);
}
