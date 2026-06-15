use crate::{diagnose, envelope, output, transport};
use anyhow::Result;

pub(crate) fn write_runtime_stamp() -> Result<()> {
    let bin_dir = gobby_core::gobby_home()?.join("bin");
    std::fs::create_dir_all(&bin_dir)?;
    let stamp_path = bin_dir.join(".ghook-runtime.json");
    let stamp = serde_json::json!({
        "schema_version": envelope::SCHEMA_VERSION,
        "ghook_version": diagnose::GHOOK_VERSION,
    });
    let bytes = serde_json::to_vec_pretty(&stamp)?;
    transport::atomic_write(&stamp_path, &bytes)?;
    output::stdout(format_args!("ghook {}\n", diagnose::GHOOK_VERSION));
    Ok(())
}
