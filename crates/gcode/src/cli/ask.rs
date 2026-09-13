use std::path::Path;

use clap::{ArgGroup, Args, ValueEnum};

#[derive(Debug, Clone, Copy, ValueEnum)]
pub(crate) enum AskRetrieval {
    Deterministic,
    Hybrid,
}

impl AskRetrieval {
    pub(crate) fn as_str(self) -> &'static str {
        match self {
            Self::Deterministic => "deterministic",
            Self::Hybrid => "hybrid",
        }
    }
}

#[derive(Debug, Args)]
#[command(group(
    ArgGroup::new("ask_action")
        .required(true)
        .multiple(false)
        .args(["question", "status", "resume", "cancel", "export"])
))]
pub(crate) struct AskArgs {
    /// Write a diagnostic bundle beneath this machine's Gobby home
    #[arg(long)]
    pub(crate) output_debug_files: bool,
    /// Question to answer from the current repository index
    #[arg(value_name = "QUESTION")]
    question: Option<String>,

    /// Absolute run deadline in seconds from start
    #[arg(
        long,
        value_name = "SECONDS",
        value_parser = clap::value_parser!(u64).range(1..),
        requires = "question"
    )]
    timeout_seconds: Option<u64>,

    /// Evidence retrieval mode for a new run
    #[arg(long, value_enum, requires = "question")]
    retrieval: Option<AskRetrieval>,

    /// Return after the durable run is created instead of waiting
    #[arg(long, requires = "question")]
    background: bool,

    /// Read current durable state
    #[arg(long, value_name = "RUN_ID")]
    status: Option<String>,

    /// Resume an interrupted run and wait for its durable result
    #[arg(long, value_name = "RUN_ID")]
    resume: Option<String>,

    /// Cancel a run and its active native child
    #[arg(long, value_name = "RUN_ID")]
    cancel: Option<String>,

    /// Export a completed immutable publication
    #[arg(long, value_name = "RUN_ID", requires = "output")]
    export: Option<String>,

    /// Directory in which to create the exported tar archive
    #[arg(long, value_name = "DIR", requires = "export")]
    output: Option<std::path::PathBuf>,
}

#[derive(Debug)]
pub(crate) enum AskAction<'a> {
    Start {
        question: &'a str,
        timeout_seconds: u64,
        retrieval: AskRetrieval,
        background: bool,
    },
    Status {
        run_id: &'a str,
    },
    Resume {
        run_id: &'a str,
    },
    Cancel {
        run_id: &'a str,
    },
    Export {
        run_id: &'a str,
        output: &'a Path,
    },
}

impl AskArgs {
    pub(crate) fn action(&self) -> AskAction<'_> {
        if let Some(question) = self.question.as_deref() {
            return AskAction::Start {
                question,
                timeout_seconds: self.timeout_seconds.unwrap_or(600),
                retrieval: self.retrieval.unwrap_or(AskRetrieval::Deterministic),
                background: self.background,
            };
        }
        if let Some(run_id) = self.status.as_deref() {
            return AskAction::Status { run_id };
        }
        if let Some(run_id) = self.resume.as_deref() {
            return AskAction::Resume { run_id };
        }
        if let Some(run_id) = self.cancel.as_deref() {
            return AskAction::Cancel { run_id };
        }
        if let (Some(run_id), Some(output)) = (self.export.as_deref(), self.output.as_deref()) {
            return AskAction::Export { run_id, output };
        }
        unreachable!("clap enforces exactly one complete Ask action")
    }
}
