//! Client-local preference persistence: `~/.gobby/client/prefs.toml`.
//!
//! The file groups [`ClientPrefs`] into `[ui]` and `[keymap]` tables. Every
//! key is optional and unknown keys are rejected by name, so a typo never
//! silently falls back to a default.

use crate::ui::settings::ClientPrefs;
use serde::{Deserialize, Serialize};
use std::fs::{self, OpenOptions};
use std::io::{self, Write};
use std::path::{Path, PathBuf};
use thiserror::Error;
use uuid::Uuid;

/// Prefs file location relative to the gobby home.
pub const PREFS_FILE: &str = "client/prefs.toml";

#[derive(Debug, Error)]
pub enum PrefsError {
    #[error(transparent)]
    Io(#[from] io::Error),
    #[error("{0}")]
    Parse(String),
}

/// On-disk shape: `[ui]` holds the knobs the settings overlay edits, `[keymap]`
/// the override path. `layout` is not persisted.
#[derive(Debug, Default, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
struct PrefsFile {
    ui: UiPrefs,
    keymap: KeymapPrefs,
}

#[derive(Debug, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
struct UiPrefs {
    theme: String,
    mouse_capture: bool,
    pane_borders: bool,
    pane_scrollbars: bool,
    pane_gaps: bool,
    confirm_close: bool,
    hide_tab_bar_when_single_tab: bool,
    sidebar_width: u16,
}

impl Default for UiPrefs {
    fn default() -> Self {
        Self::from(&ClientPrefs::default())
    }
}

impl From<&ClientPrefs> for UiPrefs {
    fn from(prefs: &ClientPrefs) -> Self {
        Self {
            theme: prefs.theme.clone(),
            mouse_capture: prefs.mouse_capture,
            pane_borders: prefs.pane_borders,
            pane_scrollbars: prefs.pane_scrollbars,
            pane_gaps: prefs.pane_gaps,
            confirm_close: prefs.confirm_close,
            hide_tab_bar_when_single_tab: prefs.hide_tab_bar_when_single_tab,
            sidebar_width: prefs.sidebar_width,
        }
    }
}

#[derive(Debug, Default, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
struct KeymapPrefs {
    /// Keymap override file; empty means the default path.
    path: String,
}

impl From<&ClientPrefs> for PrefsFile {
    fn from(prefs: &ClientPrefs) -> Self {
        Self {
            ui: UiPrefs::from(prefs),
            keymap: KeymapPrefs {
                path: prefs.keybinds.clone(),
            },
        }
    }
}

impl From<PrefsFile> for ClientPrefs {
    fn from(file: PrefsFile) -> Self {
        let PrefsFile { ui, keymap } = file;
        Self {
            theme: ui.theme,
            keybinds: keymap.path,
            mouse_capture: ui.mouse_capture,
            pane_borders: ui.pane_borders,
            pane_scrollbars: ui.pane_scrollbars,
            pane_gaps: ui.pane_gaps,
            confirm_close: ui.confirm_close,
            hide_tab_bar_when_single_tab: ui.hide_tab_bar_when_single_tab,
            sidebar_width: ui.sidebar_width,
            ..Self::default()
        }
    }
}

pub fn prefs_path(gobby_home: &Path) -> PathBuf {
    gobby_home.join(PREFS_FILE)
}

/// Loads the prefs file; a missing file yields [`ClientPrefs::default`].
pub fn load_prefs(gobby_home: &Path) -> Result<ClientPrefs, PrefsError> {
    let path = prefs_path(gobby_home);
    let text = match fs::read_to_string(&path) {
        Ok(text) => text,
        Err(error) if error.kind() == io::ErrorKind::NotFound => {
            return Ok(ClientPrefs::default());
        }
        Err(error) => return Err(error.into()),
    };
    let file: PrefsFile = toml::from_str(&text).map_err(|error| parse_error(&text, &error))?;
    Ok(file.into())
}

/// Writes the prefs atomically (temp file, then rename) and returns the path.
pub fn save_prefs(gobby_home: &Path, prefs: &ClientPrefs) -> Result<PathBuf, PrefsError> {
    let path = prefs_path(gobby_home);
    let text = toml::to_string(&PrefsFile::from(prefs)).map_err(io::Error::other)?;
    let parent = path
        .parent()
        .ok_or_else(|| io::Error::other("prefs path has no parent directory"))?;
    fs::create_dir_all(parent)?;
    let tmp = parent.join(format!(".prefs.toml.{}.tmp", Uuid::new_v4()));
    let result = (|| {
        let mut file = OpenOptions::new().write(true).create_new(true).open(&tmp)?;
        file.write_all(text.as_bytes())?;
        file.sync_all()?;
        fs::rename(&tmp, &path)
    })();
    if let Err(error) = result {
        let _ = fs::remove_file(&tmp);
        return Err(error.into());
    }
    Ok(path)
}

fn parse_error(text: &str, error: &toml::de::Error) -> PrefsError {
    let message = error.message();
    match error.span() {
        Some(span) => {
            let line = text[..span.start.min(text.len())].matches('\n').count() + 1;
            PrefsError::Parse(format!("line {line}: {message}"))
        }
        None => PrefsError::Parse(message.to_string()),
    }
}
