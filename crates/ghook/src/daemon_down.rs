//! Spawned sessions pause on a refused daemon connection; operators can repair it.

use crate::action::{HookAction, continue_action, is_codex_interrupt};
use crate::cli_config::CliConfig;
use crate::planned_shutdown::{is_stop_hook, restart_in_progress};
use crate::transport::DeliveryFailureKind;

pub(crate) fn action(
    hook_type: &str,
    cfg: &CliConfig,
    failure_kind: DeliveryFailureKind,
) -> Option<HookAction> {
    if !cfg.spawned_agent
        || !matches!(failure_kind, DeliveryFailureKind::Connect)
        || is_codex_interrupt(cfg.source, hook_type)
    {
        return None;
    }
    // Claude ends the turn on a batch hook's exit 2. During a restart the turn
    // continues instead; the next tool call still pauses at its pre-tool hook.
    let restart_gap = hook_type == "post-tool-batch" && restart_in_progress();
    Some(if is_stop_like_hook(hook_type) || restart_gap {
        continue_action(cfg.source, hook_type)
    } else {
        HookAction {
            exit_code: 2,
            stdout_json: None,
            stderr_message: Some("[Gobby] Daemon down. Please wait.".to_string()),
        }
    })
}

fn is_stop_like_hook(hook_type: &str) -> bool {
    is_stop_hook(hook_type)
        || matches!(
            hook_type,
            "subagent-stop" | "SubagentStop" | "subagent_stop"
        )
}
