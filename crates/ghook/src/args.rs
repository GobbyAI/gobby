use clap::{Parser, Subcommand};

#[derive(Parser, Debug)]
#[command(
    name = "ghook",
    about = "Gobby sandbox-tolerant hook dispatcher",
    disable_version_flag = true
)]
pub(crate) struct Args {
    #[command(subcommand)]
    pub(crate) command: Option<Command>,

    /// Normal hook-invocation mode. Required for enqueue/POST.
    #[arg(long)]
    pub(crate) gobby_owned: bool,

    /// Print diagnostic JSON for the given cli/type, then exit.
    #[arg(long)]
    pub(crate) diagnose: bool,

    /// Print version and write ~/.gobby/bin/.ghook-runtime.json stamp.
    #[arg(long)]
    pub(crate) version: bool,

    /// Host CLI name (claude, codex, qwen, droid, grok, agy).
    #[arg(long)]
    pub(crate) cli: Option<String>,

    /// Hook type (e.g. session-start, SessionStart, PreToolUse).
    #[arg(long = "type")]
    pub(crate) hook_type: Option<String>,

    /// Detach from the parent's session/process group before the POST.
    #[arg(long)]
    pub(crate) detach: bool,

    /// Exit after the durable inbox enqueue without posting to the daemon.
    #[arg(long)]
    pub(crate) enqueue_only: bool,
}

#[derive(Debug, Subcommand)]
pub(crate) enum Command {
    /// Print the embedded schema identity.
    SchemaIdentity {
        #[arg(long)]
        json: bool,
    },
}
