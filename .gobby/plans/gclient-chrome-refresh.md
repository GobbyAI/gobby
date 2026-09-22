Plan artifact: `.gobby/plans/gclient-chrome-refresh.md`

# gclient chrome refresh: deterministic session titles, structured chrome, a first frame, and one tagline

**Plan ID:** gclient-chrome-refresh

## Context
`kind: framing`

Session titles today follow `manual > latest active task > saved heuristic > provisional`,
and the heuristic (the first four words of the first prompt) produces noisy titles. The
user wants three deterministic formats and no heuristic: provisional
`project_name#session_ref: Provider`, with a claimed task
`project_name#session_ref: Task #task_ref - task_title`, and manual titles verbatim.

gclient's chrome then stops showing the title ladder and shows structured fields instead:
the task on the pane's top-left, the agent definition and session ref on the bottom-left,
the backend and address on the bottom-right, tab labels as `project:tab_ref`, and the
Sessions sidebar band split into Agents and Terminals. The frame gains a full-width menu bar
and a status bar, the sidebar hides by default behind an overlay, and launch draws a real
first frame (the goblin, the wordmark, the connect stages) instead of ten seconds of blank
terminal.

Every visual decision was reviewed on the Claude Design canvas **gclient chrome baseline**
(https://claude.ai/artifact/VhNEh6vKvGkJvBY79aSgbe, Version 22) over three rounds and signed
off on 2026-09-22. The canvas boards carry the exact strings, cell geometry and palette
roles; the Decision Record below names each decision with its canvas point number so an
executor can find the board. The last change requested at sign-off is the product tagline,
which moves everywhere the product describes itself in one line (P4).

The canvas link stopped resolving from Claude Code shortly after registration. The board
sources (`canvas/project/*.dc.html`, one file per board with a `Light` wrapper each), the
board screenshots (`render/*.png`, dark and light) and the mark generators live in
`/Users/josh/.claude/plans/gclient-chrome-refresh-sources/` on this machine; an executor who
cannot open the canvas reads the board there. The plan text carries every string and cell
count the boards decided, so the boards are reference, not the contract.

## Decision Record
`kind: framing`

Confirmed by the user on the canvas and at plan approval. Canvas point numbers in brackets.

1. Titles: provisional `project#ref: Provider`, task `project#ref: Task #ref - title`, manual
   verbatim; the heuristic source, its column and its trigger sites are deleted, and existing
   heuristic rows are rewritten to provisional at startup.
2. Sidebar width stays the user's 34 columns in the boards; the default is 26, the maximum 36.
   Agents rows are three lines; lines 2 and 3 start under the text and get 32 cells [1].
3. Tab labels read `project:tab_ref` (`gobby:0:0:1`); renamed tabs read `project:name`. The
   active tab is cut out in `panel_bg`, bold, under the accent menu bar; the row is `surface0` [2].
4. Terminals rows carry no address; the pane bottom-right does [3]. `Focused` moves from
   bottom-right to bottom-left beside the agent definition [4]. A bare terminal pane's top-left
   is empty unless the pane was renamed [5].
5. Interactive sessions with no agent definition show the provider label (`Claude Code`) [6].
   Line 3 of an Agents row is the model slug: display name lowercased and hyphenated with the
   effort appended (`fable-5.1-xhigh`); the provider leaves the row except on line 1 for
   interactive sessions [7].
6. Project prefix only in "All projects by priority" (`gobby#14155`); bare ref everywhere
   else, and always on the pane bottom-left, since the tab label carries the project [8].
7. Agents row line 1 leads with the definition name in bold and the ref in parentheses; the
   ref is laid out first so a long name is cut with an ellipsis [9]. Line 2 pins `Task #ref - `
   and scrolls only the title [26].
8. Frame: sidebar hidden by default, rail gone. Row 0 is a full-width accent menu bar; the tab
   bar keeps its own row; the last row is one `surface0` status bar with the count of what you
   cannot see on the left (click opens the sidebar) and the prefix hint plus mode word on the
   right. `Daemon unreachable` leads the left while it holds [10].
9. Sidebar states: overlay (ctrl+b b, View, the status count) draws over 34 columns with an
   accent edge, takes focus, rolls up on Esc or pane focus, and aligns the tab labels to the far
   side while open; pinned (View › Pin Sidebar) is the column layout. Side and pin persist in
   settings and mirror two new settings rows [11].
10. Menus: every item is a real action from `menu.rs` or the keymap; nothing drawn does
    nothing. A new `Agent` title carries nine actions (respond, mark seen, take and release
    control, take back, detach, open alert target, next and previous attention). `[view]` and
    `[working]` stay on the bands and are copied into View. Help › Daemon is a new small dialog
    for the daemon URL, version and health; About Gobby sits last on Help [12, 20]. The Gobby
    menu is Settings, Reload Config, Quit [20]. `New Project…` becomes `New Workspace…` [21].
    Take Control and Release Control stay on Agent and the pane menu; focus takes control only
    while no other client holds the terminal [19]. Arrange and New Grid join Window and the pane
    menu [24]. Splits stay right and down only [23]. Later product menus (Tasks, Workflows,
    Rules, Skills, MCP Servers) wait for the Rust port [27].
11. Attention on the pane frame: warning with ⍾, exited `destructive` with ◌, focus keeps
    accent; the tab label and the status count carry ⍾ for panes you cannot see; junction cells
    follow the focus rule [13]. Borderless panes are removed; every pane draws all four edges,
    a lone pane too; pane gaps stays [14].
12. Splash: the first frame is the full frame and is never blank: menu bar, empty tab row with
    skeleton tab blocks, the goblin and the client version in the pane area over four stages
    with timings (health, workspace attach, roster, first frame), the stalled stage and its
    elapsed time on the status bar. A ten-second start is a bug in its own right; the stage list
    shows where [15].
13. Daemon unreachable: the status bar leads with it and the retry countdown; an error toast
    names the URL; panes freeze in place with their last frame; the count stays for what is
    known; it clears itself when the daemon answers [16].
14. Empty tab: the dimmed goblin above `No pane open.` and the three ways out (ctrl+b w to
    attach, File › New Terminal to start, ctrl+b b for the sidebar) [17].
15. Dialogs and overlays are drawn as they exist with only these changes: pane borders leaves
    settings; sidebar side and sidebar pinned enter it; the alert log moves from `[Menu]` to
    Help; toast and status-mode strings are unchanged [18].
16. Keys overlay: the description is never cut; the keymap name is search data and draws only
    when the column has room for the longest row [22].
17. Status bar segments: two fixed parts (`Daemon unreachable` on the left while it holds, the
    mode word on the right while a mode is active); everything else is two named lists in
    `prefs.toml` `[status]`, including focus, model, context percent and tokens for the focused
    agent. Cost is not stored per session, so it is not a segment [25].
18. Marks. The goblin is rasterised from `web/public/logo.png` onto half-block cells, cropped
    to its drawn columns: 33 × 16 on the splash, 29 × 14 on About and the Empty tab. Colour
    roles: fill `accent`, tablet `overlay1`, outline and lenses `ink` (panel colour in dark
    mode, text colour in light), glints `glint` (text colour in dark, panel colour in light).
    Dimmed on the Empty tab: `overlay0` fill with panel-colour lines in dark mode, `surface1`
    fill with `overlay0` lines in light mode, no glints. Three homes only: splash, About, Empty
    tab; never the status bar, sidebar, menu bar, working dialogs, or the unreachable state.
19. Wordmark: lowercase `gobby` in braille, 54 × 8 (Helvetica Neue Condensed Black on the 2 × 4
    braille dot grid), in `accent`; verified in a Ghostty window. The lowercase drop shadow
    (49 × 9, letters in `accent` over a copy one column right and half a row down in `dim`) is
    the saved alternate and ships as data, unused.
20. Tagline: `fleet management for AI coding agents`. The product is AI fleet management and
    roadmap story B (hub plus nodes across the machines you own) ships next; the line is true
    today, where the fleet is the agents on one machine. It replaces every one-line product
    description in the repo, including the README hero; the README's walk-away loop sentence
    stays as body copy.

## Non-goals
`kind: framing`

- No ctrl+enter fix here: gclient never pushes the crossterm keyboard enhancement flags at
  host setup, but another session owns that task.
- No Split Left or Split Up menu items; right and down plus swap cover it.
- No Tasks, Workflows, Rules, Skills or MCP Servers menus; they follow the Rust port.
- No web UI product copy beyond the meta description; no gobby.ai site (it does not live in
  this repo). No edits to `.impeccable.md` (changes only through the impeccable skill's teach
  mode) or to `SECURITY.md`, whose "local-first daemon" states a security posture.
- No cost segment on the status bar (cost is not stored per session).
- No right-to-left frame (sidebar side, tab alignment, ticker direction) beyond the side
  setting; language support decides that later.
- No change to how the goblin is drawn on the web (`web/public/logo.png` stays the source).

## Constraints
`kind: framing`

- **Size guard.** Hand-maintained production files stay under 1,000 lines and the hook blocks
  threshold-crossing writes; gclient also enforces the ceiling in
  `crates/gclient/tests/source_size.rs`. Measured 2026-09-21: `crates/gclient/src/ui/chrome.rs`
  899, `crates/gclient/src/app/live.rs` 989, `crates/gclient/src/daemon/mod.rs` 987,
  `crates/gclient/src/daemon/live.rs` 985, `crates/gclient/src/app/live_loop/actions.rs` 979,
  `crates/gclient/src/app/live_loop/menu.rs` 970, `src/gobby/servers/routes/attention.py` 861.
  Every deliverable that targets one of these carries a split target.
- **Schema authority is gcore.** A migration is live only after the five derived carriers are
  refreshed (docs/contracts/plan-coverage.md, derived-carriers row) and the binary set is
  rebuilt and promoted via `uv run gobby cutover` from the main checkout, announced first with a
  `global` send_message (memory f71268af). `cutover` refuses uncommitted schema inputs.
- **gclient is promoted separately.** `cargo build --release -p gobby-client`, then install via
  new inode; the stamped coherent set is gcode, gdaemon, ghook only.
- **Client couples through the public API only** (ROADMAP target architecture). The one new
  field the client needs, task titles on the roster, is added daemon-side in P2; everything
  else it renders is already in `/api/agents/runs`, `/api/sessions` and the workspace stream.
- **Goldens are scripted with named panes** (memory f5164d15): a chrome change only visible on
  unnamed panes regenerates byte-identical goldens, so every P3 deliverable that changes a
  string adds a `screens.rs` STATES entry that exposes it and rehashes the parity frame digest
  only after the new state is visible.
- **Ticker.** Any new scrolling field feeds `max_travel` into the shared clock
  (`ViewState::title_travel`); two independent tickers on one frame are a defect.
- **Marks are data, not glyph art.** Half-block marks paint upper and lower subcell colours
  through a glyph whose fg/bg split matches; braille is a glyph range every terminal with a
  Unicode fallback font draws. No image protocols, no Sixel.
- **Design contract.** `.impeccable.md`: JetBrains Mono, deutan-safe palette, accent hue 125,
  every state signal survives a greyscale screenshot; the mascot is a greeting, never a state.
- **Validation commands.** Python:
  `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest <file>`,
  `uv run ruff check src/ tests/`, `uv run mypy src/`. Rust: `cargo nextest run -p gobby-client`,
  `GOBBY_UPDATE_SCREENS=1 cargo nextest run -p gobby-client --test screens`,
  `cargo clippy -p gobby-client`, `cargo fmt -p gobby-client -- --check`. Never the full pytest
  suite.

## P0: Design assets
`kind: framing`

The goblin and wordmark generators and the grids they emit exist only outside the repository,
in `/Users/josh/.claude/plans/gclient-chrome-refresh-sources/render/` on this machine. P0
lands them in the crate as data before any Rust draws a mark, so 3.8 embeds committed files
and the design can be regenerated from the source image later.

### 0.1 Marks and wordmark assets in the crate [category: config]
`kind: deliverable`

Targets:
- `crates/gclient/assets/marks/README.md`
- `crates/gclient/assets/marks/mask2.py`
- `crates/gclient/assets/marks/wordmark.py`
- `crates/gclient/assets/marks/goblin-33x16.grid`
- `crates/gclient/assets/marks/goblin-29x14.grid`
- `crates/gclient/assets/marks/wordmark-braille-54x8.txt`
- `crates/gclient/assets/marks/wordmark-shadow-49x9.grid`
- `crates/gclient/assets/marks/renders/goblin-33x16-dark.ans`
- `crates/gclient/assets/marks/renders/goblin-33x16-light.ans`
- `crates/gclient/assets/marks/renders/goblin-29x14-dark.ans`
- `crates/gclient/assets/marks/renders/goblin-29x14-light.ans`
- `crates/gclient/assets/marks/renders/wordmark-braille-dark.ans`
- `crates/gclient/assets/marks/renders/wordmark-braille-light.ans`
- `crates/gclient/assets/marks/renders/wordmark-shadow-dark.ans`
- `crates/gclient/assets/marks/renders/wordmark-shadow-light.ans`

`crates/gclient/assets/` does not exist yet; every target is a new file. The two generators are
copied from `/Users/josh/.claude/plans/gclient-chrome-refresh-sources/render/` (`mask2.py`,
201 lines; `wordmark.py`, 339 lines; the other scripts there built the canvas boards and stay
behind) and gain
one output mode each, `grid`, that writes the class grid 3.8 parses. Nothing in Python runs at
gclient runtime; the crate embeds the grid files with `include_str!` (3.8).

- `mask2.py <png> <cols> <rows> [text|html|ansi-dark|ansi-light|grid]` rasterises
  `web/public/logo.png` onto half-block cells (two subrows per cell), classifies each subcell by
  colour (internally `G` fill, `E` tablet, `W` glint, `K` outline and lenses, `.` empty, with
  `P` a fill alias; the `grid` mode writes them as the role letters `a`, `o`, `g`, `i`, `.`), and crops the box to the mark's drawn columns. The committed
  sizes are the splash mark, `goblin-33x16.grid` from a 56 × 16 box (the box is the image aspect; cropping keeps the 33 drawn columns), and the About and Empty tab
  mark, `goblin-29x14.grid` from a 48 × 14 box. There is no separate dimmed grid: the Empty tab
  remaps the same classes to its palette in 3.8.
- `wordmark.py <variant> [text|html|ansi-dark|ansi-light|grid] [lower]` draws the letters.
  `braille lower` is the splash wordmark: 8 rows of 54 braille glyphs (U+2800 to U+28FF), one
  colour, so `wordmark-braille-54x8.txt` is the glyph text itself and needs no class grid.
  `shadow lower` is the saved alternate, `wordmark-shadow-49x9.grid`, two half-cell roles per
  cell (`a` letter, `d` shadow, `.` empty). The font is Helvetica Neue Condensed Black from
  the macOS system font file; the README says the wordmark regenerates on macOS only and the
  committed grids are the artifact.
- The `.ans` renders are the existing `ansi-dark` and `ansi-light` modes written to files: 24-bit
  SGR sequences over the palette hex values, for `cat`-ing in a terminal to eyeball a mark
  without building gclient. They are not read by any code.
- `README.md` names the source image, the exact regeneration commands
  (`uv run python crates/gclient/assets/marks/mask2.py web/public/logo.png 56 16 grid >
  crates/gclient/assets/marks/goblin-33x16.grid` and the seven siblings), the class letters and
  their colour roles (fill `accent`, tablet `overlay1`, outline and lenses `ink`, glints
  `glint`; dimmed on the Empty tab: `overlay0` fill and panel-colour lines in dark mode,
  `surface1` fill and `overlay0` lines in light mode, no glints; wordmark `accent`, shadow
  `dim`), the grid file format below, and the rule that the mark has three homes (splash,
  About, Empty tab) and never appears anywhere else.

Grid file format (defined by 3.8, emitted here). Every grid is UTF-8 with LF line ends and no
trailing whitespace. Line 1 is a header, `# halfblock <cols>x<rows>` for the three `.grid`
files or `# braille <cols>x<rows>` for the `.txt`; further `#` lines are comments; then exactly
`rows` data lines. A half-block data line is exactly `2*cols` characters, where characters
`2c` and `2c+1` are the upper and lower subcell roles of cell `c`, from the alphabet `a` accent
(fill, or a wordmark letter), `o` overlay1 (the tablet), `i` ink (outline and lenses), `g`
glint, `d` dim (the shadow), `.` transparent. A braille data line is exactly `cols` characters,
each in U+2800 to U+28FF, with U+2800 meaning transparent. The dimmed goblin is a render-time
palette in 3.8 (`a` to the dim fill, `o` and `i` to the dim line colour, `g` transparent), not a
second file. The shadow grid already carries both letters (`a`) and shadow (`d`) in one grid,
the shadow one cell right and half a cell down.

Granularity: fifteen new files, one obligation (the assets exist, regenerate reproducibly and
are documented); the grids and renders are outputs of the two scripts, not independent work,
so this stays one leaf.

Research context:
- Observed: `crates/gclient/` holds `src/`, `tests/`, `Cargo.toml`, `LICENSE`, `NOTICE.md`,
  `UPSTREAM.md` and no `assets/` directory. `include_str!` is already used in the crate's tests
  (`crates/gclient/tests/daemon_live.rs`, the terminal WS goldens), so embedding text assets has
  precedent. Pillow is a project dependency (`pyproject.toml`, `pillow>=12.3.0`), so
  `uv run python` runs both generators without extra installs.
- Observed generator behaviour (`mask2.py` and `wordmark.py` in the sources folder above): `text` mode
  prints glyphs to stdout and the goblin's class rows to stderr; `html` mode paints cell
  backgrounds for the canvas boards; the ANSI modes emit 24-bit SGR. Half-block cells are
  painted as upper and lower subcell colours, never as glyph art, so any terminal font works;
  braille is a glyph range every terminal with a Unicode fallback font draws (verified in a
  Ghostty window, canvas Version 21).
- Rejected: embedding the `.ans` renders as the runtime format (they carry hex colours, not
  palette roles, so they cannot follow the theme); rendering from `logo.png` at runtime (an
  image dependency in the client for a fixed mark); a dimmed goblin grid (the dim is a palette
  remap of the same classes).
- Planned check: each regeneration command in the README rewrites its committed file
  byte-identically (`git diff --exit-code crates/gclient/assets/marks/`); `cat` of each `.ans`
  file in Ghostty shows the mark; `wc -L` of each grid matches its name.

**Acceptance:**

- 0.1.1 - Both generators live in the crate with a `grid` mode and the README's commands
  regenerate every committed grid byte-identically. file: `crates/gclient/assets/marks/mask2.py`.
  file: `crates/gclient/assets/marks/wordmark.py`. file: `crates/gclient/assets/marks/README.md`.
- 0.1.2 - The goblin grids are committed at 33 × 16 and 29 × 14 in the class format the README
  documents. file: `crates/gclient/assets/marks/goblin-33x16.grid`.
  file: `crates/gclient/assets/marks/goblin-29x14.grid`.
- 0.1.3 - The braille wordmark text (54 × 8) and the drop shadow class grid (49 × 9) are
  committed. file: `crates/gclient/assets/marks/wordmark-braille-54x8.txt`.
  file: `crates/gclient/assets/marks/wordmark-shadow-49x9.grid`.
- 0.1.4 - Dark and light `.ans` renders of all four marks are committed under `renders/`.
  file: `crates/gclient/assets/marks/renders/goblin-33x16-dark.ans`.
  file: `crates/gclient/assets/marks/renders/goblin-33x16-light.ans`.
  file: `crates/gclient/assets/marks/renders/goblin-29x14-dark.ans`.
  file: `crates/gclient/assets/marks/renders/goblin-29x14-light.ans`.
  file: `crates/gclient/assets/marks/renders/wordmark-braille-dark.ans`.
  file: `crates/gclient/assets/marks/renders/wordmark-braille-light.ans`.
  file: `crates/gclient/assets/marks/renders/wordmark-shadow-dark.ans`.
  file: `crates/gclient/assets/marks/renders/wordmark-shadow-light.ans`.
- 0.1.5 - The README states the colour roles, the three homes rule and the macOS-only wordmark
  regeneration. behavior: "three homes" in `crates/gclient/assets/marks/README.md`.

## P1: Session titles
`kind: framing`

Three deterministic title formats replace the prompt heuristic: provisional
`project#seq: Provider`, task `project#seq: Task #ref - title`, manual verbatim. The
sections land in import-safe order: 1.1 restores the provider suffix on the formatter
(no deletions, so nothing importing the storage layer breaks); 1.2 removes every trigger
and the lifecycle helpers that call the heuristic storage symbols; 1.3 deletes the
storage-layer heuristic symbols, the model field, and the SQL branch, and folds the
heuristic-row rewrite into the existing startup sweep; 1.4 drops the column with
migration 447 and its carriers; 1.5 fixes the Telegram canonical title and rewrites the
docs. 1.5 depends only on 1.1 and can run alongside 1.2 through 1.4. Memory a3e246d0 records the
heuristic lifecycle this phase removes; the session that closes 1.2 updates it.

### 1.1 Provisional titles carry the provider label again [category: code]
`kind: deliverable`

Targets:
- `src/gobby/storage/sessions/_title_defaults.py::format_provisional_session_title`
- `tests/storage/sessions/test_register_fallback.py::test_register_session_happy_path_caches_persisted_provisional_title`
- `tests/sessions/test_handoff.py::test_title_lifecycle_is_provisional_task_manual_and_clear_sticky`

Restore the body of `format_provisional_session_title` to
`return f"{project_name.strip()}#{session_seq_num}: {provider_title_label(source)}"`,
removing the `del source` line that commit `c08258e051` introduced. `provider_title_label`
already exists in the same module (it maps `claude` to `Claude`, `codex` to `Codex`,
`claude_code` to `Claude Code`, and so on, falling back to the raw source or `Unknown`),
and every call site already passes `source`, so nothing else in production changes.

Update the two tests that pin the bare format: in
`test_register_session_happy_path_caches_persisted_provisional_title` (registers with
`source="codex"`) the expected title becomes `f"test-project#{session.seq_num}: Codex"`, and
in `test_title_lifecycle_is_provisional_task_manual_and_clear_sticky` the assertion at the
provisional step becomes `f"handoff-test#{session.seq_num}: {provider_title_label(session.source)}"`
(import the helper rather than hard-coding the label so the test states the contract).

Consumers unchanged:
- `src/gobby/storage/sessions/_crud.py` — no-edit-reason: its three `format_provisional_session_title` calls (registration reuse, insert, conflict recovery) already pass `existing.source`, `source`, and `conflicting.source`.
- `tests/servers/routes/test_agent_spawn_routes.py` — no-edit-reason: it asserts against `format_provisional_session_title(...)` itself, so it follows the new format.
- `web/src/lib/sessionTitle.ts` — no-edit-reason: `SESSION_PREFIX` requires the `:` separator and already nulls provisional titles; `sessionTitle.test.ts` asserts the `project#N: Provider` shape.

Research context:
- `git show c08258e051 -- src/gobby/storage/sessions/_title_defaults.py` (checked) shows the
  exact reversal: the old body was the f-string above; the commit replaced it with
  `del source` + `f"{project_name.strip()}#{session_seq_num}"` and added
  `format_heuristic_session_title` and `HEURISTIC_TITLE_SOURCE` (both deleted in 1.3).
- `gcode grep -F provider_title_label -- src tests` (checked): only the definition exists;
  it is dead code today and becomes live again here.
- `gcode grep -F format_provisional_session_title -- src tests` (checked): callers are
  `_crud.py` (three sites), `title_lifecycle.py` (two sites), and
  `tests/servers/routes/test_agent_spawn_routes.py`.
- Bare-format assertions (checked with `gcode grep -F '#{session.seq_num}"' -- tests`; the
  other hits are session refs, not titles): `tests/storage/sessions/test_register_fallback.py`
  (`f"test-project#{session.seq_num}"`) and `tests/sessions/test_handoff.py`
  (`f"handoff-test#{session.seq_num}"`). Planned: also run the registration tests named in
  the verification line, since `test_storage_sessions_registration.py` was touched by the
  same commit.
- Rejected: keeping the bare form and adding the provider only in the clients; the
  provider suffix is part of the persisted title contract that `sessionTitle.ts` and the
  Telegram canonical title (1.5) already assume.
- Planned verification: `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/storage/sessions/test_register_fallback.py tests/sessions/test_handoff.py tests/servers/routes/test_agent_spawn_routes.py tests/storage/sessions/test_storage_sessions_registration.py`
  then `uv run ruff check src/ && uv run mypy src/`.

**Acceptance:**

- 1.1.1 - A registered codex session without a claim is titled `project#N: Codex`. symbol: `format_provisional_session_title`. test: `tests/storage/sessions/test_register_fallback.py::test_register_session_happy_path_caches_persisted_provisional_title`.
- 1.1.2 - The handoff lifecycle test asserts the provider-suffixed provisional title before the task and manual steps. test: `tests/sessions/test_handoff.py::test_title_lifecycle_is_provisional_task_manual_and_clear_sticky`.
- 1.1.3 - The spawn-route test still passes because it derives its expectation from the formatter. test: `tests/servers/routes/test_agent_spawn_routes.py::test_spawn_claims_task_for_web_chat`.

### 1.2 Remove the prompt heuristic triggers and lifecycle helpers [category: code] (depends: 1.1)
`kind: deliverable`

Targets:
- `src/gobby/sessions/title_lifecycle.py::*` — scope-reason: the stopword table, regex, limit, `heuristic_title_suffix`, `promote_heuristic_title`, the heuristic branch of `recompute_automatic_title`, and the module imports are deleted together
- `src/gobby/hooks/event_handlers/_agent.py::AgentEventHandlerMixin.handle_before_agent`
- `src/gobby/agents/spawn_executor.py::_spawn_codex_terminal`
- `src/gobby/agents/spawn_executor_codex.py`
- `src/gobby/agents/spawn_executor_providers.py::seed_heuristic_title_from_prompt`
- `src/gobby/servers/websocket/chat/_stream_persistence.py::ChatStreamPersistence.persist_user_message`
- `tests/sessions/test_title_lifecycle.py::*` — scope-reason: the five heuristic tests and the heuristic imports go; the reasoning-effort tests stay
- `tests/hooks/test_hooks_manager.py::*` — scope-reason: three title tests change shape and share the `promote_heuristic_title` patch target
- `tests/agents/test_spawn_executor.py::TestExecuteSpawn.test_codex_spawn_seeds_heuristic_from_clean_prompt`
- `tests/servers/websocket/chat/test_stream_persistence.py::*` — scope-reason: both tests exist only to cover the promotion call being removed
- `tests/sessions/test_handoff.py::test_clear_successor_generates_its_own_heuristic`

Delete from `title_lifecycle.py`: `_HEURISTIC_WORD_RE`, `_HEURISTIC_STOPWORDS`,
`_HEURISTIC_SUFFIX_LIMIT`, `heuristic_title_suffix`, `promote_heuristic_title`, the `re`
import, and the `HEURISTIC_TITLE_SOURCE` / `format_heuristic_session_title` imports. In
`recompute_automatic_title` drop the `elif isinstance(getattr(session, "heuristic_title", None), str)`
branch so the ladder is open claimed task, else provisional (`format_provisional_session_title`
with `context.source`). `update_title_for_claim`, `clear_successor_title`, and
`apply_clear_successor_title` are untouched.

Remove the four triggers. In `handle_before_agent` delete the
`promote_heuristic_title(self._session_manager, session_id, prompt)` call and the module
import; keep the surrounding prompt-metadata persistence and its warning log. In
`spawn_executor_providers.py` delete `seed_heuristic_title_from_prompt` and the
`promote_heuristic_title` import. In `_spawn_codex_terminal` delete the
`await asyncio.to_thread(seed_heuristic_title_from_prompt, request, plan.child_session_id)`
call and the `seed_heuristic_title_from_prompt` import; the following
`SessionVariableManager(...).merge_variables(... {"_agent_context_injected": True})` call
stays inside the same `if plan.inject_persona and request.session_manager is not None`
branch. In `persist_user_message` delete the `prompt_text` assembly, the
`run_db(self.owner, promote_heuristic_title, session_manager, db_session_id, prompt_text)`
call, its `try/except` and the "Failed to promote web-chat session title" debug log, and the
module import; `session_manager` and `db_session_id` lookups go too if nothing else in the
function reads them.

`spawn_executor.py` is 998 lines, so this section also splits it: move
`_spawn_codex_terminal` (32 lines; it is the only codex-only function, and the shared
`_runtime_spawn` stays where it is) out of `spawn_executor.py` into the new
`src/gobby/agents/spawn_executor_codex.py`, re-import the function at its old name so the
provider dispatch table, `getattr(spawn_executor, "_spawn_codex_terminal")` and existing
`patch("gobby.agents.spawn_executor...")` sites keep working, and confirm both files stay
under the repository's 1,000-line ceiling (about 970 and 40 lines; no 850 figure is claimed,
council round 2, adversary finding 3).

Tests: delete `test_heuristic_suffix_filters_only_confirmed_english_stopwords`,
`test_heuristic_suffix_caps_four_words_at_sixty_characters`,
`test_first_heuristic_wins_and_survives_task_precedence`,
`test_manual_title_keeps_display_while_first_heuristic_is_saved`, and
`test_concurrent_promotions_persist_one_complete_heuristic` from
`tests/sessions/test_title_lifecycle.py` and retitle the module docstring to the
reasoning-effort tests that remain; add a NEW test
`test_recompute_automatic_title_falls_back_to_provisional_after_close` that claims a task,
closes it, calls `recompute_automatic_title`, and asserts the `project#N: Provider` title
with `title_source == "provisional"`. In `tests/hooks/test_hooks_manager.py` rename
`test_first_codex_prompt_persists_heuristic_title_and_terminal_identity` to
`test_first_codex_prompt_persists_terminal_identity`, dropping the
`title_source == "heuristic"` assertions and asserting the title is still the provisional
one; rewrite `test_ordinary_spawned_session_promotes_its_own_title` to assert the spawned
child keeps its provisional title after its first prompt; and drop the
`patch("gobby.hooks.event_handlers._agent.promote_heuristic_title")` from
`test_internal_native_subagent_prompt_does_not_title_parent`, asserting the parent title is
unchanged directly. Delete `test_codex_spawn_seeds_heuristic_from_clean_prompt`. Delete both
tests in `tests/servers/websocket/chat/test_stream_persistence.py` and replace them with a
NEW `test_persisted_first_user_message_leaves_provisional_title` that persists a user message
and asserts `title_source == "provisional"` and the title unchanged. Delete
`test_clear_successor_generates_its_own_heuristic` and the `promote_heuristic_title` import
from `tests/sessions/test_handoff.py`.

Consumers unchanged:
- `src/gobby/hooks/event_handlers/__init__.py` — no-edit-reason: re-exports the mixin; `handle_before_agent` keeps its signature.
- `src/gobby/servers/websocket/chat/_lifecycle.py` — no-edit-reason: dispatches by event type into the same handler signature.
- `tests/hooks/test_agent_events_coverage.py` — no-edit-reason: exercises `handle_before_agent` with an unchanged signature and asserts no heuristic title (`gcode grep -F heuristic` over the file is empty).
- `tests/hooks/test_agent_handlers.py` — no-edit-reason: same; no heuristic assertion.
- `tests/hooks/test_handler_execution.py` — no-edit-reason: same; no heuristic assertion.
- `tests/hooks/test_immediate_help.py` — no-edit-reason: same; no heuristic assertion.
- `tests/hooks/test_session_activation_reconciliation.py` — no-edit-reason: same; no heuristic assertion.
- `tests/hooks/test_transcript_path_derivation.py` — no-edit-reason: same; no heuristic assertion.
- `tests/agents/test_srt_spawn.py` — no-edit-reason: it parametrises spawn names and reads `getattr(spawn_executor, "_spawn_codex_terminal")` through `inspect.getsource`; the re-import at the old name keeps the attribute resolving, and it asserts wrap and `_runtime_spawn` call counts, not titles.
- `src/gobby/servers/websocket/chat/_streaming.py` — no-edit-reason: its one call `await persistence.persist_user_message(session, content, attachments)` matches the unchanged signature.

Research context:
- Trigger call paths (checked with `gcode symbol-at`): `handle_before_agent` calls
  `promote_heuristic_title` inside the prompt-metadata `try` at the top of the handler;
  `_spawn_codex_terminal` calls `seed_heuristic_title_from_prompt` via `asyncio.to_thread`
  right before merging `_agent_context_injected`; `seed_heuristic_title_from_prompt` calls
  `promote_heuristic_title(session_manager._storage, child_session_id, request.prompt or "")`;
  `persist_user_message` calls it through `run_db` after `persist_message` succeeds.
- `gcode grep -F promote_heuristic_title -- src tests` (checked): production callers are
  exactly the three above; test patch sites are `tests/hooks/test_hooks_manager.py` (755),
  `tests/agents/test_spawn_executor.py` (1097, 1131),
  `tests/servers/websocket/chat/test_stream_persistence.py` (69), and the direct import in
  `tests/sessions/test_handoff.py` (49).
- `docs/plans/morning-recovery-v2.md` records why the codex seed existed (the preamble was
  titling sessions); with no heuristic the problem disappears.
- Rejected: keeping `heuristic_title_suffix` as a utility; nothing else imports it.
- Planned verification: `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/sessions/test_title_lifecycle.py tests/hooks/test_hooks_manager.py tests/agents/test_spawn_executor.py tests/servers/websocket/chat/test_stream_persistence.py tests/sessions/test_handoff.py tests/mcp_proxy/tools/spawn_agent/test_execution.py`
  then `uv run ruff check src/ && uv run mypy src/`; `wc -l src/gobby/agents/spawn_executor.py src/gobby/agents/spawn_executor_codex.py` both under 1,000.

**Acceptance:**

- 1.2.1 - `title_lifecycle.py` exports no heuristic symbol and `recompute_automatic_title` falls back straight to the provisional title after the last claim closes. file: `src/gobby/sessions/title_lifecycle.py`. test: `tests/sessions/test_title_lifecycle.py::test_recompute_automatic_title_falls_back_to_provisional_after_close` (new).
- 1.2.2 - A first codex prompt records terminal identity but leaves the provisional title. symbol: `AgentEventHandlerMixin.handle_before_agent`. test: `tests/hooks/test_hooks_manager.py::test_first_codex_prompt_persists_terminal_identity` (renamed).
- 1.2.3 - Codex spawns no longer seed a title from the prompt. symbol: `_spawn_codex_terminal`. file: `src/gobby/agents/spawn_executor_codex.py`. behavior: "seed_heuristic_title_from_prompt" absent in `src/gobby/agents/spawn_executor_providers.py`.
- 1.2.4 - A persisted first web-chat message leaves the provisional title in place. symbol: `ChatStreamPersistence.persist_user_message`. test: `tests/servers/websocket/chat/test_stream_persistence.py::test_persisted_first_user_message_leaves_provisional_title` (new).
- 1.2.5 - `spawn_executor.py` and the new codex module are each under 1,000 lines (the repository ceiling), `spawn_executor._spawn_codex_terminal` still resolves as an attribute, and the provider dispatch still resolves codex spawns. file: `src/gobby/agents/spawn_executor.py`. test: `tests/agents/test_spawn_executor.py::test_codex_agent_prompt_precedes_task_prompt`.

### 1.3 Delete the heuristic title source from storage [category: code] (depends: 1.2)
`kind: deliverable`

Targets:
- `src/gobby/storage/sessions/_title_defaults.py::HEURISTIC_TITLE_SOURCE`
- `src/gobby/storage/sessions/_title_defaults.py::format_heuristic_session_title`
- `src/gobby/storage/sessions/_title_update.py::TITLE_UPDATE_ALLOWED_SQL`
- `src/gobby/storage/sessions/_title_fields.py::_TitleFieldMixin.normalize_automatic_title_refs`
- `src/gobby/storage/sessions/_manager.py::*` — scope-reason: `HEURISTIC_TITLE_SOURCE` leaves the `SessionManager._VALID_TITLE_SOURCES` class attribute (not an indexed symbol of its own) and the module import goes with it; no method signature changes, so the 198 call-graph consumers of the class are untouched
- `src/gobby/storage/session_models.py::*` — scope-reason: the `heuristic_title` field leaves `Session` and the `from_row` read of that column leaves with it; no other symbol in the module changes, and its constructors and `from_row` callers keep working because the field was optional
- `tests/storage/sessions/test_storage_sessions_models.py::TestSession.test_full_and_brief_expose_nullable_effort_but_hide_heuristic`
- `tests/storage/sessions/test_title_fields.py`

Delete `HEURISTIC_TITLE_SOURCE` and `format_heuristic_session_title` from
`_title_defaults.py`. In `TITLE_UPDATE_ALLOWED_SQL` remove the whole
`incoming.title_source = '{HEURISTIC_TITLE_SOURCE}'` disjunct and its import, leaving
manual, task-over-nonmanual, and provisional-over-empty/provisional/fallback-task. Remove
`HEURISTIC_TITLE_SOURCE` from `SessionManager._VALID_TITLE_SOURCES` and its import in
`_manager.py`, so `require_valid_title_source` (used by `_crud.py` and `_bulk_update.py`)
rejects `heuristic`. Remove the `heuristic_title: str | None = None` field from `Session`
and the `heuristic_title=cls._get_optional(row, "heuristic_title")` line from
`Session.from_row`.

Rewrite `normalize_automatic_title_refs` so it is the one startup sweep for legacy rows:
select `s.id, s.title, s.title_source, s.source, s.seq_num, s.project_id, p.name` for rows
with `title_source IN ('provisional', 'heuristic', 'task')` and a `seq_num` (drop the
`heuristic_title` column from the SELECT, the `OR s.heuristic_title IS NOT NULL` predicate,
the `normalized_heuristic` computation, and the `heuristic_title` SET and guard clauses of the
UPDATE). For a row whose `title_source` is `heuristic`, the target title is
`format_provisional_session_title(project, seq_num, source)` and the target source is
`PROVISIONAL_TITLE_SOURCE`; for other rows the target title is the existing prefix
normalisation and the source is unchanged. Guard the UPDATE with
`title IS NOT DISTINCT FROM %s AND title_source IS NOT DISTINCT FROM %s` only, set
`title_source` alongside `title`, and keep the existing change notification and
title-change side effects. This runs from `init_storage_and_config` at every daemon start,
so heuristic rows are rewritten before or after migration 447 lands (1.4) with the same
result.

Tests: in `test_full_and_brief_expose_nullable_effort_but_hide_heuristic` rename to
`test_full_and_brief_expose_nullable_effort`, drop the `heuristic_title=` kwarg, set
`title_source="task"`, and keep the `reasoning_effort` assertions. Add a NEW
`tests/storage/sessions/test_title_fields.py::test_normalize_rewrites_heuristic_rows_to_provisional`
that inserts a session row with `title_source='heuristic'` and a stale title, runs
`normalize_automatic_title_refs`, and asserts `project#N: Provider` with
`title_source == "provisional"`, and
`test_normalize_keeps_task_titles_and_renames_prefix` covering the existing prefix path.

Consumers unchanged:
- `tests/storage/test_sessions_import.py` — no-edit-reason: it snapshots public `SessionManager` method signatures only; no signature changes here.
- `src/gobby/runner_init/storage.py` — no-edit-reason: `init_storage_and_config` already calls `runner.session_manager.normalize_automatic_title_refs()`; the rewrite lives inside the sweep.
- `src/gobby/storage/sessions/_bulk_update.py` — no-edit-reason: validates against `_VALID_TITLE_SOURCES` through the class attribute and never names `heuristic`.
- `src/gobby/storage/sessions/_crud.py` — no-edit-reason: same, via `require_valid_title_source`.
- `src/gobby/cli/tasks/_utils/rendering.py` — no-edit-reason: reads `.ref` or `terminal_context` from `Session.from_row` rows and never passes `heuristic_title`.
- `src/gobby/servers/routes/tasks.py` — no-edit-reason: same `from_row` consumer; no `heuristic_title` read or kwarg.
- `src/gobby/sessions/clear_continuation.py` — no-edit-reason: same `from_row` consumer; no `heuristic_title` read or kwarg.
- `src/gobby/sessions/compact_continuation.py` — no-edit-reason: same `from_row` consumer; no `heuristic_title` read or kwarg.
- `src/gobby/sessions/compact_identity.py` — no-edit-reason: same `from_row` consumer; no `heuristic_title` read or kwarg.
- `src/gobby/storage/agent_resume.py` — no-edit-reason: same `from_row` consumer; no `heuristic_title` read or kwarg.
- `src/gobby/storage/agents/_lifecycle.py` — no-edit-reason: same `from_row` consumer; no `heuristic_title` read or kwarg.
- `src/gobby/storage/session_activity.py` — no-edit-reason: same `from_row` consumer; no `heuristic_title` read or kwarg.
- `src/gobby/storage/session_lifecycle.py` — no-edit-reason: same `from_row` consumer; no `heuristic_title` read or kwarg.
- `src/gobby/storage/sessions/_discovery.py` — no-edit-reason: same `from_row` consumer; no `heuristic_title` read or kwarg.
- `src/gobby/storage/sessions/_identity_crud.py` — no-edit-reason: same `from_row` consumer; no `heuristic_title` read or kwarg.
- `src/gobby/storage/sessions/_lineage_discovery.py` — no-edit-reason: same `from_row` consumer; no `heuristic_title` read or kwarg.
- `src/gobby/storage/sessions/_query.py` — no-edit-reason: same `from_row` consumer; no `heuristic_title` read or kwarg.
- `src/gobby/storage/sessions/_terminal.py` — no-edit-reason: same `from_row` consumer; no `heuristic_title` read or kwarg.
- `src/gobby/storage/sessions/_terminal_revival.py` — no-edit-reason: same `from_row` consumer; no `heuristic_title` read or kwarg.
- `src/gobby/storage/sessions/_transcript.py` — no-edit-reason: same `from_row` consumer; no `heuristic_title` read or kwarg.
- `src/gobby/storage/terminals.py` — no-edit-reason: same `from_row` consumer; no `heuristic_title` read or kwarg.
- `tests/storage/sessions/test_compact_identity_reconciliation.py` — no-edit-reason: constructs `Session` and reads `from_row` rows without `heuristic_title`.
- `tests/storage/sessions/test_edge_cases.py` — no-edit-reason: constructs `Session` and reads `from_row` rows without `heuristic_title`.
- `tests/storage/test_local_model_flags.py` — no-edit-reason: constructs `Session` and reads `from_row` rows without `heuristic_title`.

Research context:
- Scope forms (council round 1, cr-1): `_manager.py` and `session_models.py` are `::*`
  targets because the changed carriers (`_VALID_TITLE_SOURCES` and the `heuristic_title`
  field) are not indexed symbols of their own, and exact and wildcard scopes cannot mix in one
  file. The `from_row` consumer sweep above (`gcode usages Session.from_row`, 20 files, none an
  edit target anywhere in this plan) is recorded as sweep evidence; under `::*` the validator
  does not require it.
- `gcode grep -F HEURISTIC_TITLE_SOURCE -- src tests` (checked): after 1.2 the only
  remaining references are `_title_defaults.py`, `_title_update.py`, and `_manager.py`.
- `gcode grep -F heuristic_title -- src tests` (checked): SQL references to the column are
  `_title_fields.py` (SELECT, UPDATE SET, UPDATE guard) and the `promote_heuristic_title`
  UPDATE removed in 1.2; `Session.from_row` reads it with `_get_optional`, and
  `to_dict`/`to_brief` already omit it. No test constructs `Session(heuristic_title=...)`
  other than the models test above.
- The startup call site is `src/gobby/runner_init/storage.py::init_storage_and_config`
  (`runner.session_manager.normalize_automatic_title_refs()`); project name, seq, and source
  are in scope inside the sweep, not in the runner, which is why the rewrite lives here.
- No CHECK constraint exists on `sessions.title_source` (`gcode grep -F title_source --
  crates/gcore/assets/schema`, checked: plain `title_source text` in baseline.sql).
- Rejected: doing the row rewrite in migration 447 SQL; the provider label map is Python and
  would be duplicated.
- Planned verification: `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/storage/sessions/ tests/storage/test_sessions_import.py tests/sessions/test_title_lifecycle.py`
  then `uv run ruff check src/ && uv run mypy src/`.

**Acceptance:**

- 1.3.1 - `heuristic` is not a valid `title_source` and the formatter module has no heuristic symbol. symbol: `SessionManager`. file: `src/gobby/storage/sessions/_title_defaults.py`.
- 1.3.2 - `TITLE_UPDATE_ALLOWED_SQL` contains no heuristic branch. symbol: `TITLE_UPDATE_ALLOWED_SQL`. behavior: "heuristic" absent in `src/gobby/storage/sessions/_title_update.py`.
- 1.3.3 - `Session` has no `heuristic_title` field and `from_row` does not read the column. symbol: `Session.from_row`. test: `tests/storage/sessions/test_storage_sessions_models.py::TestSession.test_full_and_brief_expose_nullable_effort` (renamed).
- 1.3.4 - The startup sweep rewrites `title_source='heuristic'` rows to the provider provisional title. symbol: `_TitleFieldMixin.normalize_automatic_title_refs`. test: `tests/storage/sessions/test_title_fields.py::test_normalize_rewrites_heuristic_rows_to_provisional` (new).
- 1.3.5 - Prefix renames for task titles still work without touching the removed column. test: `tests/storage/sessions/test_title_fields.py::test_normalize_keeps_task_titles_and_renames_prefix` (new).

### 1.4 Migration 447: drop sessions.heuristic_title [category: code] (depends: 1.3)
`kind: deliverable`

Targets:
- `crates/gcore/assets/schema/migrations/447_drop_session_heuristic_title.sql`
- `crates/gcore/src/schema/assets.rs::MIGRATIONS`
- `crates/gcore/src/schema/verify.rs::is_live_mutable_seed_field`
- `crates/gcore/assets/schema/catalog.manifest.json::*` — scope-reason: regenerated by the manifest freshness test
- `crates/gcore/src/grant/bundle.rs::*` — scope-reason: golden checksum, root hash, and latest_version literals move together
- `crates/gcore/tests/schema_contract.rs::embedded_assets_publish_a_complete_schema_identity`
- `crates/gdaemon/tests/cli_contract.rs::version_json_reports_exact_schema_identity_contract`
- `src/gobby/storage/schema_expected_identity.json::*` — scope-reason: regenerated from the rebuilt gdaemon

Add migration 447 containing `ALTER TABLE sessions DROP COLUMN heuristic_title;` (the
column was added by `436_add_session_heuristic_title_and_reasoning_effort.sql`;
`reasoning_effort` stays). Remove the `| "heuristic_title"` arm from the `"sessions"`
match in `is_live_mutable_seed_field` so the seed verifier no longer tolerates a column that
does not exist. Then run the carrier procedure below and commit migration plus carriers
together, because `uv run gobby cutover` refuses uncommitted schema inputs.

Research context:
- Precedent commit `541320efc2` (migration 444) shows the carrier procedure; there is no
  single regenerate command. Order: (1) append an `EmbeddedMigration { version: 447,
  filename, checksum: <sha256 of the file bytes>, sql: include_str!(...) }` entry to
  `MIGRATIONS` in `crates/gcore/src/schema/assets.rs`; (2) regenerate the catalog
  manifest with the env-gated test
  `GOBBY_SCHEMA_TEST_DATABASE_URL=<scratch pg url> UPDATE_GCORE_SCHEMA_MANIFEST=1 cargo test -p gobby-core --test catalog_manifest_freshness catalog_manifest_is_fresh_for_embedded_assets`
  (this drops the `sessions.heuristic_title` entry from the manifest); (3)
  `cargo build -p gobby-daemon` then `target/debug/gdaemon schema-identity --json` prints
  `latest_checksum` and `assets_root_hash` (`root_hash()` covers baseline, migrations,
  seed, and manifest, so it is final only after step 2); (4) update
  `GOLDEN_LATEST_CHECKSUM`, `GOLDEN_ASSETS_ROOT_HASH`, and `latest_version: 447` in
  `crates/gcore/src/grant/bundle.rs`, the literals in `schema_contract.rs`, and
  `latest_version` in `cli_contract.rs`; (5)
  `uv run python scripts/generate_schema_expected_identity.py --gdaemon target/debug/gdaemon`
  rewrites `src/gobby/storage/schema_expected_identity.json` (CI re-derives and compares).
- Latest existing migration is `445_drop_task_validation_system_prompt.sql` (checked with
  `ls crates/gcore/assets/schema/migrations | tail -3`). The uncommitted
  `.gobby/plans/runbooks.md` holds 446 and 445 is the latest committed migration, so this plan takes 447; whichever plan lands second renumbers to the next free version.
- `gcode grep -F heuristic_title -- crates` (checked): migration 436, the catalog manifest
  entry, the `assets.rs` include of 436, and the `verify.rs` allowlist arm are the only
  crate references.
- Live: rebuild with `cargo build --release -p gobby-code -p gobby-daemon -p gobby-hooks`,
  promote through `promote_workspace_binary_set` (rebuild `gclient` alongside), announce a
  `global` `send_message`, and run `uv run gobby cutover` in a quiet window. The Python
  code from 1.3 no longer references the column, so either order of cutover and Python
  deploy is safe.
- Rejected: leaving the column in place as dead storage; the verifier allowlist and the
  model would keep carrying a field nothing writes.

**Acceptance:**

- 1.4.1 - Migration 447 drops `sessions.heuristic_title`. file: `crates/gcore/assets/schema/migrations/447_drop_session_heuristic_title.sql`.
- 1.4.2 - `MIGRATIONS` embeds version 447 with its checksum and the catalog manifest no longer lists `sessions.heuristic_title`. symbol: `MIGRATIONS`. file: `crates/gcore/assets/schema/catalog.manifest.json`.
- 1.4.3 - The seed verifier allowlist no longer names `heuristic_title`. symbol: `is_live_mutable_seed_field`.
- 1.4.4 - The schema identity contract tests pass with `latest_version` 447. test: `crates/gcore/tests/schema_contract.rs::embedded_assets_publish_a_complete_schema_identity`. test: `crates/gdaemon/tests/cli_contract.rs::version_json_reports_exact_schema_identity_contract`.
- 1.4.5 - `schema_expected_identity.json` matches the rebuilt gdaemon's identity. file: `src/gobby/storage/schema_expected_identity.json`.

### 1.5 Canonical Telegram title and session docs [category: code] (depends: 1.1)
`kind: deliverable`

Targets:
- `src/gobby/communications/session_events.py::_canonical_session_title`
- `tests/communications/test_session_events.py::*` — scope-reason: two tests are added and the existing automatic-title tests are re-pinned to the verbatim rule
- `docs/guides/sessions.md`
- `docs/reference-audit/sessions.json::*` — scope-reason: the session-title entries describing heuristic promotion are rewritten together

In `_canonical_session_title`, after computing `ref`, return `title` verbatim when
`title == ref` or `title.startswith(f"{ref}:")` (in addition to the existing
`(ref):` legacy check), so a provisional `gobby#12: Claude` or task
`gobby#12: Task #7 - Fix` renders as itself instead of `gobby#12 - gobby#12: Claude`.
Keep the `#N` legacy-prefix stripping and the `f"{ref} - {title}"` fallback for manual
titles that do not start with the ref.

Rewrite the title paragraph of `docs/guides/sessions.md` (the block that today describes
manual > latest active task > saved heuristic > provisional and the internal
`heuristic_title` field): titles are provisional `project#seq: Provider`, task
`project#seq: Task #ref - title`, or manual verbatim; the ladder is manual > open claimed
task > provisional; `title_source` is one of `provisional`, `task`, `manual`; a `/clear`
successor inherits a manual title or recomputes. Update the matching entries in
`docs/reference-audit/sessions.json` (the entries near the `heuristic` promotion text) to
the same contract.

Tests: add a NEW `test_canonical_title_keeps_ref_prefixed_titles_verbatim` in
`tests/communications/test_session_events.py` covering the provisional and task forms, and
a NEW `test_canonical_title_prefixes_manual_titles` asserting `gobby#12 - My manual title`.
Re-pin the existing `test_automatic_title_contains_reference_once` and
`test_format_session_status_message_does_not_duplicate_legacy_ref` to the verbatim rule if
they currently expect `ref - ref: Provider` or a stripped automatic title.

Research context:
- `_canonical_session_title` (checked with `gcode symbol-at`): builds `ref` from
  `transition.session_ref` or `project_id#seq_num`, keeps `(ref):`-prefixed titles, strips a
  legacy `#N` prefix, and otherwise returns `f"{ref} - {title}"`; `format_session_status_message`
  appends ` - {status}`.
- `docs/reference-audit/communications.json` lists `_canonical_session_title` as the
  implementation of the Telegram title entry (read-only; the audit text there describes the
  ref prefix, not the heuristic).
- `docs/guides/sessions.md` lines 88-101 describe heuristic promotion and the
  `heuristic_title` field (checked via the heuristic sweep);
  `docs/contracts/session-boundary.md` already matches the new contract and needs no edit.
- Checked: `tests/communications/test_session_events.py` exists with seven tests
  (`test_automatic_title_contains_reference_once`,
  `test_format_session_status_message_does_not_duplicate_legacy_ref`,
  `test_format_session_status_message_uses_session_fallback_without_title`, and four
  routing tests). Planned: confirm the exact `SessionStatusTransition.session_ref` shape in
  `src/gobby/sessions/status_events.py` before writing the fixtures.
- Planned verification: `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/communications/`
  then `uv run ruff check src/ && uv run mypy src/`.

**Acceptance:**

- 1.5.1 - Provisional and task titles that already start with the session ref are used verbatim in status messages. symbol: `_canonical_session_title`. test: `tests/communications/test_session_events.py::test_canonical_title_keeps_ref_prefixed_titles_verbatim` (new).
- 1.5.2 - Manual titles are still prefixed with the ref. test: `tests/communications/test_session_events.py::test_canonical_title_prefixes_manual_titles` (new).
- 1.5.3 - The sessions guide describes the three deterministic formats and no heuristic. file: `docs/guides/sessions.md`. behavior: "heuristic" absent in `docs/guides/sessions.md`.
- 1.5.4 - The reference audit matches the guide. file: `docs/reference-audit/sessions.json`.

## P2: Daemon payload
`kind: framing`

Two additive payload changes on the Python daemon that the Rust client consumes in a later
phase (3.1): the attention roster gains task titles for runs and the open claimed task for
sessions, and terminal inventories report the spawned shell instead of the literal `shell`.

### 2.1 Roster entries carry task titles and claimed tasks [category: code]
`kind: deliverable`

Targets:
- `src/gobby/storage/attention.py::AttentionStateManager.load_roster_rows`
- `src/gobby/storage/attention.py::AttentionRosterRow`
- `src/gobby/storage/attention.py::AttentionRosterRow.from_row`
- `src/gobby/servers/routes/attention.py::*` — scope-reason: the roster serialisation helpers move out to the new module and the `roster` handler inside `create_attention_router` imports them
- `src/gobby/servers/routes/attention_roster.py`
- `tests/servers/test_attention_roster.py::*` — scope-reason: two roster tests are added beside the `_roster_run` / `_roster_session` helpers they reuse

In `load_roster_rows` extend the run query with `task.title AS task_title` (the query
already does `LEFT JOIN tasks task ON task.id = run.task_id`), and replace the session
query's `NULL::text AS task_id, NULL::text AS task_ref, NULL::text AS task_stage` with a
`LEFT JOIN LATERAL (SELECT id, seq_num, title FROM tasks WHERE claimed_by_session_id = session.id AND closed_at IS NULL ORDER BY claimed_at DESC NULLS LAST, id LIMIT 1) claimed ON TRUE`
projecting `claimed.id::text AS task_id`, the same `'#' || seq_num` / `LEFT(id, 8)`
`task_ref` CASE the run query uses, `NULL::text AS task_stage`, and
`claimed.title AS task_title`. Add `task_title: str | None = None` to `AttentionRosterRow`
(the dataclass has no defaults today, and three keyword constructors outside the roster tests
build it, so the default keeps them compiling) and read it in `from_row` the way `task_ref` is
read; `from_row` always sets it explicitly.

Split `src/gobby/servers/routes/attention.py` (861 lines) before adding: move `_load_roster_entries`,
`_model_display_name`, `_serialize_attention`, `_terminal_block`, `_run_tmux_payload`,
`_session_tmux_payload`, `_metadata_payload`, and `_serialize_timestamp` out of
`attention.py` into the new `src/gobby/servers/routes/attention_roster.py`, and import them
in `attention.py` for the `roster` handler and for the existing test patch sites. Then in
`_load_roster_entries` build `task = {"id", "ref", "stage", "title"}` for a run when
`task_id` and `task_ref` are set (adding `"title": run.task_title`), and for a session emit
the same dict from the row's claimed-task columns instead of the current `"task": None`.
Every existing key is kept, so current clients are unaffected.

Tests: add a NEW `test_roster_run_entries_carry_the_task_title` and a NEW
`test_roster_session_entries_carry_the_open_claimed_task` to
`tests/servers/test_attention_roster.py` using the existing `_roster_run` / `_roster_session`
helpers; the second claims a task for the session, asserts `task == {"id", "ref", "stage": None, "title"}`,
closes the task, and asserts `task is None`.

Consumers unchanged:
- `tests/servers/test_attention_native_roster.py` — no-edit-reason: `test_bound_native_session_is_on_the_roster` asserts presence, not the `task` payload.
- `tests/agents/test_attention_metadata.py` — no-edit-reason: builds `AttentionRosterRow(...)` by keyword (line 111) without `task_title`; the `= None` default keeps it valid.
- `tests/config/test_live_policy_consumers.py` — no-edit-reason: same keyword constructor (line 214); the default covers it.
- `tests/servers/routes/test_config_startup_stragglers.py` — no-edit-reason: same keyword constructor (line 24), and it imports `_run_tmux_payload` from `routes/attention.py`, which the re-import keeps at that path.
- `src/gobby/sessions/turn_lifecycle.py` — no-edit-reason: its `from_row` call (line 346) is `AttentionState.from_row(row)`, a different class that the bare-name index match conflates; it never builds a roster row.

Research context:
- `gcode symbol-at src/gobby/storage/attention.py:272` (checked): `load_roster_rows` runs
  one round trip with a run query (`FROM agent_runs run LEFT JOIN tasks task ON task.id = run.task_id`,
  a LATERAL on `task_stage_states` for `task_stage`, `LEFT JOIN terminals`) unioned with a
  session query that hard-codes `NULL::text` for `task_id`, `task_ref`, `task_stage` and
  projects `session.source AS provider, session.model`.
- `AttentionRosterRow.from_row` (checked) reads `task_id`, `task_ref`, `task_stage` with
  `row.get(...)`; `_load_roster_entries` (src/gobby/servers/routes/attention.py, checked) builds
  `{"id", "ref", "stage"}` for runs and `"task": None` for sessions.
- `gcode outline src/gobby/servers/routes/attention.py` (checked): the roster helpers at
  lines 622-783 are module-level functions, so the move is mechanical; `wc -l` is 861.
- Already on the client: provider and model reach the roster through
  `session.source AS provider, session.model` and the run query's provider/model columns;
  `agent_name` is a column of `agent_runs` read by `src/gobby/storage/agents/_models.py::AgentRun`
  (`agent_name=row["agent_name"]`) and serialised through `/api/agents/runs`;
  `reasoning_effort` is in `Session.to_dict`/`to_brief` (`/api/sessions`) per
  `tests/storage/sessions/test_storage_sessions_models.py`. Planned: cite the exact route
  symbols under `src/gobby/servers/routes/agents.py` and `src/gobby/servers/routes/sessions/`
  in the close summary for 3.1.
- Consumer for 3.1 (read-only): `crates/gclient/src/daemon/mod.rs::RosterEntry` with
  `task: Option<TaskRef>` (`TaskRef { id, reference, ... }` at the same module) and
  `crates/gclient/src/daemon/rest.rs::AttentionRoster { epoch, seq, entries }`.
- Rejected: computing the session's claimed task client-side from `/api/tasks`; the roster
  is the one call the sidebar makes per refresh. Rejected: a second query per session; the
  LATERAL keeps the single round trip that `test_roster_cold_path_is_bounded_and_cursor_invalidates_cache` pins.
- Planned verification: `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/servers/test_attention_roster.py tests/servers/test_attention_native_roster.py tests/servers/test_attention_routes_wiring.py`
  then `uv run ruff check src/ && uv run mypy src/`; `wc -l src/gobby/servers/routes/attention.py src/gobby/servers/routes/attention_roster.py` both under 850.

**Acceptance:**

- 2.1.1 - Roster run entries include `task.title`. symbol: `AttentionStateManager.load_roster_rows`. test: `tests/servers/test_attention_roster.py::test_roster_run_entries_carry_the_task_title` (new).
- 2.1.2 - Roster session entries include the open claimed task `{id, ref, stage, title}` or `null` once it closes. symbol: `AttentionRosterRow.from_row`. test: `tests/servers/test_attention_roster.py::test_roster_session_entries_carry_the_open_claimed_task` (new).
- 2.1.3 - The roster serialisation helpers live in the new module and the route still serves the same keys. file: `src/gobby/servers/routes/attention_roster.py`. test: `tests/servers/test_attention_roster.py::test_roster_spells_the_model_as_its_provider_prints_it`.
- 2.1.4 - `src/gobby/servers/routes/attention.py` and `routes/attention_roster.py` are each under 850 lines and the cold path stays one round trip. file: `src/gobby/servers/routes/attention.py`. test: `tests/servers/test_attention_roster.py::test_roster_cold_path_is_bounded_and_cursor_invalidates_cache`.

### 2.2 Terminal inventories report the spawned shell [category: code]
`kind: deliverable`

Targets:
- `src/gobby/terminals/web_spawn.py::spawn_web_terminal`
- `src/gobby/terminals/foreground.py::*` — scope-reason: a `process_shell` reader is added beside `shell_pid`, which documents the same `process` jsonb
- `src/gobby/servers/routes/terminals.py::_row_json`
- `src/gobby/servers/websocket/terminal_ws.py::TerminalWsMixin._handle_terminal_list`
- `tests/terminals/test_foreground.py::*` — scope-reason: one test for the new reader joins the existing `shell_pid` tests
- `tests/servers/test_terminals_routes.py::*` — scope-reason: one fallback test joins `test_a_native_row_reports_the_command_in_its_terminal_foreground`
- `tests/servers/test_terminal_ws_list.py::*` — scope-reason: one fallback test joins the native-row list tests
- `tests/terminals/test_web_spawn.py`

In `spawn_web_terminal`, where the `process` dict is assembled
(`{"host_terminal_id": prepared.host_terminal_id}` plus `pgid` / `start_time` from
`prepared.process`), add `"shell": os.path.basename(command[0])` when `command` is
non-empty (`zsh` for the `["zsh"]` default in `_handle_terminal_create` and for
`PANE_SHELL_COMMAND` in `workspace_ops._fill`, both of which call this function).
`record_process` merges with `process = COALESCE(process, '{}'::jsonb) || %s`, so the key
survives the later host promotion.

Add `process_shell(row) -> str | None` to `foreground.py` next to `shell_pid`: it returns
`process["shell"]` when it is a non-empty string, else `None`. In `_row_json` set
`payload["command"] = command or process_shell(row)` so the REST inventory falls back to the
recorded shell when `foreground_commands` has no live entry. In `_handle_terminal_list`
change `item["command"] = native_commands.get(row.id) or (pane.pane_command if pane is not None else None)`
to `... or process_shell(row)` as the final fallback. tmux rows always carry
`pane_current_command`, so only native rows change, and the client's `UNNAMED_PANE` (`shell`)
placeholder is no longer reached for a daemon-spawned terminal.

Tests: NEW `tests/terminals/test_web_spawn.py::test_spawn_records_the_shell_basename_in_process`
(patch the host spawn the way `tests/terminals/test_workspace_ops.py` does and assert the
`record_process` payload carries `shell == "zsh"`); NEW
`tests/terminals/test_foreground.py::test_process_shell_reads_the_recorded_basename`; NEW
`tests/servers/test_terminals_routes.py::test_a_native_row_falls_back_to_its_spawn_shell` (a
row with `process.shell = "zsh"` and no live foreground reports `command == "zsh"`); NEW
`tests/servers/test_terminal_ws_list.py::test_list_falls_back_to_the_spawn_shell_for_a_native_row`.

Consumers unchanged:
- `src/gobby/terminals/ws_protocol.py` — no-edit-reason: `inventory_item` does not carry `command`; both inventories add it after calling it.
- `src/gobby/terminals/workspace_ops.py` — no-edit-reason: `_fill` already passes `command=list(PANE_SHELL_COMMAND)` into `spawn_web_terminal`, which is where the basename is recorded.
- `src/gobby/servers/websocket/terminal_ws_create.py` — no-edit-reason: `_handle_terminal_create` already defaults `command` to `["zsh"]` and forwards it.
- `src/gobby/storage/terminal_settlement.py` — no-edit-reason: `record_process` merges jsonb, so the new key needs no schema or storage change.
- `tests/servers/test_terminal_list_watermark.py` — no-edit-reason: patches the dotted `spawn_web_terminal` name (line 326); the signature is unchanged and it asserts placement and overflow, not process keys.
- `tests/servers/test_terminal_ws_create.py` — no-edit-reason: calls `spawn_web_terminal` with the unchanged signature and asserts placement, not process keys.
- `tests/terminals/test_backend_selection.py` — no-edit-reason: same; asserts backend selection, not process keys.
- `tests/terminals/test_tmux_runtime.py` — no-edit-reason: same; asserts tmux runtime behaviour, not process keys.
- `src/gobby/servers/websocket/server.py` — no-edit-reason: its dispatch table maps `"terminal_list"` to `_handle_terminal_list` (line 448); the handler keeps its signature.
- `tests/servers/test_terminal_ws_golden.py` — no-edit-reason: calls `_handle_terminal_list` with `list.json` (line 349) and its golden asserts the existing keys, which are all kept.

Research context:
- `gcode grep -F PANE_SHELL_COMMAND -- src` (checked): defined in
  `src/gobby/terminals/workspace_ops.py` (`("zsh",)`) and used by `_fill`, not in
  `terminal_ws_create.py` as the brief assumed; the WS create default is
  `data.get("command") or ["zsh"]` in `_handle_terminal_create`.
- `spawn_web_terminal` (checked) builds `process` and calls `manager.record_process`;
  `record_process` (`src/gobby/storage/terminal_settlement.py`, checked) does
  `SET process = COALESCE(process, '{}'::jsonb) || %s`. `host_manager.py::handle_spawn_prepared`
  also calls `record_process` with `host_terminal_id`, `pgid`, `start_time` and is 909
  lines, so it is deliberately not touched.
- `foreground_commands` (checked) resolves a live shell's foreground leader via a process
  snapshot and omits keys whose shell is gone; `shell_pid` reads `process.pgid`.
- `gcode grep -F '"shell"' -- src/gobby/servers src/gobby/terminals` (checked): no Python
  emitter; the literal comes from `crates/gclient/src/app/pane.rs::UNNAMED_PANE` when
  `command` is null (read-only; another phase).
- REST shaping (checked): `list_terminals` and `get_terminal` call
  `_row_json(row, _attach(...), _foreground_commands(...).get(row.id))`; WS shaping (checked):
  `_handle_terminal_list` lines 57-81 of the method.
- `wc -l` (checked): `web_spawn.py` 271, `foreground.py` 113, `routes/terminals.py` 167,
  `terminal_ws.py` 800; `workspace_ops.py` is 964 and stays untargeted.
- Rejected: translating `shell` on the client; the brief makes the literal unreachable at
  the source. Rejected: persisting the whole `command` list; only the basename is displayed.
- Planned verification: `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/terminals/test_foreground.py tests/terminals/test_web_spawn.py tests/terminals/test_workspace_ops.py tests/servers/test_terminals_routes.py tests/servers/test_terminal_ws_list.py`
  then `uv run ruff check src/ && uv run mypy src/`.

**Acceptance:**

- 2.2.1 - Spawned terminals persist the shell basename as `process.shell`. symbol: `spawn_web_terminal`. test: `tests/terminals/test_web_spawn.py::test_spawn_records_the_shell_basename_in_process` (new).
- 2.2.2 - `process_shell` reads the recorded basename and ignores rows without one. file: `src/gobby/terminals/foreground.py`. test: `tests/terminals/test_foreground.py::test_process_shell_reads_the_recorded_basename` (new).
- 2.2.3 - The REST inventory `command` falls back to `process.shell`. symbol: `_row_json`. test: `tests/servers/test_terminals_routes.py::test_a_native_row_falls_back_to_its_spawn_shell` (new).
- 2.2.4 - The WS inventory `command` falls back to `process.shell` after the live foreground and pane command. symbol: `TerminalWsMixin._handle_terminal_list`. test: `tests/servers/test_terminal_ws_list.py::test_list_falls_back_to_the_spawn_shell_for_a_native_row` (new).
- 2.2.5 - Live foreground detection still wins over the recorded shell. test: `tests/servers/test_terminals_routes.py::test_a_native_row_reports_the_command_in_its_terminal_foreground`.

## P3: gclient chrome
`kind: framing`

The Rust client (`crates/gclient`, package `gobby-client`, ratatui 0.30 and crossterm 0.29)
stops rendering the session title ladder and draws the structured chrome the canvas boards
show: a full-width menu bar, `project:tab_ref` tab labels, four-edged panes with the task on
the top-left and the agent and address on the bottom edges, an Agents and Terminals sidebar
that hides behind an overlay by default, a status bar, menus where every item acts, and a
first frame at launch. 3.1 waits on the roster payload from 2.1; 3.8 waits on the committed
grids from 0.1; every other section stands on the current client. Load the `rust` skill and
read `crates/CLAUDE.md` before editing; goldens follow the constraint on scripted named panes.

### 3.1 Agent data model: definition name, task title, roster row split [category: code] (depends: 2.1)
`kind: deliverable`

Targets:
- `crates/gclient/src/daemon/mod.rs::TaskRef`
- `crates/gclient/src/daemon/mod.rs::RosterEntry`
- `crates/gclient/src/daemon/mod.rs::Attention`
- `crates/gclient/src/daemon/mod.rs::TerminalRef`
- `crates/gclient/src/daemon/mod.rs::tmux_session_name`
- `crates/gclient/src/daemon/mod.rs::Tmux`
- `crates/gclient/src/daemon/roster.rs`
- `crates/gclient/src/app/sidebar_model.rs::AgentEntry`
- `crates/gclient/src/app/sidebar_model.rs::build_agents`
- `crates/gclient/tests/sidebar_model.rs::entry`
- `crates/gclient/tests/sidebar_model.rs::build_joins_projects_worktrees_and_agents`
- `crates/gclient/tests/parity/sidebar.rs::*` — scope-reason: every `AgentEntry` struct literal in the fixture (`add`, `add_remote`, `add_on`) gains the new fields

`TaskRef` gains `pub title: Option<String>` under `#[serde(default)]`, matching the roster payload section 2.1 extends (run entries carry `task.title`; session entries carry their open claimed task as `{id, ref, title}` in the same `task` object). `RosterEntry` keeps its shape apart from two `#[serde(default)]` optional fields the status bar (3.7) reads: `context_percent: Option<u8>` and `tokens_used: Option<u64>`.

Split `mod.rs`: move `RosterEntry`, `Attention`, `TaskRef`, `TerminalRef`, `Tmux` and the `tmux_session_name` deserializer out of `crates/gclient/src/daemon/mod.rs` into the new `crates/gclient/src/daemon/roster.rs`, declared `mod roster;` with `pub use roster::{Attention, RosterEntry, TaskRef, TerminalRef};` so every `gobby_client::daemon::RosterEntry` import path stays valid. `mod.rs` is 987 lines with no `#[cfg(test)]`; the move takes it below the threshold.

`AgentEntry` gains `agent_definition_name: Option<String>` (from `RunRow.agent_name`, filtered non-empty, never folded into the `name` ladder, which keeps its current rungs), `task_title: Option<String>` (from `entry.task.title`), `context_percent: Option<u8>` and `tokens_used: Option<u64>` (from the roster entry). `build_agents` fills them. Add `AgentEntry::definition_label(&self) -> String`: the definition name when present, else the provider label (an interactive session with no run shows `Claude Code`, matching its provisional title); the label uses the same provider display mapping the pane's provisional title uses (see Research context). Add `AgentEntry::model_slug(&self) -> String` beside it, the one implementation of Decision 5's slug: `model_display_name` (else `model`, else an empty string) lowercased, every run of whitespace collapsed to one `-`, and `-{effort}` appended when `effort` is set (`Fable 5.1` with effort `xhigh` → `fable-5.1-xhigh`); no truncation here. 3.4b (the row's line 3, truncated to 17 cells locally) and 3.7 (the `model` status segment) both call it (council round 2, cr-5).

Consumers unchanged:
- `crates/gclient/src/app/attention.rs` — no-edit-reason: `parse_prompt` consumes `RosterEntry` by the re-exported path and reads no new field.
- `crates/gclient/src/daemon/projects.rs` — no-edit-reason: `RunRow.agent_name` is read, not renamed; run task titles arrive on the roster entry, not the run row.

Research context:
- `AgentEntry` and `build_agents` (`crates/gclient/src/app/sidebar_model.rs`): `task_ref` is `entry.task.as_ref().and_then(|t| t.reference.clone())`; the `name` ladder is session title → `run.agent_name` → tmux name → `pane.display_name()` → `UNNAMED_PANE`. `SidebarInputs` carries `rows: &SidebarRows`, `roster: &[RosterEntry]`.
- `RosterEntry` (`crates/gclient/src/daemon/mod.rs`) has `task: Option<TaskRef>`, `provider`, `model`, `model_display_name`, `terminal: Option<TerminalRef>`; `TaskRef { id, reference }` with `#[serde(rename = "ref")]`.
- `RunRow` (`crates/gclient/src/daemon/projects.rs`) carries `agent_name`, `task_id`, effort fields; `SidebarRows` joins sessions and runs by project.
- Tests building rows: `crates/gclient/tests/sidebar_model.rs::entry` builds a `RosterEntry` literal; `build_joins_projects_worktrees_and_agents` asserts the joined entries; `crates/gclient/tests/parity/sidebar.rs` builds `AgentEntry` literals in its fixture helpers (`add`, `add_remote`, `add_on`).
- Rejected: reading the task title from `RunRow` (a second join, and sessions would have none); folding the definition name into `name` (the row renderer in 3.4b needs both).
- Observed: no provider display mapping exists in the crate (`gcode grep -F '"Claude Code"' crates/gclient` is empty), so `provider_label(provider: &str) -> &str` is NEW beside `definition_label`, mirroring the daemon's `_PROVIDER_TITLE_LABELS` table in `src/gobby/storage/sessions/_title_defaults.py` exactly: `agy` → `AGY`, `claude` → `Claude`, `claude_code` → `Claude Code`, `codex` → `Codex`, `droid` → `Droid`, `grok` → `Grok`, `pipeline` → `Pipeline`, `qwen` → `Qwen`, `system` → `System`, `unknown` → `Unknown`, and any other source falls back to the raw source string the way `provider_title_label` does. That table is what the provisional session title carries after 1.1; no second client-only table is added. `AgentEntry` struct literals exist only in `crates/gclient/src/app/sidebar_model.rs` (the builder) and `crates/gclient/tests/parity/sidebar.rs` (observed with `gcode grep -F "AgentEntry {"`).
- Planned checks: `cargo nextest run -p gobby-client -E 'test(sidebar_model) | test(parity)'`; `cargo clippy -p gobby-client`.

**Acceptance:**

- 3.1.1 - `TaskRef` carries an optional `title` and the roster types live in the new module. file: `crates/gclient/src/daemon/roster.rs`.
- 3.1.2 - `AgentEntry` exposes `agent_definition_name`, `task_title`, `context_percent`, `tokens_used`, and `definition_label()` falls back to the provider label. symbol: `AgentEntry`.
- 3.1.3 - NEW regression: a run entry with `task.title` and `agent_name` yields both fields; a session-only entry with provider `claude_code` yields `definition_label() == "Claude Code"` and one with provider `claude` yields `"Claude"`. test: `crates/gclient/tests/sidebar_model.rs::build_carries_definition_name_and_task_title`.
- 3.1.4 - Existing join test still passes with the new fields defaulted. test: `crates/gclient/tests/sidebar_model.rs::build_joins_projects_worktrees_and_agents`.
- 3.1.5 - `crates/gclient/src/daemon/mod.rs` is under 850 lines after the move. file: `crates/gclient/src/daemon/mod.rs`.
- 3.1.6 - NEW regression: `model_slug()` lowercases the display name, joins whitespace runs with `-`, appends `-{effort}` when set, and falls back to `model`. symbol: `AgentEntry`. test: `crates/gclient/tests/sidebar_model.rs::model_slug_lowercases_hyphenates_and_appends_effort`.

### 3.2a Frame: hidden sidebar, rail removed, four-edged panes [category: code] (depends: 3.1)
`kind: deliverable`

Targets:
- `crates/gclient/src/ui/chrome.rs::SidebarState`
- `crates/gclient/src/ui/chrome.rs::SidebarState::default`
- `crates/gclient/src/ui/chrome.rs::SidebarState::scroll`
- `crates/gclient/src/ui/chrome.rs::SidebarState::scroll_mut`
- `crates/gclient/src/ui/chrome.rs::SidebarState::toggle_group`
- `crates/gclient/src/ui/chrome.rs::SidebarState::is_expanded`
- `crates/gclient/src/ui/chrome.rs::SidebarState::set_width_from_column`
- `crates/gclient/src/ui/chrome.rs::COLLAPSED_WIDTH`
- `crates/gclient/src/ui/chrome.rs::ViewState`
- `crates/gclient/src/ui/chrome.rs::ViewState::apply_hits`
- `crates/gclient/src/ui/chrome.rs::Chrome::apply_prefs`
- `crates/gclient/src/ui/chrome.rs::Chrome::sidebar_width`
- `crates/gclient/src/ui/chrome.rs::Chrome::compute_view`
- `crates/gclient/src/ui/chrome/sidebar_state.rs`
- `crates/gclient/src/ui/chrome_render.rs::ChromeHits`
- `crates/gclient/src/ui/chrome_render.rs::render_workspace_with`
- `crates/gclient/src/ui/chrome_render.rs::render_navigation_chrome`
- `crates/gclient/src/ui/sidebar.rs::*` — scope-reason: the rail, footer band, menu band, collapsed layout and toggle leave; layout, hits and travel helpers all change shape
- `crates/gclient/src/ui/hit.rs::Hit`
- `crates/gclient/src/ui/hit.rs::hit_test`
- `crates/gclient/src/ui/hit.rs::sidebar_hit`
- `crates/gclient/src/ui/settings.rs::ClientPrefs`
- `crates/gclient/src/ui/settings.rs::ClientPrefs::default`
- `crates/gclient/src/ui/settings.rs::SettingsRow`
- `crates/gclient/src/ui/settings.rs::SettingsRow::ALL`
- `crates/gclient/src/ui/settings.rs::row_label`
- `crates/gclient/src/ui/settings.rs::row_value`
- `crates/gclient/src/ui/settings.rs::row_values_follow_prefs`
- `crates/gclient/src/prefs.rs::UiPrefs`
- `crates/gclient/src/prefs.rs::UiPrefs::from`
- `crates/gclient/src/prefs.rs::ClientPrefs::from`
- `crates/gclient/src/ui/pane_layout.rs::apply_pane_chrome`
- `crates/gclient/src/ui/pane_layout.rs::pane_geometry`
- `crates/gclient/src/ui/pane_layout.rs::borderless_gaps_shrink_the_left_pane_by_one_cell`
- `crates/gclient/src/ui/panes.rs::render_border_lines`
- `crates/gclient/src/app/live_loop/modal_input.rs::*` — scope-reason: the settings-row toggle match loses `PaneBorders` and the sidebar collapse toggle
- `crates/gclient/src/app/live_loop/actions.rs::*` — scope-reason: the collapse/expand action becomes show/hide and its handlers move out
- `crates/gclient/src/app/live_loop/actions/sidebar.rs`
- `crates/gclient/src/app/live_loop/mouse/pointer.rs::*` — scope-reason: `Hit::SidebarToggle` handling leaves
- `crates/gclient/src/app/live_loop/mouse/wheel.rs::*` — scope-reason: the collapsed-rail wheel branch leaves
- `crates/gclient/src/ui/sidebar/tests.rs::*` — scope-reason: rail tests leave and layout tests lose the footer
- `crates/gclient/src/ui/hit/tests.rs::hit_test_covers_split_live_layout`
- `crates/gclient/tests/parity/chrome.rs::*` — scope-reason: collapsed-sidebar and sidebar-width tests are rewritten for the hidden default
- `crates/gclient/tests/parity/sidebar.rs::*` — scope-reason: the `draw_collapsed` helper and toggle test leave
- `crates/gclient/tests/parity/panes.rs::*` — scope-reason: borderless and disabled-border cases are replaced by four-edge cases
- `crates/gclient/tests/parity/dialogs.rs::*` — scope-reason: the settings toggle test for pane borders leaves
- `crates/gclient/tests/startup.rs::*` — scope-reason: the `sidebar_collapsed` pref assertions leave
- `crates/gclient/tests/screens.rs::*` — scope-reason: every golden changes with the hidden sidebar and the full-width rows
- `crates/gclient/tests/fixtures/screens/empty_workspace.txt`
- `crates/gclient/tests/fixtures/screens/projects_agents.txt`
- `crates/gclient/tests/fixtures/screens/split_live.txt`
- `crates/gclient/tests/fixtures/screens/label_ladder.txt`
- `crates/gclient/tests/fixtures/screens/pane_edges.txt`
- `crates/gclient/tests/fixtures/screens/help_dialog.txt`

Granularity: this section is one compile unit. Removing `SidebarState::collapsed` / `hide_when_collapsed` and `ClientPrefs::pane_borders` / `sidebar_collapsed` breaks every reader in the same build, so the readers (prefs, settings rows, the settings toggle, the sidebar action, pointer and wheel routing, the pane geometry) land together; a split would leave intermediate sections that do not build. Fourteen production files is the cost of one honest compile.

Split `chrome.rs`: move `SidebarState` and its `impl` (`default`, `scroll`, `scroll_mut`, `toggle_group`, `is_expanded`, `set_width_from_column`) out of `crates/gclient/src/ui/chrome.rs` into the new `crates/gclient/src/ui/chrome/sidebar_state.rs` (`mod sidebar_state; pub use sidebar_state::SidebarState;` beside the existing the `alerts` and `labels` submodules). `chrome.rs` is 899 lines with no `#[cfg(test)]`. In the moved struct replace `collapsed` and `hide_when_collapsed` with `pinned: bool` (default `false`: hidden). Remove `COLLAPSED_WIDTH`.

Split `actions.rs`: move the sidebar show/hide, width and section-scroll handlers out of `crates/gclient/src/app/live_loop/actions.rs` (979 lines, no `#[cfg(test)]`) into the new `crates/gclient/src/app/live_loop/actions/sidebar.rs`, declared `mod sidebar;` from `actions.rs`; the action that toggled collapse now toggles `pinned` (3.3 rebinds it to the overlay).

`Chrome::sidebar_width` returns `0` unless `pinned`; `Chrome::compute_view` lays the frame out as three bands: row 0 reserved (`menu_bar_rect`, drawn by 3.2b), the last row `status_rect` spanning the full frame width, and between them the sidebar column (when pinned) beside the content column whose first row is the tab bar (existing `show_tab_bar` rule) and the rest `terminal_area`. `ViewState` gains `menu_bar_rect: Rect` and loses `sidebar_toggle_hit_area`, `projects_new_hit_area` and `projects_menu_hit_area`; `apply_hits` drops `sidebar.toggle`, `sidebar.projects_new`, `sidebar.projects_menu`. `Chrome::apply_prefs` stops reading `sidebar_collapsed`.

`sidebar.rs`: delete `render_collapsed_sidebar`, `render_rail_list`, `collapsed_sections`, `collapsed_toggle_rect`, `render_toggle`, `RAIL_MIN_ROWS`, `COLLAPSE_LABEL`, `EXPAND_LABEL`, `MENU_LABEL`, `NEW_LABEL`, `BandStyle::menu`; `SidebarLayout` loses `menu` and `footer`; `SidebarHits` loses `projects_new`, `projects_menu`, `toggle`; `sidebar_layout` gives the whole height to the sections (blank-row rule as today); `section_rects` and `sessions_title_travel` lose their collapsed branch; `render_sidebar` no longer draws the menu band or the footer band. `render_navigation_chrome` calls `render_sidebar` only when `sidebar_rect.width > 0`. `Hit` loses `SidebarToggle`, `ProjectsNew`, `ProjectsMenu`; `hit_test` and `sidebar_hit` drop those arms.

Panes: `apply_pane_chrome(panes, pane_gaps)` loses the `pane_borders` parameter; every pane, a lone one included, gets `Borders::ALL`; with gaps off, a right/below neighbour still removes the shared edge so the divider is drawn once (existing rule). `pane_geometry`'s zoomed branch is always `Borders::ALL`. `render_border_lines` drops the `prefs.pane_borders` check. `ClientPrefs` loses `pane_borders` and `sidebar_collapsed`; `SettingsRow` loses `PaneBorders` (`ALL` becomes `[SettingsRow; 10]`); `row_label`/`row_value` lose the arm; `UiPrefs` keeps `pane_borders` and `sidebar_collapsed` as retired fields marked `#[serde(default, skip_serializing)]`, because `UiPrefs` carries `#[serde(default, deny_unknown_fields)]` (`prefs.rs` line 37) and an existing `prefs.toml` naming either key must still load; `ClientPrefs::from` ignores them, `UiPrefs::from` writes them as defaults, `save_prefs` never emits them, `ClientPrefs` does not carry them and no settings row shows them; `deny_unknown_fields` stays so a typo still fails (council round 2, adversary finding 1). `modal_input.rs` loses the `PaneBorders` toggle; `pointer.rs` loses the `Hit::SidebarToggle` arm; `wheel.rs` loses its collapsed branch.

Tests: delete `crates/gclient/src/ui/sidebar/tests.rs::collapsed_rail_shows_indexes_and_dots` and `rail_sections_share_the_rows_under_the_machine_dot`; update `layout_gives_the_top_half_to_machines_and_projects_at_most` and `expanded_sidebar_draws_the_bands_and_records_the_hits` for a footerless, menu-bandless layout. Rewrite `crates/gclient/tests/parity/chrome.rs::hidden_collapsed_sidebar_uses_full_width_terminal_area` as `hidden_sidebar_uses_full_width_terminal_area`, delete `collapsed_sidebar_keeps_active_workspace_highlight_in_terminal_mode`; `compute_view_clamps_sidebar_width_to_configured_max`/`_min` set `pinned = true` first. `crates/gclient/tests/parity/panes.rs`: delete `borderless_pane_gaps_add_one_empty_cell_between_panes` and `disabled_pane_borders_make_inner_rect_equal_visual_rect`; add NEW `lone_pane_draws_all_four_edges`. `crates/gclient/src/ui/pane_layout.rs`: delete the inline `borderless_gaps_shrink_the_left_pane_by_one_cell`. `crates/gclient/tests/parity/dialogs.rs`: delete the pane-borders toggle test around `borders_before`. `crates/gclient/tests/startup.rs`: `prefs_carry_sidebar_and_project_preferences` keeps `sidebar_collapsed = true` in its fixture text and drops its assertions on the field and on `chrome.sidebar.collapsed`; `prefs_round_trip_and_reject_unknown_keys` gains a retired-keys case (a file naming `pane_borders` and `sidebar_collapsed` loads to defaults, a fresh save omits both keys, and the `mouse_captre` typo case still fails). Regenerate all six goldens with `GOBBY_UPDATE_SCREENS=1`.

Consumers unchanged:
- `crates/gclient/src/ui/tab_surface.rs` — no-edit-reason: `compute_tab_surface` still splits the content column into the tab row and body; only its input rect moves.

Research context:
- Frame today: `Chrome::compute_view` splits `[sidebar_w | content]`, then content into `[Min(1) | Length(1) status]`, then the tab row; `render_workspace_with` paints `panel_bg`, calls `render_navigation_chrome` (collapsed → `render_collapsed_sidebar`) then `render_content_column` then `status::render_status_line`.
- `sidebar_layout` reserves `menu` (row 0, `BandStyle::menu`: bg accent, fg panel_bg, bold) and `footer` (`render_band(.., "", &[COLLAPSE_LABEL], BandStyle::section)`); `SidebarHits.toggle` feeds `ViewState::sidebar_toggle_hit_area` and `Hit::SidebarToggle`.
- Borders: `apply_pane_chrome(panes, pane_borders, pane_gaps)` sets `Borders::NONE` for a lone pane or when `pane_borders` is off; `render_border_lines` returns early on `!prefs.pane_borders`. The focus colour rule lives in `render_border_lines` (`accent` else `overlay0`).
- Readers of `collapsed`/`sidebar_collapsed`/`pane_borders` (checked with grep): `crates/gclient/src/ui/chrome.rs`, `crates/gclient/src/ui/chrome_render.rs`, `crates/gclient/src/ui/hit.rs`, `crates/gclient/src/ui/hit/tests.rs`, `crates/gclient/src/ui/settings.rs`, `crates/gclient/src/ui/sidebar.rs`, `crates/gclient/src/ui/sidebar/tests.rs`, `prefs.rs`, `crates/gclient/src/app/live_loop/actions.rs`, `crates/gclient/src/app/live_loop/mouse/pointer.rs`, `crates/gclient/src/app/live_loop/mouse/wheel.rs`, `crates/gclient/src/app/live_loop/modal_input.rs` (`SettingsRow::PaneBorders` arm), `crates/gclient/tests/parity/chrome.rs`, `crates/gclient/tests/parity/sidebar.rs`, `crates/gclient/tests/parity/panes.rs`, `crates/gclient/tests/parity/dialogs.rs`, `crates/gclient/tests/startup.rs`.
- Observed (council round 2, adversary finding 1): `UiPrefs` in `crates/gclient/src/prefs.rs` carries `#[serde(default, deny_unknown_fields)]` at line 37, and `crates/gclient/tests/startup.rs::prefs_round_trip_and_reject_unknown_keys` proves an unknown key is a `PrefsError::Parse` naming the key and line; `PrefsFile` and `KeymapPrefs` carry the same attribute. Retired keys therefore stay declared and are skipped on save.
- Rejected: keeping a rail behind a pref (the decision removes it); keeping `pane_borders` as a hidden pref (a dead setting); dropping `deny_unknown_fields` to let old keys through (it would also let typos through).
- Planned checks: `cargo nextest run -p gobby-client`; `GOBBY_UPDATE_SCREENS=1 cargo nextest run -p gobby-client --test screens`; `wc -l crates/gclient/src/ui/chrome.rs crates/gclient/src/app/live_loop/actions.rs` both under 850.

**Acceptance:**

- 3.2a.1 - `SidebarState` lives in the new module with `pinned` in place of `collapsed`/`hide_when_collapsed`. file: `crates/gclient/src/ui/chrome/sidebar_state.rs`.
- 3.2a.2 - A default `Chrome` yields `sidebar_rect.width == 0`, a full-width `status_rect` on the last row and `menu_bar_rect` on row 0. test: `crates/gclient/tests/parity/chrome.rs::hidden_sidebar_uses_full_width_terminal_area`.
- 3.2a.3 - A lone pane's `PaneInfo.borders == Borders::ALL` and the frame is drawn. test: `crates/gclient/tests/parity/panes.rs::lone_pane_draws_all_four_edges`.
- 3.2a.4 - Settings show ten rows and no `Pane borders` label. test: `crates/gclient/src/ui/settings.rs::row_values_follow_prefs`.
- 3.2a.5 - No rail symbol remains. behavior: "render_collapsed_sidebar" absent in `crates/gclient/src/ui/sidebar.rs`.
- 3.2a.6 - Goldens regenerate and match. test: `crates/gclient/tests/screens.rs::screens_match_committed_captures`.
- 3.2a.7 - A saved `prefs.toml` carrying `pane_borders` and `sidebar_collapsed` still loads, a fresh save omits both keys, and an unknown key still fails. symbol: `UiPrefs`. test: `crates/gclient/tests/startup.rs::prefs_round_trip_and_reject_unknown_keys` (existing, extended).

### 3.2b Menu bar row and status row slot [category: code] (depends: 3.2a)
`kind: deliverable`

Targets:
- `crates/gclient/src/ui/menu_bar.rs`
- `crates/gclient/src/ui/mod.rs`
- `crates/gclient/src/ui/chrome.rs::ViewState`
- `crates/gclient/src/ui/chrome.rs::ViewState::apply_hits`
- `crates/gclient/src/ui/chrome_render.rs::ChromeHits`
- `crates/gclient/src/ui/chrome_render.rs::render_workspace_with`
- `crates/gclient/src/ui/hit.rs::Hit`
- `crates/gclient/src/ui/hit.rs::hit_test`
- `crates/gclient/src/app/live_loop/menu.rs::*` — scope-reason: the inline `#[cfg(test)]` module moves out first, then `ContextMenuKind` gains `MenuBar`, and the `[Menu]`/`[+]` band menus become the menu-bar title menus whose item lists regroup under the seven titles
- `crates/gclient/src/app/live_loop/menu/tests.rs`
- `crates/gclient/src/ui/status.rs::render_status_line`
- `crates/gclient/src/ui/status.rs::status_line_shows_only_global_prefix_mode_and_health`
- `crates/gclient/src/ui/hit/tests.rs::hit_test_covers_split_live_layout`
- `crates/gclient/tests/parity/chrome.rs::*` — scope-reason: menu-bar rendering and hit tests are added beside the frame tests
- `crates/gclient/tests/screens.rs::*` — scope-reason: a `menu_bar` state is added and the goldens gain row 0
- `crates/gclient/tests/fixtures/screens/menu_bar.txt`

Create `crates/gclient/src/ui/menu_bar.rs` with `pub enum MenuBarMenu { Gobby, File, Edit, View, Window, Agent, Help }`, `pub const ALL: [MenuBarMenu; 7]` in exactly that order (`Gobby`, `File`, `Edit`, `View`, `Window`, `Agent`, `Help`; the signed chrome canvas is the source of this set and order), `title(self) -> &'static str`, `next()`/`previous()`, `pub struct MenuBarHits { pub titles: Vec<(usize, Rect)> }` and `pub fn render_menu_bar(frame, rect, chrome) -> MenuBarHits`: one full-width row, bg `accent`, fg `panel_bg`, bold, `MenuBarMenu::ALL` titles separated by two cells, the open title reversed. `MenuBarMenu::ALL` is the single definition of the title set; 3.11 imports it and never restates it. Register `pub mod menu_bar;` in `crates/gclient/src/ui/mod.rs`. `ChromeHits` gains `menu_bar: MenuBarHits`; `render_workspace_with` draws it first at `chrome.view.menu_bar_rect`; `ViewState` gains `menu_title_hit_areas: Vec<(usize, Rect)>` and `apply_hits` copies them. `Hit` gains `MenuTitle(usize)` and `MenuBarEmpty`; `hit_test` tests `menu_bar_rect` before the tab bar.

`chrome.rs` is at the size guard (899 lines; 3.2a split `SidebarState` out of it). This section does not grow it: the bar's titles, hit rects and renderer live in the new `crates/gclient/src/ui/menu_bar.rs`, and `chrome.rs` only gains the two `ViewState` fields.

`menu.rs` first moves its tests out: `menu.rs` is 970 lines in total, of which the inline `#[cfg(test)] mod tests` from line 450 to the end is 520 lines, and both the repository monolith hook and `crates/gclient/tests/source_size.rs::no_src_file_at_or_above_1000_lines` count every line, so the regrouping below (about 30 lines) would cross the ceiling at this section's close. Before regrouping, move that module verbatim to the new `crates/gclient/src/app/live_loop/menu/tests.rs`, declared in `menu.rs` as a `#[cfg(test)]` module with a `#[path]` attribute naming the new file (the crate's convention for out-of-line test modules; the precedents are listed in Research context); no test changes here. Then `ContextMenuKind` gains `MenuBar(MenuBarMenu)` and `build_menu` dispatches it to a provisional `menu_bar_items(ws, chrome, menu)` builder in `menu.rs` that regroups today's items under the titles: the context-menu state that the sidebar's `[Menu]` and `[+]` opened (alert log, new project, view scope and order) now opens from a title click: Gobby → alert log and settings; File → new project, new tab, new pane; Edit → `copy mode`, `rename pane`, `rename tab`, `rename terminal`, `clear pane name` and the right-click passthrough toggle (the existing pane-menu items, applied to the focused pane); View → the `[view]` scope and order items (Sidebar and Pin Sidebar items are 3.3); Window → tab and pane focus items; Help → keys; the Agent title's items are section 3.11 (the title renders, its menu is empty here). The menu overlay anchors under its title rect instead of the band.

`render_status_line`: the row becomes bg `surface0`; the `prefix <label>` hint and the mode word move to the right end (right-aligned, mode word in `accent` when a mode is active); the left part keeps today's content (`Daemon unreachable.`, overflow metadata) until 3.7 replaces it. Update the inline test `status_line_shows_only_global_prefix_mode_and_health` for right alignment. Add a `menu_bar` entry to `STATES` (a workspace with one tab and the View menu open) and its golden.

Consumers unchanged:
- `crates/gclient/src/ui/context_menu.rs` — no-edit-reason: it renders whatever `ContextMenuState` it is given; anchoring is the state's job.

Research context:
- `render_workspace_with` (`crates/gclient/src/ui/chrome_render.rs`) paints in order: sidebar, content column, status line, toasts, mode overlays; `Mode::ContextMenu` renders `context_menu::render_context_menu` last and undimmed.
- `ChromeHits { tab_bar, sidebar, control_indicator, toast, settings, menu_rows, dialog_buttons }`; `Chrome::apply_hits` places `menu_rows` into the open menu.
- `render_status_line` today: bg `surface_dim`, left-to-right `[overflow metadata │] prefix <label> [│ mode] [│ Daemon unreachable.] [│ title]`, returns the indicator rect; `mode_name` maps `Mode` to the word.
- `menu.rs` measured (`wc -l`, checked): 970 lines in total, `#[cfg(test)]` at line 450, so 520 inline test lines. `crates/gclient/tests/source_size.rs::no_src_file_at_or_above_1000_lines` uses `text.lines().count()` and the repository monolith hook counts the same way; neither stops at `#[cfg(test)]`, so the file has 29 lines of headroom and the test move above is what keeps this section's regroup under the ceiling.
- Out-of-line test module precedents (checked): `crates/gclient/src/ui/sidebar.rs:670`, `crates/gclient/src/ui/panes.rs:555` and `crates/gclient/src/ui/hit.rs:275` each declare `#[cfg(test)] #[path = "<mod>/tests.rs"] mod tests;`, which is the convention the new file follows.
- Assumption (unverified): the exact opener function names there; `gcode outline crates/gclient/src/app/live_loop/menu.rs` at implementation time.
- Planned checks: `cargo nextest run -p gobby-client -E 'test(chrome) | test(hit) | test(status) | test(screens)'`.

**Acceptance:**

- 3.2b.1 - The seven titles render on row 0 in accent. file: `crates/gclient/src/ui/menu_bar.rs`.
- 3.2b.2 - NEW regression: clicking a title returns `Hit::MenuTitle(i)` and opens that menu under it. test: `crates/gclient/tests/parity/chrome.rs::menu_bar_titles_hit_and_open_under_their_cell`.
- 3.2b.3 - Prefix hint and mode word sit at the right end on `surface0`. test: `crates/gclient/src/ui/status.rs::status_line_shows_only_global_prefix_mode_and_health`.
- 3.2b.4 - Hit precedence: menu bar before tab bar. test: `crates/gclient/src/ui/hit/tests.rs::hit_test_covers_split_live_layout`.
- 3.2b.5 - Golden for the open menu. file: `crates/gclient/tests/fixtures/screens/menu_bar.txt`.
- 3.2b.6 - The menu tests live in the new file and `menu.rs` stays under the ceiling after the regroup. file: `crates/gclient/src/app/live_loop/menu/tests.rs`. test: `crates/gclient/tests/source_size.rs::no_src_file_at_or_above_1000_lines` (existing).

### 3.3 Sidebar states: overlay, pinned, side [category: code] (depends: 3.2a, 3.2b)
`kind: deliverable`

Targets:
- `crates/gclient/src/ui/chrome/sidebar_state.rs`
- `crates/gclient/src/ui/chrome.rs::Chrome::sidebar_width`
- `crates/gclient/src/ui/chrome.rs::Chrome::compute_view`
- `crates/gclient/src/ui/chrome.rs::Chrome::apply_prefs`
- `crates/gclient/src/ui/chrome.rs::Chrome::focus_pane`
- `crates/gclient/src/ui/chrome_render.rs::render_workspace_with`
- `crates/gclient/src/ui/chrome_render.rs::render_navigation_chrome`
- `crates/gclient/src/ui/sidebar.rs::*` — scope-reason: the edge column, side and overlay accent edge touch layout, render and hit helpers
- `crates/gclient/src/ui/settings.rs::ClientPrefs`
- `crates/gclient/src/ui/settings.rs::ClientPrefs::default`
- `crates/gclient/src/ui/settings.rs::SettingsRow`
- `crates/gclient/src/ui/settings.rs::SettingsRow::ALL`
- `crates/gclient/src/ui/settings.rs::row_label`
- `crates/gclient/src/ui/settings.rs::row_value`
- `crates/gclient/src/ui/settings.rs::row_values_follow_prefs`
- `crates/gclient/src/prefs.rs::UiPrefs`
- `crates/gclient/src/prefs.rs::UiPrefs::from`
- `crates/gclient/src/prefs.rs::ClientPrefs::from`
- `crates/gclient/src/app/live_loop/actions/sidebar.rs`
- `crates/gclient/src/app/live_loop/modal_input.rs::*` — scope-reason: Esc in the overlay and the two new settings toggles
- `crates/gclient/src/app/live_loop/menu.rs::*` — scope-reason: View gains Sidebar and Pin Sidebar items, `MenuAction` gains `PinSidebar`, and `apply_local_menu_action` gains its arm
- `crates/gclient/src/ui/tabs.rs::render_tab_bar`
- `crates/gclient/src/ui/sidebar/tests.rs::*` — scope-reason: overlay and right-side layout tests are added
- `crates/gclient/tests/parity/chrome.rs::*` — scope-reason: overlay geometry and focus roll-up tests are added
- `crates/gclient/tests/parity/dialogs.rs::*` — scope-reason: settings toggles for side and pin are covered
- `crates/gclient/tests/screens.rs::*` — scope-reason: a `sidebar_overlay` state is added
- `crates/gclient/tests/fixtures/screens/sidebar_overlay.txt`

Granularity: overlay state, its persistence and its three openers (key, menu item, settings) are one behaviour; the ten production files are the state struct, the two layout/render entry points, the sidebar module, prefs and settings, the action handler, modal input, the menu, and the tab bar alignment.

`chrome.rs` is at the size guard (899 lines); 3.2a split `SidebarState` into `crates/gclient/src/ui/chrome/sidebar_state.rs`, and every field this section adds lands in that new file, so `chrome.rs` shrinks rather than grows.

`SidebarState` gains `overlay: bool` (never saved) and `side: SidebarSide` (`Left` default, `Right`), keeps `pinned`. `Chrome::sidebar_width` returns the clamped width only when `pinned`; `compute_view` lays the pinned column on `side` (right side: `[content | sidebar]`), and when `overlay` is set computes `sidebar_rect` as a 34-column rect on `side` over the content area between the menu bar and the status row without shrinking `terminal_area`. `render_workspace_with` draws the overlay after the content column and before toasts (`render_navigation_chrome` gains the overlay branch, which paints `panel_bg` over the area and an `accent` edge column on the inner side via `draw_separator_column`). The overlay takes focus: `Mode::Navigate` on open; Esc (in `modal_input.rs`) or any pane focus (`Chrome::focus_pane`) clears `overlay`. Openers: the `ctrl+b b` chord action in `actions/sidebar.rs` (`Action::ToggleSidebar`, the show/hide action from 3.2a, now toggles `overlay`; when `pinned`, it toggles `pinned` instead), View › Sidebar (`Act(ToggleSidebar)`, the same action), View › Pin Sidebar, and the status count (3.7). Pinning is not a keymap action: `actions/sidebar.rs` gains `toggle_sidebar_pin(chrome)`, which flips `pinned`, persists `prefs.sidebar_pinned`, and clears `overlay` when the result is pinned; the `SidebarPinned` settings row in `modal_input.rs` and View › Pin Sidebar both call it. The menu item is `MenuAction::PinSidebar`, added to `MenuAction` here and dispatched from `apply_local_menu_action` in `menu.rs` (a local chrome mutation like `ToggleGroup`, so it is reachable from the live dispatcher's fall-through and from the scripted loop alike). No new keymap binding is added. `render_tab_bar` right-aligns the labels (leading pad) while `overlay` is set and `side == Left`, left-aligns them while `side == Right`, so labels stay on the far side of the overlay.

Persistence: `ClientPrefs` gains `sidebar_side: SidebarSide` and `sidebar_pinned: bool`; `UiPrefs` and both `From` impls carry them, declared beside the two retired fields 3.2a keeps (`pane_borders`, `sidebar_collapsed`, parsed and ignored); `apply_prefs` copies them into `SidebarState`. `SettingsRow` gains `SidebarSide` (`Left`/`Right`, toggles) and `SidebarPinned` (`on`/`off`) after `SidebarWidth` (`ALL` becomes `[SettingsRow; 12]`); `row_label`/`row_value`/the toggle in `modal_input.rs` cover them; `row_values_follow_prefs` asserts both. Add a `sidebar_overlay` `STATES` entry (overlay open over a split) and its golden.

Consumers unchanged:
- `crates/gclient/src/ui/tab_surface.rs` — no-edit-reason: it calls `render_tab_bar` with the same arguments and passes the workspace view through; the alignment decision is made inside the callee from `SidebarState`.

Research context:
- `SidebarState` fields after 3.2a: `pinned`, `width`, `min_width`, `max_width`, `scrolls`, `selected`, `project_order`, `expanded_project`, `project_labels`, `machine_filter`, `all_projects`, `all_sessions`.
- `Chrome::focus_pane` (`crates/gclient/src/ui/chrome.rs`) is the single pane-focus entry; `Mode::Navigate` is the sidebar's keyboard mode; `draw_separator_column` paints the edge in `accent` while navigating.
- `render_tab_bar` pads each label with `format!(" {:width$}", name)`; alignment is the pad side.
- Observed: `crates/gclient/src/ui/keymap/names.rs` already binds `Action::ToggleSidebar` as `toggle_sidebar` ("Toggle the sidebar", `prefix+b`), and that is the only sidebar `Action`; its arm at `crates/gclient/src/app/live_loop/actions.rs:505` flips `collapsed` and persists `sidebar_collapsed` (line 507), which 3.2a rewrites to the overlay/pinned behaviour above. `SettingsRow::PaneBorders` at `modal_input.rs:392` writes prefs directly rather than through an `Action`, which is the precedent the `SidebarPinned` row follows. `gcode grep -F PinSidebar crates/gclient` is empty today. Persistence writers for `sidebar_pinned` are `actions/sidebar.rs` and `modal_input.rs` only. Prefs key names: `sidebar_side = "left"|"right"`, `sidebar_pinned = false`.
- Planned checks: `cargo nextest run -p gobby-client -E 'test(sidebar) | test(chrome) | test(dialogs) | test(screens)'`.

**Acceptance:**

- 3.3.1 - `SidebarState { pinned, overlay, side }` with `overlay` never serialized. file: `crates/gclient/src/ui/chrome/sidebar_state.rs`.
- 3.3.2 - NEW regression: overlay yields a 34-column `sidebar_rect` and an unchanged `terminal_area`; pinned yields the column layout on the chosen side. test: `crates/gclient/tests/parity/chrome.rs::overlay_covers_34_columns_without_moving_panes`.
- 3.3.3 - NEW regression: Esc and `focus_pane` clear the overlay. test: `crates/gclient/tests/parity/chrome.rs::overlay_rolls_up_on_escape_and_pane_focus`.
- 3.3.4 - Two settings rows persist through `prefs.toml`. test: `crates/gclient/src/ui/settings.rs::row_values_follow_prefs`.
- 3.3.5 - Tab labels sit on the far side while the overlay is open. symbol: `render_tab_bar`.
- 3.3.6 - Golden. file: `crates/gclient/tests/fixtures/screens/sidebar_overlay.txt`.
- 3.3.7 - View › Pin Sidebar (`MenuAction::PinSidebar`) and the `SidebarPinned` settings row share `toggle_sidebar_pin`, which persists `sidebar_pinned` and closes the overlay when pinning. symbol: `toggle_sidebar_pin`. file: `crates/gclient/src/app/live_loop/actions/sidebar.rs`.

### 3.4a Sidebar sections: Machines, Projects, Agents, Terminals [category: code] (depends: 3.3)
`kind: deliverable`

Targets:
- `crates/gclient/src/ui/hit.rs::SidebarSection`
- `crates/gclient/src/ui/hit.rs::SidebarSection::ALL`
- `crates/gclient/src/ui/hit.rs::SidebarSection::title`
- `crates/gclient/src/ui/hit.rs::Hit`
- `crates/gclient/src/ui/hit.rs::sidebar_hit`
- `crates/gclient/src/ui/hit.rs::sidebar_section_at`
- `crates/gclient/src/ui/sidebar.rs::*` — scope-reason: four section rects, the Agents/Terminals height share, band titles and travel helpers
- `crates/gclient/src/ui/sidebar/sessions.rs::*` — operation: delete — scope-reason: renamed to `agents.rs` with `git mv`; nothing stays behind
- `crates/gclient/src/ui/sidebar/agents.rs`
- `crates/gclient/src/ui/sidebar/terminals.rs`
- `crates/gclient/src/ui/chrome/sidebar_state.rs`
- `crates/gclient/src/ui/chrome.rs::ViewState`
- `crates/gclient/src/ui/chrome.rs::ViewState::apply_hits`
- `crates/gclient/src/ui/chrome.rs::Chrome::compute_view`
- `crates/gclient/src/ui/sidebar/tests.rs::*` — scope-reason: layout tests gain the fourth section
- `crates/gclient/src/ui/hit/tests.rs::sidebar_scrollbar_lane_hits_by_section`
- `crates/gclient/src/ui/panes/tests.rs::*` — scope-reason: its `sidebar::sessions` import moves to `sidebar::agents`
- `crates/gclient/tests/parity/sidebar.rs::*` — scope-reason: helpers address `Agents`/`Terminals` and the row fixtures split by section

`SidebarSection` becomes `{ Machines, Projects, Agents, Terminals }` (`ALL: [SidebarSection; 4]`, titles `Agents` and `Terminals`). Every `[_; 3]` keyed by `SidebarSection::index` becomes `[_; 4]`: `SidebarState::scrolls`, `SidebarLayout::sections`, `SidebarHits::scrollbars`, `ViewState::sidebar_section_rects`, `ViewState::sidebar_scrollbar_hit_areas`, the return of `section_rects`. `sidebar_layout` keeps the top-half cap for machines and projects and splits the remaining rows between Agents and Terminals (Terminals gets `rows / 2`, Agents the rest plus the remainder; a Terminals section with no rows collapses to its band so Agents takes the rest). `section_gap_rows` gives Agents `GAP_ROWS` and Terminals `0`. Rename `sessions_title_travel` → `agents_title_travel` (Agents rows only; Terminal rows never ticker). `Hit::SessionsView` → `Hit::AgentsView`; `SidebarHits.sessions_view` → `agents_view`; `ViewState.sessions_view_hit_area` → `agents_view_hit_area`; `[view]` stays on the Agents band and `[working]` on the Projects band.

Rename `crates/gclient/src/ui/sidebar/sessions.rs` to `crates/gclient/src/ui/sidebar/agents.rs` (`git mv`; `sidebar.rs` declares `pub mod agents; pub mod terminals;`), keeping every public item (`ALL_MACHINES`, `VIEW_LABEL`, `TERMINAL_ROW`, `agent_label`, `next_machine_filter`, `agent_blocked`, `machine_admits`, `session_rows` → `agent_rows`, `attention_order`, `render_sessions` → `render_agents`). Move `bare_terminals` out of it into the new `crates/gclient/src/ui/sidebar/terminals.rs` as `terminal_rows(ws, chrome) -> Vec<SidebarRow>` and `render_terminals(frame, area, rows, chrome, hits)` (band title `Terminals`, no controls, `render_section_rows` under it, hits into `SidebarHits.agents` by `TERMINAL_ROW` id as today). `render_sidebar` calls `machines`, `projects`, `agents::render_agents`, `terminals::render_terminals` in section order. Every importer of `crate::ui::sidebar::sessions` (found by `gcode grep -F "sidebar::sessions"`) switches to `sidebar::agents`; those are one-line import edits. `compute_view` reads the Agents rect for `agents_title_travel`.

Consumers unchanged:
- `crates/gclient/src/ui/sidebar/machines.rs` — no-edit-reason: it renders `layout.sections[0]` and reads no section count.
- `crates/gclient/src/ui/sidebar/projects.rs` — no-edit-reason: same, `sections[1]`.

Research context:
- `SidebarSection` (`crates/gclient/src/ui/hit.rs`): `Machines, Projects, Sessions`, `ALL`, `index`, `title`. `sidebar_section_at` uses `sidebar_section_rects`. `ViewState` arrays: `sidebar_section_rects: [Rect; 3]`, `sidebar_scrollbar_hit_areas: [Option<Rect>; 3]`; `SidebarState::scrolls: [usize; 3]`.
- `sidebar_layout(area, machine_rows, project_rows)`: top-half cap, `sessions = rows - machines - projects`; `section_rects` sums `project_rows` heights; `section_body_rect`/`section_gap_rows`; `render_section_rows` and `section_list` are section-agnostic.
- `sessions.rs`: `session_rows` builds candidates from `visible_agents` + `bare_terminals` (rows with `TERMINAL_ROW` ids, `RowKind::Agent`, tokens `[address, backend.label()]`), `arrange`/`push_children` nest runs; `render_sessions` draws the band with `VIEW_LABEL`.
- Importers of `sidebar::sessions` (observed with `gcode grep -F "sidebar::sessions"`): `crates/gclient/src/ui/chrome.rs` and `crates/gclient/src/ui/panes/tests.rs` only; both are one-line `use` edits.
- Planned checks: `cargo nextest run -p gobby-client -E 'test(sidebar) | test(hit)'`.

**Acceptance:**

- 3.4a.1 - Four sections with titles `Machines`, `Projects`, `Agents`, `Terminals`. symbol: `SidebarSection`.
- 3.4a.2 - `sidebar_layout` yields four rects; Agents and Terminals share the remainder. test: `crates/gclient/src/ui/sidebar/tests.rs::layout_gives_the_top_half_to_machines_and_projects_at_most`.
- 3.4a.3 - Scrollbar lanes hit by all four sections. test: `crates/gclient/src/ui/hit/tests.rs::sidebar_scrollbar_lane_hits_by_section`.
- 3.4a.4 - The module rename lands. file: `crates/gclient/src/ui/sidebar/agents.rs`.
- 3.4a.5 - Bare terminals render under their own band. file: `crates/gclient/src/ui/sidebar/terminals.rs`.

### 3.4b Agent and terminal rows [category: code] (depends: 3.1, 3.4a)
`kind: deliverable`

Targets:
- `crates/gclient/src/ui/sidebar_rows.rs::*` — scope-reason: the row model, heights, span builders, travel and second/third line all change
- `crates/gclient/src/ui/sidebar/agents.rs`
- `crates/gclient/src/ui/sidebar/terminals.rs`
- `crates/gclient/src/ui/sidebar_rows/tests.rs::*` — scope-reason: every row-line test is rewritten for the three-line agent row and the terminal row
- `crates/gclient/tests/parity/sidebar.rs::*` — scope-reason: row rendering assertions change kind by kind
- `crates/gclient/tests/screens.rs::*` — scope-reason: an `agent_rows` state is added and `projects_agents` regenerates
- `crates/gclient/tests/fixtures/screens/agent_rows.txt`
- `crates/gclient/tests/fixtures/screens/projects_agents.txt`

`RowKind` gains `Terminal`; `SidebarRow::height` returns 3 for `Agent`, 2 for `Terminal`. `SidebarRow` gains `definition: String` (line 1 name), `reference: String` (the `(ref)` suffix: bare `#ref`, or `project#ref` only when `chrome.sidebar.all_sessions` is set and `agent_sort == Priority`, the "All projects by priority" view), `provider: Option<String>` (line 1 trailing token for interactive sessions only), `task: Option<(String, String)>` (`(ref, title)`), `model_slug: String`; `tokens` and `title_prefix` leave. `row_line` for `Agent`: marker, nest prefix, state glyph, then the reference laid out first (`(ref)` right after the name, both bold `text` when selected or active, else `subtext0` with no BOLD, uniform with Project rows), and the definition cut with `…` to the remaining budget (24 cells at width 34, 16 at 26, given the 1-cell marker, glyph, space and a 7-cell ` (#ref)`), then ` · provider` when `provider` is set and fits. `row_second_line` for `Agent`: indent, then the pinned `Task #ref - ` prefix (never scrolls) and the title through `ticker_window` on the remaining budget, or `No assigned task` dim; `row_travel` measures the title overrun after the pinned prefix so it feeds `max_travel` as today. New `row_third_line`: indent then `model_slug` in `overlay0` with no DIM; the third line is drawn by `render_section_rows` for height-3 rows (that call lands with 3.4a's `sidebar.rs` scope). `Terminal` rows: line 1 the foreground app or spawn shell (`pane.display_name()`), line 2 `gclient` or `tmux` (`pane.backend.label()`); no address on any row. `model_slug` is `agent.model_slug()` (the 3.1 helper) truncated to 17 cells; the truncation is row-only and lives here.

`agents.rs::agent_candidate` fills `definition` from `agent.definition_label()`, `reference` from `session_ref` (bare, or project-prefixed in the priority all-projects view via `project_label`), `provider` only when `!agent.managed`, `task` from `task_ref`/`task_title`, `model_slug` from `agent.model_slug()`; `agent_label`/`agent_title` keep their navigator use. `terminals.rs::terminal_rows` builds `RowKind::Terminal` rows with `label` and `detail = backend.label()`.

Tests: rewrite `crates/gclient/src/ui/sidebar_rows/tests.rs::session_rows_render_session_effort_without_a_stray_separator` as `agent_rows_render_three_lines_with_the_model_slug`, `agent_address_prefix_stays_fixed_while_unicode_title_scrolls` as `task_prefix_stays_fixed_while_the_title_scrolls`, `agent_lines_carry_needs_you_and_nest_under_their_session` for the new line 1; keep `marquee_shares_one_period_and_parks_shorter_titles` and `every_overflowing_row_uses_the_selected_scroll_direction` green against the task title. In `crates/gclient/tests/parity/sidebar.rs` update `default_agent_rows_remove_redundant_state_text`, `occurrence_false_removes_default_workspace_bold_and_agent_dim`, `default_agent_row_gap_packs_rendering_and_scroll_geometry`, `variable_agent_heights_pack_the_bottom_and_reveal_targets`, `stripped_terminal_title_renders_with_unicode_width_truncation`. Add an `agent_rows` `STATES` entry (a 34-column pinned sidebar with a 30-cell definition name, a long task title, and one bare terminal) and regenerate `projects_agents`.

Consumers unchanged:
- `crates/gclient/src/ui/navigator.rs` — no-edit-reason: it queries `agent_label` and row ids, which keep their meaning.

Research context:
- `row_line` (`crates/gclient/src/ui/sidebar_rows.rs`) styles `RowKind::Agent` titles `subtext0 + BOLD` when unselected while `Project` rows are plain `subtext0`; `agent_spans` lays prefix then title; `row_second_line` renders `tokens` dim `overlay0`; `row_travel` computes overrun for `Agent` rows only; `ticker_window(text, budget, ticker, max_travel, direction)`; `TICKER_MIN_WINDOW = 4`.
- `agent_candidate` (`sidebar/sessions.rs`, renamed in 3.4a) builds `tokens = [address, provider, model+effort, machine]` and `title_prefix = "{project?}{#ref}: "` when `all_sessions`.
- `AgentEntry` after 3.1: `agent_definition_name`, `task_title`, `task_ref`, `session_ref`, `managed`, `model_display_name`, `effort`, `definition_label()`, `model_slug()`.
- Budget arithmetic at 34 columns: marker 1 + glyph 1 + space 1 + ` (#1234)` 8 = 11 → 23 cells for a 4-digit ref; the decision's 24/16 figures assume a 7-cell reference; implement as `budget - (reference width + 1)` and pin the 24/16 figures in the test with a matching fixture ref.
- Planned checks: `cargo nextest run -p gobby-client -E 'test(sidebar_rows) | test(sidebar) | test(screens)'`.

**Acceptance:**

- 3.4b.1 - `RowKind::Terminal` exists and `height()` is 3/2. symbol: `RowKind`.
- 3.4b.2 - Three-line agent row: bold name cut with an ellipsis before `(ref)`, slug on line 3 without DIM. test: `crates/gclient/src/ui/sidebar_rows/tests.rs::agent_rows_render_three_lines_with_the_model_slug`.
- 3.4b.3 - `Task #ref - ` pinned while the title scrolls; `No assigned task` otherwise. test: `crates/gclient/src/ui/sidebar_rows/tests.rs::task_prefix_stays_fixed_while_the_title_scrolls`.
- 3.4b.4 - Terminal rows show app over backend, no address. test: `crates/gclient/tests/parity/sidebar.rs::stripped_terminal_title_renders_with_unicode_width_truncation`.
- 3.4b.5 - Unselected Agent and Project rows share one weight. test: `crates/gclient/tests/parity/sidebar.rs::occurrence_false_removes_default_workspace_bold_and_agent_dim`.
- 3.4b.6 - Golden. file: `crates/gclient/tests/fixtures/screens/agent_rows.txt`.

### 3.5 Tab labels and bar style [category: code] (depends: 3.4b)
`kind: deliverable`

Targets:
- `crates/gclient/src/ui/tabs.rs::MIN_TAB_WIDTH`
- `crates/gclient/src/ui/tabs.rs::tab_display_name`
- `crates/gclient/src/ui/tabs.rs::tab_chrome_label`
- `crates/gclient/src/ui/tabs.rs::tab_width`
- `crates/gclient/src/ui/tabs.rs::max_tab_scroll`
- `crates/gclient/src/ui/tabs.rs::layout_tab_hit_areas`
- `crates/gclient/src/ui/tabs.rs::centered_tab_scroll`
- `crates/gclient/src/ui/tabs.rs::compute_tab_bar_view`
- `crates/gclient/src/ui/tabs.rs::render_tab_bar`
- `crates/gclient/src/ui/tabs.rs::chrome_with_tabs`
- `crates/gclient/src/ui/tabs.rs::draw`
- `crates/gclient/src/ui/tabs.rs::fitting_tabs_expose_every_tab_and_the_new_tab_button`
- `crates/gclient/src/ui/tabs.rs::overflowing_tabs_get_scroll_arrows_and_follow_the_active_tab`
- `crates/gclient/tests/parity/tabs.rs::*` — scope-reason: every label assertion changes to the `project:ref` form
- `crates/gclient/tests/parity/chrome.rs::*` — scope-reason: the tab-bar colour and overflow tests change
- `crates/gclient/tests/screens.rs::*` — scope-reason: goldens with tabs regenerate
- `crates/gclient/tests/fixtures/screens/split_live.txt`
- `crates/gclient/tests/fixtures/screens/projects_agents.txt`

`tab_chrome_label(ws, chrome, tabs, idx)` emits `{project}:{tab.id}` for an auto-named tab (`gobby:0:0:1`, project from `sidebar_rows::project_label(ws, chrome, project_id)` for the focused project, else the id) and `{project}:{title}` for a renamed one; ` Z` stays appended when zoomed. `tab_display_name` keeps returning the bare name for the navigator and the rename dialog. Labels of tabs whose panes you cannot see (any non-active tab) carry a leading `⍾ ` when any pane in that tab holds attention: map `ws.attention_entry_ids()` → `ws.sidebar().agents` `terminal_id` → `ws.pane_for_terminal` → `tab.slot_for`. `MIN_TAB_WIDTH` grows to 12. Because labels now need `ws`, `tab_width`, `layout_tab_hit_areas`, `max_tab_scroll`, `centered_tab_scroll` and `compute_tab_bar_view` take the precomputed label list (`&[String]`) instead of recomputing from `tabs`; `render_tab_bar` builds the list once. Bar style: the row is `surface0`; the active tab is cut out in `panel_bg` with bold `text`; inactive tabs `overlay1` on `surface0`, auto-named ones without DIM (the ref is now meaningful); the attention mark is drawn in the warning role. The 3.3 alignment rule stays. Update the inline tests (`chrome_with_tabs`, `draw` build a scripted workspace) and `crates/gclient/tests/parity/tabs.rs` (`tab_bar_marks_zoomed_tabs_without_renaming_them`, `active_auto_named_tab_keeps_readable_weight`, `zoom_marker_counts_toward_tab_width`, `tab_width_uses_display_width_for_cjk_labels`, `tab_bar_renders_trailing_cjk_character`, `tab_bar_clicks_activate_spawn_and_scroll`, `tab_drag_reorders_or_clicks`) and `crates/gclient/tests/parity/chrome.rs` (`tab_bar_dims_auto_named_tabs_and_emphasizes_custom_tabs` → `tab_bar_cuts_the_active_tab_out_in_panel_bg`, `tab_bar_uses_surface_dim_when_panel_background_resets`, `new_tab_button_tracks_rightmost_tab_when_tabs_fit`, `tab_bar_shows_scroll_controls_when_tabs_overflow`, `tab_bar_clamps_manual_scroll_at_last_visible_tab`).

Consumers unchanged:
- `crates/gclient/src/ui/navigator.rs` — no-edit-reason: it uses `tab_display_name`, which is unchanged.
- `crates/gclient/src/ui/tab_surface.rs` — no-edit-reason: it passes `ws` through to `render_tab_bar` already.

Research context:
- `tabs.rs`: `MIN_TAB_WIDTH = 8`, `tab_is_auto_named` (empty title), `tab_display_name` (index+1 or title), `tab_chrome_label` (adds ` Z`), `tab_width` (+4, min), `render_tab_bar(frame, area, _ws, chrome)` styles the active tab `panel_contrast_fg` on `accent` and auto-named tabs DIM. `Tab { id, title, layout, slots, worktree_id }`; `Tab::slot_for(pane)`.
- Attention: `WorkspaceView::attention_entry_ids`, `AgentEntry.terminal_id`, `WorkspaceView::pane_for_terminal`.
- Observed: the tab ref is `node:workspace:tab` composed from the `reference` fields the workspace stream carries (`crates/gclient/src/daemon/workspace.rs`, the user guide's `tab-0:0:1`); `Tab.id` is the daemon tab id, or a `local_tab_id` for scripted tabs, which fall back to index+1. Attention is joined roster entry → agent row → `terminal_id` → pane, never by treating the entry subject as a terminal id (memory 2c8899dc). `⍾` (U+237E) is a literal in the code and the tests.
- Planned checks: `cargo nextest run -p gobby-client -E 'test(tabs) | test(chrome) | test(screens)'`.

**Acceptance:**

- 3.5.1 - Auto-named label is `project:tab_id`, renamed is `project:title`. test: `crates/gclient/tests/parity/tabs.rs::tab_bar_marks_zoomed_tabs_without_renaming_them`.
- 3.5.2 - NEW regression: a hidden tab with a blocked pane carries `⍾`. test: `crates/gclient/tests/parity/tabs.rs::hidden_tab_with_attention_carries_the_mark`.
- 3.5.3 - Active tab is `panel_bg` bold on a `surface0` row. test: `crates/gclient/tests/parity/chrome.rs::tab_bar_cuts_the_active_tab_out_in_panel_bg`.
- 3.5.4 - `MIN_TAB_WIDTH` fits `gobby:0:0:1`. symbol: `MIN_TAB_WIDTH`.
- 3.5.5 - Goldens. test: `crates/gclient/tests/screens.rs::screens_match_committed_captures`.

### 3.6 Pane chrome: task title, footer corners, frame colour, no shell literal [category: code] (depends: 2.2, 3.1, 3.2b, 3.5)
`kind: deliverable`

Targets:
- `crates/gclient/src/ui/pane_chrome.rs::pane_title`
- `crates/gclient/src/ui/pane_chrome.rs::MetadataTone`
- `crates/gclient/src/ui/pane_chrome.rs::PaneMetadata`
- `crates/gclient/src/ui/pane_chrome.rs::pane_metadata`
- `crates/gclient/src/ui/pane_chrome.rs::has_bottom_edge`
- `crates/gclient/src/ui/pane_chrome.rs::top_reserve`
- `crates/gclient/src/ui/pane_chrome.rs::metadata_rect`
- `crates/gclient/src/ui/pane_chrome.rs::title_travel`
- `crates/gclient/src/ui/pane_chrome.rs::control_indicator_hit_area`
- `crates/gclient/src/ui/panes.rs::render_panes`
- `crates/gclient/src/ui/panes.rs::render_border_lines`
- `crates/gclient/src/ui/panes.rs::render_pane_border_titles`
- `crates/gclient/src/ui/panes.rs::render_pane_metadata`
- `crates/gclient/src/app/pane.rs::UNNAMED_PANE`
- `crates/gclient/src/app/pane.rs::Pane::display_name`
- `crates/gclient/src/app/mod.rs::*` — scope-reason: the `UNNAMED_PANE` re-export leaves the `pub use pane::{…}` list, and the pane-access methods of `impl<D: Daemon> Workspace<D>` move out with a `mod workspace_panes;` declaration
- `crates/gclient/src/app/workspace_panes.rs`
- `crates/gclient/src/ui/chrome/labels.rs::attention_label`
- `crates/gclient/src/ui/chrome/labels.rs::terminal_label`
- `crates/gclient/src/app/live_loop/orphans.rs::*` — scope-reason: both `"shell"` literals leave with the daemon-supplied command
- `crates/gclient/src/app/sidebar_model.rs::build_agents`
- `crates/gclient/src/ui/status.rs::focused_overflow`
- `crates/gclient/src/ui/status.rs::render_status_line`
- `crates/gclient/src/ui/status.rs::borderless_focused_pane_keeps_its_metadata_and_title_here`
- `crates/gclient/src/ui/status.rs::metadata_too_wide_for_its_pane_edge_lands_here_without_the_title`
- `crates/gclient/src/ui/pane_chrome/tests.rs::*` — scope-reason: every metadata and title test changes shape
- `crates/gclient/src/ui/panes/tests.rs::*` — scope-reason: title, metadata and frame-colour tests change
- `crates/gclient/tests/terminal_naming.rs::*` — scope-reason: the `shell` assertions become command assertions
- `crates/gclient/tests/parity/panes.rs::*` — scope-reason: junction and focus-colour tests gain attention and exited states
- `crates/gclient/tests/parity/status.rs::*` — scope-reason: `control_indicator_is_a_button` asserts the footer strings that move to the corners
- `crates/gclient/tests/sidebar_model.rs::build_joins_projects_worktrees_and_agents`
- `crates/gclient/tests/screens.rs::*` — scope-reason: an `unnamed_pane` state is added; `label_ladder` and `pane_edges` regenerate
- `crates/gclient/tests/fixtures/screens/unnamed_pane.txt`
- `crates/gclient/tests/fixtures/screens/label_ladder.txt`
- `crates/gclient/tests/fixtures/screens/pane_edges.txt`

`pane_title`: for a pane whose roster agent has `task_ref`, `Task #ref - {task_title}` (the title part omitted when `task_title` is `None`); else the manual title (`pane.label`); else the provisional title (`session_title`, then `definition_label()`); a bare terminal pane (no roster agent) returns an empty string unless renamed, so `header_title` draws nothing. `PaneMetadata` becomes `PaneFooter { left: String, right: String, tone: MetadataTone, actionable: bool }`: `left` is `{definition} ({project}#{ref})` for an agent pane or `{fg_app}` (`display_name`) for a terminal, followed by ` · Focused` / ` · Read-only` / ` · Uncertain` under today's rules; `right` is `{backend} {address}`: for a gclient pane the `node:workspace:tab:pane` ref the workspace stream carries (`gclient 0:0:1:2`), for a tmux pane `Pane.address` (`tmux %15`); backend alone when neither is known. `metadata_rect` becomes `footer_rects(info, footer) -> Option<(Rect, Rect)>` on the bottom edge (left corner +1, right corner −1 as today); since every pane now has four edges the no-bottom-edge fallback is unreachable, but `has_bottom_edge` still gates it and `top_reserve` returns 0 when the edge exists, so `title_travel` stays honest; `control_indicator_hit_area` returns the left rect. `render_pane_metadata` draws both corners; `render_border_lines` colours the frame by state: `accent` when focused, the warning role with `⍾` set in the top-left corner cell when the pane's agent holds attention, the destructive role with `◌` when the pane's terminal has exited (`pane_state == Exited`, or the `terminal_state` the daemon reports), `overlay0` otherwise; junction cells follow the focus rule as today (`line_touches_pane`). `render_pane_border_titles` bolds the focused title only.

Delete `UNNAMED_PANE` and its `display_name` rung: `display_name` returns `label` → `command` (the foreground or spawn command the daemon now always supplies, section 2.2) → `short_terminal_id(&self.terminal_id)` (the 8-character prefix `orphans.rs` already shows); `build_agents` drops the `UNNAMED_PANE` fallback (`pane.display_name()` is the last rung). The other two readers go with it (council round 2, adversary finding 2): `crates/gclient/src/app/mod.rs` drops `UNNAMED_PANE` from its `pub use pane::{…}` list, and in `crates/gclient/src/ui/chrome/labels.rs` the `use crate::app::{PaneId, UNNAMED_PANE}` import becomes `{short_terminal_id, PaneId}`, `attention_label`'s no-pane branch returns `short_terminal_id(attention_subject(entry_id)).to_string()` and `terminal_label`'s no-pane, no-agent fallback returns `short_terminal_id(terminal_id).to_string()`, the same last rung `display_name` uses. Remove the two `"shell"` literals in `orphans.rs` (the adopted orphan's title and name come from the daemon row's `command`). `focused_overflow`/`render_status_line` read `PaneFooter.left` for the overflow slot. Update `crates/gclient/src/ui/pane_chrome/tests.rs` (`pane_title_prefers_session_then_label_then_terminal_name` → `pane_title_prefers_task_then_label_then_provisional`, `pane_metadata_maps_focus_and_exception_states_per_backend`, `metadata_sits_bottom_right_one_cell_short_of_the_corner`, delete `pane_without_its_own_bottom_edge_keeps_metadata_top_right`, `title_travel_measures_each_header_window`), `crates/gclient/src/ui/panes/tests.rs` (`render_panes_draws_titles_and_focus_marker`, `bottom_metadata_renders_with_and_without_pane_gaps`, `stacked_panes_without_gaps_keep_metadata_on_their_own_title_row`, `hovered_read_only_metadata_underlines_only_while_actionable`), `crates/gclient/tests/terminal_naming.rs` (the `["shell", "shell"]` and `["shell"]` assertions), the `status.rs` inline tests (rename `borderless_focused_pane_keeps_its_metadata_and_title_here` → `focused_pane_footer_overflows_to_the_status_row`), and add `unnamed_pane` to `STATES` (a pane opened without a label so the empty top-left and the `command` rung are visible; the other goldens are scripted with named panes).

Split `crates/gclient/src/app/mod.rs` (927 lines, no `#[cfg(test)]`, so the one-line re-export edit already sits above the 850-line growth threshold): move the pane-access methods of `impl<D: Daemon> Workspace<D>` — `pane`, `pane_mut`, `pane_for_terminal`, `pane_by_attachment`, `replace_frame_source`, `recv_pane_frame`, `record_source_message`, `pane_count`, `pane_for_attachment_mut`, `retire_attachment` and `remove_terminal` (lines 804-888 and 898-926 today, about 114 lines) — out of `crates/gclient/src/app/mod.rs` into the new `crates/gclient/src/app/workspace_panes.rs`, a second `impl<D: Daemon> Workspace<D>` block declared `mod workspace_panes;` beside the existing module list (a child module of `app`, so the private `Workspace` fields stay reachable). The attention and shutdown methods stay; nothing changes signature, so no caller moves. This is the same extension-file pattern 3.2a and 3.10 apply to the chrome module; 3.10's later one-line `pub mod startup_stages;` then lands in a file under the threshold.

Consumers unchanged:
- `crates/gclient/src/ui/navigator.rs` — no-edit-reason: it lists rows by `agent_label`, not by pane title.

Research context:
- `crates/gclient/src/app/mod.rs` (927 lines): module list at lines 3-17 (`mod pane;` is private, so every reader outside `app` reaches `UNNAMED_PANE` through the re-export at line 39), `impl Workspace` (scripted, lines 191-720), `impl<D: Daemon> Workspace<D>` (lines 723-926, the pane-access, attention and shutdown methods).
- `pane_chrome.rs`: `pane_title` ladder session_title → `pane.label` → `display_name`; `pane_metadata` builds `"{backend} · Focused|Read-only|Uncertain"` with `MetadataTone`; `metadata_rect` bottom-right when `has_bottom_edge` else top-right via `top_reserve`; `title_travel` and `control_indicator_hit_area`.
- `panes.rs`: `render_panes` collects `(pane_title, pane_metadata)` per pane, `header_title` tickers the title, `render_border_lines` colours by `is_focused`, `render_pane_metadata` draws ` text ` at the rect and underlines on hover.
- `crates/gclient/src/app/pane.rs`: `UNNAMED_PANE = "shell"` (line 78), `Pane::display_name` with the `UNNAMED_PANE` rung, `short_terminal_id` (line 84), `Pane.terminal_id` (line 136); `Backend::label` → `gclient`/`tmux`; `Pane { label, address, command, .. }`.
- Readers of `UNNAMED_PANE` (`gcode grep -w UNNAMED_PANE`, council round 2): `crates/gclient/src/app/pane.rs` lines 78 and 310, `crates/gclient/src/app/mod.rs` line 39 (the `pub use` list), `crates/gclient/src/app/sidebar_model.rs` lines 17 and 210 (`build_agents`), `crates/gclient/src/ui/chrome/labels.rs` lines 11, 105 (`attention_label`) and 137 (`terminal_label`). No other file names it.
- `orphans.rs` literals at lines 248 (`"title": "shell"`) and 310 (`name: "shell".to_string()`) are both inside its `#[cfg(test)]` module (starts at line 220), so the edit is test cleanup: the fixtures name the daemon `command` instead.
- Palette roles (observed in `theme.rs`): the token set has `accent`, `warning`, `destructive`; `Palette` exposes them as `accent`, `yellow` (warning) and `red` (destructive) beside `overlay0`, `text`, `subtext0`, `surface0/1/dim`, `panel_bg`; `crates/gclient/tests/theme.rs::palette_entries_bind_the_same_tokens_the_render_paints_with` guards the binding.
- Rejected: keeping a top-right metadata fallback (dead with four edges); keeping `UNNAMED_PANE` as a last-resort rung (2.2 makes `command` always present).
- Planned checks: `cargo nextest run -p gobby-client -E 'test(pane) | test(terminal_naming) | test(status) | test(screens)'`.

**Acceptance:**

- 3.6.1 - Title ladder task → manual → provisional; a bare terminal is empty unless renamed. test: `crates/gclient/src/ui/pane_chrome/tests.rs::pane_title_prefers_task_then_label_then_provisional`.
- 3.6.2 - Footer corners: `definition (project#ref) · Focused` left, `gclient 0:0:1:2` right. test: `crates/gclient/src/ui/panes/tests.rs::bottom_metadata_renders_with_and_without_pane_gaps`.
- 3.6.3 - NEW regression: attention frames use the warning role with `⍾`, exited frames the destructive role with `◌`, junctions follow focus. test: `crates/gclient/tests/parity/panes.rs::frame_colour_follows_focus_attention_and_exit`.
- 3.6.4 - No `shell` literal remains. behavior: "\"shell\"" absent in `crates/gclient/src/app/pane.rs`.
- 3.6.5 - The `UNNAMED_PANE` rung is gone from the model ladder. symbol: `build_agents`.
- 3.6.6 - Golden exposing the unnamed rung. file: `crates/gclient/tests/fixtures/screens/unnamed_pane.txt`.
- 3.6.7 - The re-export is gone. behavior: "UNNAMED_PANE" absent in `crates/gclient/src/app/mod.rs`.
- 3.6.8 - The label fallbacks use the terminal id rung. behavior: "UNNAMED_PANE" absent in `crates/gclient/src/ui/chrome/labels.rs`. symbol: `terminal_label`.
- 3.6.9 - The pane-access methods live in the new extension file and `crates/gclient/src/app/mod.rs` is under 850 lines. file: `crates/gclient/src/app/workspace_panes.rs`. behavior: "fn pane_for_terminal" absent in `crates/gclient/src/app/mod.rs`.

### 3.7 Status bar segments [category: code] (depends: 3.2b, 3.3, 3.6)
`kind: deliverable`

Targets:
- `crates/gclient/src/ui/status_segments.rs`
- `crates/gclient/src/ui/mod.rs`
- `crates/gclient/src/ui/status.rs::render_status_line`
- `crates/gclient/src/ui/status.rs::focused_overflow`
- `crates/gclient/src/ui/status.rs::status_line_shows_only_global_prefix_mode_and_health`
- `crates/gclient/src/ui/status.rs::borderless_focused_pane_keeps_its_metadata_and_title_here` (renamed `focused_pane_footer_overflows_to_the_status_row` by 3.6)
- `crates/gclient/src/ui/settings.rs::ClientPrefs`
- `crates/gclient/src/ui/settings.rs::ClientPrefs::default`
- `crates/gclient/src/prefs.rs::PrefsFile`
- `crates/gclient/src/prefs.rs::PrefsFile::from`
- `crates/gclient/src/prefs.rs::ClientPrefs::from`
- `crates/gclient/src/ui/chrome.rs::ViewState`
- `crates/gclient/src/ui/chrome.rs::ViewState::apply_hits`
- `crates/gclient/src/ui/chrome_render.rs::ChromeHits`
- `crates/gclient/src/ui/chrome_render.rs::render_workspace_with`
- `crates/gclient/src/ui/hit.rs::Hit`
- `crates/gclient/src/ui/hit.rs::hit_test`
- `crates/gclient/src/app/live_loop/mouse/pointer.rs::*` — scope-reason: `Hit::StatusCount` opens the overlay
- `crates/gclient/src/ui/hit/tests.rs::hit_test_covers_split_live_layout`
- `crates/gclient/tests/parity/status.rs::*` — scope-reason: segment rendering tests are added
- `crates/gclient/tests/parity/chrome.rs::*` — scope-reason: the status-count click test is added
- `crates/gclient/tests/screens.rs::*` — scope-reason: a `status_segments` state is added
- `crates/gclient/tests/fixtures/screens/status_segments.txt`

Granularity: the segment registry, its prefs table, the status renderer, the hit plumbing for the count and the click handler are one feature; nine production files because the hit path crosses `chrome.rs`, `chrome_render.rs`, `hit.rs` and `pointer.rs` by design.

`chrome.rs` is at the size guard (899 lines; 3.2a split `SidebarState` out of it). The segment registry and its text builders move into the new `crates/gclient/src/ui/status_segments.rs`; `chrome.rs` gains one `ViewState` field.

Create `crates/gclient/src/ui/status_segments.rs`: `pub enum StatusSegment { Focus, Model, Context, Tokens }` with `StatusSegment::parse(&str) -> Option<Self>` (names `focus`, `model`, `context`, `tokens`; anything else is `None` and the list entry is skipped) and `pub fn segment_text(seg, ws, chrome) -> Option<String>`: `focus` = the focused pane's footer left text (`PaneFooter.left` via `focused_overflow`), `model` = the focused agent's `model_slug()` (3.1, untruncated), `context` = `{context_percent}%`, `tokens` = `tokens_used` with thousands separators. The two fixed parts of Decision 17 are not segments and cannot be listed: the fixed left slot writes `Daemon unreachable` while `!ws.daemon_ready()` (3.9 extends this same slot with the connecting stage and the retry countdown from `chrome.connection`; 3.7 writes only the two words) and then `{n} need you` for the `attention_entry_ids` not visible in the active tab, each emitted once by the fixed slot and never by a list (council round 2, cr-2). Register `pub mod status_segments;` in `crates/gclient/src/ui/mod.rs`. Cost is not a segment.

`ClientPrefs` gains `status_left: Vec<String>` and `status_right: Vec<String>` (defaults `["focus", "model"]` and `["context", "tokens"]`); `PrefsFile` gains a `status: StatusPrefs { left, right }` table (`[status]` in `prefs.toml`), carried by both `From` impls. `render_status_line` returns `StatusHits { control_indicator: Option<Rect>, count: Option<Rect> }`: the fixed left slot first (`Daemon unreachable` while it holds, then the attention count, whose rect is `count`), then the `status_left` segments joined by ` │ `, then the `status_right` segments right-aligned before the fixed `prefix <label>` hint and the mode word (3.2b). `ChromeHits` gains `status_count`, `ViewState` gains `status_count_hit_area`, `Hit` gains `StatusCount` (tested before `Status` in `hit_test`), and `pointer.rs` opens the sidebar overlay (3.3) on it. Toast and mode strings unchanged. Add `status_segments` to `STATES` (a focused agent with model, context and tokens, one blocked agent in another tab) and its golden.

Research context:
- `render_status_line` after 3.2b: `surface0`, left content then right-aligned prefix and mode; returns the control-indicator rect that `render_workspace_with` stores as `hits.control_indicator`.
- `prefs.rs`: `PrefsFile { ui: UiPrefs, keymap: KeymapPrefs }`, `PREFS_FILE = "client/prefs.toml"`, `load_prefs`/`save_prefs`; sub-tables emit after plain values.
- `WorkspaceView::attention_entry_ids`, `daemon_ready`; `Chrome::active_tab`, `Tab::slot_for`.
- Assumption: the roster payload will carry `context_percent`/`tokens_used` (fields added optional in 3.1; segments render `—` while absent).
- Fixed parts (Decisions 8 and 17, council round 2): `Daemon unreachable` and the hidden-attention count are written by the fixed left slot only; 3.9 owns the connecting-stage text and the retry countdown through `chrome.connection`, so this section carries no countdown accessor.
- Planned checks: `cargo nextest run -p gobby-client -E 'test(status) | test(hit) | test(chrome) | test(screens)'`.

**Acceptance:**

- 3.7.1 - Segment names parse and render from `[status]` lists. file: `crates/gclient/src/ui/status_segments.rs`.
- 3.7.2 - `prefs.toml` round-trips `status.left`/`status.right`. symbol: `PrefsFile`.
- 3.7.3 - NEW regression: the left slot shows `Daemon unreachable` then the hidden-attention count; the right end keeps prefix and mode. test: `crates/gclient/tests/parity/status.rs::status_bar_orders_fixed_slots_and_configured_segments`.
- 3.7.4 - NEW regression: clicking the count opens the sidebar overlay. test: `crates/gclient/tests/parity/chrome.rs::status_count_click_opens_the_sidebar_overlay`.
- 3.7.5 - Hit precedence for the count cell. test: `crates/gclient/src/ui/hit/tests.rs::hit_test_covers_split_live_layout`.
- 3.7.6 - Golden. file: `crates/gclient/tests/fixtures/screens/status_segments.txt`.

### 3.8 Marks module and the ink, glint and dim theme roles [category: code] (depends: 0.1, 3.7)
`kind: deliverable`

Targets:
- `crates/gclient/src/ui/marks.rs`
- `crates/gclient/src/ui/mod.rs`
- `crates/gclient/src/theme.rs::*` — scope-reason: Neutrals, Theme::new, Palette, Palette::entries and Palette::from_theme all change together for the three new roles
- `crates/gclient/tests/theme.rs::*` — scope-reason: NEUTRAL_NAMES, the 16-entry assertion, the ramp test and the entries-to-fields map all change for the three new roles
- `crates/gclient/tests/marks.rs`

`crates/gclient/src/ui/marks.rs` is a new native module (its first line is the native `upstream: none` header the carve guard requires) that embeds the four 0.1 assets with `include_str!("../../assets/marks/goblin-33x16.grid")`, `include_str!("../../assets/marks/goblin-29x14.grid")`, `include_str!("../../assets/marks/wordmark-braille-54x8.txt")` and `include_str!("../../assets/marks/wordmark-shadow-49x9.grid")`, parses each once behind a `std::sync::OnceLock` into a `Mark { kind: MarkKind, cols: u16, rows: u16, cells: Vec<Cell> }` (row-major), and exposes `pub fn goblin_large() -> &'static Mark`, `goblin_small()`, `wordmark()`, `wordmark_shadow()`, `pub fn parse(text: &str) -> Result<Mark, MarkError>` and `pub fn render_mark(frame: &mut Frame, origin: (u16, u16), mark: &Mark, palette: &MarkPalette)`. A malformed asset is a `MarkError` naming the line and the reason; the embedded assets are parsed in a test so a bad 0.1 output fails `cargo nextest` rather than the first launch. `render_mark` clips to `frame.area()` and never writes outside it.

The on-disk grid format is defined here and 0.1 produces it. Every mark is one UTF-8, LF-terminated text file. Line 1 is the header `# halfblock <cols>x<rows>` or `# braille <cols>x<rows>` (kind, then width and height in cells, e.g. `# halfblock 33x16`, `# braille 54x8`). Any further line starting with `#` is a comment and is skipped. Exactly `rows` data lines follow. A `halfblock` data line is exactly `2*cols` characters: for cell `c` (0-based) the characters at `2c` and `2c+1` are the upper and lower subcell role letters. The role alphabet is `a` accent, `o` overlay1, `i` ink, `g` glint, `d` dim and `.` transparent; any other character is a parse error. A `braille` data line is exactly `cols` characters, each in U+2800..=U+28FF; U+2800 (blank braille) means transparent. Trailing whitespace is an error, so widths are checked by character count rather than by trimming.

Rendering rule for a half-block cell with upper role `U` and lower role `L`: both transparent leaves the frame cell untouched; `U` painted and `L` transparent writes the glyph `▀` (U+2580) with `fg = colour(U)` and leaves the cell's existing `bg` alone; `U` transparent and `L` painted writes `▄` (U+2584) with `fg = colour(L)` and leaves `bg` alone; both painted writes `▀` with `fg = colour(U)` and `bg = colour(L)` (when `U == L` this is a solid block of that colour, which is why no `█` case exists). A ratatui cell has exactly one `bg`, so a cell with two painted halves always needs a glyph whose fg/bg split matches the upper/lower split, and `▀` is that glyph; the mark's own colours never land as two backgrounds. A braille cell writes the glyph with `fg = palette.braille` and leaves `bg` alone. `MarkPalette { accent, overlay1, ink, glint, dim, braille: Color }` maps letters to colours; `MarkPalette::normal(p: &Palette)` binds `a→p.accent`, `o→p.overlay1`, `i→p.ink`, `g→p.glint`, `d→p.dim`, `braille→p.accent`; `MarkPalette::shadow(p)` is `normal` with `braille→p.dim`, the palette binding for the wordmark's drop-shadow data (no renderer in this plan calls it or `wordmark_shadow()`; Decision 19 ships the shadow as data, unused); `MarkPalette::dimmed(p, kind: ThemeKind)` binds `a→fill`, `o→lines`, `i→lines`, `d→lines`, `g→transparent` where `fill` is `p.overlay0` in dark and `p.surface1` in light, and `lines` is `p.panel_bg` in dark and `p.overlay0` in light. The dimmed goblin is therefore the same `.grid` file drawn through `MarkPalette::dimmed`; there is no dimmed asset. `MarkPalette` carries `Option<Color>` per letter so `dimmed` can make `g` transparent without a second alphabet.

`crates/gclient/src/theme.rs` gains the three roles. `dim` is a ninth neutral token: `Neutrals` gains `pub dim: Token`, built as `Token::neutral("dim", 0.36)` in dark and `Token::neutral("dim", 0.81)` in light (OKLCH on the neutral ramp, i.e. hue `BRAND_HUE`, chroma `NEUTRAL_CHROMA`; these land near `#3f403c` and `#bfc1bc`), and `Neutrals::all` returns `[&Token; 9]` with `dim` inserted between `surface1` and `overlay0` so the ramp stays strictly ordered in both kinds (dark 0.32 < 0.36 < 0.55, light 0.885 > 0.81 > 0.62). `ink` and `glint` are kind-dependent aliases, not new tokens: in dark `ink = panel_bg` and `glint = text`; in light `ink = text` and `glint = panel_bg`. `Palette` gains `pub ink`, `pub glint` and `pub dim: Color`; `Palette::entries` returns `[(&'static str, Token); 19]` with `("ink", …)`, `("glint", …)` and `("dim", n.dim)` appended after `peach`, resolving the aliases by `theme.kind`; `Palette::from_theme` binds the same three. `Neutrals::surfaces` is unchanged (dim is not a surface). `crates/gclient/src/ui/mod.rs` declares `pub mod marks;`.

`crates/gclient/tests/theme.rs` is updated to the new contract: `NEUTRAL_NAMES` gains `"ink"`, `"glint"` and `"dim"`; the `entries.len()` assertion becomes 19; the ramp test keeps reading `Neutrals::all` and now proves nine ordered tokens; `palette_entries_bind_the_same_tokens_the_render_paints_with` gains the three match arms; a new test proves `dim` sits between `surface1` and `overlay0` in both kinds and that `ink`/`glint` swap between kinds. `crates/gclient/tests/marks.rs` is a new test file covering the parser (declared size, alphabet, header errors), the half-block rule (every one of the four cell cases against a `TestBackend`), braille transparency, clipping at the frame edge, the dimmed palette, and that all four embedded assets parse to their declared sizes.

Consumers unchanged:
- `crates/gclient/src/ui/widgets.rs` — no-edit-reason: `panel_contrast_fg` and every other renderer read existing `Palette` fields, which keep their names and values.

Research context:
- Observed: `crates/gclient/src/theme.rs` (378 lines) holds `Token::neutral(name, lightness)` on `BRAND_HUE`/`NEUTRAL_CHROMA`, `Neutrals` (8 tokens; dark lightness 0.16/0.20/0.26/0.32/0.55/0.62/0.76/0.92, light 0.985/0.955/0.925/0.885/0.62/0.52/0.40/0.20), `Neutrals::all` (`[&Token; 8]`), `Neutrals::surfaces` (`[&Token; 4]`), `Palette` (16 `Color` fields, `mauve == subtext0`, `peach == yellow`, `teal == blue`), `Palette::entries` (`[(&str, Token); 16]`) and `Palette::from_theme`. No `include_str!` exists in the crate today and there is no `crates/gclient/assets/` directory; 0.1 creates it.
- Tests observed: `crates/gclient/tests/theme.rs::tokens_match_design_contract_and_survive_monochrome` asserts `entries.len() == 16`, that every neutral name in `NEUTRAL_NAMES` (which already lists the alias `mauve`) has `BRAND_HUE` and chroma in 0.005..=0.008, that no role is pure black or white, and that the `Neutrals::all` ramp is monotonic; `palette_entries_bind_the_same_tokens_the_render_paints_with` panics on any entry name without a match arm. `crates/gclient/tests/screens.rs::roles`/`role_name` normalise golden colours through `Palette::entries`. `crates/gclient/tests/ui_carve_guard.rs::carve_matches_upstream_map_and_renders_data` requires every file under `crates/gclient/src/ui/` other than a `mod.rs` or `tests.rs` to start with the native `upstream: none` header or the herdr header, and rejects the forbidden tokens listed in its `FORBIDDEN` table.
- Approach: aliases for `ink`/`glint` (like `mauve`) keep the ramp tests meaningful; `dim` as a real token keeps a single OKLCH source of truth. Rejected: a separate dimmed asset (doubles 0.1's output for a palette mapping); painting half-blocks as two backgrounds (a ratatui cell has one `bg`); using `█`/space for solid cells (the golden diff would then show two glyphs for one colour case).
- Verify before editing: `gcode grep -F "Palette {" crates/gclient` should show `Palette::from_theme` as the only struct-literal constructor (not run in this draft; a second constructor would need the three fields too).
- Shared `crates/gclient/src/ui/mod.rs` order (council round 2, cr-1 and cr-4): 3.2b declares `menu_bar`, 3.7 `status_segments`, this section `marks`, 3.9 `splash`; the dependency chain 3.2b → 3.7 → 3.8 → 3.9 orders those four one-line declarations and is the only reason 3.8 depends on 3.7.
- Planned checks: `cargo nextest run -p gobby-client --test theme --test marks --test screens --test ui_carve_guard`, `cargo clippy -p gobby-client`, `cargo fmt -p gobby-client -- --check`.

**Acceptance:**

- 3.8.1 - The on-disk format above parses through `parse`, rejects a bad header, a bad letter, a short or long line and a non-braille glyph, and all four embedded assets parse to their declared sizes. test: `crates/gclient/tests/marks.rs::halfblock_grid_parses_to_its_declared_size` (new, plus sibling error-case tests in the same file).
- 3.8.2 - `render_mark` implements the four-case half-block rule exactly and leaves transparent cells untouched. test: `crates/gclient/tests/marks.rs::halfblock_cells_paint_upper_and_lower_halves_by_the_stated_rule` (new).
- 3.8.3 - Braille glyphs paint in the palette's braille role, U+2800 stays transparent, and the shadow palette paints braille in `dim`. test: `crates/gclient/tests/marks.rs::braille_glyphs_paint_in_the_given_role_and_blank_cells_stay_transparent` (new).
- 3.8.4 - `MarkPalette::dimmed` drops glints and uses overlay0/panel_bg fill/lines in dark and surface1/overlay0 in light. test: `crates/gclient/tests/marks.rs::dimmed_palette_drops_glints_and_uses_theme_fill_and_lines` (new).
- 3.8.5 - `Palette::entries` has 19 roles, `ink`/`glint` swap by kind, `dim` sits between `surface1` and `overlay0`, and the monochrome/contrast contract still holds. test: `crates/gclient/tests/theme.rs::palette_entries_bind_the_same_tokens_the_render_paints_with` (existing, extended) and `crates/gclient/tests/theme.rs::dim_sits_between_surface1_and_overlay0_and_ink_glint_swap_by_kind` (new).
- 3.8.6 - The module carries the native header and adds no forbidden token. file: `crates/gclient/src/ui/marks.rs`.

### 3.9 Splash first frame and Daemon unreachable [category: code] (depends: 3.2a, 3.7, 3.8, 3.10)
`kind: deliverable`

Targets:
- `crates/gclient/src/ui/splash.rs`
- `crates/gclient/src/ui/mod.rs`
- `crates/gclient/src/ui/chrome_render.rs::render_content_column`
- `crates/gclient/src/ui/status.rs::render_status_line`
- `crates/gclient/src/ui/status.rs::status_line_shows_only_global_prefix_mode_and_health`
- `crates/gclient/src/app/live_loop.rs::*` — scope-reason: run_live_loop's tick mirrors the retry deadline, begin_reconnect raises the toast, and the reconnect helpers with their inline test move out
- `crates/gclient/src/app/live_loop/reconnect.rs`
- `crates/gclient/src/app/attach.rs::Pane::retire_attachment`
- `crates/gclient/tests/screens.rs::*` — scope-reason: STATES gains three entries and their scripted builders
- `crates/gclient/tests/fixtures/screens/splash_connecting.txt`
- `crates/gclient/tests/fixtures/screens/splash_attach_running.txt`
- `crates/gclient/tests/fixtures/screens/daemon_unreachable.txt`
- `crates/gclient/tests/splash.rs`

The first frame is the full frame and is never blank. While `chrome.connection.stages` (the record 3.10 puts on `Chrome`) still has an unfinished stage, `crates/gclient/src/ui/chrome_render.rs::render_content_column` calls `splash::render_splash(frame, terminal_area, chrome)` instead of the empty state, and paints skeleton tab blocks over `chrome.view.tab_bar_rect`: from the row's left edge, blocks 11 cells wide in `surface1` separated by one cell of the row's background, as many as fit, none labelled. The 3.2a menu bar row and the 3.7 status bar row render exactly as they do in any frame, so menus work on the first frame.

`crates/gclient/src/ui/splash.rs` is a new native module (native `upstream: none` header), declared `pub mod splash;` in `crates/gclient/src/ui/mod.rs` after `marks` (council round 2, cr-4). It lays out one group: the goblin 33×16 from `marks::goblin_large()` on the left, a gap of 4 cells, and a right column 54 cells wide and 16 rows tall: the braille wordmark 54×8 (`marks::wordmark()` in `MarkPalette::normal`, alone; `wordmark_shadow()` is never drawn, Decision 19 ships the shadow as unused data, council round 2, cr-3), then the version line `gclient <ver> · daemon <ver> · <machine>` in `text` (the daemon version from `chrome.connection.daemon_version`, `—` until stage 1 has answered; the machine from `chrome.connection.machine`), a blank row, then four stage rows in the fixed order `daemon health`, `workspace attach`, `roster`, `first frame`: glyph `●` in `accent` when done, `◐` in `accent` when running, `○` in `overlay0` when pending; the timing right-aligned in the column in `overlay1`: a done stage shows `<took> s`, the running stage shows `<elapsed> s and waiting`, a pending stage shows nothing; below, one row `Connecting to <url> · menus work now · the status bar names the stage that stalls` in `overlay0`. The group (33 + 4 + 54 = 91 cells wide, 16 rows) is centred in the pane area; when the area is narrower than 91 or shorter than 16 the goblin is dropped first, then the wordmark, so the stage list is the last thing to go, and nothing draws outside the area. Timings are formatted from `chrome.connection.now` (stamped by the loop each tick) so the same state renders identically twice; the format is one decimal (`9.8 s`).

The status bar's left slot (3.7) carries `◐ connecting · <stage> · <elapsed>` while a stage is running, where `<stage>` is the running stage's label and `<elapsed>` is that stage's elapsed time in the same one-decimal format; `crates/gclient/src/ui/status.rs::render_status_line` produces that segment from `chrome.connection`. When the daemon drops later, the same slot leads with `Daemon unreachable · retry in <n> s` (whole seconds, from `chrome.connection.retry_at` against `chrome.connection.now`), or `Daemon unreachable · retrying` while an attempt is in flight; the old `" │ Daemon unreachable."` suffix goes away, and the inline test `status_line_shows_only_global_prefix_mode_and_health` asserts the new leading form.

In `crates/gclient/src/app/live_loop.rs`, the reconnect helpers (`begin_reconnect`, `handle_reconnect_outcome`, `handle_live_event`, `recv_daemon_event`, `await_reconnect_job`, `wait_for_reconnect`, `settle_sidebar_banner` and the inline test `sidebar_banner_raises_one_toast_per_outage`) move out of live_loop.rs into `crates/gclient/src/app/live_loop/reconnect.rs`; this is the split that keeps live_loop.rs (915 production lines today) under the ceiling after 3.10's changes. `begin_reconnect` gains the `chrome` parameter and, on the transition from ready to not ready (not on every retry), notifies `Toast::error(format!("Daemon unreachable at {url}: {error}"))` with the URL from `chrome.connection.url`; every retry-scheduling path writes `supervisor.next_attempt_at()` into `chrome.connection.retry_at`, the tick branch of `run_live_loop` re-mirrors it and stamps `chrome.connection.now`, and `handle_reconnect_outcome` clears `retry_at` on `handshake_complete` so the segment and the toast condition clear themselves when the daemon answers. Nothing else in the drop path changes: the roster, attention and sidebar models keep their last contents until the reconnect's reconcile replaces them, so every count the sidebar or tab bar shows stays what was last known rather than dropping to zero.

Panes freeze with their last frame: `crates/gclient/src/app/attach.rs::Pane::retire_attachment` resets only the attachment, lease and control fields and keeps the pane's last `FrameData`, cursor and scroll metrics, so `views::grid::render` keeps drawing the last frame while the daemon is away (the 3.6 pane note still marks the pane's state on its border).

`crates/gclient/tests/screens.rs::STATES` gains `splash_connecting` (stage 1 running at 9.8 s, no version yet), `splash_attach_running` (stage 1 done in 0.3 s, stage 2 running at 2.1 s, version known) and `daemon_unreachable` (a `split_live` workspace with `retry_at` 3 s ahead of `now`), built through `StartupStages::for_test`; their goldens are the three new fixture files. `crates/gclient/tests/splash.rs` is a new test file for the layout rules (centring, the drop order at narrow and short areas, clipping), the status segment text, the once-per-outage toast that names the URL, and the frozen frame.

Consumers unchanged:
- `crates/gclient/src/app/live_attach.rs` — no-edit-reason: `retire_pane_attachment` calls `Pane::retire_attachment` with the same signature; only which fields the callee resets changes.
- `crates/gclient/src/ui/tab_surface.rs` — no-edit-reason: it is reached only once a tab exists, which is after the splash state ends.

Research context:
- Observed: `crates/gclient/src/ui/chrome_render.rs::render_content_column` returns early with `panes::render_empty` when `chrome.tabs().tabs.is_empty()`; `render_workspace_with` calls `status::render_status_line(frame, status_rect, ws, chrome)`. `crates/gclient/src/ui/status.rs::render_status_line` appends `" │ Daemon unreachable."` in red when `!ws.daemon_ready()`; its inline test `status_line_shows_only_global_prefix_mode_and_health` asserts that text. `crates/gclient/src/app/live_loop.rs::begin_reconnect(workspace, supervisor, daemon, error)` is called from `run_live_loop` and `handle_live_event`; `settle_sidebar_banner` raises one toast per sidebar outage; `ReconnectSupervisor::next_attempt_at` (in `crates/gclient/src/app/run_loop.rs`) is the retry deadline. `crates/gclient/src/app/attach.rs::Pane::retire_attachment` is the reset called from `crates/gclient/src/app/live_attach.rs::retire_pane_attachment`. `crates/gclient/tests/screens.rs` renders `STATES` at 120×40 through `render_workspace`, requires two identical captures (`deterministic_capture`), and names colours by `Palette::entries`.
- Approach: the splash is a state of the normal frame drawn by the normal renderer, so the menu bar, tab row and status bar need no special path; the record lives on `Chrome` (3.10) because both the pane-area renderer and the status renderer already receive `chrome` and neither receives the loop's supervisor. Rejected: a dedicated loading screen drawn before the loop (would need a second render path and would go blank on the hand-over); `Instant::now()` in the renderer (breaks `deterministic_capture`).
- Verification planned: `GOBBY_UPDATE_SCREENS=1 cargo nextest run -p gobby-client --test screens` to write the three goldens, then `cargo nextest run -p gobby-client --test screens --test splash --test ui_carve_guard`, `cargo nextest run -p gobby-client -E 'test(status_line_shows_only_global_prefix_mode_and_health) | test(sidebar_banner_raises_one_toast_per_outage)'`, `cargo clippy -p gobby-client`.

**Acceptance:**

- 3.9.1 - The first drawn frame while stage 1 runs shows the menu bar, skeleton tab blocks, the centred goblin, wordmark, version line, four stage rows with `◐ daemon health … 9.8 s and waiting`, and the connecting line. file: `crates/gclient/tests/fixtures/screens/splash_connecting.txt`.
- 3.9.2 - The group is centred and degrades goblin-first, wordmark-second, never clipping. test: `crates/gclient/tests/splash.rs::group_is_centred_in_the_pane_area_and_never_clips` (new).
- 3.9.3 - The status bar's left slot names the running stage and its elapsed time, and leads with `Daemon unreachable · retry in <n> s` after a drop. test: `crates/gclient/tests/splash.rs::status_segment_names_the_running_stage_and_the_retry_countdown` (new) and `crates/gclient/src/ui/status.rs::status_line_shows_only_global_prefix_mode_and_health` (existing, updated).
- 3.9.4 - A drop raises exactly one error toast naming the URL per outage, and the segment clears when the handshake completes. test: `crates/gclient/tests/splash.rs::a_dropped_daemon_toasts_the_url_once_and_clears_on_reconnect` (new).
- 3.9.5 - A pane whose attachment retired keeps its last frame. test: `crates/gclient/tests/splash.rs::panes_keep_their_last_frame_while_the_daemon_is_away` (new). symbol: `Pane::retire_attachment`.
- 3.9.6 - The reconnect helpers live in the new file and live_loop.rs stays under 1,000 lines. file: `crates/gclient/src/app/live_loop/reconnect.rs`.

### 3.10 Startup latency: draw first, stage the connect, log the timings [category: code] (depends: 3.2a, 3.7)
`kind: deliverable`

Targets:
- `crates/gclient/src/app/startup_stages.rs`
- `crates/gclient/src/app/mod.rs::*` — scope-reason: one `pub mod startup_stages;` declaration beside the existing module list
- `crates/gclient/src/views/mod.rs::run_ready`
- `crates/gclient/src/daemon/live.rs::*` — scope-reason: connect_or_wait is rebuilt on a new public unconnected constructor and the connection-lifecycle methods move out
- `crates/gclient/src/daemon/live_connect.rs`
- `crates/gclient/src/app/live.rs::*` — scope-reason: reconcile_subscribe_first and fetch_roster change and the sidebar refetch machinery moves out
- `crates/gclient/src/app/live_sidebar.rs`
- `crates/gclient/src/app/live_loop.rs::*` — scope-reason: run_live_loop's blocking prelude is replaced by the staged driver and reconcile_ready moves out
- `crates/gclient/src/app/live_loop/startup.rs`
- `crates/gclient/src/ui/chrome.rs::Chrome`
- `crates/gclient/src/ui/chrome.rs::Chrome::new`
- `crates/gclient/src/ui/chrome.rs::Chrome::project_workspace`
- `crates/gclient/src/ui/chrome.rs::project_node`
- `crates/gclient/src/ui/chrome/project_workspace.rs`
- `crates/gclient/tests/startup_stages.rs`
- `crates/gclient/tests/startup_latency.rs`
- `crates/gclient/tests/mock_daemon/mod.rs::*` — scope-reason: the mock gains a hold-and-release knob for the workspace_attach reply beside its existing Notify gates

Granularity: this section touches ten production files because the launch path is spread across `views`, `daemon`, `app` and `ui` by construction, and four of those files (`crates/gclient/src/daemon/live.rs` at 985 production lines, `crates/gclient/src/app/live.rs` at 987, `crates/gclient/src/app/live_loop.rs` at 915, `crates/gclient/src/ui/chrome.rs` at 899) each require their own split file; the change is one contiguous reordering verified by one new integration test file, and cutting it in two would leave an intermediate state that either still blocks the first draw or draws a frame with no record to show.

Where the time goes today: `crates/gclient/src/views/mod.rs::run_ready` awaits `LiveDaemon::connect_or_wait` (the WebSocket open) and `attach_live_workspace` before it even creates the `Terminal`, then `run_live_loop` awaits `reconcile_ready` — subscribe, workspace attach, roster pages, attention roster, `fetch_sidebar_rows` (which awaits the whole per-project REST fan-out: projects, then source status, worktrees, sessions and agent runs for every checked-out project) and the event drain — then `restore_focused` (a shell spawn when the workspace has no tab) and `focus_live_pane`, and only then draws. Nothing is on screen until every one of those answers, and the sidebar fan-out is the long pole because it scales with the number of projects and runs git status for each.

The fix reorders the launch so the first frame precedes every network wait, and stages the rest. `crates/gclient/src/app/startup_stages.rs` is a new module with `pub enum StartupStage { DaemonHealth, WorkspaceAttach, Roster, FirstFrame }` (`label()` returns `daemon health`, `workspace attach`, `roster`, `first frame`), `pub enum StageState { Pending, Running { since: Instant }, Done { took: Duration } }`, and `pub struct StartupStages { states: [StageState; 4], pub now: Instant, started: Instant }` with `begin(now)`, `mark_running(stage, now)`, `mark_done(stage, now)`, `running() -> Option<StartupStage>`, `finished() -> bool`, `elapsed(stage) -> Option<Duration>`, `for_test(states, now)` and `summary() -> String` (`health=0.31s attach=2.10s roster=0.42s first_frame=0.08s total=2.91s`). `pub struct ConnectionView { pub url: String, pub machine: String, pub daemon_version: Option<String>, pub launch_project: Option<String>, pub stages: Option<StartupStages>, pub retry_at: Option<Instant>, pub now: Instant }` is the record `crates/gclient/src/ui/chrome.rs` holds as `pub connection: ConnectionView` on `Chrome`, initialised by `Chrome::new` with an empty URL, `stages: None`, `now: Instant::now()`. `crates/gclient/src/app/mod.rs` declares the module. As the split that keeps chrome.rs under the ceiling after the field lands, `Chrome::project_workspace` and the free function `project_node` always move from chrome.rs to `crates/gclient/src/ui/chrome/project_workspace.rs` in this section (an `impl Chrome` extension file like the existing `alerts` and `labels` submodules); 3.2a moves only `SidebarState` and leaves both of these in place, so the chrome.rs edit here is that move plus the `connection` field on top of it.

`crates/gclient/src/daemon/live.rs` gains `pub fn unconnected(base_url, token) -> Result<Self, DaemonError>` (the current private `build`, made the public non-blocking constructor; `state.ready` starts false with `last_error: None`), and `connect_or_wait` becomes `unconnected` followed by the first `open_connection`, so its callers and tests see no change. The connection-lifecycle methods `open_connection`, `subscribe_events`, `reconnect` and `close` move out of live.rs into `crates/gclient/src/daemon/live_connect.rs` as `pub(super)` inherent methods that the `Daemon` trait impl in live.rs delegates to; that move is the split that keeps live.rs under the ceiling. `live_connect.rs` also gains `pub async fn config_version(&self) -> Result<Option<String>, DaemonError>` reading the daemon config route (GET api/config) through the existing `RestClient::json` path and returning its `version` field, which stage 1 records into `chrome.connection.daemon_version`.

`crates/gclient/src/views/mod.rs::run_ready` becomes: `LiveDaemon::unconnected(url, token)?`, `Workspace::live(daemon)` plus the existing setters, `Chrome::new` plus prefs/keymap/nested/host notice as today, `Terminal::new`, then `chrome.connection = ConnectionView { url, machine: local machine id, launch_project: ready.project, stages: Some(StartupStages::begin(Instant::now())), .. }`, then `run_live_loop`. The pre-loop `attach_live_workspace` and the `initial_project` selection leave `run_ready`; the attach stage performs them.

`crates/gclient/src/app/live_loop.rs::run_live_loop` no longer awaits `reconcile_ready` before its first draw. It draws once immediately (the splash of 3.9, or, before 3.9 lands, the empty state with the status bar), then enters the staged driver in the new `crates/gclient/src/app/live_loop/startup.rs`: `run_startup_stages(workspace, terminal, chrome, input, supervisor, control_tx) -> Result<StartupOutcome, FrameError>` runs one boxed stage future at a time over a cloned `LiveDaemon` inside a `tokio::select!` with the input receiver, the exit/resize/suspend signals and the render tick, so menus, dialogs, quit and resize work while a stage waits, and a render tick re-stamps `chrome.connection.now` and redraws. Stage 1 `daemon health`: `daemon.reconnect(Generation(0))` (the existing WebSocket open and subscribe) followed by `config_version`; on `Unavailable`/`GoingAway`/`Timeout` the stage stays running, the supervisor is armed exactly as the daemon-down launch does today, and the loop's reconnect path completes the stages after the first successful handshake; any other error ends the launch as it does today. Stage 2 `workspace attach`: `daemon.attach_workspace(node, workspace)`, then `apply_workspace_snapshot`, then `select_project(initial_project(chrome.connection.launch_project, focused_project_id))`. Stage 3 `roster`: `collect_roster_pages(daemon, project) -> (Vec<TerminalRow>, Snapshot)` (the paging loop split out of `fetch_roster`, which keeps its signature and now calls the collector then `install_live_rows`) and `daemon.attention_roster()`, then `install_live_rows` and `install_attention`. Stage 4 `first frame`: `attach_ready_panes`, `sync_live_chrome`, `restore_focused`, `focus_live_pane`, the event drain, and then the stage completes when the focused pane's `frames_rendered() > 0`, or immediately when there is no pane to attach. The sidebar fan-out leaves the critical path entirely: the attach stage sets `pending_sidebar` (projects, every checked-out project's rows, sessions, roster) exactly as `fetch_sidebar_rows` does, but does not await `flush_sidebar_refetches`; the loop's existing tick-driven `start_sidebar_refetch`/`sidebar_job` branch performs it in the background after the first frame. `reconcile_ready` moves to startup.rs and is used only by the reconnect path, where it likewise stops awaiting the fan-out (`reconcile_subscribe_first` in `crates/gclient/src/app/live.rs` sets `pending_sidebar` instead of calling `fetch_sidebar_rows`), so a reconnect never freezes the window either. Each stage boundary logs `tracing::info!(stage, took_ms)` and the driver logs `summary()` once when stage 4 completes; `stages` stays `Some` afterwards so the Daemon dialog (3.13) can show the timings, and `finished()` is what the renderers test.

As the split that keeps live.rs under the ceiling, the sidebar refetch machinery (`SidebarFetch`, `SidebarFetchFuture`, `SidebarRequest` and `SidebarRequest::run`, `optional`, `Workspace::start_sidebar_refetch`, `apply_sidebar_fetch`, `flush_sidebar_refetches`, `request_git_refresh_if_due`, `request_roster_refresh_if_due`) moves from live.rs to `crates/gclient/src/app/live_sidebar.rs`, and `live_sidebar.rs` gains `pub fn roster_refreshed_at(&self) -> Instant` for 3.13.

`crates/gclient/tests/mock_daemon/mod.rs` gains a hold for the `workspace_attach` reply: `hold_attach()` stores an `Arc<Notify>` on `MockState` and returns it as the release handle, and the async send loop that serves `websocket_reply` awaits that Notify only when the request type is `workspace_attach`, before writing the reply frame (the same Notify pattern the mock's `websocket_read_gate` uses). The latency test holds, asserts the first frame, then calls `notify_one()`. The two existing knobs are the wrong gates: `suppress_ws("workspace_attach")` drops the reply entirely, and `pause_websocket_reads` stalls every later read, subscribe and health included. `crates/gclient/tests/startup_stages.rs` covers the record; `crates/gclient/tests/startup_latency.rs` drives `run_live_loop` against the mock with a `TestBackend` and asserts the ordering.

Consumers unchanged:
- `crates/gclient/src/startup.rs` — no-edit-reason: `run` calls `run_ready(ready, &mut guard)` with the same signature and result.
- `crates/gclient/tests/daemon_live.rs` — no-edit-reason: `connect_or_wait_tolerates_a_daemon_that_is_down` keeps passing because `connect_or_wait` keeps its behaviour on top of `unconnected`.
- `crates/gclient/src/app/live_workspace.rs` — no-edit-reason: `attach_live_workspace` is still the attach call; only who awaits it moves.
- `crates/gclient/src/ui/chrome/alerts.rs` — no-edit-reason: the `impl Chrome` extension pattern this section follows; nothing in it changes.

Research context:
- Observed entry points: `crates/gclient/src/startup.rs::run` → `prepare_at` (health probe of the health route GET api/health, prefs, keymap; an unreachable daemon becomes `host_notice`) → `start_session` → `crates/gclient/src/views/mod.rs::run_ready` (85 lines). `crates/gclient/src/daemon/live.rs::LiveDaemon::connect_or_wait` = private `build` + `open_connection`; `build` is non-blocking. `crates/gclient/src/app/live.rs::Workspace::reconcile_subscribe_first` runs subscribe → `attach_live_workspace` → `fetch_roster` → `fetch_attention` → `fetch_sidebar_rows` → `drain_receiver`; `fetch_sidebar_rows` sets `pending_sidebar` then awaits `flush_sidebar_refetches`; `SidebarRequest::run` is the fan-out (`attention_roster`, `projects`, per project `source_status` + `worktrees`, then `sessions` + `agent_runs`). `crates/gclient/src/app/live_loop.rs::run_live_loop` awaits `reconcile_ready` then `restore_focused`/`focus_live_pane` before the first `render_live_workspace`; `handle_reconnect_outcome` awaits `reconcile_ready` inline; the tick branch already runs `start_sidebar_refetch` beside the loop. `Workspace::live` (`crates/gclient/src/app/live.rs`) initialises `daemon_ready: false`, `pending_sidebar: PendingSidebar::default()`. `ReconnectSupervisor` lives in `crates/gclient/src/app/run_loop.rs` (`request`, `start_due_attempt` boxing `daemon.reconnect(observed)`, `complete_attempt`, `handshake_complete`). The daemon version is not in the health route; GET api/config returns `"version": get_version()` (`src/gobby/servers/routes/admin/_config.py`).
- `crates/gclient/src/app/live_loop/projects.rs` is not edited by this section: its three `fetch_sidebar_rows().await` calls are user actions after registering or opening a project, where awaiting the refetch is right, and `fetch_sidebar_rows` keeps its signature and behaviour. It is not listed under Consumers unchanged because 3.12 and 3.13 edit `project_dialog_key` in that file (council round 1, cr-2).
- Mock daemon knobs (checked, `crates/gclient/tests/mock_daemon/mod.rs`, 1055 lines, a test file outside the ceiling): `websocket_reply` (line 895) returns the `workspace_attach` body at once and the serve loop (lines 687-694) sends it immediately; `suppress_ws` (line 403) makes the reply `None`; `pause_websocket_reads` (line 445) installs `websocket_read_gate: Option<Arc<Notify>>`, consumed through the `__mock_pause_reads` event (line 712).
- Precedent: `git log --grep 22668 --oneline` → `741761aa98` (linearize warm roster cache hits), `96c96eca43` (assert roster phase timings), `be5f77c40a` (bound attention roster latency): the roster fetch already has bounded, timed phases; this section extends the same discipline to the whole launch.
- Ownership decision: 3.10 owns the timing record and the `Chrome` field; 3.9 renders it. 3.10 is standalone: before 3.9 lands, the first frame is the ordinary frame with the empty pane area, and the status bar shows its existing content.
- Rejected: keeping `connect_or_wait` and drawing a placeholder frame with no workspace (the renderers need a `WorkspaceView`); storing the record on `Workspace` (would need a `WorkspaceView` method and touch two more files at the ceiling); running the stages inline with the select (blocks input during attach, which is the bug being fixed).
- Checks already run: `wc -l` — `crates/gclient/src/app/live_loop.rs` 937 (915 before `#[cfg(test)]`), `crates/gclient/src/app/live.rs` 989 (987), `crates/gclient/src/daemon/live.rs` 985, `crates/gclient/src/ui/chrome.rs` 899, `crates/gclient/src/views/mod.rs` 85. Planned: `cargo nextest run -p gobby-client --test startup_stages --test startup_latency --test client_loop --test daemon_live --test startup --test source_size`, `cargo clippy -p gobby-client`, and a manual launch with `RUST_LOG=gobby_client=info` reading the four stage lines and the summary in `~/.gobby/logs/`.

**Acceptance:**

- 3.10.1 - A frame is drawn before the daemon answers the workspace attach, and menus open during that wait. test: `crates/gclient/tests/startup_latency.rs::the_first_frame_is_drawn_before_the_daemon_answers` (new).
- 3.10.2 - The sidebar REST fan-out starts only after the first-frame stage completes, on the loop's background job. test: `crates/gclient/tests/startup_latency.rs::the_sidebar_fan_out_runs_after_the_first_frame` (new).
- 3.10.3 - Stages advance in order, `elapsed`/`summary` are computed from the stored `now`, and `for_test` builds any state. test: `crates/gclient/tests/startup_stages.rs::stages_advance_in_order_and_report_took_and_waiting` (new).
- 3.10.4 - Each stage logs `took_ms` once and the summary logs once per launch. test: `crates/gclient/tests/startup_latency.rs::stage_timings_are_logged_once_per_launch` (new).
- 3.10.5 - A daemon that is down at launch still opens the window, waits, and restores on return; a project-less start still opens one shell. test: `crates/gclient/tests/client_loop.rs::launch_with_the_daemon_down_waits_and_restores_when_it_returns` (existing) and `crates/gclient/tests/client_loop.rs::first_run_opens_one_shell_and_never_auto_opens` (existing).
- 3.10.6 - The four split files exist and no source file reaches 1,000 lines. file: `crates/gclient/src/daemon/live_connect.rs`. file: `crates/gclient/src/app/live_sidebar.rs`. file: `crates/gclient/src/app/live_loop/startup.rs`. file: `crates/gclient/src/ui/chrome/project_workspace.rs`. test: `crates/gclient/tests/source_size.rs::no_src_file_at_or_above_1000_lines` (existing).

### 3.11 Menus: definitions, the Agent menu, and every item a real action [category: code] (depends: 3.2a, 3.2b, 3.3)
`kind: deliverable`

Targets:
- `crates/gclient/src/app/live_loop/menu_bar.rs`
- `crates/gclient/src/app/live_loop/menu.rs::*` — scope-reason: `MenuAction` gains the later sections' variants, `build_menu` dispatches `MenuBar` to the moved builder, and the item builders move out (the inline tests were already moved by 3.2b)
- `crates/gclient/src/app/live_loop/menu/items.rs`
- `crates/gclient/src/app/live_loop/menu/tests.rs`
- `crates/gclient/src/ui/keymap/names.rs::BINDINGS`
- `crates/gclient/src/app/live_loop/actions.rs::*` — scope-reason: apply_live_menu_action and focus_menu_target move out and the module declaration changes
- `crates/gclient/src/app/live_loop/menu_dispatch.rs`
- `crates/gclient/tests/menu_bar.rs`
- `crates/gclient/tests/client_loop.rs::context_menu_dispatches_items_and_closes_outside`
- `crates/gclient/tests/fixtures/screens/help_dialog.txt`

The menu bar row (3.2b) draws titles and opens a menu; this section owns what the menus contain and what every item does. `crates/gclient/src/app/live_loop/menu_bar.rs` is a new module holding `pub fn menu_bar_items<W: WorkspaceView>(ws: &W, chrome: &Chrome, menu: MenuBarMenu) -> Vec<MenuItem>`, moved out of `menu.rs` where 3.2b created it provisionally. `MenuBarMenu` and `MenuBarMenu::ALL` (`Gobby`, `File`, `Edit`, `View`, `Window`, `Agent`, `Help`, seven titles in that order, per the signed chrome canvas) are defined once, in the `ui::menu_bar` module 3.2b creates; this section imports `MenuBarMenu` from there and does not restate the enum, its order or its titles. The 3.2b row reads `MenuBarMenu::ALL` for its titles and opens a menu through the existing `open_menu(ws, chrome, ContextMenuKind::MenuBar(menu), anchor)`, so item navigation, activation and closing reuse `menu_key`, `menu_mouse` and `render_context_menu` unchanged. The contents: Gobby — `settings` (`Act(Settings)`), `reload config` (`Act(ReloadConfig)`), `quit` (`Act(Quit)`). File — `new terminal` (`Act(NewTerminal)`), `new tab` (`Act(NewTab)`), `new workspace…` (`Act(NewProject)`; renamed from `new project`), `rename tab`, `close tab`, `destroy orphaned terminals…` (`DestroyOrphans`), `detach` (`Act(Detach)`). Edit — `copy mode` (`Act(CopyMode)`), `rename pane` (`Act(RenamePane)`), `rename tab` (`Act(RenameTab)`), `rename terminal` (`Act(RenameTerminal)`), `clear pane name` (`ClearPaneName(pane)`, `enabled_if` the focused pane has a label), and `send right-clicks to pane` / `use gclient menu` (`TogglePassthrough(pane)`, the existing toggle wording); the pane-scoped items resolve `pane` through `chrome.focused_pane()` and are `enabled_if` a pane is focused, and every one of these actions exists today in `pane_items`. Agent — exactly nine, in this order: `respond` (`Act(Respond)`), `mark seen` (`MarkSeen(entry)` where `entry` is the focused pane's attention entry from `blocked_entry`), `take control` (`Act(TakeControl)`), `release control` (`Act(ReleaseControl)`), `take back` (`Act(TakeBack)`), `detach` (`Act(Detach)`), `open alert target` (`Act(OpenNotificationTarget)`), `next attention` (`Act(NextAttention)`), `previous attention` (`Act(PreviousAttention)`); `respond` and `mark seen` are built through `enabled_if` and draw disabled when the focused pane has no attention entry, which is the menu's existing convention for an item whose target is absent, not a drawn control that does nothing. View — `this project` or `all projects` and `grouped` or `priority` (the `[view]` band's two choices, from `sessions_view_items`), `working projects` or `all projects` (`Act(ToggleProjectsFilter)`, the `[working]` band action), `show sidebar` (`Act(ToggleSidebar)`) and `pin sidebar` (`MenuAction::PinSidebar`, the variant 3.3 adds, dispatched from `apply_local_menu_action` to `toggle_sidebar_pin`). Window — `split right`, `split down`, `zoom`/`unzoom`, `close pane`, `resize mode` (all `Act`; `copy mode` lives on Edit); the `arrange` and `new grid…` entries are appended by 3.12. Help — `keys` (`Act(Help)`), `alerts…` (`ShowAlerts`, moved here from the `[Menu]` global menu); `daemon` and `about gobby` are appended by 3.13, with `about gobby` last. Take Control and Release Control stay on the pane context menu through the existing `control_item`.

`crates/gclient/src/app/live_loop/menu.rs` changes: `ContextMenuKind::MenuBar(MenuBarMenu)` already exists (3.2b); `build_menu` now dispatches it to `menu_bar::menu_bar_items`; `MenuAction` gains the variants every later P3 section draws, defined here so each later section adds only its entries and its handler: `Arrange(ArrangeLayout)` with `pub enum ArrangeLayout { EvenHorizontal, EvenVertical, MainHorizontal, MainVertical, Tiled }` (data only here; 3.12 gives it behaviour), `OpenNewGrid`, `NewGrid { rows: u8, cols: u8 }`, `ShowDaemon`, `ShowAbout`; none of these is drawn by this section, and until 3.12 and 3.13 add their arms they fall through at the three existing sites: `apply_local_menu_action` (`menu.rs`, `_ => return false`), `apply_scripted_menu_action` (the scripted run loop, which sends every non-`Act` variant to `apply_local_menu_action` and returns `Ok(false)`), and `apply_live_menu_action` (`_` → `apply_daemon_menu_action`, else `apply_local_menu_action`). As the split that keeps menu.rs under the ceiling once the new variants land, the item builders (`pane_items`, `tab_items`, `project_items`, `worktree_items`, `agent_items`, `orphaned_terminal`, `sessions_view_items`, `choice`, `global_items`, `item`, `enabled_if`, `control_item`, `blocked_entry`) move from menu.rs to `crates/gclient/src/app/live_loop/menu/items.rs`; the inline test module was already moved to `crates/gclient/src/app/live_loop/menu/tests.rs` by 3.2b, so this section moves only the builders. In `items.rs`, `global_items` loses `alerts…` (now under Help) and renames `new project` to `new workspace…`; its tests in `tests.rs` update the two index assertions (the target is listed as a bare path because the file does not exist until 3.2b lands, and the scope of this section's edit to it is exactly those two assertions in `menus_list_items_per_target_and_state`).

`crates/gclient/src/ui/keymap/names.rs::BINDINGS` changes the `new_project` description from `Add a project` to `Add a workspace`; the binding name and chord are unchanged, so overrides, the parity groups and the keymap tests are untouched, and the keys overlay golden `crates/gclient/tests/fixtures/screens/help_dialog.txt` is regenerated for the new text.

As the split that keeps actions.rs (979 production lines) under the ceiling, `apply_live_menu_action` and `focus_menu_target` move from actions.rs to `crates/gclient/src/app/live_loop/menu_dispatch.rs`; `focus_menu_target` gains an explicit `ContextMenuKind::MenuBar(_) => {}` arm beside its existing `_ => {}` (a bar menu acts on the focused pane; the explicit arm documents it). `crates/gclient/tests/menu_bar.rs` is a new test file that, for every menu in `MenuBarMenu::ALL`, builds the items against a scripted workspace in two states (a pane with an attention entry focused; no pane) and activates each enabled item: non-`Act` items are activated by calling `apply_live_menu_action` directly (the function this section moves to `menu_dispatch.rs`, and the only dispatcher that reaches `MarkSeen`, `ShowAlerts`, `DestroyOrphans`, `PinSidebar` and the later `Arrange`, `OpenNewGrid`, `ShowDaemon`, `ShowAbout` handlers), while `Act` items may go through the scripted loop; the scripted runner is left as it is, and an `Ok(false)` fall-through from `apply_scripted_menu_action` counts as a failure rather than a dispatch. The test asserts that every activation either changes chrome/workspace state, opens a dialog or mode, or sends a daemon request — the executable form of "nothing drawn does nothing" — plus the Agent order and the File/Help wording. `crates/gclient/tests/client_loop.rs::context_menu_dispatches_items_and_closes_outside` is re-run and updated only if it indexes the global menu's items.

Consumers unchanged:
- `crates/gclient/src/ui/context_menu.rs` — no-edit-reason: `render_context_menu` and `popup_rect` render any `ContextMenuState` regardless of kind.
- `crates/gclient/src/app/live_loop/mouse/mod.rs` — no-edit-reason: `menu_mouse` hits `item_rects` regardless of kind.
- `crates/gclient/src/app/run_loop.rs` — no-edit-reason: `apply_scripted_menu_action` (lines 267-294, checked) routes `Act` to the scripted focus and action path and every other variant to `apply_local_menu_action` then `Ok(false)`; the new test does not rely on it for non-`Act` items, so it is untouched.
- `crates/gclient/tests/keymap.rs` — no-edit-reason: it asserts names and chords, not descriptions.

Research context:
- Observed: no menu bar exists today (`gcode grep -F MenuBar crates/gclient` and `menu_bar` are empty); the only menu code is the right-click machinery in `crates/gclient/src/app/live_loop/menu.rs` — `ContextMenuKind {Pane, Tab, Project, Worktree, Agent, SessionsView, Global}`, `MenuAction` (24 variants including `Act(Action)`, `TakeControl`, `ReleaseControl`, `Respond`, `MarkSeen`, `ShowAlerts`, `DestroyOrphans`), `build_menu`, `open_menu`, `close_menu`, `activate_menu`, `apply_local_menu_action`, `global_items` (`new terminal`, `new tab`, `new project`, `settings`, `keybinding help`, `alerts…`, `reload config`, `toggle sidebar`, `destroy orphaned terminals…`, `detach`, `quit`) — with `render_context_menu` in `crates/gclient/src/ui/context_menu.rs`, key handling in `crates/gclient/src/app/live_loop/modal_input.rs::menu_key`, and dispatch in `crates/gclient/src/app/live_loop/actions.rs::apply_live_menu_action` (`Act` → `focus_menu_target` then `handle_live_action`; `MarkSeen` → `mark_agent_seen`; `ShowAlerts` → `open_alerts_dialog`). The nine Agent actions exist as `Action::{Respond, TakeControl, ReleaseControl, TakeBack, Detach, OpenNotificationTarget, NextAttention, PreviousAttention}` in `crates/gclient/src/ui/keymap/names.rs` and `MenuAction::MarkSeen(entry_id)`; `blocked_entry(ws, pane)` resolves a pane's attention entry. The band actions exist as `Action::{ToggleSessionsScope, ToggleAgentSort, ToggleProjectsFilter, ToggleSidebar}`. `crates/gclient/UPSTREAM.md` rejects herdr's `src/ui/menus.rs`, and `crates/gclient/tests/ui_carve_guard.rs` asserts that path does not exist, which is why the new module lives under `crates/gclient/src/app/live_loop/`. `crates/gclient/tests/source_size.rs::no_src_file_at_or_above_1000_lines` counts total lines; menu.rs was 970 lines before 3.2b moved its 520 inline test lines out, so after 3.2b it is about 480 lines and the builder move here keeps it there once the new variants and dispatch land.
- Dispatch sites (checked with `gcode symbol-at`): `apply_local_menu_action` (`menu.rs` lines 177-198) handles `SwapWithFocused`, `ClearPaneName`, `TogglePassthrough`, `ToggleGroup` and returns `false` from its `_` arm; `apply_scripted_menu_action` (`run_loop.rs` lines 267-294) sends every non-`Act` variant to `apply_local_menu_action` and returns `Ok(false)`, so a scripted activation of `MarkSeen`, `ShowAlerts`, `DestroyOrphans` or any later variant never reaches `apply_live_menu_action`; `apply_live_menu_action` (`actions.rs` lines 288-360) handles `Act` through `focus_menu_target` and `handle_live_action`, has arms for `Respond`, `MarkSeen`, `ShowAlerts`, `DestroyOrphans`, `TakeControl`, `ReleaseControl` and the rest, and its `_` arm tries `apply_daemon_menu_action` then `apply_local_menu_action`; `focus_menu_target` (`actions.rs` lines 365-389) already has a `_ => {}` arm, so `ContextMenuKind::MenuBar` compiles without the explicit arm, which is added for clarity only.
- Sidebar pinning (3.3, checked): there is no `pin_sidebar` keymap action; `pin sidebar` is `MenuAction::PinSidebar`, dispatched from `apply_local_menu_action` to `toggle_sidebar_pin` in `actions/sidebar.rs`, and `show sidebar` stays `Act(ToggleSidebar)`.
- Decision: the Help › Daemon / About and Window › Arrange / New Grid entries are added by 3.13 and 3.12 rather than here (the brief placed the entries in 3.11): an entry whose handler does not exist yet would be a drawn control that does nothing at this section's close gate, which is exactly what this section forbids. The `MenuAction` variants are declared here so those sections touch neither menu.rs nor actions.rs.
- Existing tests to keep green: `crates/gclient/src/app/live_loop/menu/tests.rs::menus_list_items_per_target_and_state` (moved by 3.2b; asserts `items[2]` is `NewProject` and `items[5]` is `ShowAlerts` on the global menu; the second assertion changes), `::global_menu_offers_destroy_orphaned_terminals`, `::row_menus_list_items_per_target_and_state`, `crates/gclient/tests/client_loop.rs::row_menus_dispatch_project_and_agent_actions`, `crates/gclient/tests/parity/dialogs.rs::context_menu_renders_anchored_and_clamped`.
- Verification planned: `cargo nextest run -p gobby-client --test menu_bar --test client_loop --test keymap --test source_size -E 'test(menu)'`, `GOBBY_UPDATE_SCREENS=1 cargo nextest run -p gobby-client --test screens` (help_dialog golden), `cargo clippy -p gobby-client`.

**Acceptance:**

- 3.11.1 - Every enabled item of every bar menu, activated, changes state, opens a mode or dialog, or sends a request. test: `crates/gclient/tests/menu_bar.rs::every_menu_bar_item_dispatches_to_a_handler` (new).
- 3.11.2 - The Agent menu lists exactly the nine actions in the stated order, with `respond`/`mark seen` disabled when no attention entry applies. test: `crates/gclient/tests/menu_bar.rs::agent_menu_lists_the_nine_actions_in_order` (new).
- 3.11.3 - File says `new workspace…`, Help holds `alerts…`, the global menu no longer does, and the keymap description reads `Add a workspace`. test: `crates/gclient/tests/menu_bar.rs::file_menu_says_new_workspace_and_help_holds_the_alert_log` (new). file: `crates/gclient/src/ui/keymap/names.rs`.
- 3.11.4 - View carries the `[view]` and `[working]` band choices plus `show sidebar` as `Act(ToggleSidebar)` and `pin sidebar` as `MenuAction::PinSidebar`. behavior: "pin sidebar" in `crates/gclient/src/app/live_loop/menu_bar.rs`.
- 3.11.5 - The item builders live in `menu/items.rs` (the tests were moved to `menu/tests.rs` by 3.2b and only their two global-menu index assertions change here), dispatch lives in menu_dispatch.rs, and the existing global-menu tests pass with their updated indices. test: `crates/gclient/src/app/live_loop/menu/tests.rs::menus_list_items_per_target_and_state` (existing, updated). file: `crates/gclient/src/app/live_loop/menu_dispatch.rs`.
- 3.11.6 - The keys overlay golden shows `Add a workspace`. file: `crates/gclient/tests/fixtures/screens/help_dialog.txt`.
- 3.11.7 - The menu-bar dispatch test calls apply_live_menu_action for every enabled non-Act item, and an Ok(false) fall-through from apply_scripted_menu_action is a failure. test: `crates/gclient/tests/menu_bar.rs::every_menu_bar_item_dispatches_to_a_handler`.

### 3.12 Window › Arrange and New Grid [category: code] (depends: 3.11)
`kind: deliverable`

Targets:
- `crates/gclient/src/app/live_loop/arrange.rs`
- `crates/gclient/src/app/live_loop/menu_bar.rs`
- `crates/gclient/src/app/live_loop/menu/items.rs`
- `crates/gclient/src/app/live_loop/menu_dispatch.rs`
- `crates/gclient/src/ui/dialogs.rs::*` — scope-reason: Dialog gains NewGrid, render_dialog gains its arm, and the grid preview renderer is added beside the other small dialogs
- `crates/gclient/src/app/live_loop/projects.rs::project_dialog_key`
- `crates/gclient/tests/arrange.rs`

The daemon has no layout op: its workspace ops are `pane.split`, `pane.swap`, `pane.move{pane, tab, beside, axis}` and `pane.resize{pane, ratio}`, and storage places a moved pane as the second child of a fresh 0.5 split on `beside` (or on the whole tab when `beside` is absent) after collapsing its source split. Arrange is therefore a client-side computation over those two ops. `crates/gclient/src/app/live_loop/arrange.rs` is a new module with `pub fn plan_arrange(layout: ArrangeLayout, tab_id: &str, panes: &[String]) -> Vec<WorkspaceOp>` (pure; `panes` are daemon pane ids in the tab's visual order, i.e. `tab.layout.panes()` mapped through `daemon_pane_id`) and `pub(super) async fn apply_arrange(workspace, chrome, layout) -> Result<(), FrameError>`. The plan builds a right-leaning chain with moves, then sets ratios: even-horizontal moves `P[i]` beside `P[i-1]` on the horizontal axis for `i` in `1..N`, then resizes `P[i]` to 1 ÷ (N − i) for `i` in `0..N-1` (each resize sets the split directly holding that pane, and `P[N-1]` shares the last split with `P[N-2]`); even-vertical is the same on the vertical axis; main-vertical moves `P[1]` beside `P[0]` horizontally, then `P[i]` beside `P[i-1]` vertically for `i` in `2..N`, and resizes `P[0]` to `0.5` and `P[i]` to 1 ÷ (N − i) for `i` in `1..N-1`; main-horizontal swaps the two axes; tiled takes `rows = ceil(sqrt(N))`, cols = ceil(N ÷ rows), splits the panes into `rows` consecutive groups, moves each group's head beside the previous head vertically, then each group member beside its predecessor horizontally, and resizes heads to 1 ÷ (rows − r) and members to 1 ÷ (len − j). `apply_arrange` sends the ops one at a time through `send_workspace_op` (order matters, and each `pane.moved` event the daemon emits reaches the chrome through the existing workspace-event path), then re-focuses the pane that was focused; a tab with one pane sends nothing and notifies `Toast::info("Nothing to arrange: one pane")`; a local (non-daemon) tab sends nothing and notifies a warning, matching `apply_daemon_menu_action`'s convention.

New Grid: `arrange.rs` also has `pub(super) async fn create_grid(workspace, chrome, rows: u8, cols: u8) -> Result<(), FrameError>`: it spawns the first shell with `spawn_live_shell(Placement::Tab)`, then `cols-1` shells with `Placement::SplitRight` from the head of the row, then for each column `rows-1` shells with `Placement::SplitDown`, and finally applies the tiled plan's ratio ops for a `rows×cols` grid so the cells are even; rows and columns are 1 to 4 each. `crates/gclient/src/ui/dialogs.rs` gains `Dialog::NewGrid { rows: u8, cols: u8 }`, a `render_dialog` arm and `render_new_grid`: a 44×14 modal (`render_modal_shell`, header ` new grid`), a preview area that draws the `rows×cols` cells with the same box-drawing borders the panes use, a caption `<rows> × <cols> shells`, and a footer in the keybind-help style `←→ columns · ↑↓ rows · enter create · esc cancel` (hints, not buttons, so there is no dead mouse target). Dialog keys already live in one match: `route_modal_key` (in the modal-input module, which this section does not edit) matches `Mode` and delegates `Mode::ProjectDialog` to `project_dialog_key(chrome, key)` in `crates/gclient/src/app/live_loop/projects.rs`, which matches `Dialog` exhaustively (a new `Dialog` variant fails to compile until it has an arm there). So `project_dialog_key` gains a `Dialog::NewGrid { rows, cols }` arm beside `Dialog::Alerts`: arrows adjust within 1..=4, Enter yields `ModalOutcome::Menu { kind: ContextMenuKind::Global, action: MenuAction::NewGrid { rows, cols } }` so the confirmation flows through the menu dispatcher, Esc closes. `route_modal_key` is not edited. The dialog is opened from the `OpenNewGrid` arm in `menu_dispatch.rs` by setting `chrome.dialog` and `Mode::ProjectDialog` exactly as `open_alerts_dialog` does.

Entries: `crates/gclient/src/app/live_loop/menu_bar.rs` appends to Window five `arrange: even horizontal` … `arrange: tiled` items (`Arrange(layout)`) and `new grid…` (`OpenNewGrid`); `crates/gclient/src/app/live_loop/menu/items.rs::pane_items` appends the same six after `zoom` so both are on the pane context menu. `crates/gclient/src/app/live_loop/menu_dispatch.rs::apply_live_menu_action` gains the arms `Arrange(layout) => apply_arrange`, `OpenNewGrid => open the dialog at 2×2`, `NewGrid { rows, cols } => create_grid`. `crates/gclient/tests/arrange.rs` covers the plans (every layout for N = 1, 2, 3, 5: the exact op sequence, including that tiled uses `ceil(sqrt N)` rows), the dialog's clamping and confirmation outcome, and `create_grid` against a scripted daemon (the spawn and placement count).

Consumers unchanged:
- `crates/gclient/src/app/live_loop/workspace_actions.rs` — no-edit-reason: `send_workspace_op`, `daemon_pane_id`, `active_daemon_tab` and `resize_daemon_split` are called as they are.
- `crates/gclient/src/daemon/workspace.rs` — no-edit-reason: `WorkspaceOp::PaneMove` and `PaneResize` already carry every field the plan needs.

Research context:
- Dialog key routing (checked): `route_modal_key` (`crates/gclient/src/app/live_loop/modal_input.rs` lines 93-111) matches `Mode` and calls `project_dialog_key(chrome, key)` for `Mode::ProjectDialog`; `open_alerts_dialog` (same file, lines 121-124) sets `chrome.dialog = Some(Dialog::Alerts { scroll: 0 })` and `chrome.mode = Mode::ProjectDialog`; `project_dialog_key` (`projects.rs` lines 528-651; the file is 816 lines) matches `Dialog` exhaustively with arms for `NewProject`, `Alerts` (Up/k, Down/j, Enter, Esc and q close) and the confirm-close, rename and respond dialogs.
- Consumers of `project_dialog_key` (checked): `route_modal_key` (same arguments, untouched here) and `crates/gclient/tests/client_loop.rs::project_dialog_keys_produce_daemon_requests` (drives `NewProject` dialog keys only and never opens the grid dialog). Both files belong to other sections' Target inventories (3.2a/3.3 and 3.11), so they are recorded here rather than under Consumers unchanged.
- Observed: `src/gobby/terminals/workspace_ops.py::WorkspaceOps` has `pane_split`, `pane_swap`, `pane_move`, `pane_resize` and no layout op; `src/gobby/storage/workspaces.py::_place` builds `split(axis, 0.5, [beside, moved])`, `WorkspaceManager.move_pane` removes the pane from its old spot first (source split collapses), and `set_ratio` sets the split directly holding the pane (a single-pane tab is an error). Client side: `WorkspaceOp::{PaneMove{pane, tab, beside, axis}, PaneResize{pane, ratio}}` (the daemon workspace module), `LayoutNode::Split{axis, ratio, children}`; `send_workspace_op` in the workspace actions module (toasts a `workspace_error`), `daemon_pane_id(chrome, slot)`, `active_daemon_tab(chrome)`, `resize_daemon_split`; `Tab { id, layout: TileLayout, slots }` on `Chrome` with `TileLayout::panes()` giving the visual order; `spawn_live_shell(workspace, chrome, placement, cwd, worktree_id)` in the live-loop actions module and `Placement {Tab, SplitRight, SplitDown}` in the mouse module; `open_alerts_dialog` sets `Dialog::Alerts` with `Mode::ProjectDialog`; `crates/gclient/src/ui/dialogs.rs` (614 lines) holds `Dialog`, `render_dialog` and the small dialogs; the modal widgets are in the `widgets` module.
- Rejected: a daemon-side layout op (would be a Python change outside this crate and is not needed: the two existing ops express every tmux layout); submenus in the context menu widget (none exist; flat items keep the widget unchanged); dialog buttons (would need mouse arms in the mouse module; hints keep the dialog keyboard-complete without a dead target).
- Verification planned: `cargo nextest run -p gobby-client --test arrange --test client_loop -E 'test(arrange) | test(grid) | test(context_menu)'`, `cargo clippy -p gobby-client`; a manual check against a running daemon with a three-pane tab for each of the five layouts.

**Acceptance:**

- 3.12.1 - Each of the five layouts plans the stated move-then-ratio sequence for three panes. test: `crates/gclient/tests/arrange.rs::each_layout_plans_moves_then_ratios_for_three_panes` (new).
- 3.12.2 - Tiled uses `ceil(sqrt N)` rows and even ratios for five panes. test: `crates/gclient/tests/arrange.rs::tiled_uses_ceil_sqrt_rows_and_even_ratios` (new).
- 3.12.3 - A one-pane or local tab sends nothing and says so. test: `crates/gclient/tests/arrange.rs::a_single_pane_tab_plans_nothing_and_says_so` (new).
- 3.12.4 - The New Grid dialog clamps rows and columns to 1..=4, previews them, and confirms as a `NewGrid` menu action. test: `crates/gclient/tests/arrange.rs::new_grid_dialog_clamps_to_one_through_four_and_confirms_dims` (new).
- 3.12.5 - `create_grid` spawns rows×cols shells with the stated placements and evens the cells. test: `crates/gclient/tests/arrange.rs::create_grid_spawns_rows_times_cols_shells_and_evens_them` (new).
- 3.12.6 - Both the Window menu and the pane context menu carry the five arrange items and `new grid…`. behavior: "new grid…" in `crates/gclient/src/app/live_loop/menu/items.rs`.

### 3.13 Help › Daemon and Help › About Gobby dialogs [category: code] (depends: 3.8, 3.9, 3.10, 3.11, 3.12)
`kind: deliverable`

Targets:
- `crates/gclient/src/ui/dialogs/info.rs`
- `crates/gclient/src/ui/dialogs.rs::*` — scope-reason: Dialog gains Daemon and About, render_dialog gains their arms, and the info submodule is declared
- `crates/gclient/src/app/live_loop/menu_bar.rs`
- `crates/gclient/src/app/live_loop/menu_dispatch.rs`
- `crates/gclient/src/app/live_sidebar.rs`
- `crates/gclient/src/app/live_loop/projects.rs::project_dialog_key`
- `crates/gclient/tests/info_dialogs.rs`

Both dialogs are snapshots taken when they open, so their renderers need only `chrome`. `crates/gclient/src/ui/dialogs.rs` gains `Dialog::Daemon { url: String, gclient_version: String, daemon_version: Option<String>, health: String, last_roster_refresh: Option<Duration>, stages: Option<String> }` and `Dialog::About { url: String, gclient_version: String, daemon_version: Option<String>, machine: String }`, with `render_dialog` arms into the new `crates/gclient/src/ui/dialogs/info.rs` (native `upstream: none` header). `render_daemon` is a small modal (56×11): header ` daemon` with `esc close` on the right in accent (the keybind-help button style), then rows with `subtext0` labels and `text` values: `url`, `gclient`, `daemon` (`—` when unknown), `health` (`ok`, or `unreachable: <error>` when `daemon_ready` was false at open, or `connecting · <stage>` while startup runs), `roster` (`refreshed <n> s ago`), and `startup` (the `StartupStages::summary` line when the launch finished). `render_about` is exactly 72×15: header ` about gobby` with `esc close` on the right in accent; the goblin 29×14 from `marks::goblin_small()` in `MarkPalette::normal` at the left of the body; then, in the column to its right, `Gobby` bold in `text`, the tagline `fleet management for AI coding agents` in `subtext0`, a blank row, `gclient <ver>`, `daemon <ver>`, `url <url>` and `machine <name>` rows with `subtext0` labels and `text` values, a blank row, and `gobby.ai` in `overlay0`; when the area cannot hold 72×15 the goblin is dropped and the text column is drawn alone.

`crates/gclient/src/app/live_loop/menu_dispatch.rs` gains `open_daemon_dialog(workspace, chrome)` and `open_about_dialog(workspace, chrome)`, which build the snapshots from `chrome.connection` (url, machine, `daemon_version`, `stages`), `env!("CARGO_PKG_VERSION")`, `workspace.daemon_ready()`/`daemon_error()`, and `workspace.roster_refreshed_at()` — the accessor added to `crates/gclient/src/app/live_sidebar.rs` beside the field's writer — and set `Mode::ProjectDialog`; `apply_live_menu_action` gains the `ShowDaemon` and `ShowAbout` arms. `crates/gclient/src/app/live_loop/menu_bar.rs` appends to Help `daemon` (`ShowDaemon`) and, last, `about gobby` (`ShowAbout`). `crates/gclient/src/app/live_loop/projects.rs::project_dialog_key` (the exhaustive `Dialog` match that `route_modal_key` delegates `Mode::ProjectDialog` to, as 3.12 records) gains one arm for `Dialog::Daemon { .. } | Dialog::About { .. }` beside `Dialog::Alerts` that closes on Esc, Enter or `q` (the alerts dialog's keys) and consumes everything else; `route_modal_key` is not edited. `crates/gclient/tests/info_dialogs.rs` renders both dialogs against a `TestBackend` and checks the geometry, every row, the role of every styled span it names, and that Help's last item is `about gobby`.

Research context:
- Dialog key routing (checked, as in 3.12): `project_dialog_key` (`projects.rs` lines 528-651) is the exhaustive `Dialog` match, and 3.12 already adds its `NewGrid` arm there; 3.13 depends on 3.12, so the shared file is ordered. Its consumers, `route_modal_key` in `crates/gclient/src/app/live_loop/modal_input.rs` (same arguments, not edited here) and `crates/gclient/tests/client_loop.rs::project_dialog_keys_produce_daemon_requests` (drives `NewProject` keys only), are edit targets of other sections (3.2a/3.3 and 3.11), so they are recorded here rather than under Consumers unchanged.
- Observed: `crates/gclient/src/ui/dialogs.rs::render_dialog` dispatches on `chrome.dialog` to per-dialog renderers, the alerts dialog (`crates/gclient/src/ui/dialogs/alerts.rs`, 72 wide) is the closest sibling, and the header/button helpers are `render_modal_shell`, `render_modal_header`, `action_button_width`, `render_action_button` in `crates/gclient/src/ui/widgets.rs` (the keys overlay draws `esc close` with them). The client holds the daemon URL as `LiveInner.base_url` (`pub(super)`, no accessor) and the launch URL in `startup::Ready.daemon_url`; 3.10 copies it into `chrome.connection.url`, along with the machine id and the daemon version from the config route. `Workspace::roster_refreshed_at` is a private field stamped by `start_sidebar_refetch` (moved to `crates/gclient/src/app/live_sidebar.rs` by 3.10). Health is `workspace.daemon_ready()` and `daemon_error()`.
- Rejected: a new REST call at open time (would block the click; the snapshot uses what the client already holds); separate files per dialog (both are static info dialogs sharing the label/value row helper).
- Verification planned: `cargo nextest run -p gobby-client --test info_dialogs --test menu_bar`, `cargo clippy -p gobby-client`, `cargo nextest run -p gobby-client --test ui_carve_guard`.

**Acceptance:**

- 3.13.1 - About is 72×15 with the title ` about gobby`, `esc close` in accent at the right, the goblin 29×14 at left, and the stated rows in their stated roles. test: `crates/gclient/tests/info_dialogs.rs::about_dialog_is_72_by_15_with_the_goblin_and_the_stated_rows` (new).
- 3.13.2 - Daemon shows url, both versions, health, the last roster refresh age and the startup summary. test: `crates/gclient/tests/info_dialogs.rs::daemon_dialog_shows_url_versions_health_and_last_roster_refresh` (new).
- 3.13.3 - Help lists `keys`, `alerts…`, `daemon`, `about gobby` in that order, and both entries open their dialogs. test: `crates/gclient/tests/info_dialogs.rs::help_menu_ends_with_about_gobby_and_both_entries_open` (new).
- 3.13.4 - Esc, Enter and `q` close either dialog; other keys are consumed. test: `crates/gclient/tests/info_dialogs.rs::info_dialogs_close_on_esc_enter_and_q` (new).
- 3.13.5 - The renderer carries the native header. file: `crates/gclient/src/ui/dialogs/info.rs`.

### 3.14 Empty tab [category: code] (depends: 3.6, 3.8)
`kind: deliverable`

Targets:
- `crates/gclient/src/ui/panes.rs::render_empty`
- `crates/gclient/src/ui/panes/tests.rs::*` — scope-reason: `empty_state_names_the_next_step_without_exclamation` is extended and a keymap test is added beside it
- `crates/gclient/tests/fixtures/screens/empty_workspace.txt`
- `crates/gclient/tests/fixtures/screens/projects_agents.txt`

`crates/gclient/src/ui/panes.rs::render_empty` replaces today's two lines (whose second line assumes a visible sidebar) with a centred block: the dimmed goblin 29×14 (`marks::goblin_small()` through `MarkPalette::dimmed(p, chrome.theme.kind)`), a blank row, `No pane open.` in `overlay1` bold, and three rows in `overlay0` with the chord in `subtext0`: `ctrl+b w  attach a terminal`, `File › New Terminal  start one`, `ctrl+b b  open the sidebar`; the chord text comes from `chrome.keymap.prefix_label` plus the binding's key so a shifted prefix (nested) or an override shows the real chord (`terminal_picker` and `toggle_sidebar` bindings; a binding with no chord shows `unset`). The block is 29 wide and 19 tall; when the area is shorter than 19 rows the goblin is dropped and the four text rows are centred alone, and when narrower than 29 the rows are truncated with the existing `text` helpers; an area under 2×8 draws only the background as today. No exclamation mark appears anywhere. The goldens `empty_workspace` and `projects_agents` (both show the empty tab area) are regenerated.

Consumers unchanged:
- `crates/gclient/src/ui/tab_surface.rs` — no-edit-reason: `render_tab_surface` keeps calling `render_empty` for a tab with no panes.

Research context:
- Observed: `crates/gclient/src/ui/panes.rs::render_empty` (183–209) paints `panel_bg`, returns early under 2×8, and centres `No pane open.` (overlay1 bold) over `select a terminal in the sidebar to attach` (overlay0); callers are `crates/gclient/src/ui/chrome_render.rs::render_content_column` and `crates/gclient/src/ui/tab_surface.rs::render_tab_surface`; the inline test `crates/gclient/src/ui/panes/tests.rs::empty_state_names_the_next_step_without_exclamation` asserts the heading and the absence of `!` at 60×10 (which is now the goblin-dropped form). Chords: `terminal_picker` is `prefix+w` and `toggle_sidebar` is `prefix+b` in `crates/gclient/src/ui/keymap/names.rs`; `chrome.keymap.binding(name)` returns the live chords and `prefix_label` the prefix. The goldens `crates/gclient/tests/fixtures/screens/empty_workspace.txt` and `projects_agents.txt` contain the old second line.
- Rejected: keeping the sidebar sentence (the sidebar is an overlay after 3.3 and may be hidden); a fixed `ctrl+b` literal (wrong under a nested prefix or an override).
- Verification planned: `cargo nextest run -p gobby-client -E 'test(empty_state)'`, `GOBBY_UPDATE_SCREENS=1 cargo nextest run -p gobby-client --test screens`, then `cargo nextest run -p gobby-client --test screens`.

**Acceptance:**

- 3.14.1 - The empty tab shows the dimmed goblin above `No pane open.` and the three ways out, with live chords. file: `crates/gclient/tests/fixtures/screens/empty_workspace.txt`.
- 3.14.2 - A short area drops the goblin and keeps the four text rows, with no exclamation mark. test: `crates/gclient/src/ui/panes/tests.rs::empty_state_names_the_next_step_without_exclamation` (existing, extended to assert the three rows and the dropped goblin at 60×10).
- 3.14.3 - The chords follow the keymap: a nested prefix and an `unset` binding render as such. test: `crates/gclient/src/ui/panes/tests.rs::empty_state_chords_follow_the_live_keymap` (new, in the existing inline test module).
- 3.14.4 - The projects-and-agents golden shows the same empty tab. file: `crates/gclient/tests/fixtures/screens/projects_agents.txt`.

### 3.15 Keys overlay: the description is never cut [category: code] (depends: 3.11)
`kind: deliverable`

Targets:
- `crates/gclient/src/ui/keybind_help.rs::*` — scope-reason: help_lines is split into a width-aware row builder, render_body measures before it draws, and the column constants change
- `crates/gclient/tests/keybind_help.rs`
- `crates/gclient/tests/fixtures/screens/help_dialog.txt`

Today `help_lines` builds every row as `" {keys:<w} " + description + " ({name})"` and `render_body` clips each row at its column width, so a narrow window cuts the description and often the name with it. `crates/gclient/src/ui/keybind_help.rs` gains `pub fn help_rows(chrome: &Chrome, width: u16) -> Vec<Line<'static>>`: it computes `key_width` as today, `longest = max over rows of (key_width + 2 + description chars + 2 + name chars)` and `longest_without_name = max of (key_width + 2 + description chars)`, and draws the name suffix only when `width >= longest`, so the name column appears for every row or for none; when the width cannot even hold `longest_without_name`, the description wraps onto a continuation row indented under the description column rather than being cut. `help_lines(chrome)` keeps its signature and returns the unbounded form (`help_rows(chrome, u16::MAX)`), which keeps the parity tests and the scroll bound in `modal_input` unchanged. `render_body` chooses the column count first (two columns only when `2 * longest_without_name + gap <= text_width`, replacing the fixed `HELP_COLUMN_MIN_WIDTH` threshold), then builds the rows for that column width, then paginates; the name remains search data throughout (`filter_help_entries` is unchanged). `crates/gclient/tests/keybind_help.rs` is a new test file rendering the overlay at 120×40, 80×30 and 56×24 and asserting that every description is present in full at each width, that names appear at 120 and not at 56, that hiding the name does not change what the slash search finds, and that the row count with names equals the row count without. The golden `help_dialog.txt` is regenerated (at 120×40 both columns have room, so the names still draw; the golden changes only where 3.11 renamed the description).

Consumers unchanged:
- `crates/gclient/tests/ui_carve_guard.rs` — no-edit-reason: it calls `filtered_entries`, which is unchanged.

Research context:
- Observed: `crates/gclient/src/ui/keybind_help.rs` (261 lines; `HELP_MAX_WIDTH` 120, `HELP_MAX_HEIGHT` 40, `HELP_COLUMN_MIN_WIDTH` 50, `HELP_COLUMN_GAP` 2): `help_lines` (55–96) formats the three spans in `mauve` bold / `text` / `overlay1`; `render_keybind_help` (98–187) draws the shell, the `esc close` button, the search row and the footer, then `render_body(frame, stack.content, chrome, help_lines(chrome))`; `render_body` (191–241) picks 1 or 2 columns, computes `column_width`, and renders each chunk as a `Paragraph` in a `Rect` of that width, which is where the cut happens. Callers of `help_lines`: `render_keybind_help`, `crates/gclient/src/app/live_loop/modal_input.rs::keybind_help_key` (row count), `crates/gclient/tests/parity/chrome.rs::rendered_help_text`. `filter_help_entries`/`filtered_entries` match name, description and keys case-insensitively (inline test `filter_matches_name_description_and_keys_case_insensitively`). The golden `crates/gclient/tests/fixtures/screens/help_dialog.txt` row 09 shows `prefix+shift+n   Add a project (new_project)` in two columns at 120×40.
- Rejected: truncating the name first (the description is what the user reads); a fixed threshold for hiding names (the longest row is known, so the decision is exact).
- Verification planned: `cargo nextest run -p gobby-client --test keybind_help --test parity -E 'test(keybind_help)'`, `GOBBY_UPDATE_SCREENS=1 cargo nextest run -p gobby-client --test screens`, `cargo clippy -p gobby-client`.

**Acceptance:**

- 3.15.1 - At 120, 80 and 56 columns every description is present in full; a too-narrow column wraps rather than cuts. test: `crates/gclient/tests/keybind_help.rs::the_description_column_is_never_cut_at_narrow_widths` (new).
- 3.15.2 - Names draw only when the longest row fits, for all rows or none. test: `crates/gclient/tests/keybind_help.rs::the_keymap_name_draws_only_when_the_longest_row_fits` (new).
- 3.15.3 - Hidden names stay searchable. test: `crates/gclient/tests/keybind_help.rs::name_stays_searchable_when_hidden` (new).
- 3.15.4 - `help_lines` keeps its text, so the parity cases and the scroll bound are unchanged. symbol: `help_lines`.
- 3.15.5 - The 120×40 golden is unchanged except for 3.11's description. file: `crates/gclient/tests/fixtures/screens/help_dialog.txt`.

### 3.16 User guide [category: docs] (depends: 3.1, 3.2a, 3.2b, 3.3, 3.4a, 3.4b, 3.5, 3.6, 3.7, 3.8, 3.9, 3.10, 3.11, 3.12, 3.13, 3.14, 3.15)
`kind: deliverable`

Targets:
- `docs/guides/gclient-user-guide.md`
- `docs/guides/README.md`

`docs/guides/gclient-user-guide.md` is brought to the P3 frame. §Layout: the diagram is redrawn with the accent menu bar on row 0 (`Gobby  File  Edit  View  Window  Agent  Help`), the tab bar row, the pane area, and the status bar row, with the sidebar shown as the overlay it now is (3.3) rather than a fixed column; the prose after it describes the menu bar and each menu's items (the seven menus of 3.11, the Window arrange/grid items of 3.12, the Help daemon/about items of 3.13), the empty tab (3.14: the dimmed goblin, `No pane open.` and the three ways out), the launch splash (3.9/3.10: the four stages, the version line, that menus work while a stage waits, and where the timings are logged), and the status bar's left slot (`◐ connecting · <stage> · <elapsed>`, `Daemon unreachable · retry in <n> s`) replacing the sentence about `Daemon unreachable.`; the alerts paragraph points to Help › alerts… instead of the global menu. §What a terminal is called: rung 3 (the literal `shell`) is dropped and the paragraph after it is rewritten to say that a terminal with neither a given name nor a foreground command shows its address alone. The keybinding tables: the `new_project` row reads `Add a workspace`, and the tables gain any chord 3.3 added for pinning; the Client table notes the keys overlay hides binding names when the window is narrow (3.15). §Mouse: the context-menu table's Pane row gains the arrange and new grid items, its `[Menu]` row is replaced by a row per bar menu, and the `[view]` row notes the same choices live under View. §Daemon restarts and reconnects: steps 1 and the launch-down paragraph describe the new status segment, the URL-naming toast, the frozen panes and the self-clearing, and the splash stages. `docs/guides/README.md` keeps its row for the guide; its description is updated only if the guide's section names change.

Research context:
- Observed: `docs/guides/gclient-user-guide.md` — §Layout at line 49 with the diagram at 51–72 (shows `[Menu] [+]`, a fixed sidebar and `prefix ctrl+]` in the status line), status-line prose at 156–166, alerts at 168–174 (`alerts…` on the global menu), §What a terminal is called at 175–204 (rung 3 at 185–186, and the paragraph 188–190 justifying it), §Default keybindings at 281 (Tabs 288, Panes 298, Terminals and control 315, Sidebar and projects 333 with the `new_project` row at 340, Client 353), §Mouse context-menu table at 636–644 (`[Menu]` row at 643), §Daemon restarts and reconnects at 677–715 (step 1 at 686–687, the launch-down paragraph at 705–712). `docs/guides/README.md` row 89 names the guide.
- The exact menu wording, chords and dialog rows are read from the merged 3.11–3.15 code at drafting time, not from this plan, so the guide never drifts from the tree.
- Verification planned: `uv run gobby test-types audit tests/ --baseline .gobby/test-types-baseline.json --fail-on-new` is not affected (docs only); the pre-push hook's lint set passes; a read-through against the running client for each documented item.

**Acceptance:**

- 3.16.1 - The layout diagram shows the menu bar row, tab row, pane area and status row, and the prose names all seven menus and their items. behavior: "Gobby  File  Edit  View  Window  Agent  Help" in `docs/guides/gclient-user-guide.md`.
- 3.16.2 - §What a terminal is called has two rungs and no `shell` literal. behavior: "The command in its foreground" in `docs/guides/gclient-user-guide.md`.
- 3.16.3 - The keybinding table reads `Add a workspace` and the context-menu table has no `[Menu]` row. behavior: "Add a workspace" in `docs/guides/gclient-user-guide.md`.
- 3.16.4 - The reconnect section describes the splash stages, the status segment, the URL toast and the frozen panes. behavior: "retry in" in `docs/guides/gclient-user-guide.md`.
- 3.16.5 - The index row still resolves to the guide. file: `docs/guides/README.md`.

## P4: Tagline
`kind: framing`

The product describes itself in one line in fourteen places, with three different lines live
at once (the README hero, "local-first daemon that unifies", "local-first control plane").
Decision 20 replaces all of them with `fleet management for AI coding agents`. This phase has
no code dependency on the others; it runs alongside P3 and must land before 3.13 draws the
About dialog.

### 4.1 One tagline everywhere [category: docs]
`kind: deliverable`

Targets:
- `src/gobby/cli/__init__.py::*` — scope-reason: only the `cli` group docstring, which is the `gobby --help` text, changes
- `src/gobby/__init__.py::*` — scope-reason: only the module docstring changes
- `src/gobby/install/shared/services/gobby-daemon.service.j2`
- `src/gobby/install/shared/services/gobby-daemon.task.xml.j2`
- `README.md`
- `package.json::description`
- `pyproject.toml`
- `AGENTS.md`
- `ROADMAP.md`
- `docs/architecture/index.md`
- `docs/architecture/architecture.md`
- `ONBOARDING.md`
- `src/gobby/install/shared/workflows/agents/default.yaml::*` — scope-reason: only the persona prompt sentence changes
- `web/index.html`

The line is `fleet management for AI coding agents`: lowercase where it stands alone under
the product name (the About dialog, 3.13), sentence case where it opens a sentence. "AI fleet
management" is the category phrase for longer copy. Per location:

- `src/gobby/cli/__init__.py`: the `cli` group docstring becomes
  `Gobby - fleet management for AI coding agents.`; `src/gobby/__init__.py`: the module
  docstring's first line becomes the same.
- The two service templates: `Description=Gobby Daemon - fleet management for AI coding agents`
  and the matching `<Description>` element.
- `README.md`: the hero `<h3>` becomes `Gobby<br>Fleet management for AI coding agents.`; the
  loop sentence at the top of the body (`That's the loop. Hand Gobby a task, walk away, come
  back to a PR.`) stays; the "What Gobby is" lead becomes `Gobby is fleet management for the AI
  coding CLIs you already use — Claude Code, Codex, Factory Droid, Grok, Qwen CLI, and AGY — and
  gives them what they're missing: …` with the existing feature list; the sentence `It is the
  control plane the agents you already have are missing.` becomes `It is the fleet manager the
  agents you already have are missing.` `package.json` at the repo root copies the README hero
  into its `description` and follows it.
- `pyproject.toml` `description`: keep the bottleneck hook, then `Gobby is fleet management for
  AI coding agents: a local daemon that turns a task into a PR across Claude Code, Codex, Droid,
  Grok, Qwen, and AGY. …` with the rest unchanged.
- `AGENTS.md` first paragraph: `Gobby is fleet management for AI coding agents, a local-first
  daemon that unifies them: session tracking and handoffs across …` (the list unchanged).
  `CLAUDE.md` imports it and needs no edit.
- `ROADMAP.md` lead: `Gobby is fleet management for AI coding agents, a local-first control
  plane: persistent sessions, task graphs, …` (the list unchanged).
- `docs/architecture/index.md` and `docs/architecture/architecture.md`: the lead sentence becomes
  `**Gobby** is fleet management for AI coding agents.`, and the existing mechanism sentence
  (the hook interface, the MCP proxy, the rule engine) follows it unchanged.
- `ONBOARDING.md` checklist row: `gobby — Fleet management for AI coding agents: MCP proxy with
  progressive discovery, task management, sessions, memory, and code search.`
- `src/gobby/install/shared/workflows/agents/default.yaml`: the persona sentence `Gobby is a
  local-first daemon unifying AI coding assistants under one persistent platform.` becomes
  `Gobby is fleet management for AI coding agents: one local-first platform for sessions,
  tasks, memory, rules, workflows, agents, pipelines, skills, and an MCP proxy.`; the
  architect line and the capability list stay.
- `web/index.html`: add `<meta name="description" content="Gobby: fleet management for AI
  coding agents.">` after the charset meta.

Granularity: fourteen files but one obligation, the same one-line replacement with a single
sweep as its check; none of the edits is independently closeable, so this stays one leaf.

Research context:
- Footprint search on 2026-09-22 (observed): the one-liners live at `README.md` line 3 (hero)
  and lines 53-60 (lead paragraph and the control-plane sentence), `package.json` line 4 (copies
  the hero), `pyproject.toml` line 4, `AGENTS.md` lines 3-6, `ROADMAP.md` lines 3-5,
  `src/gobby/__init__.py` line 1, `src/gobby/cli/__init__.py` line 86 (the `cli` group
  docstring, entry point `gobby = "gobby.cli:cli"`), `docs/architecture/index.md` line 7,
  `docs/architecture/architecture.md` line 7, `ONBOARDING.md` line 33, the two service
  templates (line 2 and line 4), `default.yaml` lines 30-31, and `web/index.html` line 11
  (`<title>Gobby</title>` with no meta description). `web/package.json` has no description
  field and stays that way.
- Left alone on purpose: `SECURITY.md` line 27 states the local-first security posture;
  `README.md` line 191 is a competitor-table property claim; `crates/*/Cargo.toml` descriptions
  are component-level; `.impeccable.md` lines 40-42 quote a market mold and the contract changes
  only through the impeccable skill; the test fixtures
  `web/src/components/activity/memory/__tests__/KnowledgeGraph.test.tsx` (lines 152, 164) and
  `tests/e2e/test_memory_dream_gc_e2e.py` (line 57) embed the old sentence as memory content,
  not copy. No test asserts the `gobby --help` docstring (`gcode grep -F "Local-first daemon"
  tests/` is empty, observed).
- Rejected: keeping the README's walk-away line as the hero (it describes the build loop, one
  feature) and keeping "local-first" in the line (hosted Gobby, roadmap story C, breaks it).
- Planned check: the sweep below returns only `SECURITY.md` and the README loop sentence;
  `uv run gobby --help` prints the new first line.

**Acceptance:**

- 4.1.1 - `gobby --help` and the package docstring open with `Gobby - fleet management for AI
  coding agents.` file: `src/gobby/cli/__init__.py`. file: `src/gobby/__init__.py`.
- 4.1.2 - Both service templates describe the daemon as `Gobby Daemon - fleet management for AI
  coding agents`. file: `src/gobby/install/shared/services/gobby-daemon.service.j2`.
  file: `src/gobby/install/shared/services/gobby-daemon.task.xml.j2`.
- 4.1.3 - The README hero, its lead paragraph, the root `package.json` description and the
  `pyproject.toml` description carry the line; the loop sentence stays in the README body.
  file: `README.md`. file: `package.json`. file: `pyproject.toml`.
- 4.1.4 - `AGENTS.md`, `ROADMAP.md`, both architecture docs and `ONBOARDING.md` open with the
  line and keep their mechanism lists. file: `AGENTS.md`. file: `ROADMAP.md`.
  file: `docs/architecture/index.md`. file: `docs/architecture/architecture.md`.
  file: `ONBOARDING.md`.
- 4.1.5 - The default agent persona states the line and `web/index.html` carries it as the meta
  description. file: `src/gobby/install/shared/workflows/agents/default.yaml`.
  file: `web/index.html`.
- 4.1.6 - `rg -n "local-first daemon|Local-first daemon|control plane for AI coding|unify your AI coding|Walk away. End with a PR" --glob '!tests/**' --glob '!web/src/**/__tests__/**' --glob '!.gobby/**'`
  matches only `SECURITY.md` and the README loop sentence. behavior: "local-first daemon" in `SECURITY.md`.

## V1 Verification
`kind: verification`

- Python (P1, P2):
  `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/sessions/test_title_lifecycle.py tests/storage/sessions/ tests/hooks/test_hooks_manager.py tests/agents/test_spawn_executor.py tests/servers/routes/ tests/servers/websocket/chat/test_stream_persistence.py tests/sessions/test_handoff.py`,
  then `uv run ruff check src/ tests/ && uv run mypy src/`. The migration lands through an
  announced `uv run gobby cutover`; `gdaemon schema-identity --json` matches the carriers.
- Rust (P0, P3): `cargo nextest run -p gobby-client`,
  `GOBBY_UPDATE_SCREENS=1 cargo nextest run -p gobby-client --test screens` with a golden diff
  that moves every new state (splash, unreachable, empty tab, About, Agents and Terminals rows,
  the four pane corners, the menu bar, the status bar), `cargo clippy -p gobby-client`,
  `cargo fmt -p gobby-client -- --check`.
- Live, after `cargo build --release -p gobby-client` and a new-inode install: open gclient
  against a project with one spawned agent, one interactive session with a claimed task, one
  bare zsh pane and one external tmux pane. Check the first frame (goblin and wordmark, stages
  ticking, the stalled stage on the status bar), each Agents and Terminals row string in overlay
  and pinned modes, each pane corner string, that every menu item does something, that the keys
  overlay cuts no description, Help › About and Help › Daemon, the Empty tab, and the
  unreachable state by stopping the daemon. Read the provider line and the marks in both themes,
  in Ghostty and one other terminal for the braille glyphs.
- Web: `sessionTitle.test.ts` passes unchanged; activity panels still hide the provisional
  provider suffix.
- Tagline (P4): the 4.1.6 sweep; `uv run gobby --help`.

**Enhancement round 1 of 2** — `kind: enhancement`; enhancer_run `d60a53a9-129f-434a-86ad-fe249147d191` (plan-enhancer-taskless, grok-4.7, session gobby#14246); artifact `.gobby/plans/gclient-chrome-refresh-enhancement-round1.md`; suggestions_presented 8; votes: cr-1 accept (consumer inventories for 1.2, 1.3, 2.1, 2.2 close the validator's consumer-coverage warnings; `_manager.py` and `session_models.py` become `::*` because exact and wildcard scopes cannot mix), cr-2 accept (dialog keys go into `project_dialog_key`, 3.12 and 3.13), cr-3 accept (the client provider table mirrors `_PROVIDER_TITLE_LABELS`, 3.1), cr-4 accept (the menu-bar test calls `apply_live_menu_action` for non-Act items, 3.11), cr-5 accept (3.2b moves the menu tests out before regrouping; the production-only exemption was false), cr-6 accept (`toggle_sidebar_pin` and `MenuAction::PinSidebar`, 3.3 and 3.11), cr-7 accept (`hold_attach` is a Notify gate on the attach reply, 3.10), cr-8 accept (3.10 always moves `project_workspace` and `project_node`). All eight per Program Director Decision 37.

**Adversary round 1 of 2** — `kind: review`; run `2b73b50b-548c-46a6-9874-f58631ba1918`; evidence `996c11d2-3783-46f4-a910-9dc104ae0a7a`; reviewed hash `b996672ea2fed25e79a51be76321d63ce7750457d291a9e3a68e88cf017cbd3e`; verdict needs_review; artifact `.gobby/plans/gclient-chrome-refresh-adversary-round1.md`; findings 1 through 7 accepted per Program Director Decision 37 (gcr-r1-provider-label → 3.1; gcr-r1-menu-title-set → 3.2b and 3.11; gcr-r1-pin-sidebar → 3.3 and 3.11; gcr-r1-menu-line-ceiling → 3.2b and 3.11; gcr-r1-roster-field → 2.1; gcr-r1-scripted-harness → 3.11 with its typed `add_acceptance` repair as 3.11.7 verbatim; gcr-r1-dialog-match → 3.12 and 3.13). Finding 2 is folded as the corrected fix rather than as written: the signed gclient chrome canvas shows an Edit menu, so the title set keeps Edit with seven titles in the order Gobby, File, Edit, View, Window, Agent, Help, and `MenuBarMenu::ALL` in `crates/gclient/src/ui/menu_bar.rs` (3.2b) is the single definition 3.11 reads.

**Enhancement round 2 of 2** — `kind: enhancement`; the refreshed-hash council round on `4065ada943990c6383d98d2021aa472e354eba3e59e0e1fa47b0ddd89ce930a8` (commit `79191b6bea37957c73521882b7c8c61e5e9f0341`); enhancer gobby#14285 (grok-4.7); verdict relayed as inter-session message `0fd8fc75-5f4a-4da3-bfad-eba3210beab2` (full verdict `08b6abc4-75e1-4ee1-a3e7-f60a02feaf47`); artifact `.gobby/plans/gclient-chrome-refresh-council-round2.md`; suggestions_presented 5; votes: cr-1 fold (3.2b depends on 3.2a only; 3.8 depends on 0.1 and 3.7), cr-2 fold (`StatusSegment` is the four list segments; `Daemon unreachable` and the hidden count are the fixed slot only, Decisions 8 and 17), cr-3 fold (the splash draws the wordmark alone; Decision 19), cr-4 fold (3.9 declares `pub mod splash;`), cr-5 fold (`AgentEntry::model_slug` in 3.1, called by 3.4b and 3.7).

**Adversary round 2 of 2** — `kind: review`; reviewer gobby#14287 (grok-4.7); reviewed hash `4065ada943990c6383d98d2021aa472e354eba3e59e0e1fa47b0ddd89ce930a8`; verdict needs_review (3 blockers); relayed as inter-session message `39b362ac-64b1-4591-b780-a767bd6167c6` (full verdict `2089453c-28b3-4705-ad9d-dbeb3a085f3a`); artifact `.gobby/plans/gclient-chrome-refresh-council-round2.md`; findings 1 through 3 folded (gcr-r2-retired-pref-keys → 3.2a and 3.3, retired `UiPrefs` fields parsed and skipped on save; gcr-r2-unnamed-pane-consumers → 3.6, `app/mod.rs` and `chrome/labels.rs` targeted with the `short_terminal_id` rung, plus the `workspace_panes.rs` extension-file split the validator's size-growth rule requires of any edit to the 927-line `app/mod.rs`; gcr-r2-spawn-executor-ceiling → 1.2, the repository 1,000-line ceiling replaces the 850 claim). No item contested; nothing escalated.

## M1 Task Manifest
`kind: manifest`

```yaml
- title: Marks and wordmark assets in the crate
  category: config
  task_type: feature
  depends_on: []
  validation_criteria: "0.1.1: Both generators live in the crate with a `grid` mode\
    \ and the README's commands regenerate every committed grid byte-identically.\
    \ file: `crates/gclient/assets/marks/mask2.py`. file: `crates/gclient/assets/marks/wordmark.py`.\
    \ file: `crates/gclient/assets/marks/README.md`.\n0.1.2: The goblin grids are\
    \ committed at 33 \xD7 16 and 29 \xD7 14 in the class format the README documents.\
    \ file: `crates/gclient/assets/marks/goblin-33x16.grid`. file: `crates/gclient/assets/marks/goblin-29x14.grid`.\n\
    0.1.3: The braille wordmark text (54 \xD7 8) and the drop shadow class grid (49\
    \ \xD7 9) are committed. file: `crates/gclient/assets/marks/wordmark-braille-54x8.txt`.\
    \ file: `crates/gclient/assets/marks/wordmark-shadow-49x9.grid`.\n0.1.4: Dark\
    \ and light `.ans` renders of all four marks are committed under `renders/`. file:\
    \ `crates/gclient/assets/marks/renders/goblin-33x16-dark.ans`. file: `crates/gclient/assets/marks/renders/goblin-33x16-light.ans`.\
    \ file: `crates/gclient/assets/marks/renders/goblin-29x14-dark.ans`. file: `crates/gclient/assets/marks/renders/goblin-29x14-light.ans`.\
    \ file: `crates/gclient/assets/marks/renders/wordmark-braille-dark.ans`. file:\
    \ `crates/gclient/assets/marks/renders/wordmark-braille-light.ans`. file: `crates/gclient/assets/marks/renders/wordmark-shadow-dark.ans`.\
    \ file: `crates/gclient/assets/marks/renders/wordmark-shadow-light.ans`.\n0.1.5:\
    \ The README states the colour roles, the three homes rule and the macOS-only\
    \ wordmark regeneration. behavior: \"three homes\" in `crates/gclient/assets/marks/README.md`."
  labels:
  - covers:gclient-chrome-refresh:0.1:0.1.1
  - covers:gclient-chrome-refresh:0.1:0.1.2
  - covers:gclient-chrome-refresh:0.1:0.1.3
  - covers:gclient-chrome-refresh:0.1:0.1.4
  - covers:gclient-chrome-refresh:0.1:0.1.5
  tdd: true
  source_section: '0.1'
  assigned_agent: backend-developer
- title: Provisional titles carry the provider label again
  category: code
  task_type: feature
  depends_on: []
  validation_criteria: '1.1.1: A registered codex session without a claim is titled
    `project#N: Codex`. symbol: `format_provisional_session_title`. test: `tests/storage/sessions/test_register_fallback.py::test_register_session_happy_path_caches_persisted_provisional_title`.

    1.1.2: The handoff lifecycle test asserts the provider-suffixed provisional title
    before the task and manual steps. test: `tests/sessions/test_handoff.py::test_title_lifecycle_is_provisional_task_manual_and_clear_sticky`.

    1.1.3: The spawn-route test still passes because it derives its expectation from
    the formatter. test: `tests/servers/routes/test_agent_spawn_routes.py::test_spawn_claims_task_for_web_chat`.'
  labels:
  - covers:gclient-chrome-refresh:1.1:1.1.1
  - covers:gclient-chrome-refresh:1.1:1.1.2
  - covers:gclient-chrome-refresh:1.1:1.1.3
  tdd: true
  source_section: '1.1'
  implementation_domain: backend
- title: Remove the prompt heuristic triggers and lifecycle helpers
  category: code
  task_type: feature
  depends_on:
  - '1.1'
  validation_criteria: '1.2.1: `title_lifecycle.py` exports no heuristic symbol and
    `recompute_automatic_title` falls back straight to the provisional title after
    the last claim closes. file: `src/gobby/sessions/title_lifecycle.py`. test: `tests/sessions/test_title_lifecycle.py::test_recompute_automatic_title_falls_back_to_provisional_after_close`
    (new).

    1.2.2: A first codex prompt records terminal identity but leaves the provisional
    title. symbol: `AgentEventHandlerMixin.handle_before_agent`. test: `tests/hooks/test_hooks_manager.py::test_first_codex_prompt_persists_terminal_identity`
    (renamed).

    1.2.3: Codex spawns no longer seed a title from the prompt. symbol: `_spawn_codex_terminal`.
    file: `src/gobby/agents/spawn_executor_codex.py`. behavior: "seed_heuristic_title_from_prompt"
    absent in `src/gobby/agents/spawn_executor_providers.py`.

    1.2.4: A persisted first web-chat message leaves the provisional title in place.
    symbol: `ChatStreamPersistence.persist_user_message`. test: `tests/servers/websocket/chat/test_stream_persistence.py::test_persisted_first_user_message_leaves_provisional_title`
    (new).

    1.2.5: `spawn_executor.py` and the new codex module are each under 1,000 lines
    (the repository ceiling), `spawn_executor._spawn_codex_terminal` still resolves
    as an attribute, and the provider dispatch still resolves codex spawns. file:
    `src/gobby/agents/spawn_executor.py`. test: `tests/agents/test_spawn_executor.py::test_codex_agent_prompt_precedes_task_prompt`.'
  labels:
  - covers:gclient-chrome-refresh:1.2:1.2.1
  - covers:gclient-chrome-refresh:1.2:1.2.2
  - covers:gclient-chrome-refresh:1.2:1.2.3
  - covers:gclient-chrome-refresh:1.2:1.2.4
  - covers:gclient-chrome-refresh:1.2:1.2.5
  tdd: true
  source_section: '1.2'
  implementation_domain: backend
- title: Delete the heuristic title source from storage
  category: code
  task_type: feature
  depends_on:
  - '1.2'
  validation_criteria: '1.3.1: `heuristic` is not a valid `title_source` and the formatter
    module has no heuristic symbol. symbol: `SessionManager`. file: `src/gobby/storage/sessions/_title_defaults.py`.

    1.3.2: `TITLE_UPDATE_ALLOWED_SQL` contains no heuristic branch. symbol: `TITLE_UPDATE_ALLOWED_SQL`.
    behavior: "heuristic" absent in `src/gobby/storage/sessions/_title_update.py`.

    1.3.3: `Session` has no `heuristic_title` field and `from_row` does not read the
    column. symbol: `Session.from_row`. test: `tests/storage/sessions/test_storage_sessions_models.py::TestSession.test_full_and_brief_expose_nullable_effort`
    (renamed).

    1.3.4: The startup sweep rewrites `title_source=''heuristic''` rows to the provider
    provisional title. symbol: `_TitleFieldMixin.normalize_automatic_title_refs`.
    test: `tests/storage/sessions/test_title_fields.py::test_normalize_rewrites_heuristic_rows_to_provisional`
    (new).

    1.3.5: Prefix renames for task titles still work without touching the removed
    column. test: `tests/storage/sessions/test_title_fields.py::test_normalize_keeps_task_titles_and_renames_prefix`
    (new).'
  labels:
  - covers:gclient-chrome-refresh:1.3:1.3.1
  - covers:gclient-chrome-refresh:1.3:1.3.2
  - covers:gclient-chrome-refresh:1.3:1.3.3
  - covers:gclient-chrome-refresh:1.3:1.3.4
  - covers:gclient-chrome-refresh:1.3:1.3.5
  tdd: true
  source_section: '1.3'
  implementation_domain: backend
- title: 'Migration 447: drop sessions.heuristic_title'
  category: code
  task_type: feature
  depends_on:
  - '1.3'
  validation_criteria: '1.4.1: Migration 447 drops `sessions.heuristic_title`. file:
    `crates/gcore/assets/schema/migrations/447_drop_session_heuristic_title.sql`.

    1.4.2: `MIGRATIONS` embeds version 447 with its checksum and the catalog manifest
    no longer lists `sessions.heuristic_title`. symbol: `MIGRATIONS`. file: `crates/gcore/assets/schema/catalog.manifest.json`.

    1.4.3: The seed verifier allowlist no longer names `heuristic_title`. symbol:
    `is_live_mutable_seed_field`.

    1.4.4: The schema identity contract tests pass with `latest_version` 447. test:
    `crates/gcore/tests/schema_contract.rs::embedded_assets_publish_a_complete_schema_identity`.
    test: `crates/gdaemon/tests/cli_contract.rs::version_json_reports_exact_schema_identity_contract`.

    1.4.5: `schema_expected_identity.json` matches the rebuilt gdaemon''s identity.
    file: `src/gobby/storage/schema_expected_identity.json`.'
  labels:
  - covers:gclient-chrome-refresh:1.4:1.4.1
  - covers:gclient-chrome-refresh:1.4:1.4.2
  - covers:gclient-chrome-refresh:1.4:1.4.3
  - covers:gclient-chrome-refresh:1.4:1.4.4
  - covers:gclient-chrome-refresh:1.4:1.4.5
  tdd: true
  source_section: '1.4'
  implementation_domain: backend
- title: Canonical Telegram title and session docs
  category: code
  task_type: feature
  depends_on:
  - '1.1'
  validation_criteria: '1.5.1: Provisional and task titles that already start with
    the session ref are used verbatim in status messages. symbol: `_canonical_session_title`.
    test: `tests/communications/test_session_events.py::test_canonical_title_keeps_ref_prefixed_titles_verbatim`
    (new).

    1.5.2: Manual titles are still prefixed with the ref. test: `tests/communications/test_session_events.py::test_canonical_title_prefixes_manual_titles`
    (new).

    1.5.3: The sessions guide describes the three deterministic formats and no heuristic.
    file: `docs/guides/sessions.md`. behavior: "heuristic" absent in `docs/guides/sessions.md`.

    1.5.4: The reference audit matches the guide. file: `docs/reference-audit/sessions.json`.'
  labels:
  - covers:gclient-chrome-refresh:1.5:1.5.1
  - covers:gclient-chrome-refresh:1.5:1.5.2
  - covers:gclient-chrome-refresh:1.5:1.5.3
  - covers:gclient-chrome-refresh:1.5:1.5.4
  tdd: true
  source_section: '1.5'
  implementation_domain: backend
- title: Roster entries carry task titles and claimed tasks
  category: code
  task_type: feature
  depends_on: []
  validation_criteria: '2.1.1: Roster run entries include `task.title`. symbol: `AttentionStateManager.load_roster_rows`.
    test: `tests/servers/test_attention_roster.py::test_roster_run_entries_carry_the_task_title`
    (new).

    2.1.2: Roster session entries include the open claimed task `{id, ref, stage,
    title}` or `null` once it closes. symbol: `AttentionRosterRow.from_row`. test:
    `tests/servers/test_attention_roster.py::test_roster_session_entries_carry_the_open_claimed_task`
    (new).

    2.1.3: The roster serialisation helpers live in the new module and the route still
    serves the same keys. file: `src/gobby/servers/routes/attention_roster.py`. test:
    `tests/servers/test_attention_roster.py::test_roster_spells_the_model_as_its_provider_prints_it`.

    2.1.4: `src/gobby/servers/routes/attention.py` and `routes/attention_roster.py`
    are each under 850 lines and the cold path stays one round trip. file: `src/gobby/servers/routes/attention.py`.
    test: `tests/servers/test_attention_roster.py::test_roster_cold_path_is_bounded_and_cursor_invalidates_cache`.'
  labels:
  - covers:gclient-chrome-refresh:2.1:2.1.1
  - covers:gclient-chrome-refresh:2.1:2.1.2
  - covers:gclient-chrome-refresh:2.1:2.1.3
  - covers:gclient-chrome-refresh:2.1:2.1.4
  tdd: true
  source_section: '2.1'
  implementation_domain: backend
- title: Terminal inventories report the spawned shell
  category: code
  task_type: feature
  depends_on: []
  validation_criteria: '2.2.1: Spawned terminals persist the shell basename as `process.shell`.
    symbol: `spawn_web_terminal`. test: `tests/terminals/test_web_spawn.py::test_spawn_records_the_shell_basename_in_process`
    (new).

    2.2.2: `process_shell` reads the recorded basename and ignores rows without one.
    file: `src/gobby/terminals/foreground.py`. test: `tests/terminals/test_foreground.py::test_process_shell_reads_the_recorded_basename`
    (new).

    2.2.3: The REST inventory `command` falls back to `process.shell`. symbol: `_row_json`.
    test: `tests/servers/test_terminals_routes.py::test_a_native_row_falls_back_to_its_spawn_shell`
    (new).

    2.2.4: The WS inventory `command` falls back to `process.shell` after the live
    foreground and pane command. symbol: `TerminalWsMixin._handle_terminal_list`.
    test: `tests/servers/test_terminal_ws_list.py::test_list_falls_back_to_the_spawn_shell_for_a_native_row`
    (new).

    2.2.5: Live foreground detection still wins over the recorded shell. test: `tests/servers/test_terminals_routes.py::test_a_native_row_reports_the_command_in_its_terminal_foreground`.'
  labels:
  - covers:gclient-chrome-refresh:2.2:2.2.1
  - covers:gclient-chrome-refresh:2.2:2.2.2
  - covers:gclient-chrome-refresh:2.2:2.2.3
  - covers:gclient-chrome-refresh:2.2:2.2.4
  - covers:gclient-chrome-refresh:2.2:2.2.5
  tdd: true
  source_section: '2.2'
  implementation_domain: backend
- title: 'Agent data model: definition name, task title, roster row split'
  category: code
  task_type: feature
  depends_on:
  - '2.1'
  validation_criteria: '3.1.1: `TaskRef` carries an optional `title` and the roster
    types live in the new module. file: `crates/gclient/src/daemon/roster.rs`.

    3.1.2: `AgentEntry` exposes `agent_definition_name`, `task_title`, `context_percent`,
    `tokens_used`, and `definition_label()` falls back to the provider label. symbol:
    `AgentEntry`.

    3.1.3: NEW regression: a run entry with `task.title` and `agent_name` yields both fields;
    a session-only entry with provider `claude_code` yields `definition_label() ==
    "Claude Code"` and one with provider `claude` yields `"Claude"`. test: `crates/gclient/tests/sidebar_model.rs::build_carries_definition_name_and_task_title`.

    3.1.4: Existing join test still passes with the new fields defaulted. test: `crates/gclient/tests/sidebar_model.rs::build_joins_projects_worktrees_and_agents`.

    3.1.5: `crates/gclient/src/daemon/mod.rs` is under 850 lines after the move. file:
    `crates/gclient/src/daemon/mod.rs`.

    3.1.6: NEW regression: `model_slug()` lowercases the display name, joins whitespace
    runs with `-`, appends `-{effort}` when set, and falls back to `model`. symbol:
    `AgentEntry`. test: `crates/gclient/tests/sidebar_model.rs::model_slug_lowercases_hyphenates_and_appends_effort`.'
  labels:
  - covers:gclient-chrome-refresh:3.1:3.1.1
  - covers:gclient-chrome-refresh:3.1:3.1.2
  - covers:gclient-chrome-refresh:3.1:3.1.3
  - covers:gclient-chrome-refresh:3.1:3.1.4
  - covers:gclient-chrome-refresh:3.1:3.1.5
  - covers:gclient-chrome-refresh:3.1:3.1.6
  tdd: true
  source_section: '3.1'
  implementation_domain: backend
- title: 'Frame: hidden sidebar, rail removed, four-edged panes'
  category: code
  task_type: feature
  depends_on:
  - '3.1'
  validation_criteria: '3.2a.1: `SidebarState` lives in the new module with `pinned`
    in place of `collapsed`/`hide_when_collapsed`. file: `crates/gclient/src/ui/chrome/sidebar_state.rs`.

    3.2a.2: A default `Chrome` yields `sidebar_rect.width == 0`, a full-width `status_rect`
    on the last row and `menu_bar_rect` on row 0. test: `crates/gclient/tests/parity/chrome.rs::hidden_sidebar_uses_full_width_terminal_area`.

    3.2a.3: A lone pane''s `PaneInfo.borders == Borders::ALL` and the frame is drawn.
    test: `crates/gclient/tests/parity/panes.rs::lone_pane_draws_all_four_edges`.

    3.2a.4: Settings show ten rows and no `Pane borders` label. test: `crates/gclient/src/ui/settings.rs::row_values_follow_prefs`.

    3.2a.5: No rail symbol remains. behavior: "render_collapsed_sidebar" absent in
    `crates/gclient/src/ui/sidebar.rs`.

    3.2a.6: Goldens regenerate and match. test: `crates/gclient/tests/screens.rs::screens_match_committed_captures`.

    3.2a.7: A saved `prefs.toml` carrying `pane_borders` and `sidebar_collapsed` still
    loads, a fresh save omits both keys, and an unknown key still fails. symbol: `UiPrefs`.
    test: `crates/gclient/tests/startup.rs::prefs_round_trip_and_reject_unknown_keys`
    (existing, extended).'
  labels:
  - covers:gclient-chrome-refresh:3.2a:3.2a.1
  - covers:gclient-chrome-refresh:3.2a:3.2a.2
  - covers:gclient-chrome-refresh:3.2a:3.2a.3
  - covers:gclient-chrome-refresh:3.2a:3.2a.4
  - covers:gclient-chrome-refresh:3.2a:3.2a.5
  - covers:gclient-chrome-refresh:3.2a:3.2a.6
  - covers:gclient-chrome-refresh:3.2a:3.2a.7
  tdd: true
  source_section: 3.2a
  implementation_domain: frontend
- title: Menu bar row and status row slot
  category: code
  task_type: feature
  depends_on:
  - 3.2a
  validation_criteria: '3.2b.1: The seven titles render on row 0 in accent. file:
    `crates/gclient/src/ui/menu_bar.rs`.

    3.2b.2: NEW regression: clicking a title returns `Hit::MenuTitle(i)` and opens that
    menu under it. test: `crates/gclient/tests/parity/chrome.rs::menu_bar_titles_hit_and_open_under_their_cell`.

    3.2b.3: Prefix hint and mode word sit at the right end on `surface0`. test: `crates/gclient/src/ui/status.rs::status_line_shows_only_global_prefix_mode_and_health`.

    3.2b.4: Hit precedence: menu bar before tab bar. test: `crates/gclient/src/ui/hit/tests.rs::hit_test_covers_split_live_layout`.

    3.2b.5: Golden for the open menu. file: `crates/gclient/tests/fixtures/screens/menu_bar.txt`.

    3.2b.6: The menu tests live in the new file and `menu.rs` stays under the ceiling
    after the regroup. file: `crates/gclient/src/app/live_loop/menu/tests.rs`. test:
    `crates/gclient/tests/source_size.rs::no_src_file_at_or_above_1000_lines` (existing).'
  labels:
  - covers:gclient-chrome-refresh:3.2b:3.2b.1
  - covers:gclient-chrome-refresh:3.2b:3.2b.2
  - covers:gclient-chrome-refresh:3.2b:3.2b.3
  - covers:gclient-chrome-refresh:3.2b:3.2b.4
  - covers:gclient-chrome-refresh:3.2b:3.2b.5
  - covers:gclient-chrome-refresh:3.2b:3.2b.6
  tdd: true
  source_section: 3.2b
  implementation_domain: frontend
- title: 'Sidebar states: overlay, pinned, side'
  category: code
  task_type: feature
  depends_on:
  - 3.2a
  - 3.2b
  validation_criteria: "3.3.1: `SidebarState { pinned, overlay, side }` with `overlay`\
    \ never serialized. file: `crates/gclient/src/ui/chrome/sidebar_state.rs`.\n3.3.2:\
    \ NEW regression: overlay yields a 34-column `sidebar_rect` and an unchanged `terminal_area`;\
    \ pinned yields the column layout on the chosen side. test: `crates/gclient/tests/parity/chrome.rs::overlay_covers_34_columns_without_moving_panes`.\n\
    3.3.3: NEW regression: Esc and `focus_pane` clear the overlay. test: `crates/gclient/tests/parity/chrome.rs::overlay_rolls_up_on_escape_and_pane_focus`.\n\
    3.3.4: Two settings rows persist through `prefs.toml`. test: `crates/gclient/src/ui/settings.rs::row_values_follow_prefs`.\n\
    3.3.5: Tab labels sit on the far side while the overlay is open. symbol: `render_tab_bar`.\n\
    3.3.6: Golden. file: `crates/gclient/tests/fixtures/screens/sidebar_overlay.txt`.\n\
    3.3.7: View \u203A Pin Sidebar (`MenuAction::PinSidebar`) and the `SidebarPinned`\
    \ settings row share `toggle_sidebar_pin`, which persists `sidebar_pinned` and\
    \ closes the overlay when pinning. symbol: `toggle_sidebar_pin`. file: `crates/gclient/src/app/live_loop/actions/sidebar.rs`."
  labels:
  - covers:gclient-chrome-refresh:3.3:3.3.1
  - covers:gclient-chrome-refresh:3.3:3.3.2
  - covers:gclient-chrome-refresh:3.3:3.3.3
  - covers:gclient-chrome-refresh:3.3:3.3.4
  - covers:gclient-chrome-refresh:3.3:3.3.5
  - covers:gclient-chrome-refresh:3.3:3.3.6
  - covers:gclient-chrome-refresh:3.3:3.3.7
  tdd: true
  source_section: '3.3'
  implementation_domain: frontend
- title: 'Sidebar sections: Machines, Projects, Agents, Terminals'
  category: code
  task_type: feature
  depends_on:
  - '3.3'
  validation_criteria: '3.4a.1: Four sections with titles `Machines`, `Projects`,
    `Agents`, `Terminals`. symbol: `SidebarSection`.

    3.4a.2: `sidebar_layout` yields four rects; Agents and Terminals share the remainder.
    test: `crates/gclient/src/ui/sidebar/tests.rs::layout_gives_the_top_half_to_machines_and_projects_at_most`.

    3.4a.3: Scrollbar lanes hit by all four sections. test: `crates/gclient/src/ui/hit/tests.rs::sidebar_scrollbar_lane_hits_by_section`.

    3.4a.4: The module rename lands. file: `crates/gclient/src/ui/sidebar/agents.rs`.

    3.4a.5: Bare terminals render under their own band. file: `crates/gclient/src/ui/sidebar/terminals.rs`.'
  labels:
  - covers:gclient-chrome-refresh:3.4a:3.4a.1
  - covers:gclient-chrome-refresh:3.4a:3.4a.2
  - covers:gclient-chrome-refresh:3.4a:3.4a.3
  - covers:gclient-chrome-refresh:3.4a:3.4a.4
  - covers:gclient-chrome-refresh:3.4a:3.4a.5
  tdd: true
  source_section: 3.4a
  implementation_domain: frontend
- title: Agent and terminal rows
  category: code
  task_type: feature
  depends_on:
  - '3.1'
  - 3.4a
  validation_criteria: '3.4b.1: `RowKind::Terminal` exists and `height()` is 3/2.
    symbol: `RowKind`.

    3.4b.2: Three-line agent row: bold name cut with an ellipsis before `(ref)`, slug
    on line 3 without DIM. test: `crates/gclient/src/ui/sidebar_rows/tests.rs::agent_rows_render_three_lines_with_the_model_slug`.

    3.4b.3: `Task #ref - ` pinned while the title scrolls; `No assigned task` otherwise.
    test: `crates/gclient/src/ui/sidebar_rows/tests.rs::task_prefix_stays_fixed_while_the_title_scrolls`.

    3.4b.4: Terminal rows show app over backend, no address. test: `crates/gclient/tests/parity/sidebar.rs::stripped_terminal_title_renders_with_unicode_width_truncation`.

    3.4b.5: Unselected Agent and Project rows share one weight. test: `crates/gclient/tests/parity/sidebar.rs::occurrence_false_removes_default_workspace_bold_and_agent_dim`.

    3.4b.6: Golden. file: `crates/gclient/tests/fixtures/screens/agent_rows.txt`.'
  labels:
  - covers:gclient-chrome-refresh:3.4b:3.4b.1
  - covers:gclient-chrome-refresh:3.4b:3.4b.2
  - covers:gclient-chrome-refresh:3.4b:3.4b.3
  - covers:gclient-chrome-refresh:3.4b:3.4b.4
  - covers:gclient-chrome-refresh:3.4b:3.4b.5
  - covers:gclient-chrome-refresh:3.4b:3.4b.6
  tdd: true
  source_section: 3.4b
  implementation_domain: fullstack
- title: Tab labels and bar style
  category: code
  task_type: feature
  depends_on:
  - 3.4b
  validation_criteria: "3.5.1: Auto-named label is `project:tab_id`, renamed is `project:title`.\
    \ test: `crates/gclient/tests/parity/tabs.rs::tab_bar_marks_zoomed_tabs_without_renaming_them`.\n\
    3.5.2: NEW regression: a hidden tab with a blocked pane carries `\u237E`. test: `crates/gclient/tests/parity/tabs.rs::hidden_tab_with_attention_carries_the_mark`.\n\
    3.5.3: Active tab is `panel_bg` bold on a `surface0` row. test: `crates/gclient/tests/parity/chrome.rs::tab_bar_cuts_the_active_tab_out_in_panel_bg`.\n\
    3.5.4: `MIN_TAB_WIDTH` fits `gobby:0:0:1`. symbol: `MIN_TAB_WIDTH`.\n3.5.5: Goldens.\
    \ test: `crates/gclient/tests/screens.rs::screens_match_committed_captures`."
  labels:
  - covers:gclient-chrome-refresh:3.5:3.5.1
  - covers:gclient-chrome-refresh:3.5:3.5.2
  - covers:gclient-chrome-refresh:3.5:3.5.3
  - covers:gclient-chrome-refresh:3.5:3.5.4
  - covers:gclient-chrome-refresh:3.5:3.5.5
  tdd: true
  source_section: '3.5'
  implementation_domain: backend
- title: 'Pane chrome: task title, footer corners, frame colour, no shell literal'
  category: code
  task_type: feature
  depends_on:
  - '2.2'
  - '3.1'
  - 3.2b
  - '3.5'
  validation_criteria: "3.6.1: Title ladder task \u2192 manual \u2192 provisional;\
    \ a bare terminal is empty unless renamed. test: `crates/gclient/src/ui/pane_chrome/tests.rs::pane_title_prefers_task_then_label_then_provisional`.\n\
    3.6.2: Footer corners: `definition (project#ref) \xB7 Focused` left, `gclient\
    \ 0:0:1:2` right. test: `crates/gclient/src/ui/panes/tests.rs::bottom_metadata_renders_with_and_without_pane_gaps`.\n\
    3.6.3: NEW regression: attention frames use the warning role with `\u237E`, exited frames\
    \ the destructive role with `\u25CC`, junctions follow focus. test: `crates/gclient/tests/parity/panes.rs::frame_colour_follows_focus_attention_and_exit`.\n\
    3.6.4: No `shell` literal remains. behavior: \"\\\"shell\\\"\" absent in `crates/gclient/src/app/pane.rs`.\n\
    3.6.5: The `UNNAMED_PANE` rung is gone from the model ladder. symbol: `build_agents`.\n\
    3.6.6: Golden exposing the unnamed rung. file: `crates/gclient/tests/fixtures/screens/unnamed_pane.txt`.\n\
    3.6.7: The re-export is gone. behavior: \"UNNAMED_PANE\" absent in `crates/gclient/src/app/mod.rs`.\n\
    3.6.8: The label fallbacks use the terminal id rung. behavior: \"UNNAMED_PANE\"\
    \ absent in `crates/gclient/src/ui/chrome/labels.rs`. symbol: `terminal_label`.\n\
    3.6.9: The pane-access methods live in the new extension file and `crates/gclient/src/app/mod.rs`\
    \ is under 850 lines. file: `crates/gclient/src/app/workspace_panes.rs`. behavior:\
    \ \"fn pane_for_terminal\" absent in `crates/gclient/src/app/mod.rs`."
  labels:
  - covers:gclient-chrome-refresh:3.6:3.6.1
  - covers:gclient-chrome-refresh:3.6:3.6.2
  - covers:gclient-chrome-refresh:3.6:3.6.3
  - covers:gclient-chrome-refresh:3.6:3.6.4
  - covers:gclient-chrome-refresh:3.6:3.6.5
  - covers:gclient-chrome-refresh:3.6:3.6.6
  - covers:gclient-chrome-refresh:3.6:3.6.7
  - covers:gclient-chrome-refresh:3.6:3.6.8
  - covers:gclient-chrome-refresh:3.6:3.6.9
  tdd: true
  source_section: '3.6'
  implementation_domain: fullstack
- title: Status bar segments
  category: code
  task_type: feature
  depends_on:
  - 3.2b
  - '3.3'
  - '3.6'
  validation_criteria: '3.7.1: Segment names parse and render from `[status]` lists.
    file: `crates/gclient/src/ui/status_segments.rs`.

    3.7.2: `prefs.toml` round-trips `status.left`/`status.right`. symbol: `PrefsFile`.

    3.7.3: NEW regression: the left slot shows `Daemon unreachable` then the hidden-attention
    count; the right end keeps prefix and mode. test: `crates/gclient/tests/parity/status.rs::status_bar_orders_fixed_slots_and_configured_segments`.

    3.7.4: NEW regression: clicking the count opens the sidebar overlay. test: `crates/gclient/tests/parity/chrome.rs::status_count_click_opens_the_sidebar_overlay`.

    3.7.5: Hit precedence for the count cell. test: `crates/gclient/src/ui/hit/tests.rs::hit_test_covers_split_live_layout`.

    3.7.6: Golden. file: `crates/gclient/tests/fixtures/screens/status_segments.txt`.'
  labels:
  - covers:gclient-chrome-refresh:3.7:3.7.1
  - covers:gclient-chrome-refresh:3.7:3.7.2
  - covers:gclient-chrome-refresh:3.7:3.7.3
  - covers:gclient-chrome-refresh:3.7:3.7.4
  - covers:gclient-chrome-refresh:3.7:3.7.5
  - covers:gclient-chrome-refresh:3.7:3.7.6
  tdd: true
  source_section: '3.7'
  implementation_domain: fullstack
- title: Marks module and the ink, glint and dim theme roles
  category: code
  task_type: feature
  depends_on:
  - '0.1'
  - '3.7'
  validation_criteria: '3.8.1: The on-disk format above parses through `parse`, rejects
    a bad header, a bad letter, a short or long line and a non-braille glyph, and
    all four embedded assets parse to their declared sizes. test: `crates/gclient/tests/marks.rs::halfblock_grid_parses_to_its_declared_size`
    (new, plus sibling error-case tests in the same file).

    3.8.2: `render_mark` implements the four-case half-block rule exactly and leaves
    transparent cells untouched. test: `crates/gclient/tests/marks.rs::halfblock_cells_paint_upper_and_lower_halves_by_the_stated_rule`
    (new).

    3.8.3: Braille glyphs paint in the palette''s braille role, U+2800 stays transparent,
    and the shadow palette paints braille in `dim`. test: `crates/gclient/tests/marks.rs::braille_glyphs_paint_in_the_given_role_and_blank_cells_stay_transparent`
    (new).

    3.8.4: `MarkPalette::dimmed` drops glints and uses overlay0/panel_bg fill/lines
    in dark and surface1/overlay0 in light. test: `crates/gclient/tests/marks.rs::dimmed_palette_drops_glints_and_uses_theme_fill_and_lines`
    (new).

    3.8.5: `Palette::entries` has 19 roles, `ink`/`glint` swap by kind, `dim` sits
    between `surface1` and `overlay0`, and the monochrome/contrast contract still
    holds. test: `crates/gclient/tests/theme.rs::palette_entries_bind_the_same_tokens_the_render_paints_with`
    (existing, extended) and `crates/gclient/tests/theme.rs::dim_sits_between_surface1_and_overlay0_and_ink_glint_swap_by_kind`
    (new).

    3.8.6: The module carries the native header and adds no forbidden token. file:
    `crates/gclient/src/ui/marks.rs`.'
  labels:
  - covers:gclient-chrome-refresh:3.8:3.8.1
  - covers:gclient-chrome-refresh:3.8:3.8.2
  - covers:gclient-chrome-refresh:3.8:3.8.3
  - covers:gclient-chrome-refresh:3.8:3.8.4
  - covers:gclient-chrome-refresh:3.8:3.8.5
  - covers:gclient-chrome-refresh:3.8:3.8.6
  tdd: true
  source_section: '3.8'
  implementation_domain: frontend
- title: Splash first frame and Daemon unreachable
  category: code
  task_type: feature
  depends_on:
  - 3.2a
  - '3.7'
  - '3.8'
  - '3.10'
  validation_criteria: "3.9.1: The first drawn frame while stage 1 runs shows the\
    \ menu bar, skeleton tab blocks, the centred goblin, wordmark, version line, four\
    \ stage rows with `\u25D0 daemon health \u2026 9.8 s and waiting`, and the connecting\
    \ line. file: `crates/gclient/tests/fixtures/screens/splash_connecting.txt`.\n\
    3.9.2: The group is centred and degrades goblin-first, wordmark-second, never\
    \ clipping. test: `crates/gclient/tests/splash.rs::group_is_centred_in_the_pane_area_and_never_clips`\
    \ (new).\n3.9.3: The status bar's left slot names the running stage and its elapsed\
    \ time, and leads with `Daemon unreachable \xB7 retry in <n> s` after a drop.\
    \ test: `crates/gclient/tests/splash.rs::status_segment_names_the_running_stage_and_the_retry_countdown`\
    \ (new) and `crates/gclient/src/ui/status.rs::status_line_shows_only_global_prefix_mode_and_health`\
    \ (existing, updated).\n3.9.4: A drop raises exactly one error toast naming the\
    \ URL per outage, and the segment clears when the handshake completes. test: `crates/gclient/tests/splash.rs::a_dropped_daemon_toasts_the_url_once_and_clears_on_reconnect`\
    \ (new).\n3.9.5: A pane whose attachment retired keeps its last frame. test: `crates/gclient/tests/splash.rs::panes_keep_their_last_frame_while_the_daemon_is_away`\
    \ (new). symbol: `Pane::retire_attachment`.\n3.9.6: The reconnect helpers live\
    \ in the new file and live_loop.rs stays under 1,000 lines. file: `crates/gclient/src/app/live_loop/reconnect.rs`."
  labels:
  - covers:gclient-chrome-refresh:3.9:3.9.1
  - covers:gclient-chrome-refresh:3.9:3.9.2
  - covers:gclient-chrome-refresh:3.9:3.9.3
  - covers:gclient-chrome-refresh:3.9:3.9.4
  - covers:gclient-chrome-refresh:3.9:3.9.5
  - covers:gclient-chrome-refresh:3.9:3.9.6
  tdd: true
  source_section: '3.9'
  implementation_domain: fullstack
- title: 'Startup latency: draw first, stage the connect, log the timings'
  category: code
  task_type: feature
  depends_on:
  - 3.2a
  - '3.7'
  validation_criteria: '3.10.1: A frame is drawn before the daemon answers the workspace
    attach, and menus open during that wait. test: `crates/gclient/tests/startup_latency.rs::the_first_frame_is_drawn_before_the_daemon_answers`
    (new).

    3.10.2: The sidebar REST fan-out starts only after the first-frame stage completes,
    on the loop''s background job. test: `crates/gclient/tests/startup_latency.rs::the_sidebar_fan_out_runs_after_the_first_frame`
    (new).

    3.10.3: Stages advance in order, `elapsed`/`summary` are computed from the stored
    `now`, and `for_test` builds any state. test: `crates/gclient/tests/startup_stages.rs::stages_advance_in_order_and_report_took_and_waiting`
    (new).

    3.10.4: Each stage logs `took_ms` once and the summary logs once per launch. test:
    `crates/gclient/tests/startup_latency.rs::stage_timings_are_logged_once_per_launch`
    (new).

    3.10.5: A daemon that is down at launch still opens the window, waits, and restores
    on return; a project-less start still opens one shell. test: `crates/gclient/tests/client_loop.rs::launch_with_the_daemon_down_waits_and_restores_when_it_returns`
    (existing) and `crates/gclient/tests/client_loop.rs::first_run_opens_one_shell_and_never_auto_opens`
    (existing).

    3.10.6: The four split files exist and no source file reaches 1,000 lines. file:
    `crates/gclient/src/daemon/live_connect.rs`. file: `crates/gclient/src/app/live_sidebar.rs`.
    file: `crates/gclient/src/app/live_loop/startup.rs`. file: `crates/gclient/src/ui/chrome/project_workspace.rs`.
    test: `crates/gclient/tests/source_size.rs::no_src_file_at_or_above_1000_lines`
    (existing).'
  labels:
  - covers:gclient-chrome-refresh:3.10:3.10.1
  - covers:gclient-chrome-refresh:3.10:3.10.2
  - covers:gclient-chrome-refresh:3.10:3.10.3
  - covers:gclient-chrome-refresh:3.10:3.10.4
  - covers:gclient-chrome-refresh:3.10:3.10.5
  - covers:gclient-chrome-refresh:3.10:3.10.6
  tdd: true
  source_section: '3.10'
  implementation_domain: fullstack
- title: 'Menus: definitions, the Agent menu, and every item a real action'
  category: code
  task_type: feature
  depends_on:
  - 3.2a
  - 3.2b
  - '3.3'
  validation_criteria: "3.11.1: Every enabled item of every bar menu, activated, changes\
    \ state, opens a mode or dialog, or sends a request. test: `crates/gclient/tests/menu_bar.rs::every_menu_bar_item_dispatches_to_a_handler`\
    \ (new).\n3.11.2: The Agent menu lists exactly the nine actions in the stated\
    \ order, with `respond`/`mark seen` disabled when no attention entry applies.\
    \ test: `crates/gclient/tests/menu_bar.rs::agent_menu_lists_the_nine_actions_in_order`\
    \ (new).\n3.11.3: File says `new workspace\u2026`, Help holds `alerts\u2026`,\
    \ the global menu no longer does, and the keymap description reads `Add a workspace`.\
    \ test: `crates/gclient/tests/menu_bar.rs::file_menu_says_new_workspace_and_help_holds_the_alert_log`\
    \ (new). file: `crates/gclient/src/ui/keymap/names.rs`.\n3.11.4: View carries\
    \ the `[view]` and `[working]` band choices plus `show sidebar` as `Act(ToggleSidebar)`\
    \ and `pin sidebar` as `MenuAction::PinSidebar`. behavior: \"pin sidebar\" in\
    \ `crates/gclient/src/app/live_loop/menu_bar.rs`.\n3.11.5: The item builders live\
    \ in `menu/items.rs` (the tests were moved to `menu/tests.rs` by 3.2b and only\
    \ their two global-menu index assertions change here), dispatch lives in menu_dispatch.rs,\
    \ and the existing global-menu tests pass with their updated indices. test: `crates/gclient/src/app/live_loop/menu/tests.rs::menus_list_items_per_target_and_state`\
    \ (existing, updated). file: `crates/gclient/src/app/live_loop/menu_dispatch.rs`.\n\
    3.11.6: The keys overlay golden shows `Add a workspace`. file: `crates/gclient/tests/fixtures/screens/help_dialog.txt`.\n\
    3.11.7: The menu-bar dispatch test calls apply_live_menu_action for every enabled\
    \ non-Act item, and an Ok(false) fall-through from apply_scripted_menu_action\
    \ is a failure. test: `crates/gclient/tests/menu_bar.rs::every_menu_bar_item_dispatches_to_a_handler`."
  labels:
  - covers:gclient-chrome-refresh:3.11:3.11.1
  - covers:gclient-chrome-refresh:3.11:3.11.2
  - covers:gclient-chrome-refresh:3.11:3.11.3
  - covers:gclient-chrome-refresh:3.11:3.11.4
  - covers:gclient-chrome-refresh:3.11:3.11.5
  - covers:gclient-chrome-refresh:3.11:3.11.6
  - covers:gclient-chrome-refresh:3.11:3.11.7
  tdd: true
  source_section: '3.11'
  implementation_domain: frontend
- title: "Window \u203A Arrange and New Grid"
  category: code
  task_type: feature
  depends_on:
  - '3.11'
  validation_criteria: "3.12.1: Each of the five layouts plans the stated move-then-ratio\
    \ sequence for three panes. test: `crates/gclient/tests/arrange.rs::each_layout_plans_moves_then_ratios_for_three_panes`\
    \ (new).\n3.12.2: Tiled uses `ceil(sqrt N)` rows and even ratios for five panes.\
    \ test: `crates/gclient/tests/arrange.rs::tiled_uses_ceil_sqrt_rows_and_even_ratios`\
    \ (new).\n3.12.3: A one-pane or local tab sends nothing and says so. test: `crates/gclient/tests/arrange.rs::a_single_pane_tab_plans_nothing_and_says_so`\
    \ (new).\n3.12.4: The New Grid dialog clamps rows and columns to 1..=4, previews\
    \ them, and confirms as a `NewGrid` menu action. test: `crates/gclient/tests/arrange.rs::new_grid_dialog_clamps_to_one_through_four_and_confirms_dims`\
    \ (new).\n3.12.5: `create_grid` spawns rows\xD7cols shells with the stated placements\
    \ and evens the cells. test: `crates/gclient/tests/arrange.rs::create_grid_spawns_rows_times_cols_shells_and_evens_them`\
    \ (new).\n3.12.6: Both the Window menu and the pane context menu carry the five\
    \ arrange items and `new grid\u2026`. behavior: \"new grid\u2026\" in `crates/gclient/src/app/live_loop/menu/items.rs`."
  labels:
  - covers:gclient-chrome-refresh:3.12:3.12.1
  - covers:gclient-chrome-refresh:3.12:3.12.2
  - covers:gclient-chrome-refresh:3.12:3.12.3
  - covers:gclient-chrome-refresh:3.12:3.12.4
  - covers:gclient-chrome-refresh:3.12:3.12.5
  - covers:gclient-chrome-refresh:3.12:3.12.6
  tdd: true
  source_section: '3.12'
  implementation_domain: backend
- title: "Help \u203A Daemon and Help \u203A About Gobby dialogs"
  category: code
  task_type: feature
  depends_on:
  - '3.8'
  - '3.9'
  - '3.10'
  - '3.11'
  - '3.12'
  validation_criteria: "3.13.1: About is 72\xD715 with the title ` about gobby`, `esc\
    \ close` in accent at the right, the goblin 29\xD714 at left, and the stated rows\
    \ in their stated roles. test: `crates/gclient/tests/info_dialogs.rs::about_dialog_is_72_by_15_with_the_goblin_and_the_stated_rows`\
    \ (new).\n3.13.2: Daemon shows url, both versions, health, the last roster refresh\
    \ age and the startup summary. test: `crates/gclient/tests/info_dialogs.rs::daemon_dialog_shows_url_versions_health_and_last_roster_refresh`\
    \ (new).\n3.13.3: Help lists `keys`, `alerts\u2026`, `daemon`, `about gobby` in\
    \ that order, and both entries open their dialogs. test: `crates/gclient/tests/info_dialogs.rs::help_menu_ends_with_about_gobby_and_both_entries_open`\
    \ (new).\n3.13.4: Esc, Enter and `q` close either dialog; other keys are consumed.\
    \ test: `crates/gclient/tests/info_dialogs.rs::info_dialogs_close_on_esc_enter_and_q`\
    \ (new).\n3.13.5: The renderer carries the native header. file: `crates/gclient/src/ui/dialogs/info.rs`."
  labels:
  - covers:gclient-chrome-refresh:3.13:3.13.1
  - covers:gclient-chrome-refresh:3.13:3.13.2
  - covers:gclient-chrome-refresh:3.13:3.13.3
  - covers:gclient-chrome-refresh:3.13:3.13.4
  - covers:gclient-chrome-refresh:3.13:3.13.5
  tdd: true
  source_section: '3.13'
  implementation_domain: fullstack
- title: Empty tab
  category: code
  task_type: feature
  depends_on:
  - '3.6'
  - '3.8'
  validation_criteria: "3.14.1: The empty tab shows the dimmed goblin above `No pane\
    \ open.` and the three ways out, with live chords. file: `crates/gclient/tests/fixtures/screens/empty_workspace.txt`.\n\
    3.14.2: A short area drops the goblin and keeps the four text rows, with no exclamation\
    \ mark. test: `crates/gclient/src/ui/panes/tests.rs::empty_state_names_the_next_step_without_exclamation`\
    \ (existing, extended to assert the three rows and the dropped goblin at 60\xD7\
    10).\n3.14.3: The chords follow the keymap: a nested prefix and an `unset` binding\
    \ render as such. test: `crates/gclient/src/ui/panes/tests.rs::empty_state_chords_follow_the_live_keymap`\
    \ (new, in the existing inline test module).\n3.14.4: The projects-and-agents\
    \ golden shows the same empty tab. file: `crates/gclient/tests/fixtures/screens/projects_agents.txt`."
  labels:
  - covers:gclient-chrome-refresh:3.14:3.14.1
  - covers:gclient-chrome-refresh:3.14:3.14.2
  - covers:gclient-chrome-refresh:3.14:3.14.3
  - covers:gclient-chrome-refresh:3.14:3.14.4
  tdd: true
  source_section: '3.14'
  implementation_domain: frontend
- title: 'Keys overlay: the description is never cut'
  category: code
  task_type: feature
  depends_on:
  - '3.11'
  validation_criteria: "3.15.1: At 120, 80 and 56 columns every description is present\
    \ in full; a too-narrow column wraps rather than cuts. test: `crates/gclient/tests/keybind_help.rs::the_description_column_is_never_cut_at_narrow_widths`\
    \ (new).\n3.15.2: Names draw only when the longest row fits, for all rows or none.\
    \ test: `crates/gclient/tests/keybind_help.rs::the_keymap_name_draws_only_when_the_longest_row_fits`\
    \ (new).\n3.15.3: Hidden names stay searchable. test: `crates/gclient/tests/keybind_help.rs::name_stays_searchable_when_hidden`\
    \ (new).\n3.15.4: `help_lines` keeps its text, so the parity cases and the scroll\
    \ bound are unchanged. symbol: `help_lines`.\n3.15.5: The 120\xD740 golden is\
    \ unchanged except for 3.11's description. file: `crates/gclient/tests/fixtures/screens/help_dialog.txt`."
  labels:
  - covers:gclient-chrome-refresh:3.15:3.15.1
  - covers:gclient-chrome-refresh:3.15:3.15.2
  - covers:gclient-chrome-refresh:3.15:3.15.3
  - covers:gclient-chrome-refresh:3.15:3.15.4
  - covers:gclient-chrome-refresh:3.15:3.15.5
  tdd: true
  source_section: '3.15'
  implementation_domain: backend
- title: User guide
  category: docs
  task_type: feature
  depends_on:
  - '3.1'
  - 3.2a
  - 3.2b
  - '3.3'
  - 3.4a
  - 3.4b
  - '3.5'
  - '3.6'
  - '3.7'
  - '3.8'
  - '3.9'
  - '3.10'
  - '3.11'
  - '3.12'
  - '3.13'
  - '3.14'
  - '3.15'
  validation_criteria: "3.16.1: The layout diagram shows the menu bar row, tab row,\
    \ pane area and status row, and the prose names all seven menus and their items.\
    \ behavior: \"Gobby  File  Edit  View  Window  Agent  Help\" in `docs/guides/gclient-user-guide.md`.\n\
    3.16.2: \xA7What a terminal is called has two rungs and no `shell` literal. behavior:\
    \ \"The command in its foreground\" in `docs/guides/gclient-user-guide.md`.\n\
    3.16.3: The keybinding table reads `Add a workspace` and the context-menu table\
    \ has no `[Menu]` row. behavior: \"Add a workspace\" in `docs/guides/gclient-user-guide.md`.\n\
    3.16.4: The reconnect section describes the splash stages, the status segment,\
    \ the URL toast and the frozen panes. behavior: \"retry in\" in `docs/guides/gclient-user-guide.md`.\n\
    3.16.5: The index row still resolves to the guide. file: `docs/guides/README.md`."
  labels:
  - covers:gclient-chrome-refresh:3.16:3.16.1
  - covers:gclient-chrome-refresh:3.16:3.16.2
  - covers:gclient-chrome-refresh:3.16:3.16.3
  - covers:gclient-chrome-refresh:3.16:3.16.4
  - covers:gclient-chrome-refresh:3.16:3.16.5
  tdd: false
  source_section: '3.16'
  assigned_agent: tech-writer
- title: One tagline everywhere
  category: docs
  task_type: feature
  depends_on: []
  validation_criteria: '4.1.1: `gobby --help` and the package docstring open with
    `Gobby - fleet management for AI coding agents.` file: `src/gobby/cli/__init__.py`.
    file: `src/gobby/__init__.py`.

    4.1.2: Both service templates describe the daemon as `Gobby Daemon - fleet management
    for AI coding agents`. file: `src/gobby/install/shared/services/gobby-daemon.service.j2`.
    file: `src/gobby/install/shared/services/gobby-daemon.task.xml.j2`.

    4.1.3: The README hero, its lead paragraph, the root `package.json` description
    and the `pyproject.toml` description carry the line; the loop sentence stays in
    the README body. file: `README.md`. file: `package.json`. file: `pyproject.toml`.

    4.1.4: `AGENTS.md`, `ROADMAP.md`, both architecture docs and `ONBOARDING.md` open
    with the line and keep their mechanism lists. file: `AGENTS.md`. file: `ROADMAP.md`.
    file: `docs/architecture/index.md`. file: `docs/architecture/architecture.md`.
    file: `ONBOARDING.md`.

    4.1.5: The default agent persona states the line and `web/index.html` carries
    it as the meta description. file: `src/gobby/install/shared/workflows/agents/default.yaml`.
    file: `web/index.html`.

    4.1.6: `rg -n "local-first daemon|Local-first daemon|control plane for AI coding|unify
    your AI coding|Walk away. End with a PR" --glob ''!tests/**'' --glob ''!web/src/**/__tests__/**''
    --glob ''!.gobby/**''` matches only `SECURITY.md` and the README loop sentence.
    behavior: "local-first daemon" in `SECURITY.md`.'
  labels:
  - covers:gclient-chrome-refresh:4.1:4.1.1
  - covers:gclient-chrome-refresh:4.1:4.1.2
  - covers:gclient-chrome-refresh:4.1:4.1.3
  - covers:gclient-chrome-refresh:4.1:4.1.4
  - covers:gclient-chrome-refresh:4.1:4.1.5
  - covers:gclient-chrome-refresh:4.1:4.1.6
  tdd: false
  source_section: '4.1'
  assigned_agent: tech-writer
```
