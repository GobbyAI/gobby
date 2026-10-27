use std::path::Path;

use postgres::Client;

use crate::config::{Context, ProjectIndexScope};
use crate::db;
use crate::index::walker;

use super::file::{ExplicitFileRoute, explicit_file_route};
use super::overlay::index_overlay_files;
use super::primary;
use super::types::{IndexOptions, IndexOutcome, IndexProgressSink, IndexRequest};

/// `index_files` for the freshness refresh: the same pipeline on a connection
/// bounded by `db::connect_freshness_readwrite`, so a row held by another
/// session fails the refresh instead of blocking it indefinitely.
pub fn index_files_for_freshness(
    request: IndexRequest,
    ctx: &Context,
    options: IndexOptions<'_>,
) -> anyhow::Result<IndexOutcome> {
    let mut conn = db::connect_freshness_readwrite(&ctx.database_url)?;
    index_files_with_connection(&mut conn, request, ctx, options)
}

pub fn index_files(
    request: IndexRequest,
    ctx: &Context,
    options: IndexOptions<'_>,
) -> anyhow::Result<IndexOutcome> {
    let mut timings = super::IndexTimings::new("index.connect");
    let mut conn = db::connect_readwrite(&ctx.database_url)?;
    timings.phase("index.pipeline");
    index_files_with_connection(&mut conn, request, ctx, options)
}

fn index_files_with_connection(
    conn: &mut Client,
    request: IndexRequest,
    ctx: &Context,
    mut options: IndexOptions<'_>,
) -> anyhow::Result<IndexOutcome> {
    if matches!(ctx.index_scope, ProjectIndexScope::Overlay { .. }) {
        return index_overlay_files(conn, &request, ctx, &mut options);
    }
    primary::index_with_connection(conn, &request, ctx, &mut options)
}

pub(super) struct ActiveIndexProgress<'a> {
    sink: Option<&'a mut dyn IndexProgressSink>,
    started: bool,
}

impl<'a> ActiveIndexProgress<'a> {
    pub(super) fn new(sink: Option<&'a mut dyn IndexProgressSink>, total: usize) -> Self {
        let mut progress = Self {
            sink,
            started: total > 0,
        };
        if progress.started
            && let Some(sink) = progress.sink.as_deref_mut()
        {
            sink.start(total);
        }
        progress
    }

    pub(super) fn advance(&mut self, file_path: &str) {
        if let Some(sink) = self.sink.as_deref_mut() {
            sink.advance(file_path);
        }
    }
}

impl Drop for ActiveIndexProgress<'_> {
    fn drop(&mut self) {
        if self.started
            && let Some(sink) = self.sink.as_deref_mut()
        {
            sink.finish();
        }
    }
}

pub(super) fn discovery_options(ctx: &Context) -> walker::DiscoveryOptions {
    walker::DiscoveryOptions {
        respect_gitignore: ctx.indexing.respect_gitignore,
    }
}

pub(super) fn explicit_route_with_discovery_options(
    root_path: &Path,
    abs: &Path,
    excludes: &[&str],
    options: walker::DiscoveryOptions,
) -> ExplicitFileRoute {
    if !options.respect_gitignore {
        return explicit_file_route(root_path, abs, excludes);
    }
    match walker::classify_explicit_file_with_options(root_path, abs, excludes, options) {
        Some(walker::FileClassification::Ast) => ExplicitFileRoute::Ast,
        Some(walker::FileClassification::ContentOnly) => ExplicitFileRoute::ContentOnly,
        None => ExplicitFileRoute::Skip,
    }
}

#[cfg(test)]
mod progress_tests {
    use super::*;

    #[derive(Default)]
    struct RecordingProgress {
        events: Vec<String>,
    }

    impl IndexProgressSink for RecordingProgress {
        fn start(&mut self, total: usize) {
            self.events.push(format!("start:{total}"));
        }

        fn advance(&mut self, file_path: &str) {
            self.events.push(format!("advance:{file_path}"));
        }

        fn finish(&mut self) {
            self.events.push("finish".to_string());
        }
    }

    #[test]
    fn index_progress_reports_start_advance_and_finish() {
        let mut sink = RecordingProgress::default();
        {
            let mut progress = ActiveIndexProgress::new(Some(&mut sink), 2);
            progress.advance("src/one.rs");
            progress.advance("src/two.rs");
        }

        assert_eq!(
            sink.events,
            vec![
                "start:2",
                "advance:src/one.rs",
                "advance:src/two.rs",
                "finish"
            ]
        );
    }

    #[test]
    fn index_progress_zero_total_is_noop() {
        let mut sink = RecordingProgress::default();
        {
            let _progress = ActiveIndexProgress::new(Some(&mut sink), 0);
        }

        assert!(sink.events.is_empty());
    }
}
