use gobby_wiki::{
    BenchmarkOptions, Command, GraphCommandOptions, IngestFileOptions, PageWriteMode, PurgeTarget,
    ReadTarget, ScopeSelection, SyncSessionsOptions, WikiError,
};
use std::path::{Path, PathBuf};

use super::{
    CliCommand, CompileKind, ExportArgs, ExportSubcommand, PageMode, PageSubcommand,
    ReviewReportArgs, ScopeArgs,
};

#[cfg(test)]
pub(super) fn command_from_cli(
    command: CliCommand,
    scope: ScopeSelection,
) -> Result<Command, WikiError> {
    command_from_cli_with_runtime(command, scope, false, false)
}

pub(super) fn command_from_cli_with_runtime(
    command: CliCommand,
    scope: ScopeSelection,
    quiet: bool,
    verbose: bool,
) -> Result<Command, WikiError> {
    match command {
        CliCommand::Contract => unreachable!("contract command is handled before runtime dispatch"),
        CliCommand::SchemaIdentity { .. } => {
            unreachable!("schema-identity command is handled before runtime dispatch")
        }
        CliCommand::Code(args) => {
            let project_root = match scope {
                ScopeSelection::Detect => {
                    detect_project_root_from(&std::env::current_dir().map_err(|source| {
                        WikiError::Io {
                            action: "resolve current project directory",
                            path: None,
                            source,
                        }
                    })?)?
                }
                ScopeSelection::ProjectRoot(project_root) => project_root,
                ScopeSelection::Topic(topic) => {
                    return Err(WikiError::InvalidScope {
                        detail: format!(
                            "gwiki code requires project scope; topic '{topic}' is unsupported"
                        ),
                    });
                }
            };
            Ok(Command::Code(args.into_options(
                project_root,
                quiet,
                verbose,
            )))
        }
        CliCommand::Init => Ok(Command::Init { scope }),
        CliCommand::Index { force } => Ok(Command::Index { scope, force }),
        CliCommand::Collect => Ok(Command::Collect { scope }),
        CliCommand::IngestFile {
            path,
            no_ai,
            translate,
            target_lang,
            video_frame_interval_seconds,
        } => Ok(Command::IngestFile {
            path,
            scope,
            options: IngestFileOptions {
                no_ai,
                translate,
                target_lang,
                video_frame_interval_seconds,
            },
        }),
        CliCommand::IngestUrl {
            urls,
            max_age_hours,
        } => Ok(Command::IngestUrl {
            urls,
            scope,
            max_age_hours,
        }),
        CliCommand::SyncSessions(args) => Ok(Command::SyncSessions {
            scope,
            options: SyncSessionsOptions {
                archive_dir: args.archive_dir,
                wiki_dir: args.wiki_dir,
                limit: args.limit,
                raw: args.raw,
                summarize: args.summarize,
                enrich: !args.no_enrich,
            },
        }),
        CliCommand::Refresh(args) => Ok(Command::Refresh {
            scope,
            source_ids: args.id,
            dry_run: args.dry_run,
        }),
        CliCommand::Sources => Ok(Command::Sources { scope }),
        CliCommand::RemoveSource(args) => {
            if args.dry_run && args.yes {
                return Err(WikiError::InvalidInput {
                    field: "remove-source",
                    message: "pass only one of --dry-run or --yes".to_string(),
                });
            }
            if !args.dry_run && !args.yes {
                return Err(WikiError::InvalidInput {
                    field: "remove-source",
                    message: "destructive source removal requires --yes; use --dry-run to preview"
                        .to_string(),
                });
            }
            Ok(Command::RemoveSource {
                id: args.id,
                scope,
                dry_run: args.dry_run,
                keep_asset: args.keep_asset,
            })
        }
        CliCommand::Purge(args) => Ok(Command::Purge {
            target: args.project_id.map_or_else(
                || PurgeTarget::selection(scope),
                |project_id| PurgeTarget::project_id(project_id.to_string()),
            ),
            yes: args.yes,
        }),
        CliCommand::Prune(args) => Ok(Command::Prune { force: args.force }),
        CliCommand::Search(args) => Ok(Command::Search {
            query: args.query,
            scope,
            limit: args.limit,
            include_semantic: !args.no_semantic,
            token_budget: args.token_budget,
            include_candidates: args.include_candidates,
        }),
        CliCommand::Read(args) => {
            let target = match (args.path, args.title) {
                (Some(path), None) => ReadTarget::Path(path),
                (None, Some(title)) => ReadTarget::Title(title),
                _ => {
                    return Err(WikiError::InvalidInput {
                        field: "read",
                        message: "pass exactly one of --path or --title".to_string(),
                    });
                }
            };
            Ok(Command::Read { target, scope })
        }
        CliCommand::Pages(args) => Ok(Command::Pages {
            scope,
            prefix: args.prefix,
        }),
        CliCommand::Page(args) => Ok(match args.command {
            PageSubcommand::Write(write) => Command::PageWrite {
                scope,
                path: write.path,
                mode: match write.mode {
                    PageMode::Upsert => PageWriteMode::Upsert,
                    PageMode::Create => PageWriteMode::Create,
                },
                expected_hash: write.expected_hash,
            },
            PageSubcommand::Delete(delete) => Command::PageDelete {
                scope,
                path: delete.path,
            },
        }),
        CliCommand::Backlinks(args) => Ok(Command::Backlinks {
            page: args.page,
            scope,
        }),
        CliCommand::LinkSuggest(args) => Ok(Command::LinkSuggest {
            scope,
            limit: args.limit,
        }),
        CliCommand::Benchmark(args) => Ok(Command::Benchmark {
            scope,
            options: BenchmarkOptions {
                retrieval_candidates: args.retrieval_candidates,
            },
        }),
        CliCommand::Compile(args) => Ok(Command::Compile {
            topic: args.topic,
            outline: args.outline,
            source: args.source,
            target_kind: args.kind.into(),
            target_page: args.target,
            write_intent: args.write_intent,
            ai: routing_from_no_ai(args.no_ai),
            scope,
        }),
        CliCommand::Export(args) => Ok(Command::Export {
            scope,
            command: args.into(),
        }),
        CliCommand::Graph(args) => Ok(Command::Graph {
            scope,
            options: GraphCommandOptions {
                stdout: args.stdout,
                include: args.include,
            },
        }),
        CliCommand::GraphContext => Ok(Command::GraphContext { scope }),
        CliCommand::ReviewReport(args) => Ok(Command::ReviewReport {
            scope,
            options: args.into(),
        }),
        CliCommand::Audit => Ok(Command::Audit { scope }),
        CliCommand::Lint => Ok(Command::Lint { scope }),
        CliCommand::Normalize(args) => Ok(Command::Normalize {
            scope,
            check: args.check,
        }),
        CliCommand::Health => Ok(Command::Health { scope }),
        CliCommand::Librarian(args) => Ok(Command::Librarian {
            scope,
            ai: routing_from_no_ai(args.no_ai),
        }),
        CliCommand::Upkeep(args) => Ok(Command::Upkeep {
            scope,
            options: gobby_wiki::UpkeepOptions {
                max_pages: args.max_pages,
                min_mentions: args.min_mentions,
                max_sources_per_page: args.max_sources_per_page,
                dry_run: args.dry_run,
                time_budget_seconds: args.time_budget_seconds,
            },
            ai: routing_from_no_ai(args.no_ai),
        }),
        CliCommand::Recap(args) => Ok(Command::Recap {
            scope,
            options: gobby_wiki::RecapOptions { date: args.date },
            ai: routing_from_no_ai(args.no_ai),
        }),
        CliCommand::Status => Ok(Command::Status { scope }),
        CliCommand::Trust => Ok(Command::Trust { scope }),
        CliCommand::CitationQuality => Ok(Command::CitationQuality { scope }),
    }
}

fn routing_from_no_ai(no_ai: bool) -> gobby_core::config::AiRouting {
    if no_ai {
        gobby_core::config::AiRouting::Off
    } else {
        gobby_core::config::AiRouting::Daemon
    }
}

pub(crate) fn command_is_mutating(command: &Command) -> bool {
    !matches!(
        command,
        Command::Search { .. }
            | Command::Read { .. }
            | Command::Pages { .. }
            | Command::Backlinks { .. }
            | Command::LinkSuggest { .. }
            | Command::Benchmark { .. }
            | Command::Sources { .. }
            | Command::Status { .. }
            | Command::Trust { .. }
            | Command::Lint { .. }
            | Command::Graph { .. }
            | Command::GraphContext { .. }
            | Command::ReviewReport { .. }
            | Command::Code(_)
    )
}

pub(super) fn detect_project_root_from(start: &Path) -> Result<PathBuf, WikiError> {
    gobby_core::project::find_project_root(start).ok_or_else(|| WikiError::InvalidScope {
        detail: format!("no Gobby project found from {}", start.display()),
    })
}

impl From<CompileKind> for gobby_wiki::synthesis::ArticleKind {
    fn from(kind: CompileKind) -> Self {
        match kind {
            CompileKind::Source => Self::Source,
            CompileKind::Concept => Self::Concept,
            CompileKind::Topic => Self::Topic,
        }
    }
}

impl From<ExportArgs> for gobby_wiki::exports::ExportCommand {
    fn from(args: ExportArgs) -> Self {
        match args.command {
            ExportSubcommand::Pages => Self::AgentPages,
            ExportSubcommand::WorkflowAssets { output } => {
                Self::WorkflowAssets { filename: output }
            }
            ExportSubcommand::Report { output, source } => Self::ReportFile {
                filename: output,
                source_path: source,
            },
        }
    }
}

impl From<ReviewReportArgs> for gobby_wiki::ReviewReportOptions {
    fn from(args: ReviewReportArgs) -> Self {
        Self {
            files: args.files,
            symbols: args.symbols,
            diff_path: args.diff_path,
            output: args.output,
        }
    }
}

impl From<ScopeArgs> for ScopeSelection {
    fn from(scope: ScopeArgs) -> Self {
        if let Some(topic) = scope.topic {
            Self::topic(topic)
        } else if let Some(project_root) = scope.project {
            Self::project(project_root)
        } else {
            Self::detect()
        }
    }
}
