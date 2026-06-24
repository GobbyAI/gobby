use clap::Parser;

#[derive(Parser, Debug)]
#[command(
    name = "ghook",
    about = "Gobby sandbox-tolerant hook dispatcher",
    disable_version_flag = true
)]
pub(crate) struct Args {
    /// Normal hook-invocation mode. Required for enqueue/POST.
    #[arg(long)]
    pub(crate) gobby_owned: bool,

    /// Print diagnostic JSON for the given cli/type, then exit.
    #[arg(long)]
    pub(crate) diagnose: bool,

    /// Print version and write ~/.gobby/bin/.ghook-runtime.json stamp.
    #[arg(long)]
    pub(crate) version: bool,

    /// Host CLI name (claude, codex, gemini, qwen, droid, grok, agy).
    #[arg(long)]
    pub(crate) cli: Option<String>,

    /// Hook type (e.g. session-start, SessionStart, PreToolUse).
    #[arg(long = "type")]
    pub(crate) hook_type: Option<String>,

    /// Detach from the parent's session/process group before the POST.
    #[arg(long)]
    pub(crate) detach: bool,
}
