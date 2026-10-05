//! The named themes, picked at runtime under View › Theme and in Settings.
//! A theme draws in every appearance; the appearance (dark, light or
//! system) is a preference of its own.

use serde::{Deserialize, Serialize};

/// A shipped theme, saved as `[ui] palette` in prefs.toml.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum ThemeName {
    /// The design-contract palette as it stands.
    #[default]
    Classic,
}

impl ThemeName {
    /// Every shipped theme, in the order the pickers list them.
    pub const ALL: [ThemeName; 1] = [ThemeName::Classic];

    pub fn label(self) -> &'static str {
        match self {
            ThemeName::Classic => "Classic",
        }
    }
}
