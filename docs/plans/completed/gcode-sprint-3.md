# gcode Sprint 3: Release + Integration

## Context

Sprints 1-2 delivered a fully functional Rust CLI (`gcode`) with tree-sitter parsing, PostgreSQL indexing, FTS5 search, Neo4j graph, Qdrant vectors, llama-cpp-2 embeddings, RRF hybrid search, Fernet decryption, and savings tracking. 31 tests pass, build clean.

Sprint 3 makes gcode the primary code index interface, replacing the gobby-code MCP server. Agents (both Claude Code subagents and gobby-spawned agents) get steered to `gcode` via Bash instead of MCP progressive discovery. The gobby-code MCP is gated behind a feature flag for transition. Daemon retains SymbolSummarizer (needs LLM) and uses gcode for faster indexing.

**COMPLETED:** gcode extracted to standalone repo at `github.com/GobbyAI/gobby-code` (repo: gobby-code, binary: gcode). Cargo feature gates, CI workflow (6 targets), and README added. 31 tests pass both with and without embeddings.

## Architecture Decisions

| Decision | Choice | Why |
|----------|--------|-----|
| Repository | Standalone at GobbyAI/gobby-code | Zero source coupling to gobby; follows gsqz precedent |
| Cargo feature | `embeddings` (default on) wrapping `llama-cpp-2` | CI Linux can't use Metal; `--no-default-features` skips llama.cpp |
| Install method | GitHub releases (primary), `cargo install --git` (fallback) | Standalone repo; not on crates.io yet |
| Agent steering | gcode via Bash (prompt hints + PATH) | Simpler than MCP discovery (1 call vs 4), faster, standalone |
| MCP gating | Feature flag `use_native_code_index: bool = True` | True = Python MCP (current), False = disabled. Default True for now, flip to False once gcode proven |
| Daemon indexing | Maintenance loop calls `gcode index` | Rust indexing is dramatically faster than Python |
| AFTER_TOOL trigger | Calls `gcode index --files` via subprocess | Replaces Python CodeIndexTrigger |
| Post-commit hook | gcode fallback when daemon unavailable | Standalone indexing in worktrees/clones |
| SymbolSummarizer | Stays in Python maintenance loop | Needs LLM service; gcode reads summaries from DB |

## Phase 1: Rust — Cargo Feature + CI

### 1.1 Make llama-cpp-2 an optional Cargo feature [category: code]

Target: `GobbyAI/gobby-code` repo — `Cargo.toml`, `src/search/semantic.rs`

**Cargo.toml:**
```toml
[features]
default = ["embeddings"]
embeddings = ["dep:llama-cpp-2"]

[dependencies]
llama-cpp-2 = { version = "0.1", features = ["metal"], optional = true }
```

**semantic.rs:**
- Wrap llama-cpp-2 imports, `EmbeddingModelInner`, `EMBEDDING_MODEL`, `ensure_model_loaded()`, `embed_text()`, `embed_texts()` in `#[cfg(feature = "embeddings")]`
- Add `#[cfg(not(feature = "embeddings"))]` stubs returning `None`/empty
- `vector_search()`, `upsert_vectors()`, `semantic_search()`, `symbol_embed_text()` stay unconditional (graceful degradation)

**Verify:** `cargo build --no-default-features` compiles without cmake. `cargo test` passes both ways.

### 1.2 GitHub Actions CI workflow [category: config]

Target: `.github/workflows/gcode-build.yml` (new file)

Build matrix (6 targets, matching gsqz):
- `macos-latest` / `aarch64-apple-darwin` / `--features embeddings` (install cmake)
- `macos-13` / `x86_64-apple-darwin` / `--features embeddings` (install cmake)
- `ubuntu-latest` / `x86_64-unknown-linux-gnu` / `--no-default-features`
- `ubuntu-latest` / `aarch64-unknown-linux-gnu` / `--no-default-features` (cross)
- `windows-latest` / `x86_64-pc-windows-msvc` / `--no-default-features`
- `windows-latest` / `aarch64-pc-windows-msvc` / `--no-default-features` (cross)

Steps per job: checkout → Rust toolchain + target → cmake (macOS only) → cargo build --release → cargo test (skip cross) → tar/zip artifact → upload artifact.

Windows builds use `--no-default-features` (no llama-cpp-2/Metal). Binary is `gcode.exe`.

Path filter: `paths: ['rust/gcode/**']`. Triggered on push to main and PRs.

**Release publishing:** Add a separate job that runs on tag push (`v*`). Downloads all build artifacts, creates a GitHub Release, and uploads binaries as release assets:

```yaml
release:
  if: startsWith(github.ref, 'refs/tags/v')
  needs: build
  runs-on: ubuntu-latest
  steps:
    - uses: actions/download-artifact@v4
    - name: Create Release
      uses: softprops/action-gh-release@v2
      with:
        files: |
          gcode-aarch64-apple-darwin/gcode-aarch64-apple-darwin.tar.gz
          gcode-x86_64-apple-darwin/gcode-x86_64-apple-darwin.tar.gz
          gcode-x86_64-unknown-linux-gnu/gcode-x86_64-unknown-linux-gnu.tar.gz
          gcode-aarch64-unknown-linux-gnu/gcode-aarch64-unknown-linux-gnu.tar.gz
          gcode-x86_64-pc-windows-msvc/gcode-x86_64-pc-windows-msvc.zip
          gcode-aarch64-pc-windows-msvc/gcode-aarch64-pc-windows-msvc.zip
        generate_release_notes: true
```

Each build job packages the binary as `gcode-{target}.tar.gz` (or `.zip` for Windows) before uploading.

## Phase 2: Python — Installation

### 2.1 Add `_install_gcode()` to install_setup.py [category: code]

Target: `src/gobby/cli/install_setup.py`

Follow the `_install_gsqz()` pattern (lines 416-511) but build from source:

**Constants** (after line 214):
```python
_GCODE_VERSION_STAMP = ".gcode-version"
_GCODE_BIN_NAME = "gcode.exe" if sys.platform == "win32" else "gcode"
```

**New constants** (alongside gsqz constants):
```python
_GCODE_RELEASE_URL = "https://github.com/GobbyAI/gobby-code/releases/latest/download/gcode-{target}.tar.gz"
_GCODE_VERSIONED_RELEASE_URL = "https://github.com/GobbyAI/gobby-code/releases/download/v{version}/gcode-{target}.tar.gz"
_GCODE_TARGETS = _GSQZ_TARGETS  # Same platform mapping
```

**`_install_gcode(force=False)` function — fallback chain like gsqz:**
1. Check `~/.gobby/bin/gcode` exists + version stamp matches current gobby version → skip
2. Detect platform target triple (reuse `_GSQZ_TARGETS` mapping)
3. **Strategy 1: GitHub release download** — `_install_gcode_from_github(bin_dir, target, version)`. Same pattern as `_install_gsqz_from_github()` but with gcode release URL. Fast, no deps.
4. **Strategy 2: Build from source** — Find repo root (walk up from `__file__` for `rust/gcode/Cargo.toml`). Run `cargo build --release --manifest-path ...`. Copy binary to `~/.gobby/bin/gcode`. Only if cargo on PATH.
5. chmod 0o755, write version stamp, ensure PATH
6. Return result dict: `{"installed": bool, "upgraded": bool, "version": str, "method": str}`

GitHub download is preferred (fast, no cargo needed). Cargo build is fallback for dev environments or when releases aren't published yet.

**Wire into `run_daemon_setup()`** after the gsqz block (~line 184):
```python
try:
    gcode_result = _install_gcode()
    if gcode_result.get("installed"):
        verb = "Upgraded" if gcode_result.get("upgraded") else "Installed"
        click.echo(f"{verb} gcode {gcode_result.get('version', '')} (code index CLI)")
    elif gcode_result.get("skipped"):
        click.echo("gcode already installed and up to date")
    else:
        reason = gcode_result.get("reason", "unknown error")
        click.echo(f"Warning: Failed to install gcode: {reason}")
except Exception as e:
    click.echo(f"Warning: Failed to install gcode: {e}")
```

## Phase 3: Python — Replace Indexing with gcode

### 3.1 Replace CodeIndexTrigger with gcode subprocess [category: code]

Target: `src/gobby/code_index/trigger.py`

The current `CodeIndexTrigger` (lines 21-99) debounces file changes and calls the Python `CodeIndexer`. Replace the flush logic to call `gcode index --files` via subprocess instead.

In `_flush()` (line 78), replace `self._indexer.index_files(...)` with:
```python
async def _flush(self) -> None:
    """Flush pending files by calling gcode."""
    files = list(self._pending.keys())
    self._pending.clear()
    if not files:
        return

    gcode_bin = Path.home() / ".gobby" / "bin" / "gcode"
    if not gcode_bin.exists():
        logger.warning("gcode not installed — skipping incremental index. Run `gobby install`.")
        return

    try:
        proc = await asyncio.create_subprocess_exec(
            str(gcode_bin), "index", "--files", *files, "--quiet",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.wait(), timeout=30)
    except Exception as e:
        logger.warning(f"gcode index failed: {e}")
```

No Python fallback. If gcode isn't installed, warn and skip — user needs to run `gobby install`.

### 3.2 Replace maintenance loop indexing with gcode [category: code]

Target: `src/gobby/code_index/maintenance.py`

In `_run_maintenance()`, replace `indexer.index_directory()` with a `gcode index` subprocess call:

```python
async def _run_maintenance(
    indexer: CodeIndexer,
    summarizer: SymbolSummarizer | None = None,
) -> None:
    projects = indexer.storage.list_indexed_projects()
    gcode_bin = Path.home() / ".gobby" / "bin" / "gcode"

    for project in projects:
        if not project.root_path:
            continue

        # Index with gcode
        if not gcode_bin.exists():
            logger.warning("gcode not installed — skipping maintenance index. Run `gobby install`.")
            continue

        try:
            proc = await asyncio.create_subprocess_exec(
                str(gcode_bin), "index", str(project.root_path),
                "--quiet",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(proc.wait(), timeout=120)
        except Exception as e:
            logger.warning(f"Maintenance reindex failed for {project.id}: {e}")

        # Generate summaries (Python only — needs LLM)
        if summarizer is not None:
            try:
                unsummarized = indexer.storage.get_symbols_without_summaries(
                    project_id=project.id, limit=50
                )
                if unsummarized:
                    def source_reader(fp: str, bs: int, be: int) -> str | None:
                        full = Path(project.root_path) / fp
                        try:
                            return full.read_bytes()[bs:be].decode("utf-8", errors="replace")
                        except (OSError, IndexError):
                            return None

                    summaries = await summarizer.generate_summaries(unsummarized, source_reader)
                    for sym_id, text in summaries.items():
                        indexer.storage.update_symbol_summary(sym_id, text)
                    if summaries:
                        logger.debug(f"Generated {len(summaries)} summaries for {project.id}")
            except Exception as e:
                logger.warning(f"Summary generation failed for {project.id}: {e}")
```

**Update loop signature** to accept optional `summarizer` parameter.

**Update `runner_lifecycle.py`** (~line 199) to pass summarizer. Create `symbol_summarizer` on `GobbyRunner` (it doesn't exist yet):

In `runner.py` init, after code_indexer setup:
```python
self.symbol_summarizer: SymbolSummarizer | None = None
if self.config.code_index.summary_enabled and self.llm_service:
    from gobby.code_index.summarizer import SymbolSummarizer
    self.symbol_summarizer = SymbolSummarizer(self.llm_service, self.config.code_index)
```

### 3.3 Update post-commit hook with gcode fallback [category: code]

Target: `src/gobby/cli/installers/git_hooks.py` (lines 162-192)

Replace the `HOOK_TEMPLATES["post-commit"]` content. Keep daemon HTTP API as primary (fast, async), add gcode CLI fallback when daemon is unreachable:

```bash
CHANGED_FILES=$(git diff-tree --no-commit-id --name-only -r HEAD 2>/dev/null)
if [ -n "$CHANGED_FILES" ]; then
    INDEXED=false

    # Strategy 1: Daemon HTTP API (fast, async)
    if command -v gobby >/dev/null 2>&1; then
        # ... existing daemon curl logic ...
        if curl -s -X POST ... >/dev/null 2>&1; then
            INDEXED=true
        fi
    fi

    # Strategy 2: gcode CLI fallback (standalone, no daemon needed)
    if [ "$INDEXED" = "false" ]; then
        GCODE="${HOME}/.gobby/bin/gcode"
        if [ -x "$GCODE" ]; then
            $GCODE index --files $CHANGED_FILES --quiet &
        fi
    fi
fi
```

## Phase 4: Python — Agent Steering + MCP Gating

### 4.1 Subagent PATH injection [category: code]

Target: `src/gobby/agents/spawners/base.py` (line 113)

In `make_spawn_env()`, before `return spawn_env`, add `~/.gobby/bin` to PATH:
```python
# Ensure ~/.gobby/bin is on PATH (gcode, gsqz)
gobby_bin = str(Path.home() / ".gobby" / "bin")
current_path = spawn_env.get("PATH", "")
if gobby_bin not in current_path.split(os.pathsep):
    spawn_env["PATH"] = f"{gobby_bin}{os.pathsep}{current_path}"
```

### 4.2 Agent prompt hints for gcode [category: code]

Target: `src/gobby/agents/context.py`

Add gcode usage hint that gets injected into subagent prompts when the binary exists:

```python
GCODE_HINT = """## Code Index (gcode)
You have `gcode` on PATH for fast code symbol search and navigation:
- `gcode search "query"` — hybrid search (FTS + semantic + graph)
- `gcode outline path/to/file.py` — symbol outline (cheaper than reading whole file)
- `gcode symbol <id>` — get source for a specific symbol by ID
- `gcode callers <name>` — find callers of a function
- `gcode blast-radius <name>` — transitive impact analysis
Use `gcode --help` for all commands. Prefer gcode over reading entire files."""
```

In `format_injected_prompt()`, prepend hint when `~/.gobby/bin/gcode` exists:
```python
gcode_path = Path.home() / ".gobby" / "bin" / "gcode"
if gcode_path.exists():
    result = GCODE_HINT + "\n\n" + result
```

### 4.3 Update code-index rule to steer to gcode [category: code]

Target: `src/gobby/install/shared/workflows/rules/code-index/block-grep-indexed-files.yaml`

The `intercept-grep-with-code-index` rule currently intercepts Grep calls and proxies them through gobby-code MCP (`search_content`), then blocks with a message pointing to gobby-code tools. Update to steer to gcode instead:

```yaml
rules:
  intercept-grep-with-code-index:
    description: "Intercept Grep calls in indexed projects — steer to gcode CLI"
    event: before_tool
    enabled: true
    priority: 50
    when: >
      event.data.get('tool_name', '').lower() in ('grep_search', 'grep')
    effects:
      - type: block
        message: >
          Grep is blocked — this project has a code index.
          Use gcode via Bash instead:
          - `gcode search-content "query"` — full-text search across file content
          - `gcode search "query"` — hybrid symbol search (FTS + semantic + graph)
          - `gcode outline path/to/file.py` — symbol outline for a file
          - `gcode tree` — file tree with symbol counts
```

Remove the `mcp_call` effect (no longer proxying through gobby-code). The rule now just blocks and steers to gcode CLI. This works for all agents — both MCP-capable sessions and Bash-only subagents.

Also update these related files that reference gobby-code:

**`src/gobby/install/shared/workflows/rules/code-index/nudge-on-large-read.yaml`** — After large file reads, nudges agents toward gobby-code tools. Update message to suggest `gcode outline`, `gcode search`, `gcode symbol` instead.

**`src/gobby/install/shared/skills/code-index/SKILL.md`** — Auto-injected skill (`alwaysApply: true`) that teaches agents about gobby-code MCP tools. Rewrite to document gcode CLI commands instead:
- `gcode search "query"` (hybrid search)
- `gcode search-text "query"` (FTS symbol names)
- `gcode search-content "query"` (FTS file content)
- `gcode outline path/to/file.py` (symbol outline)
- `gcode symbol <id>` / `gcode symbols <id1> <id2>` (retrieval)
- `gcode callers <name>` / `gcode usages <name>` / `gcode blast-radius <name>` (impact)
- `gcode tree` (file tree)

**`src/gobby/install/shared/prompts/mcp/progressive-discovery.md`** — System prompt mentions gobby-code. Update the code search section to reference gcode CLI instead.

**`src/gobby/install/shared/workflows/rules/code-index/compress-large-reads.yaml`** — Uses `compress_output` effect with `compressor: code_index`. Rule is installed and enabled in DB, but the `compressor` field is ignored by `hook_manager.py` — it always uses the generic `OutputCompressor` with command-pattern matching. Fix:

1. **Route the `compressor` field** in `hook_manager.py` (line ~401): when `compression_cfg.get("compressor") == "code_index"`, use a `code_index` strategy instead of the default pattern-matching pipeline.

2. **Add `code_index` strategy to `OutputCompressor`** (or as a separate handler in `compression/`): extract the file path from the Read tool input, call `gcode outline <file>` via subprocess, return the outline as compressed output with a retrieval hint (`gcode symbol <id>`). Same architectural pattern as the other strategies — just one more entry, not a separate class.

3. **Update retrieval hints** in the compressed output to reference `gcode symbol <id>` / `gcode search "query"` instead of `call_tool("gobby-code", ...)`.

### 4.4 Gate gobby-code MCP behind feature flag [category: code]

Target: `src/gobby/config/code_index.py`, `src/gobby/servers/http.py`

**Add flag to CodeIndexConfig:**
```python
use_native_code_index: bool = Field(
    default=True,
    description="Use Python-native code index MCP server. Set False to disable "
    "(agents use gcode CLI instead).",
)
```

**Gate registration in `http.py`** (`_init_mcp_subsystems`, ~line 176):
```python
code_indexer = getattr(services, "code_indexer", None)
if code_indexer is not None and getattr(services, '_code_index_config', None) \
        and services._code_index_config.use_native_code_index:
    # ... existing create_code_registry() code ...
```

When `use_native_code_index=False`, the gobby-code MCP server simply doesn't register. Agents that try MCP discovery won't find it and will use gcode via Bash (steered by prompt hints).

Default is `True` for now (backward compat). Flip to `False` once gcode is proven in production.

## Phase 5: Verification

### 5.1 Manual verification checklist [category: manual]

1. `cargo build --no-default-features` — compiles without cmake
2. `cargo build` — compiles with embeddings
3. `cargo test` — all pass (both feature configs)
4. `gobby install` → gcode binary at `~/.gobby/bin/gcode`
5. Make a commit → post-commit hook runs gcode fallback when daemon down
6. Spawn a subagent → PATH includes `~/.gobby/bin`, prompt has gcode hints
7. Set `use_native_code_index: false` → gobby-code MCP not registered
8. After maintenance cycle: `SELECT id, summary FROM code_symbols WHERE summary IS NOT NULL LIMIT 5`
9. CI: push to branch → GitHub Actions builds all matrix targets

## Files Modified

| File | Change |
|------|--------|
| `rust/gcode/Cargo.toml` | Add `[features]`, make llama-cpp-2 optional |
| `rust/gcode/src/search/semantic.rs` | `#[cfg(feature = "embeddings")]` gates |
| `.github/workflows/gcode-build.yml` | New: CI build matrix (6 targets) + release publishing |
| `src/gobby/cli/install_setup.py` | Add `_install_gcode()`, wire into setup |
| `src/gobby/code_index/trigger.py` | Replace flush with gcode subprocess + fallback |
| `src/gobby/code_index/maintenance.py` | Replace indexing with gcode + add summarizer pass |
| `src/gobby/cli/installers/git_hooks.py` | Update post-commit hook with gcode fallback |
| `src/gobby/agents/spawners/base.py` | PATH injection in `make_spawn_env()` |
| `src/gobby/agents/context.py` | `GCODE_HINT` + injection in `format_injected_prompt()` |
| `src/gobby/install/shared/workflows/rules/code-index/block-grep-indexed-files.yaml` | Steer to gcode CLI instead of gobby-code MCP |
| `src/gobby/install/shared/workflows/rules/code-index/nudge-on-large-read.yaml` | Update nudge message to reference gcode |
| `src/gobby/install/shared/skills/code-index/SKILL.md` | Rewrite: document gcode CLI instead of MCP tools |
| `src/gobby/install/shared/prompts/mcp/progressive-discovery.md` | Update code search section to reference gcode |
| `src/gobby/compression/compressor.py` | Add `code_index` strategy (gcode outline) |
| `src/gobby/hooks/hook_manager.py` | Route `compressor` field from rule effects |
| `src/gobby/config/code_index.py` | Add `use_native_code_index` flag |
| `src/gobby/servers/http.py` | Gate gobby-code MCP registration on flag |
| `src/gobby/runner.py` | Create `symbol_summarizer` attribute |
| `src/gobby/runner_lifecycle.py` | Pass summarizer to maintenance loop |

## Not in Sprint 3

- Removing Python code index modules — kept as fallback; removal is follow-up
- Embedding parity verification
- Automated benchmarks
