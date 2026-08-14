use std::cell::Cell;
use std::path::PathBuf;
use std::rc::Rc;

use super::*;
use crate::commands::code::{
    AiDepth, CODE_WRITER_LOCK_RELATIVE_PATH, CODE_WRITER_LOCK_TIMEOUT, CodeCommandOptions,
    ProseDepth, VerifyScope,
};
use crate::commands::session_sync::run_persistent_write_phases;
use crate::project_lock::{
    ProjectLockBackend, ProjectRowState, acquire_writer_lock_for_test, run_with_project_lock,
};
use crate::{IngestFileOptions, SyncSessionsOptions};

const PROJECT_ID: &str = "d45545c5-ded5-4335-b115-0245752edacf";

fn code_options() -> CodeCommandOptions {
    CodeCommandOptions {
        project_root: PathBuf::from("/tmp/manual-codewiki-project"),
        out: None,
        purge: false,
        force: false,
        scope: Vec::new(),
        complete_scope: false,
        ai: None,
        ai_depth: AiDepth::default(),
        ai_prose_depth: ProseDepth::default(),
        ai_register: None,
        ai_aggregate_profile: None,
        ai_aggregate_candidates: Vec::new(),
        ai_verify_profile: None,
        ai_verify_scope: VerifyScope::default(),
        edge_limit: 5_000,
        include_docs: false,
        since: None,
        compare_to: None,
        max_workers: 1,
        repair_citations: false,
        allow_stale: false,
        quiet: false,
        verbose: false,
    }
}

#[test]
fn code_admission_preserves_the_engine_owned_lock_and_manual_project_semantics() {
    let command = Command::Code(code_options());
    assert!(matches!(
        classify_command(&command),
        CommandClassification::Code
    ));
    assert!(
        acquire_command_lock(&command)
            .expect("code admission")
            .is_none(),
        "code must bypass the generic project-row lock"
    );
    assert_eq!(CODE_WRITER_LOCK_RELATIVE_PATH, "_meta/codewiki.lock");
    assert_eq!(CODE_WRITER_LOCK_TIMEOUT, std::time::Duration::from_secs(2));
}

#[test]
fn code_compare_is_admitted_as_an_engine_owned_read_path() {
    let mut options = code_options();
    options.compare_to = Some("HEAD".to_string());
    let command = Command::Code(options);

    assert!(matches!(
        classify_command(&command),
        CommandClassification::Code
    ));
    assert!(
        acquire_command_lock(&command)
            .expect("compare admission")
            .is_none()
    );
}

#[test]
fn code_purge_remains_lock_free_code_admission() {
    let mut options = code_options();
    options.purge = true;
    options.force = true;
    let command = Command::Code(options);

    assert!(matches!(
        classify_command(&command),
        CommandClassification::Code
    ));
    assert!(
        acquire_command_lock(&command)
            .expect("purge admission")
            .is_none(),
        "code purge must remain outside generic project-row locking"
    );
}

#[test]
fn dispatch_classification_pins_every_persistent_writer_arm() {
    let scope = || ScopeSelection::topic("classification-fixture");
    let commands = vec![
        Command::Index {
            scope: scope(),
            force: false,
        },
        Command::Collect { scope: scope() },
        Command::IngestFile {
            path: PathBuf::from("source.md"),
            scope: scope(),
            options: IngestFileOptions {
                no_ai: true,
                translate: false,
                target_lang: None,
                video_frame_interval_seconds: None,
            },
        },
        Command::IngestUrl {
            urls: vec!["https://example.com".to_string()],
            scope: scope(),
            max_age_hours: 24,
        },
        Command::SyncSessions {
            scope: scope(),
            options: SyncSessionsOptions::default(),
        },
        Command::Refresh {
            scope: scope(),
            source_ids: Vec::new(),
            dry_run: false,
        },
        Command::RemoveSource {
            id: "source-id".to_string(),
            scope: scope(),
            dry_run: false,
            keep_asset: false,
        },
    ];

    for command in commands {
        assert!(matches!(
            classify_command(&command),
            CommandClassification::PersistentWriter { .. }
        ));
    }
}

#[test]
fn project_sync_sessions_is_fenced_through_every_persistent_write_phase() {
    let command = Command::SyncSessions {
        scope: ScopeSelection::project("/tmp/sync-sessions-project"),
        options: SyncSessionsOptions::default(),
    };
    let classification = classify_command(&command);
    let CommandClassification::PersistentWriter {
        command: command_name,
        ..
    } = classification
    else {
        panic!("project sync-sessions must be classified as a writer");
    };
    assert_eq!(command_name, "gwiki sync-sessions");

    let backend = RecordingBackend::live();
    let unlocks = backend.unlocks();
    let guard = acquire_writer_lock_for_test(backend, PROJECT_ID).expect("writer admission");
    let mut completed_phases = Vec::new();

    run_with_project_lock(Some(guard), || {
        run_persistent_write_phases(
            &mut completed_phases,
            |phases| {
                assert_eq!(unlocks.get(), 0, "PostgreSQL session write is fenced");
                phases.push("postgres");
                Ok(true)
            },
            |has_changes| *has_changes,
            |phases| {
                assert_eq!(unlocks.get(), 0, "Qdrant session sync is fenced");
                phases.push("qdrant");
                Ok(())
            },
            |phases| {
                assert_eq!(unlocks.get(), 0, "Falkor session sync is fenced");
                phases.push("falkor");
                Ok(())
            },
        )
        .expect("persistent sync phases");
    });
    assert_eq!(completed_phases, ["postgres", "qdrant", "falkor"]);
    assert_eq!(unlocks.get(), 1, "guard releases after all sync phases");
}

#[test]
fn dry_run_variants_are_classified_as_read_only() {
    for command in [
        Command::Refresh {
            scope: ScopeSelection::topic("classification-fixture"),
            source_ids: Vec::new(),
            dry_run: true,
        },
        Command::RemoveSource {
            id: "source-id".to_string(),
            scope: ScopeSelection::topic("classification-fixture"),
            dry_run: true,
            keep_asset: false,
        },
    ] {
        assert!(matches!(
            classify_command(&command),
            CommandClassification::ReadOnly
        ));
    }
}

#[test]
fn purge_has_its_own_serialized_cleanup_classification() {
    let command = Command::Purge {
        target: PurgeTarget::selection(ScopeSelection::topic("classification-fixture")),
        yes: true,
    };

    assert!(matches!(
        classify_command(&command),
        CommandClassification::ExplicitPurge { .. }
    ));
}

#[test]
fn id_native_purge_admission_does_not_resolve_a_project_root() {
    let project_id = "7c2f6952-2c51-4c57-a5f9-b5ac194b6599";

    assert_eq!(
        purge_project_id_for_admission(&PurgeTarget::project_id(project_id))
            .expect("ID-native admission"),
        Some(project_id.to_string())
    );
}

#[test]
fn topic_scopes_skip_project_lock_admission() {
    let selection = ScopeSelection::topic("classification-fixture");

    assert_eq!(
        project_id_for_admission(&selection).expect("topic scope"),
        None
    );
}

#[derive(Debug)]
struct RecordingBackend {
    unlocks: Rc<Cell<usize>>,
}

impl RecordingBackend {
    fn live() -> Self {
        Self {
            unlocks: Rc::new(Cell::new(0)),
        }
    }

    fn unlocks(&self) -> Rc<Cell<usize>> {
        Rc::clone(&self.unlocks)
    }
}

impl ProjectLockBackend for RecordingBackend {
    fn try_lock(&mut self, _key: i64) -> Result<bool, String> {
        Ok(true)
    }

    fn project_state(&mut self, _project_id: &str) -> Result<ProjectRowState, String> {
        Ok(ProjectRowState::Live)
    }

    fn unlock(&mut self, _key: i64) -> Result<bool, String> {
        self.unlocks.set(self.unlocks.get() + 1);
        Ok(true)
    }
}
