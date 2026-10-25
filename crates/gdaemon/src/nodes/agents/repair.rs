//! Repairs a prepared or reused workspace so its provider can run in it.

mod claude_config;
mod hygiene;
mod seed;

use super::workspace::git_output;
use anyhow::{Context, Result, bail};
use serde_json::{Map, Value, json};
use std::{
    collections::HashSet,
    fs::{self, OpenOptions},
    io::{ErrorKind, Write},
    os::unix::fs::OpenOptionsExt,
    path::{Path, PathBuf},
    sync::atomic::{AtomicU64, Ordering},
    time::Duration,
};

/// Node-local inputs that repair reads from the environment in production.
/// Tests inject them so nothing touches the real home or the real `uv`.
pub struct RepairEnv<'a> {
    /// Gobby home (`~/.gobby`); `ghook` and `gmcp` resolve under `bin/`.
    pub home: &'a Path,
    /// Droid hook root: `GOBBY_HOOKS_DIR`, or `<home>/hooks` when unset.
    pub hooks_dir: &'a Path,
    /// The user's `~/.claude.json`, patched for the claude provider.
    pub claude_json: &'a Path,
    /// `uv` for the environment seed; `None` skips the seed.
    pub uv: Option<&'a Path>,
    /// Upper bound on the `uv sync` seed.
    pub seed_timeout: Duration,
}

/// The droid hook template, embedded so the node and the Python oracle share one copy.
const DROID_TEMPLATE: &str =
    include_str!("../../../../../src/gobby/install/droid/hooks-template.json");
/// Droid hook names in the order the Python installer writes them.
const DROID_HOOK_NAMES: [&str; 9] = [
    "PreToolUse",
    "PostToolUse",
    "UserPromptSubmit",
    "Notification",
    "Stop",
    "SubagentStop",
    "PreCompact",
    "SessionStart",
    "SessionEnd",
];
const GOBBY_OWNED: &str = "--gobby-owned";
const HOOK_PLACEHOLDER: &str = "__GOBBY_HOOK_COMMAND__";

/// Runs the five repair steps for `provider` in `path`, copying hooks from
/// `source_root`. Steps the Python oracle warns on only warn here. Malformed
/// project metadata or MCP config fails the call, as it aborts spawn there.
pub async fn repair(
    source_root: &Path,
    path: &Path,
    provider: &str,
    env: &RepairEnv<'_>,
) -> Result<()> {
    match provider {
        "droid" => warn_on_error("droid isolation hooks", write_droid_hooks(env.home, path)),
        _ => copy_provider_hooks(source_root, path, provider).await?,
    }
    project_marker(source_root, path).await?;
    warn_on_error(
        "python environment seed",
        seed::preseed(path, env.uv, env.seed_timeout).await,
    );
    mcp_config(path, provider, env).await?;
    warn_on_error("isolation git hygiene", hygiene::apply(path).await);
    Ok(())
}

fn warn_on_error(what: &str, result: Result<()>) {
    if let Err(error) = result {
        eprintln!("gdaemon node: {what} failed: {error:#}");
    }
}

/// Copies the claude or codex hook directory unless git tracks it in the workspace.
async fn copy_provider_hooks(source_root: &Path, path: &Path, provider: &str) -> Result<()> {
    let dir = match provider {
        "claude" => ".claude",
        "codex" => ".codex",
        _ => return Ok(()),
    };
    let source = source_root.join(dir);
    if !source.exists() {
        return Ok(());
    }
    let tracked: HashSet<String> = git_output(path, &["ls-files", "-z", "--", dir])
        .await
        .context("cannot inspect tracked provider files; refusing to copy parent hooks")?
        .split('\0')
        .filter(|entry| !entry.is_empty())
        .map(str::to_owned)
        .collect();
    if tracked.contains(dir) {
        return Ok(());
    }
    if let Err(error) = copy_tree(&source, &path.join(dir), dir, &tracked) {
        eprintln!("gdaemon node: copying {dir} hooks failed: {error:#}");
    }
    Ok(())
}

fn copy_tree(
    source: &Path,
    target: &Path,
    relative: &str,
    tracked: &HashSet<String>,
) -> std::io::Result<()> {
    fs::create_dir_all(target)?;
    for entry in fs::read_dir(source)? {
        let entry = entry?;
        let name = entry.file_name();
        let child = format!("{relative}/{}", name.to_string_lossy());
        if entry.file_type()?.is_dir() {
            copy_tree(&entry.path(), &target.join(&name), &child, tracked)?;
        } else if !tracked.contains(&child) {
            fs::copy(entry.path(), target.join(&name))?;
        }
    }
    Ok(())
}

/// Writes `.gobby/project.json` when missing and the isolation marker that pins
/// the parent checkout. The marker's base is the workspace's selected HEAD.
async fn project_marker(source_root: &Path, path: &Path) -> Result<()> {
    let source_project = source_root.join(".gobby/project.json");
    if !source_project.exists() {
        return Ok(());
    }
    let source_bytes =
        fs::read(&source_project).with_context(|| format!("read {}", source_project.display()))?;
    let source_data: Value = serde_json::from_slice(&source_bytes)
        .with_context(|| format!("parse {}", source_project.display()))?;
    let parent_id = source_data
        .get("id")
        .and_then(Value::as_str)
        .filter(|id| !id.is_empty())
        .with_context(|| {
            format!(
                "failed to write isolation marker in isolated environment {}: project id missing",
                path.display()
            )
        })?;
    let ancestor = fs::canonicalize(source_root)
        .with_context(|| format!("canonicalize {}", source_root.display()))?
        .display()
        .to_string();

    let marker_path = path.join(".gobby/isolation.json");
    let current = read_marker(&marker_path).filter(|marker| {
        marker.contains_key("base_commit")
            && marker.get("parent_project_path") == Some(&json!(ancestor))
            && marker.get("parent_project_id") == Some(&json!(parent_id))
    });
    let marker = match current {
        Some(marker) => marker,
        None => {
            let base = git_output(path, &["rev-parse", "HEAD"]).await?;
            let mut marker = Map::new();
            marker.insert("parent_project_path".into(), json!(ancestor));
            marker.insert("parent_project_id".into(), json!(parent_id));
            marker.insert("base_commit".into(), json!(base.trim()));
            marker.insert("ancestor_checkout_path".into(), json!(ancestor));
            marker.insert("ancestor_checkout_identity".into(), json!(parent_id));
            marker
        }
    };

    let target_project = path.join(".gobby/project.json");
    if !target_project.exists() {
        write_atomic(&target_project, &source_bytes)?;
    }
    write_atomic(&marker_path, &pretty(&Value::Object(marker))?)
}

fn read_marker(path: &Path) -> Option<Map<String, Value>> {
    let bytes = fs::read(path).ok()?;
    match serde_json::from_slice(&bytes).ok()? {
        Value::Object(marker) => Some(marker),
        _ => None,
    }
}

/// Points the workspace's `gobby` MCP entry at the managed `gmcp`, and for claude
/// registers that entry under the workspace's project in `~/.claude.json`.
async fn mcp_config(path: &Path, provider: &str, env: &RepairEnv<'_>) -> Result<()> {
    let entry = json!({"command": env.home.join("bin/gmcp").display().to_string(), "args": []});
    let mcp_path = path.join(".mcp.json");
    let mut selected = load_json_object(&mcp_path)?;
    let servers = selected
        .entry("mcpServers")
        .or_insert_with(|| json!({}))
        .as_object_mut()
        .context("MCP servers configuration must be an object")?;
    servers.insert("gobby".into(), entry.clone());
    if let Err(error) = write_atomic(&mcp_path, &pretty(&Value::Object(selected))?) {
        eprintln!("gdaemon node: writing .mcp.json failed: {error:#}");
        return Ok(());
    }
    if provider != "claude" {
        return Ok(());
    }

    let config = env.claude_json.to_owned();
    let project = path.display().to_string();
    let result = tokio::task::spawn_blocking(move || {
        claude_config::update(&config, |data| {
            let projects = data
                .entry("projects")
                .or_insert_with(|| json!({}))
                .as_object_mut()
                .context("Claude projects configuration must be an object")?;
            let workspace = projects
                .entry(project)
                .or_insert_with(|| json!({}))
                .as_object_mut()
                .context("Claude workspace configuration must be an object")?;
            let servers = workspace
                .entry("mcpServers")
                .or_insert_with(|| json!({}))
                .as_object_mut()
                .context("Claude MCP server configuration must be an object")?;
            servers.insert("gobby".into(), entry);
            Ok(())
        })
    })
    .await?;
    if let Err(error) = result {
        eprintln!("gdaemon node: patching ~/.claude.json failed: {error:#}");
    }
    Ok(())
}

/// Writes the droid hooks: the user's file with Gobby's groups replaced or appended.
fn write_droid_hooks(home: &Path, path: &Path) -> Result<()> {
    let hooks_path = path.join(".factory/hooks/hooks.json");
    let existing = load_json_object(&hooks_path)?;
    let mut template: Value =
        serde_json::from_str(DROID_TEMPLATE).context("parse the embedded droid hooks template")?;
    let prefix = format!(
        "{} {GOBBY_OWNED}",
        shell_quote(&home.join("bin/ghook").display().to_string())
    );
    rewrite_template(&mut template, &prefix);
    let merged = merge_droid_hooks(existing, &template)?;
    write_atomic(&hooks_path, &pretty(&Value::Object(merged))?)
}

fn rewrite_template(template: &mut Value, prefix: &str) {
    let Some(hooks) = template.get_mut("hooks").and_then(Value::as_object_mut) else {
        return;
    };
    for (hook_type, config) in hooks.iter_mut() {
        let placeholder = format!("{prefix} --cli=droid --type={hook_type}");
        rewrite_commands(config, prefix, &placeholder);
    }
}

fn rewrite_commands(node: &mut Value, prefix: &str, placeholder: &str) {
    match node {
        Value::Array(items) => {
            for item in items {
                rewrite_commands(item, prefix, placeholder);
            }
        }
        Value::Object(fields) => {
            for field in ["command", "cmd", "script"] {
                if let Some(Value::String(command)) = fields.get_mut(field) {
                    let rewritten = rewrite_command(command, prefix, placeholder);
                    *command = rewritten;
                }
            }
            for value in fields.values_mut() {
                rewrite_commands(value, prefix, placeholder);
            }
        }
        _ => {}
    }
}

/// Keeps a Gobby command's flags and swaps only its executable prefix. A bare
/// placeholder becomes the full command for its hook type.
fn rewrite_command(command: &str, prefix: &str, placeholder: &str) -> String {
    if command.trim() == HOOK_PLACEHOLDER {
        return placeholder.to_owned();
    }
    match command.split_once(GOBBY_OWNED) {
        Some((_, suffix)) => format!("{prefix}{suffix}"),
        None => command.to_owned(),
    }
}

fn merge_droid_hooks(
    mut existing: Map<String, Value>,
    template: &Value,
) -> Result<Map<String, Value>> {
    let hooks_value = existing.entry("hooks").or_insert_with(|| json!({}));
    if !hooks_value.is_object() {
        *hooks_value = json!({});
    }
    let hooks = hooks_value
        .as_object_mut()
        .context("droid hooks must be an object")?;
    let template_hooks = template
        .get("hooks")
        .and_then(Value::as_object)
        .context("Droid hooks template does not contain a hooks object")?;
    for hook_type in DROID_HOOK_NAMES {
        let managed = template_hooks
            .get(hook_type)
            .and_then(Value::as_array)
            .filter(|groups| !groups.is_empty())
            .with_context(|| format!("Droid hooks template missing hook type: {hook_type}"))?;
        let merged = merge_groups(hooks.get(hook_type).cloned(), managed);
        hooks.insert(hook_type.to_owned(), Value::Array(merged));
    }
    Ok(existing)
}

/// Drops earlier Gobby handlers, keeps the user's handlers in place, and appends the managed groups.
fn merge_groups(existing: Option<Value>, managed: &[Value]) -> Vec<Value> {
    let existing = match existing {
        None | Some(Value::Null) => Vec::new(),
        Some(Value::Array(groups)) => groups,
        Some(other) => vec![other],
    };
    let mut merged = Vec::new();
    for group in existing {
        match group.get("hooks").and_then(Value::as_array) {
            Some(handlers) => {
                let kept: Vec<Value> = handlers
                    .iter()
                    .filter(|handler| !contains_gobby_hook(handler))
                    .cloned()
                    .collect();
                if kept.is_empty() {
                    continue;
                }
                let mut group = group;
                if let Value::Object(fields) = &mut group {
                    fields.insert("hooks".into(), Value::Array(kept));
                }
                merged.push(group);
            }
            None if !contains_gobby_hook(&group) => merged.push(group),
            None => {}
        }
    }
    merged.extend(managed.iter().cloned());
    merged
}

fn contains_gobby_hook(node: &Value) -> bool {
    match node {
        Value::String(text) => text.contains(GOBBY_OWNED),
        Value::Array(items) => items.iter().any(contains_gobby_hook),
        Value::Object(fields) => {
            ["command", "cmd", "script"].iter().any(|field| {
                matches!(fields.get(*field), Some(Value::String(command)) if command.contains(GOBBY_OWNED))
            }) || fields
                .values()
                .any(|value| matches!(value, Value::Object(_) | Value::Array(_)) && contains_gobby_hook(value))
        }
        _ => false,
    }
}

fn shell_quote(value: &str) -> String {
    let safe = !value.is_empty()
        && value
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || "_@%+=:,./-".contains(c));
    if safe {
        value.to_owned()
    } else {
        format!("'{}'", value.replace('\'', "'\"'\"'"))
    }
}

/// Reads a JSON object, treating a missing file as an empty object.
fn load_json_object(path: &Path) -> Result<Map<String, Value>> {
    if !path.exists() {
        return Ok(Map::new());
    }
    let bytes = fs::read(path).with_context(|| format!("read {}", path.display()))?;
    match serde_json::from_slice(&bytes).with_context(|| format!("parse {}", path.display()))? {
        Value::Object(object) => Ok(object),
        _ => Err(anyhow::anyhow!(
            "{} must contain a JSON object",
            path.display()
        )),
    }
}

fn pretty(value: &Value) -> Result<Vec<u8>> {
    let mut text = serde_json::to_string_pretty(value)?;
    text.push('\n');
    Ok(text.into_bytes())
}

/// Replaces `path` through a sibling temporary file, so readers never see a partial write.
fn write_atomic(path: &Path, bytes: &[u8]) -> Result<()> {
    let parent = path
        .parent()
        .with_context(|| format!("{} has no parent directory", path.display()))?;
    fs::create_dir_all(parent).with_context(|| format!("create {}", parent.display()))?;
    let temporary = write_temp(path, bytes, 0o666)?;
    if let Err(error) = fs::rename(&temporary, path) {
        let _ = fs::remove_file(&temporary);
        return Err(error).with_context(|| format!("replace {}", path.display()));
    }
    Ok(())
}

/// Attempts at a free temp name before giving up. Names carry a process-wide
/// counter, so a clash only comes from a file someone else planted.
const TEMP_ATTEMPTS: u32 = 64;

/// Writes `bytes` to a new sibling of `path` created exclusively with `mode`, and
/// returns its path. The caller renames it into place. `create_new` never follows
/// a planted symlink, and the mode is applied at creation, not after.
fn write_temp(path: &Path, bytes: &[u8], mode: u32) -> Result<PathBuf> {
    static ATTEMPTS: AtomicU64 = AtomicU64::new(0);
    let name = path
        .file_name()
        .with_context(|| format!("{} has no file name", path.display()))?
        .to_string_lossy()
        .into_owned();
    for _ in 0..TEMP_ATTEMPTS {
        let attempt = ATTEMPTS.fetch_add(1, Ordering::Relaxed);
        let temporary =
            path.with_file_name(format!(".{name}.{}.{attempt}.tmp", std::process::id()));
        match OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(mode)
            .open(&temporary)
        {
            Ok(mut file) => {
                if let Err(error) = file.write_all(bytes).and_then(|()| file.sync_all()) {
                    let _ = fs::remove_file(&temporary);
                    return Err(error).with_context(|| format!("write {}", temporary.display()));
                }
                return Ok(temporary);
            }
            Err(error) if error.kind() == ErrorKind::AlreadyExists => {}
            Err(error) => {
                return Err(error).with_context(|| format!("create {}", temporary.display()));
            }
        }
    }
    bail!(
        "could not create a temporary file beside {}",
        path.display()
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rewrite_keeps_foreign_commands_and_flags() {
        assert_eq!(
            rewrite_command(
                "ghook --gobby-owned --cli=droid --type=Stop",
                "/x/ghook --gobby-owned",
                "p"
            ),
            "/x/ghook --gobby-owned --cli=droid --type=Stop"
        );
        assert_eq!(
            rewrite_command("/usr/bin/audit", "p", "p"),
            "/usr/bin/audit"
        );
        assert_eq!(shell_quote("/a b/ghook"), "'/a b/ghook'");
    }
}
