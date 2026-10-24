//! Spawned sessions pause on a refused daemon connection; operators can repair it.

use crate::action::{HookAction, continue_action};
use crate::cli_config::CliConfig;
use crate::planned_shutdown::is_stop_hook;
use crate::transport::DeliveryFailureKind;

pub(crate) fn action(
    hook_type: &str,
    cfg: &CliConfig,
    failure_kind: DeliveryFailureKind,
) -> Option<HookAction> {
    if !cfg.spawned_agent || !matches!(failure_kind, DeliveryFailureKind::Connect) {
        return None;
    }
    Some(if is_stop_hook(hook_type) {
        continue_action(cfg.source, hook_type)
    } else {
        HookAction {
            exit_code: 2,
            stdout_json: None,
            stderr_message: Some("[Gobby] Daemon down. Please wait.".to_string()),
        }
    })
}
