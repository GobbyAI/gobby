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

    /// Print version. Writes ~/.gobby/bin/.ghook-runtime.json only when this
    /// binary is the installed bin/ghook.
    #[arg(long)]
    pub(crate) version: bool,

    /// Print the runtime stamp JSON to stdout, followed by a newline. Writes nothing.
    #[arg(long)]
    pub(crate) runtime_stamp: bool,

    /// Host CLI name (claude, codex, droid, grok, agy), or `git` for the
    /// synchronous reference-transaction guard (exit 0 only on an allow verdict).
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
