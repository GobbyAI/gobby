use std::path::Path;

use crate::WikiError;
use crate::api::IngestFileOptions;
use crate::ingest::IngestResult;
use crate::sources::{SourceManifest, SourceReplay};

pub(super) fn attach_replay_metadata(
    vault_root: &Path,
    result: &mut IngestResult,
    path: &Path,
    options: &IngestFileOptions,
) -> Result<(), WikiError> {
    let replay = SourceReplay::local_file(path.to_path_buf(), options);
    SourceManifest::update(vault_root, |manifest| {
        let Some(entry) = manifest
            .entries
            .iter_mut()
            .find(|entry| entry.id == result.record.id)
        else {
            result.record.replay = Some(replay.clone());
            return Ok(false);
        };
        if entry.replay.as_ref() == Some(&replay) {
            result.record.replay = Some(replay);
            return Ok(false);
        }
        entry.replay = Some(replay.clone());
        result.record.replay = Some(replay);
        Ok(true)
    })
}
