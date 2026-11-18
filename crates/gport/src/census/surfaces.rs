//! Section 7: route decorators in `_app_ui.py`, and the surfaces the parity ledger generator
//! prints that the frozen ledger has no row for.

use std::collections::HashSet;
use std::path::Path;
use std::process::Command;

use super::table;
use crate::rows;

const HEADING: &str = "## 7. Surfaces with no row";
const APP_UI: &str = "src/gobby/servers/_app_ui.py";
const GENERATOR_ARGS: [&str; 6] = [
    "run",
    "--no-sync",
    "python",
    "-m",
    "scripts.generate_parity_ledger",
    "--stdout",
];
const GENERATOR_COMMAND: &str =
    "uv run --no-sync python -m scripts.generate_parity_ledger --stdout";

pub fn section(root: &Path, frozen: Result<&str, &str>, no_python: bool) -> String {
    format!(
        "{HEADING}\n\n{}\n{}",
        routes_text(root, frozen),
        python_inventory(root, frozen, no_python)
    )
}

struct Route {
    line: usize,
    /// The method as the table shows it: `WEBSOCKET` for a websocket route.
    method: String,
    path: String,
    /// The key a ledger row carries for this route: `HTTP <METHOD> <path>`, or `WS transport <path>`
    /// for a websocket route, as the parity ledger generator writes them.
    key: String,
}

fn routes_text(root: &Path, frozen: Result<&str, &str>) -> String {
    let source = match std::fs::read_to_string(root.join(APP_UI)) {
        Ok(source) => source,
        Err(error) => return format!("{APP_UI} could not be read: {error}.\n"),
    };
    let found = routes(&source);
    if found.is_empty() {
        return format!("No route decorator was found in {APP_UI}.\n");
    }
    let known = frozen.ok().map(known_keys);
    let rows: Vec<Vec<String>> = found
        .into_iter()
        .map(|route| {
            let listed = match &known {
                Some(keys) if keys.contains(&route.key) => "yes",
                Some(_) => "no",
                None => "unknown",
            };
            vec![
                route.method,
                route.path,
                route.line.to_string(),
                listed.to_owned(),
            ]
        })
        .collect();
    format!(
        "Route decorators in {APP_UI}, and whether the frozen ledger has a row with each route's \
         key.\n\n{}",
        table(&["Method", "Path", "Line", "In frozen ledger"], &rows)
    )
}

/// The row keys of the frozen ledger, with each `\|` read as `|` so that they compare with the
/// keys built from source text.
fn known_keys(ledger: &str) -> HashSet<String> {
    rows::rows(ledger)
        .into_iter()
        .map(|row| row.key.replace("\\|", "|"))
        .collect()
}

/// The route decorators of a module, such as `@app.get("/path")` or `@app.websocket("/ws")`.
fn routes(source: &str) -> Vec<Route> {
    source
        .lines()
        .enumerate()
        .filter_map(|(index, line)| {
            let decorator = line.trim_start().strip_prefix('@')?;
            let (object, call) = decorator.split_once('.')?;
            let (method, args) = call.split_once('(')?;
            if !is_identifier(object) || !is_identifier(method) {
                return None;
            }
            let args = args.trim_start();
            let quote = args.chars().next().filter(|c| *c == '"' || *c == '\'')?;
            let (path, _) = args[1..].split_once(quote)?;
            let (shown, key) = if method == "websocket" {
                ("WEBSOCKET".to_owned(), format!("WS transport {path}"))
            } else {
                let verb = method.to_uppercase();
                (verb.clone(), format!("HTTP {verb} {path}"))
            };
            Some(Route {
                line: index + 1,
                method: shown,
                path: path.to_owned(),
                key,
            })
        })
        .collect()
}

fn is_identifier(text: &str) -> bool {
    !text.is_empty() && text.chars().all(|c| c.is_ascii_alphanumeric() || c == '_')
}

fn python_inventory(root: &Path, frozen: Result<&str, &str>, no_python: bool) -> String {
    if no_python {
        return "Python inventory: skipped (--no-python).\n".to_owned();
    }
    let ledger = match frozen {
        Ok(ledger) => ledger,
        Err(reason) => {
            return format!(
                "Python inventory: skipped, the frozen ledger is not readable: {reason}.\n"
            );
        }
    };
    let output = match run_generator(root) {
        Ok(stdout) => stdout,
        Err(reason) => {
            return format!(
                "Python inventory: the parity ledger generator did not run \
                 ({GENERATOR_COMMAND}): {reason}.\n"
            );
        }
    };
    let known: HashSet<String> = rows::rows(ledger).into_iter().map(|row| row.key).collect();
    let mut seen = HashSet::new();
    let missing: Vec<String> = rows::rows(&output)
        .into_iter()
        .map(|row| row.key)
        .filter(|key| !known.contains(key) && seen.insert(key.clone()))
        .collect();
    let mut text = format!(
        "Python inventory: {} surface(s) in the generator output have no frozen row.\n",
        missing.len()
    );
    for key in missing {
        text.push_str(&format!("- `{key}`\n"));
    }
    text
}

/// The generator's stdout, or the reason it did not run.
fn run_generator(root: &Path) -> Result<String, String> {
    let output = Command::new("uv")
        .args(GENERATOR_ARGS)
        .current_dir(root)
        .output()
        .map_err(|error| format!("cannot run uv: {error}"))?;
    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        let first = stderr.lines().next().unwrap_or("").trim();
        return Err(format!("exit status {}: {first}", output.status));
    }
    Ok(String::from_utf8_lossy(&output.stdout).into_owned())
}
