# Install golden tree

`golden_tree.json` is what the Python `gobby install` component runners write, and
what their uninstall steps leave, for the six components claude, codex, grok, droid,
agy and git-hooks. It is recorded on a temporary HOME and is the oracle a port of the
installers replays against. The document's schema is fixed in the module docstring of
`tests/contracts/install_corpus.py`.

## Record and replay

Record (writes `golden_tree.json`; records twice in one run and refuses if the two
recordings differ):

```bash
GOBBY_RECORD_INSTALL_CONTRACTS=1 DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/contracts/test_install_corpus.py::test_record_install_contracts -v
```

Replay and check:

```bash
DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/contracts/test_install_corpus.py -v
```

The recorder drives `run_install_components` and `run_uninstall_components` from
`src/gobby/cli/install_components.py`, one component at a time, with the default
configuration, so the hook timeout is the configured default and no database is
opened. It installs all six components in order, then uninstalls them in the same
order, and snapshots the HOME and the project after every step. Every step must report
success; a failed step stops the recording rather than freezing a partial tree.

The temporary root holds three directories: `home` (HOME, USERPROFILE, and
GOBBY_HOME at `home/.gobby`), `project` (a fresh `git init`) and `path` (an empty
directory that is the first PATH entry; the second is the directory of `git`). The
provider CLIs, `pre-commit`, `ghook`, `gcode` and `gdaemon` are absent by design, and
the recorder refuses to run if any of them resolves on that PATH. The one piece of an
existing install the runners need is a ghook version stamp,
`home/.gobby/bin/.ghook-version`, holding the version of `crates/ghook/Cargo.toml`:
without a ghook binary, the grok installer reads that stamp to clear its minimum
ghook version. The stamp is written before the first snapshot, so it is not an entry.

## Normalization

Text content and paths are written relative to four placeholders, longest host value
first, each in its configured and its resolved spelling:

- `@HOME@`: the temporary HOME.
- `@PROJECT@`: the temporary git project.
- `@INSTALL_DIR@`: `gobby.paths.get_install_dir()`, the bundled install tree.
- `@PYTHON@`: `sys.executable`.

The recording root is resolved before anything is written, because trust seeding
writes both the configured and the resolved Gobby home: a root reached through a
symlink (macOS `/var` is `/private/var`) would record two entries that normalize to
the same key. Modes are four octal digits after `umask 022`. `time.time` returns the
fixed instant 1767225600 (2026-01-01T00:00:00Z) during recording, because the
installers and the MCP config writers name their backups by the clock; the backup
names in the golden carry that instant. Bytes that are not UTF-8 are recorded
as a `sha256` digest. Directories are not entries; an installer that creates a
symlink stops the recording.

## Bundled source reference

Nothing is excluded. An installed file whose bytes equal a file under
`src/gobby/install/shared/` is an entry with `path`, `mode` and `source` (its path
under that tree) instead of its text. A replay compares the source name, so an
installer that copies a different bundled file, or changes the bytes it copies, is a
difference. When several bundled files have the same bytes, `source` is the one whose
trailing path segments match the installed path best, then the first by name.

## Uninstall disposition

Each component's uninstall entries give, for every path its install step wrote, the
state after that component's uninstall step relative to the state just before it:
`removed`, `kept` or `modified`. A path the uninstall step creates (a settings
backup, for example) is `created`. Every disposition except `removed` carries the
path's mode and content, as an install entry does. The recorder refuses an uninstall
step that changes a path its own component never installed.

## Containment

The recording never reads or writes the real HOME. Before any installer runs, the
recorder checks that `Path.home()`, `~`, the Gobby home, the native bin directory, the
Claude config file and the Gobby hooks directory all resolve under the recording root.
The environment is cleared of every variable with a prefix that redirects an
installer, git or a CLI's config home (`GOBBY_`, `CLAUDE`, `CODEX_`, `GROK_`,
`FACTORY_`, `DROID_`, `AGY_`, `GEMINI_`, `GIT_`, `XDG_`); git reads no system
config and `/dev/null` as its global config. The environment, `Path.home`, the
clock and the umask are restored when the recording ends.

`test_install_recorder_writes_only_under_temp_home` proves it: it snapshots a
sentinel HOME (with `.claude`, `.codex` and `.gitconfig`) and a file beside the
recording root before and after recording, and observes this interpreter's writes to
the checkout and the sentinel HOME with `tests/fixtures/external_write_audit.py`. The
write audit sees this interpreter only; the `git init` subprocess is covered by the
tree snapshot.

## Date stamp

Recorded 2026-11-19 on macOS 27.0.1 (Darwin 27.0.0) with Python 3.14.3 and
git 2.54.0 (Apple Git-157); the ghook stamp read 0.9.88. No provider CLI was installed
on the recording PATH, so no CLI version applies. These values live here, outside the
compared bytes.

## Platforms

The golden records POSIX modes and is recorded on macOS. The two tests that replay it
(`test_python_install_matches_golden_tree` and `test_install_recording_is_deterministic`)
skip on Windows. The installers on this path branch on the platform only for Windows
(`.exe` names in `src/gobby/utils/native_bin.py`, drive-letter paths in
`src/gobby/agents/trust.py`), so a Linux replay is expected to match, but no Linux or
Windows recording has been taken: those legs are unproven.
