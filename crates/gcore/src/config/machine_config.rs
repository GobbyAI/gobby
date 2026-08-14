//! Grant-presenting client for non-capability runtime settings.

use std::collections::BTreeMap;
use std::time::Duration;

use serde::Deserialize;

use crate::grant::{GrantBundle, GrantError};

pub const RUNTIME_CONFIG_PATH: &str = "/api/runtime/config";

#[derive(Clone, Debug, PartialEq, Eq, Deserialize)]
pub struct MachineConfig {
    pub config_revision: i64,
    pub settings: BTreeMap<String, String>,
}

pub fn fetch_machine_config(
    base_url: &str,
    grant: &GrantBundle,
    bearer: Option<&str>,
    timeout: Duration,
) -> Result<MachineConfig, GrantError> {
    let settings = crate::grant::fetch_runtime_config(base_url, grant, bearer, timeout)?;
    Ok(MachineConfig {
        config_revision: settings.config_revision,
        settings: settings.settings,
    })
}
