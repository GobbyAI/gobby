use super::{call, prepare};
use crate::daemon::Dialer;
use serde_json::{Map, Value, json};
use std::future::Ready;
use std::sync::Arc;

const REMOVED_WAIT_ERROR: &str = "gobby-workflows:wait_for_completion was removed. Start the agent or pipeline, persist its run_id or execution_id, then resume from the daemon's durable completion notification and inspect get_task, get_agent_result, or get_pipeline_status.";

#[test]
fn removed_wait_resolves_to_split_server_and_tool() -> anyhow::Result<()> {
    let inputs = [
        json!({"server_name": "gobby-workflows", "tool_name": "wait_for_completion",
            "arguments": {"completion_id": "run-1"}}),
        json!({"arguments": {"server_name": "gobby-workflows", "tool_name": "wait_for_completion",
            "arguments": {"completion_id": "run-1"}}}),
    ];
    for input in &inputs {
        let prepared = prepare(input)?;
        assert_eq!(prepared.server_name, "gobby-workflows");
        assert_eq!(prepared.tool_name, "wait_for_completion");
    }
    Ok(())
}

#[test]
fn removed_wait_returns_tool_removed_with_python_contract_text() -> anyhow::Result<()> {
    let home = tempfile::tempdir()?;
    std::fs::write(home.path().join("bootstrap.yaml"), "api_key: test\n")?;
    temp_env::with_vars(
        [
            ("GOBBY_HOME", home.path().to_str()),
            ("GOBBY_DAEMON_URL", Some("http://127.0.0.1:9")),
            ("GOBBY_MANAGED_EXECUTION_BOOTSTRAP", None),
            ("GOBBY_MANAGED_EXECUTION_ID", None),
            ("GOBBY_AGENT_API_TOKEN", None),
            ("GOBBY_AGENT_RUN_ID", None),
            ("GOBBY_SESSION_ID", None),
            ("GOBBY_PROJECT_ID", None),
            ("GOBBY_RELEASE_TOKEN", None),
        ],
        || -> anyhow::Result<()> {
            let dialer = Arc::new(Dialer::with_context(home.path(), Map::new())?);
            let runtime = tokio::runtime::Builder::new_current_thread()
                .enable_all()
                .build()?;
            let input = json!({"server_name": "gobby-workflows",
                "tool_name": "wait_for_completion", "arguments": {"completion_id": "run-1"}});
            let result: Value =
                runtime.block_on(call(dialer, input, None::<fn(Value) -> Ready<()>>));
            assert_eq!(result["success"], false);
            assert_eq!(result["error_code"], "TOOL_REMOVED");
            assert_eq!(result["server_name"], "gobby-workflows");
            assert_eq!(result["tool_name"], "wait_for_completion");
            assert_eq!(result["error"], REMOVED_WAIT_ERROR);
            Ok(())
        },
    )
}
