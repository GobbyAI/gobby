//! The routes: which files a record reaches, and the handle that changes their settings.
//!
//! A record's target picks its surface. A primary surface writes its own file in the
//! configured layout (the rule allow audit file holds the bare message, one JSON line
//! each); a parser record writes its CLI's file as the bare message, at INFO whatever the
//! configured level. WARNING and above also reach `errors.log`, except a WARNING marked
//! `pipeline_guard_refused`. Each WARNING-and-above source record counts once in
//! `logging_records_total`.

use std::collections::HashMap;
use std::collections::hash_map::Entry;
use std::fmt::Debug;
use std::io;
use std::path::PathBuf;
use std::sync::{Arc, Mutex, MutexGuard, PoisonError};

use gobby_core::telemetry::Telemetry;
use gobby_core::telemetry::names::LOGGING_RECORDS_TOTAL;
use tracing::Event;
#[cfg(test)]
use tracing::Subscriber;
use tracing::field::{Field, Visit};
#[cfg(test)]
use tracing_subscriber::Layer;
#[cfg(test)]
use tracing_subscriber::layer::Context;

use super::classify::{self, Surface};
use super::format::{Record, format_json, format_text};
use super::writer::{RotationOwner, SizeRotatingFile};
use super::{Format, Level, LogSettings, local_timestamp, raw_stderr};

/// The rotation unit of the size keys.
const MIB: u64 = 1024 * 1024;

/// The field that keeps a designed pipeline guard refusal out of `errors.log`.
const GUARD_REFUSED_FIELD: &str = "pipeline_guard_refused";

/// A handle on one set of routes, shared by the subscriber and the epoch applier.
#[derive(Clone, Debug)]
pub(crate) struct RouteHandle {
    routes: Arc<Mutex<Routes>>,
}

#[derive(Debug)]
struct Routes {
    settings: LogSettings,
    owner: RotationOwner,
    /// The open files, by path. Emptied when the settings change, so each file reopens
    /// with the new directory, threshold and backups; append mode makes that lossless.
    files: HashMap<PathBuf, SizeRotatingFile>,
}

/// The size and backup keys a file rotates on.
#[derive(Clone, Copy)]
enum Keys {
    Daemon,
    Llm,
    AllowAudit,
}

impl RouteHandle {
    pub(crate) fn new(settings: LogSettings, owner: RotationOwner) -> Self {
        Self {
            routes: Arc::new(Mutex::new(Routes {
                settings,
                owner,
                files: HashMap::new(),
            })),
        }
    }

    fn lock(&self) -> MutexGuard<'_, Routes> {
        self.routes.lock().unwrap_or_else(PoisonError::into_inner)
    }

    /// The settings the routes write under now.
    pub(crate) fn settings(&self) -> LogSettings {
        self.lock().settings.clone()
    }

    /// Writes under `settings` from the next record on.
    pub(crate) fn replace_settings(&self, settings: LogSettings) {
        let mut routes = self.lock();
        if routes.settings != settings {
            routes.settings = settings;
            routes.files.clear();
        }
    }

    /// Opens every primary file and `errors.log`, creating the directory.
    pub(crate) fn open_all(&self) -> io::Result<()> {
        let mut routes = self.lock();
        let surfaces = [
            (Surface::Daemon, Keys::Daemon),
            (Surface::Hooks, Keys::Daemon),
            (Surface::Llm, Keys::Llm),
            (Surface::Mcp, Keys::Daemon),
            (Surface::Automation, Keys::Daemon),
            (Surface::AllowAudit, Keys::AllowAudit),
            (Surface::Errors, Keys::Daemon),
        ];
        for (surface, keys) in surfaces {
            routes.file(classify::primary_file_name(surface), keys)?;
        }
        Ok(())
    }

    /// Writes `event` to its routes, when its target is in the `gobby` namespace and its
    /// level passes.
    pub(crate) fn route(&self, event: &Event<'_>) {
        let metadata = event.metadata();
        let target = metadata.target();
        let (Some(surface), Some(level)) = (
            classify::surface_for_target(target),
            Level::from_tracing(*metadata.level()),
        ) else {
            return;
        };
        let mut routes = self.lock();
        let floor = match surface {
            Surface::Parser => Level::Info,
            _ => routes.settings.level,
        };
        if level < floor {
            return;
        }
        let mut fields = Fields::default();
        event.record(&mut fields);
        let module = metadata
            .module_path()
            .and_then(|path| path.rsplit("::").next())
            .unwrap_or(target);
        let record = Record {
            time: local_timestamp(),
            level,
            target: target.to_owned(),
            module: module.to_owned(),
            message: fields.message,
            extras: fields.extras,
        };
        routes.write(surface, &record, fields.guard_refused);
    }
}

impl Routes {
    fn write(&mut self, surface: Surface, record: &Record, guard_refused: bool) {
        count(surface, record.level);
        let primary = match surface {
            Surface::Parser => match classify::parser_cli(&record.target) {
                Some(cli) => {
                    let name = format!("{cli}-parser-error.log");
                    self.append(&name, Keys::Daemon, &record.message);
                    None
                }
                None => {
                    // A name that is not a plain file name could leave the logs directory.
                    let warning = Record {
                        level: Level::Warning,
                        target: "gobby.daemon".to_owned(),
                        module: "logging".to_owned(),
                        message: format!(
                            "parser log name {:?} is not a plain file name; its record goes to daemon.log",
                            record.target
                        ),
                        extras: Vec::new(),
                        ..record.clone()
                    };
                    self.write(Surface::Daemon, &warning, false);
                    Some(Surface::Daemon)
                }
            },
            other => Some(other),
        };
        if let Some(primary) = primary {
            let (line, keys) = match primary {
                Surface::AllowAudit => (record.message.clone(), Keys::AllowAudit),
                Surface::Llm => (self.layout(record), Keys::Llm),
                _ => (self.layout(record), Keys::Daemon),
            };
            self.append(classify::primary_file_name(primary), keys, &line);
        }
        let guarded = record.level == Level::Warning && guard_refused;
        if record.level >= Level::Warning && !guarded {
            let line = self.layout(record);
            self.append(
                classify::primary_file_name(Surface::Errors),
                Keys::Daemon,
                &line,
            );
        }
    }

    fn layout(&self, record: &Record) -> String {
        match self.settings.format {
            Format::Text => format_text(record),
            Format::Json => format_json(record).unwrap_or_else(|_| format_text(record)),
        }
    }

    /// Appends `line` to `name`. A file that cannot be written is reported on stderr, as
    /// Python's handler reports its own write errors.
    fn append(&mut self, name: &str, keys: Keys, line: &str) {
        let written = self
            .file(name, keys)
            .and_then(|file| file.write_line(line.as_bytes()));
        if let Err(error) = written {
            let path = self.settings.dir.join(name);
            raw_stderr(&format!(
                "gdaemon logging: cannot write {}: {error}",
                path.display()
            ));
        }
    }

    fn file(&mut self, name: &str, keys: Keys) -> io::Result<&SizeRotatingFile> {
        let (size_mb, backups) = match keys {
            Keys::Daemon => (self.settings.max_size_mb, self.settings.backup_count),
            Keys::Llm => (
                self.settings.llm_max_size_mb,
                self.settings.llm_backup_count,
            ),
            Keys::AllowAudit => (
                self.settings.allow_audit_max_size_mb,
                self.settings.allow_audit_backup_count,
            ),
        };
        let path = self.settings.dir.join(name);
        match self.files.entry(path) {
            Entry::Occupied(entry) => Ok(entry.into_mut()),
            Entry::Vacant(entry) => {
                let file = SizeRotatingFile::open(
                    entry.key(),
                    size_mb.saturating_mul(MIB),
                    backups,
                    self.owner,
                )?;
                Ok(entry.insert(file))
            }
        }
    }
}

/// Counts one source record at WARNING or above.
fn count(surface: Surface, level: Level) {
    let severity = match level {
        Level::Warning => "WARNING",
        Level::Error => "ERROR",
        Level::Debug | Level::Info => return,
    };
    if let Some(label) = classify::metric_label(surface) {
        Telemetry::global().inc_counter(
            LOGGING_RECORDS_TOTAL,
            &[("surface", label), ("severity", severity)],
            1,
        );
    }
}

/// A record's message and its other fields, in the order they were recorded.
#[derive(Default)]
pub(super) struct Fields {
    pub(super) message: String,
    extras: Vec<(String, String)>,
    guard_refused: bool,
}

impl Fields {
    fn push(&mut self, field: &Field, value: String) {
        if field.name() == "message" {
            self.message = value;
        } else {
            self.extras.push((field.name().to_owned(), value));
        }
    }
}

impl Visit for Fields {
    fn record_str(&mut self, field: &Field, value: &str) {
        self.push(field, value.to_owned());
    }

    fn record_bool(&mut self, field: &Field, value: bool) {
        if field.name() == GUARD_REFUSED_FIELD && value {
            self.guard_refused = true;
        }
        self.push(field, value.to_string());
    }

    fn record_debug(&mut self, field: &Field, value: &dyn Debug) {
        self.push(field, format!("{value:?}"));
    }
}

/// A layer with its own routes, which tests install on their own thread.
#[cfg(test)]
#[derive(Debug)]
pub(crate) struct RoutedLayer {
    handle: RouteHandle,
}

#[cfg(test)]
impl RoutedLayer {
    /// The handle that applies new settings to this layer.
    pub(crate) fn handle(&self) -> RouteHandle {
        self.handle.clone()
    }
}

#[cfg(test)]
impl<S: Subscriber> Layer<S> for RoutedLayer {
    fn on_event(&self, event: &Event<'_>, _context: Context<'_, S>) {
        self.handle.route(event);
    }
}

/// A routed layer writing under `settings`, rotating its own files.
#[cfg(test)]
pub(crate) fn routed_layer(settings: LogSettings) -> RoutedLayer {
    RoutedLayer {
        handle: RouteHandle::new(settings, RotationOwner::Gdaemon),
    }
}
