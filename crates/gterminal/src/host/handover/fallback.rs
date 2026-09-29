//! Where a failed restore goes (Decision 12): the earlier image, exec'd once
//! with `--resume-fallback` over the unchanged state file and original fds,
//! or exit 70 when this already is that fallback, the image is not the one
//! the attempt recorded, or the exec fails.

use std::io;
use std::os::unix::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::Command;

use super::{read_state, HandoverState};
use crate::host::image::PinnedImage;

/// The exit status of a restore that cannot fall back.
pub(crate) const RESTORE_FAILED: i32 = 70;

type PanicHook = Box<dyn Fn(&std::panic::PanicHookInfo<'_>) + Sync + Send + 'static>;

/// Reads the state file a restore starts from, with the fallback that covers
/// every failure after it. An unreadable file names no earlier image, so it
/// ends the process here.
pub(crate) fn begin(state: &Path, fallback: bool) -> (HandoverState, Fallback) {
    match read_state(state) {
        Ok(carried) => {
            let covering = Fallback::new(&carried, state, fallback);
            (carried, covering)
        }
        Err(err) => Fallback::unreadable(state, fallback).fail(&err),
    }
}

#[derive(Clone)]
pub(crate) struct Fallback {
    previous_image: Option<PinnedImage>,
    argv: Vec<String>,
    state: PathBuf,
    fallback: bool,
}

impl Fallback {
    fn new(carried: &HandoverState, state: &Path, fallback: bool) -> Self {
        Self {
            previous_image: Some(PinnedImage {
                path: carried.previous_image.clone(),
                sha256: carried.attempt.previous_sha256.clone(),
            }),
            argv: carried.argv.clone(),
            state: state.to_path_buf(),
            fallback,
        }
    }

    /// A restore whose state file is unreadable has no earlier image to name.
    fn unreadable(state: &Path, fallback: bool) -> Self {
        Self {
            previous_image: None,
            argv: Vec::new(),
            state: state.to_path_buf(),
            fallback,
        }
    }

    /// Ends a restore that failed with `err`.
    pub(crate) fn fail(&self, err: &io::Error) -> ! {
        tracing::error!(
            state = %self.state.display(),
            fallback = self.fallback,
            err = %err,
            "gterm host restore failed"
        );
        self.end()
    }

    /// Routes a panic on any thread, a Stage actor's included, to the same
    /// end until the guard disarms.
    pub(crate) fn arm(&self) -> PanicGuard {
        let previous = std::panic::take_hook();
        let fallback = self.clone();
        std::panic::set_hook(Box::new(move |info| {
            // Not through tracing: the panic may have come from inside it.
            eprintln!("gterm host: restore panicked: {info}");
            fallback.end()
        }));
        PanicGuard { previous }
    }

    fn end(&self) -> ! {
        if let (false, Some(image)) = (self.fallback, &self.previous_image) {
            // Only the image the attempt recorded may run with these fds.
            let err = match image.verify() {
                Ok(()) => {
                    let mut command = Command::new(&image.path);
                    if let Some((arg0, args)) = self.argv.split_first() {
                        command.arg0(arg0).args(args);
                    }
                    command
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
