use std::cell::Cell;
use std::path::PathBuf;
use std::rc::Rc;

use super::*;
use crate::project_lock::{
    ProjectLockBackend, ProjectRowState, acquire_writer_lock_for_test, run_with_project_lock,
};
use crate::{IngestFileOptions, SyncSessionsOptions};

const PROJECT_ID: &str = "d45545c5-ded5-4335-b115-0245752edacf";

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
                transcription_routing: None,
                vision_routing: None,
                text_routing: None,
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

    run_with_project_lock(Some(guard), || {
        assert_eq!(unlocks.get(), 0, "PostgreSQL session write is fenced");
        assert_eq!(unlocks.get(), 0, "Qdrant session sync is fenced");
        assert_eq!(unlocks.get(), 0, "Falkor session sync is fenced");
    });
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
