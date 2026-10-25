//! Where a failed restore goes (Decision 12): the earlier image, exec'd once
//! with `--resume-fallback` over the unchanged state file and original fds,
//! or exit 70 when this already is that fallback, the image is not the one
//! the attempt recorded, or the exec fails.

use std::io;
use std::os::unix::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::Command;

use serde::Deserialize;

use super::{parse_state, state_value, HandoverState, RestoreFailure, RESTORE_FAILURE_ENV};
use crate::host::image::PinnedImage;

/// The exit status of a restore that cannot fall back.
pub(crate) const RESTORE_FAILED: i32 = 70;

type PanicHook = Box<dyn Fn(&std::panic::PanicHookInfo<'_>) + Sync + Send + 'static>;

/// Reads the state file a restore starts from, with the fallback that covers
/// every failure after it. The earlier image is read before this image's
/// types are applied, so a state this image cannot parse still falls back;
/// a file that names no earlier image ends the process here.
pub(crate) fn begin(state: &Path, fallback: bool) -> (HandoverState, Fallback) {
    let resumed_from = fallback.then(take_reported_failure);
    let value = state_value(state);
    let recorded = value
        .as_ref()
        .ok()
        .and_then(|value| Recorded::deserialize(value).ok());
    let covering = Fallback::new(state, resumed_from, recorded);
    match value.and_then(parse_state) {
        Ok(carried) => (carried, covering),
        Err(err) => covering.fail(RestoreFailure::StateUnreadable, &err),
    }
}

/// The reason the failing image reported, cleared so that no pane child and
/// no later exec inherits it. Std serializes the removal with Rust readers,
/// and no pane or vt engine code that could read the environment runs yet.
fn take_reported_failure() -> RestoreFailure {
    let reported = std::env::var(RESTORE_FAILURE_ENV).ok();
    std::env::remove_var(RESTORE_FAILURE_ENV);
    RestoreFailure::reported(reported.as_deref())
}

/// The part of the state file a fallback needs; format 1 has always had it.
#[derive(Deserialize)]
struct Recorded {
    previous_image: PathBuf,
    argv: Vec<String>,
    attempt: RecordedAttempt,
}

#[derive(Deserialize)]
struct RecordedAttempt {
    previous_sha256: String,
}

#[derive(Clone)]
pub(crate) struct Fallback {
    previous_image: Option<PinnedImage>,
    argv: Vec<String>,
    state: PathBuf,
    /// Why the image this one took over from failed, when this is the fallback.
    resumed_from: Option<RestoreFailure>,
}

impl Fallback {
    fn new(state: &Path, resumed_from: Option<RestoreFailure>, recorded: Option<Recorded>) -> Self {
        let (previous_image, argv) = match recorded {
            Some(recorded) => (
                Some(PinnedImage {
                    path: recorded.previous_image,
                    sha256: recorded.attempt.previous_sha256,
                }),
                recorded.argv,
            ),
            None => (None, Vec::new()),
        };
        Self {
            previous_image,
            argv,
            state: state.to_path_buf(),
            resumed_from,
        }
    }

    /// Why the failed image fell back to this one; `None` for a first restore.
    pub(crate) fn resumed_from(&self) -> Option<RestoreFailure> {
        self.resumed_from
    }

    /// Ends a restore that failed with `err`.
    pub(crate) fn fail(&self, reason: RestoreFailure, err: &io::Error) -> ! {
        tracing::error!(
            state = %self.state.display(),
            fallback = self.resumed_from.is_some(),
            reason = reason.as_str(),
            err = %err,
            "gterm host restore failed"
        );
        self.end(reason)
    }

    /// Routes a panic on any thread, a Stage actor's included, to the same
    /// end until the guard disarms.
    pub(crate) fn arm(&self) -> PanicGuard {
        let previous = std::panic::take_hook();
        let fallback = self.clone();
        std::panic::set_hook(Box::new(move |info| {
            // Not through tracing: the panic may have come from inside it.
            eprintln!("gterm host: restore panicked: {info}");
            fallback.end(RestoreFailure::Panicked)
        }));
        PanicGuard { previous }
    }

    fn end(&self, reason: RestoreFailure) -> ! {
        if let (None, Some(image)) = (self.resumed_from, &self.previous_image) {
            // Only the image the attempt recorded may run with these fds.
            let err = match image.verify() {
                Ok(()) => {
                    let mut command = Command::new(&image.path);
                    if let Some((arg0, args)) = self.argv.split_first() {
                        command.arg0(arg0).args(args);
                    }
                    command
                        .env(RESTORE_FAILURE_ENV, reason.as_str())
                        .arg("--resume-state")
                        .arg(&self.state)
                        .arg("--resume-fallback")
                        .exec()
                }
                Err(err) => err,
            };
            eprintln!("gterm host: fallback exec {}: {err}", image.path.display());
        }
        std::process::exit(RESTORE_FAILED)
    }
}

/// The panic hook [`Fallback::arm`] replaced, restored at Commit.
pub(crate) struct PanicGuard {
    previous: PanicHook,
}

impl PanicGuard {
    pub(crate) fn disarm(self) {
        std::panic::set_hook(self.previous);
    }
}
