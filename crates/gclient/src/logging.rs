//! File-backed tracing for the terminal client.

use std::fs::{self, File, OpenOptions};
use std::io::{self, Write};
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::time::{SystemTime, UNIX_EPOCH};

use anyhow::{Context, Result};
use tracing_subscriber::fmt::MakeWriter;

const LOG_FILE_NAME: &str = "gclient.log";
const SECONDS_PER_DAY: u64 = 24 * 60 * 60;

type DayClock = Arc<dyn Fn() -> u64 + Send + Sync>;

/// Install the process-wide tracing subscriber for `gclient`.
///
/// Logging is file-only. Failure to create the log file is returned to the
/// caller instead of silently falling back to stdout and corrupting the TUI.
pub fn init() -> Result<PathBuf> {
    let home = std::env::var_os("HOME").context("HOME is not set")?;
    init_in(&PathBuf::from(home).join(".gobby"))
}

/// Install file tracing below an explicit Gobby home directory.
pub fn init_in(gobby_home: &Path) -> Result<PathBuf> {
    let log_path = gobby_home.join("logs").join(LOG_FILE_NAME);
    let writer = DailyFileWriter::new(log_path.clone())
        .with_context(|| format!("failed to open {}", log_path.display()))?;
    let env_filter = tracing_subscriber::EnvFilter::try_from_default_env()
        .unwrap_or_else(|_| tracing_subscriber::EnvFilter::new("info"));

    tracing_subscriber::fmt()
        .with_ansi(false)
        .with_env_filter(env_filter)
        .with_writer(writer)
        .try_init()
        .map_err(|error| {
            anyhow::anyhow!("failed to install gclient tracing subscriber: {error}")
        })?;

    Ok(log_path)
}

#[derive(Clone)]
struct DailyFileWriter {
    state: Arc<Mutex<WriterState>>,
    clock: DayClock,
}

struct WriterState {
    path: PathBuf,
    file: Option<File>,
    day: u64,
}

struct EventWriter {
    state: Arc<Mutex<WriterState>>,
    day: u64,
}

impl DailyFileWriter {
    fn new(path: PathBuf) -> io::Result<Self> {
        Self::with_clock(path, Arc::new(current_day))
    }

    fn with_clock(path: PathBuf, clock: DayClock) -> io::Result<Self> {
        if let Some(parent) = path.parent() {
            fs::create_dir_all(parent)?;
        }

        let day = clock();
        rotate_stale_file(&path, day)?;
        let file = open_log(&path)?;

        Ok(Self {
            state: Arc::new(Mutex::new(WriterState {
                path,
                file: Some(file),
                day,
            })),
            clock,
        })
    }
}

impl<'a> MakeWriter<'a> for DailyFileWriter {
    type Writer = EventWriter;

    fn make_writer(&'a self) -> Self::Writer {
        EventWriter {
            state: Arc::clone(&self.state),
            day: (self.clock)(),
        }
    }
}

impl Write for EventWriter {
    fn write(&mut self, buffer: &[u8]) -> io::Result<usize> {
        let mut state = self
            .state
            .lock()
            .map_err(|_| io::Error::other("gclient log writer lock poisoned"))?;
        state.rotate_to(self.day)?;
        state
            .file
            .as_mut()
            .ok_or_else(|| io::Error::other("gclient log file unavailable"))?
            .write(buffer)
    }

    fn flush(&mut self) -> io::Result<()> {
        let mut state = self
            .state
            .lock()
            .map_err(|_| io::Error::other("gclient log writer lock poisoned"))?;
        state
            .file
            .as_mut()
            .ok_or_else(|| io::Error::other("gclient log file unavailable"))?
            .flush()
    }
}

impl WriterState {
    fn rotate_to(&mut self, day: u64) -> io::Result<()> {
        if self.day == day {
            return Ok(());
        }

        if let Some(mut file) = self.file.take() {
            if let Err(error) = file.flush() {
                self.file = Some(file);
                return Err(error);
            }
        }

        let rotation = next_rotation_path(&self.path, self.day);
        if self.path.exists() {
            if let Err(error) = fs::rename(&self.path, rotation) {
                self.file = Some(open_log(&self.path)?);
                return Err(error);
            }
        }

        self.file = Some(open_log(&self.path)?);
        self.day = day;
        Ok(())
    }
}

fn open_log(path: &Path) -> io::Result<File> {
    OpenOptions::new().create(true).append(true).open(path)
}

fn rotate_stale_file(path: &Path, today: u64) -> io::Result<()> {
    let metadata = match path.metadata() {
        Ok(metadata) => metadata,
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(()),
        Err(error) => return Err(error),
    };
    let modified_day = day_from(metadata.modified()?);
    if modified_day != today {
        fs::rename(path, next_rotation_path(path, modified_day))?;
    }
    Ok(())
}

fn next_rotation_path(path: &Path, day: u64) -> PathBuf {
    let file_name = path
        .file_name()
        .and_then(|name| name.to_str())
        .unwrap_or(LOG_FILE_NAME);
    let initial = path.with_file_name(format!("{file_name}.{day}"));
    if !initial.exists() {
        return initial;
    }

    for suffix in 1_u64.. {
        let candidate = path.with_file_name(format!("{file_name}.{day}.{suffix}"));
        if !candidate.exists() {
            return candidate;
        }
    }

    unreachable!("u64 rotation suffix space is exhausted")
}

fn current_day() -> u64 {
    day_from(SystemTime::now())
}

fn day_from(time: SystemTime) -> u64 {
    time.duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs()
        / SECONDS_PER_DAY
}

#[cfg(test)]
mod tests {
    use std::sync::atomic::{AtomicU64, Ordering};

    use super::*;

    #[test]
    fn tracing_is_file_only_and_rotates_at_the_day_boundary() -> Result<()> {
        let directory = tempfile::tempdir()?;
        let log_path = directory.path().join(LOG_FILE_NAME);
        let day = Arc::new(AtomicU64::new(42));
        let clock_day = Arc::clone(&day);
        let writer = DailyFileWriter::with_clock(
            log_path.clone(),
            Arc::new(move || clock_day.load(Ordering::SeqCst)),
        )?;
        let first_subscriber = tracing_subscriber::fmt()
            .without_time()
            .with_ansi(false)
            .with_target(false)
            .with_writer(writer.clone())
            .finish();

        tracing::subscriber::with_default(first_subscriber, || tracing::info!("before rotation"));
        day.store(43, Ordering::SeqCst);

        let second_subscriber = tracing_subscriber::fmt()
            .without_time()
            .with_ansi(false)
            .with_target(false)
            .with_writer(writer)
            .finish();
        tracing::subscriber::with_default(second_subscriber, || tracing::info!("after rotation"));

        let active = fs::read_to_string(&log_path)?;
        let rotated = fs::read_to_string(directory.path().join("gclient.log.42"))?;
        assert!(active.contains("after rotation"));
        assert!(!active.contains("before rotation"));
        assert!(rotated.contains("before rotation"));
        Ok(())
    }
}
