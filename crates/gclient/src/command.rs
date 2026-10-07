//! One-shot workspace commands. Parsing is kept separate from the TUI startup parser.

mod help;
mod refs;
mod verbs;

use std::collections::{BTreeMap, BTreeSet};
use std::path::PathBuf;

#[derive(Debug, Clone, Default)]
pub struct CommandEnv {
    pub pane_ref: Option<String>,
    pub workspace_id: Option<String>,
    pub tab_id: Option<String>,
}

impl CommandEnv {
    pub fn from_process() -> Self {
        Self {
            pane_ref: std::env::var("GOBBY_PANE_REF")
                .ok()
                .filter(|s| !s.is_empty()),
            workspace_id: std::env::var("GOBBY_WORKSPACE_ID")
                .ok()
                .filter(|s| !s.is_empty()),
            tab_id: std::env::var("GOBBY_TAB_ID").ok().filter(|s| !s.is_empty()),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CommandError {
    pub code: i32,
    pub message: String,
}

impl CommandError {
    fn usage(message: impl Into<String>) -> Self {
        Self {
            code: 2,
            message: message.into(),
        }
    }

    fn connection(message: impl Into<String>) -> Self {
        Self {
            code: 3,
            message: message.into(),
        }
    }
}

#[derive(Debug, Clone)]
struct Parsed {
    verb: String,
    positions: Vec<String>,
    values: BTreeMap<String, String>,
    switches: BTreeSet<String>,
}

impl Parsed {
    fn take(&mut self, name: &str) -> Option<String> {
        self.values.remove(name)
    }

    fn switch(&mut self, name: &str) -> bool {
        self.switches.remove(name)
    }

    fn position(&mut self) -> Option<String> {
        if self.positions.is_empty() {
            None
        } else {
            Some(self.positions.remove(0))
        }
    }

    fn finish(&self) -> Result<(), CommandError> {
        if self.values.is_empty() && self.switches.is_empty() && self.positions.is_empty() {
            return Ok(());
        }
        Err(CommandError::usage(format!(
            "unexpected arguments for {}",
            self.verb
        )))
    }
}

const VALUE_FLAGS: &[&str] = &[
    "--workspace",
    "--project",
    "--name",
    "--cmd",
    "--daemon-url",
    "--token-file",
    "--lines",
    "--pattern",
    "--timeout",
    "--interval",
    "--tab-ref",
    "--kind",
];
const SWITCH_FLAGS: &[&str] = &["--json", "--right", "--down", "--enter", "--help", "-h"];

fn parse(args: Vec<String>) -> Result<Parsed, CommandError> {
    let mut args = args.into_iter();
    let verb = args
        .next()
        .ok_or_else(|| CommandError::usage("missing verb"))?;
    if !help::is_verb(&verb) {
        return Err(CommandError::usage(format!("unknown verb: {verb}")));
    }
    let mut parsed = Parsed {
        verb,
        positions: Vec::new(),
        values: BTreeMap::new(),
        switches: BTreeSet::new(),
    };
    while let Some(arg) = args.next() {
        if arg == "--" {
            parsed.positions.extend(args);
            break;
        }
        let (flag, inline) = arg
            .split_once('=')
            .map_or((arg.as_str(), None), |(a, b)| (a, Some(b)));
        if VALUE_FLAGS.contains(&flag) {
            let value = inline
                .map(str::to_owned)
                .or_else(|| args.next())
                .ok_or_else(|| CommandError::usage(format!("{flag} requires a value")))?;
            if value.is_empty() || (inline.is_none() && value.starts_with("--")) {
                return Err(CommandError::usage(format!("{flag} requires a value")));
            }
            if parsed.values.insert(flag.to_owned(), value).is_some() {
                return Err(CommandError::usage(format!("{flag} repeated")));
            }
        } else if SWITCH_FLAGS.contains(&flag) && inline.is_none() {
            if !parsed.switches.insert(flag.to_owned()) {
                return Err(CommandError::usage(format!("{flag} repeated")));
            }
        } else if arg.starts_with('-') {
            return Err(CommandError::usage(format!("unknown option: {arg}")));
        } else {
            parsed.positions.push(arg);
        }
    }
    Ok(parsed)
}

pub fn is_verb(first: &str) -> bool {
    !first.is_empty() && !first.starts_with('-')
}

/// Return the documented process status; the caller decides when to exit.
/// Errors go to stderr prefixed with the verb, so a caller sees which
/// command failed.
pub fn dispatch(args: Vec<String>, env: CommandEnv) -> i32 {
    let verb = args.first().cloned().unwrap_or_default();
    match dispatch_inner(args, env) {
        Ok(code) => code,
        Err(error) => {
            eprintln!("gclient {verb}: {}", error.message);
            error.code
        }
    }
}

fn dispatch_inner(args: Vec<String>, env: CommandEnv) -> Result<i32, CommandError> {
    let mut parsed = parse(args)?;
    if parsed.switch("--help") | parsed.switch("-h") {
        print!("{}", help::verb(&parsed.verb).unwrap_or_else(help::table));
        return Ok(0);
    }
    if parsed.verb == "help" {
        // Help takes the options every verb takes and ignores them.
        parsed.switch("--json");
        for option in ["--workspace", "--daemon-url", "--token-file"] {
            parsed.take(option);
        }
        let text = match parsed.position() {
            Some(verb) => help::verb(&verb)
                .ok_or_else(|| CommandError::usage(format!("unknown verb: {verb}")))?,
            None => help::table(),
        };
        parsed.finish()?;
        print!("{text}");
        return Ok(0);
    }
    let json = parsed.switch("--json");
    let url = parsed
        .take("--daemon-url")
        .unwrap_or_else(gobby_core::daemon_url::daemon_url);
    let token_file = parsed.take("--token-file").map(PathBuf::from);
    let action = verbs::Action::parse(parsed, &env)?;
    let token = match token_file {
        Some(path) => std::fs::read_to_string(&path)
            .map(|s| s.trim().to_owned())
            .map_err(|e| CommandError::connection(format!("token file {}: {e}", path.display())))?,
        None => gobby_core::local_token::read_api_key()
            .map_err(|e| CommandError::connection(e.to_string()))?,
    };
    if token.is_empty() {
        return Err(CommandError::connection("daemon credential is empty"));
    }
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .map_err(|e| CommandError::connection(e.to_string()))?;
    let output = runtime.block_on(action.run(&url, &token))?;
    if json {
        println!(
            "{}",
            serde_json::to_string(&output.result)
                .map_err(|e| CommandError::connection(e.to_string()))?
        );
    } else if !output.plain.is_empty() {
        println!("{}", output.plain);
    }
    Ok(output.status)
}

#[cfg(test)]
#[path = "command/tests.rs"]
mod tests;
