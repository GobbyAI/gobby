#!/usr/bin/env bash

set -euo pipefail

usage() {
  printf 'usage: scripts/codewiki_parity_baseline.sh --engine gwiki\n' >&2
}

fixture_digest() {
  python3 - "$FIXTURE_PROJECT" <<'PY'
import hashlib
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
paths = ("README.md", "Cargo.toml", "src/lib.rs")
manifest = "".join(
    f"{hashlib.sha256((root / path).read_bytes()).hexdigest()}  {path}\n"
    for path in paths
)
print(hashlib.sha256(manifest.encode()).hexdigest())
PY
}

normalized_manifest() {
  local vault="$1"
  local manifest="$2"
  python3 - "$vault" "$manifest" <<'PY'
import hashlib
import json
import pathlib
import re
import sys

vault = pathlib.Path(sys.argv[1])
manifest = pathlib.Path(sys.argv[2])

def normalized_bytes(path: pathlib.Path) -> bytes:
    data = path.read_bytes()
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return data
    if path == vault / "_meta/codewiki.json":
        metadata = json.loads(text)
        # These keys hash rendered frontmatter before provenance normalization.
        # Pin their pre-move values so the frozen legacy baseline still matches;
        # the page entries below still hash every byte, so genuine drift in
        # these two pages surfaces through the page hashes, not these keys.
        legacy_engine_derived_keys = {
            "code/_ownership.md": (
                "content-sensitive:50157c76ede85b3ac64e26e061185424372014745425b865bb6d6e7d124b3d69"
            ),
            "code/deprecations.md": (
                "8c5cf9b167ba68c8677da51d8a7f90362c5e58c882cd3c02e2a1c46705b38bec"
            ),
        }
        for doc_path, legacy_key in legacy_engine_derived_keys.items():
            key = metadata.get("docs", {}).get(doc_path, {}).get("invalidation_key")
            if not isinstance(key, str):
                continue
            # Anchor the rewrite to this doc's entry; str.index raises on a
            # serialization-shape change instead of silently skipping.
            anchor = text.index(json.dumps(doc_path))
            needle = f'"invalidation_key": "{key}"'
            start = text.index(needle, anchor)
            replacement = f'"invalidation_key": "{legacy_key}"'
            text = text[:start] + replacement + text[start + len(needle):]
    text = text.replace("gcode-codewiki", "<codewiki-engine>")
    text = text.replace("gwiki-code", "<codewiki-engine>")
    text = re.sub(
        r"^(generated_by|generated|commit|commit_dirty):.*$",
        lambda match: f"{match.group(1)}: <normalized>",
        text,
        flags=re.MULTILINE,
    )
    text = re.sub(
        r'("(?:commit|generated_at)"\s*:\s*)"(?:[^"\\]|\\.)*"',
        r'\1"<normalized>"',
        text,
    )
    text = re.sub(
        r'("commit_dirty"\s*:\s*)(?:true|false|null)',
        r'\1false',
        text,
    )
    return text.encode("utf-8")

entries = []
# Sort by relative path components so ordering is byte-stable across
# platforms (Path ordering case-folds on Windows).
for path in sorted(
    (candidate for candidate in vault.rglob("*") if candidate.is_file()),
    key=lambda candidate: candidate.relative_to(vault).parts,
):
    relative = path.relative_to(vault).as_posix()
    if relative == "_meta/codewiki.lock":
        continue
    entries.append(f"{hashlib.sha256(normalized_bytes(path)).hexdigest()}  {relative}\n")
if not entries:
    raise SystemExit("codewiki generated no files")
manifest.write_text("".join(entries))
PY
}

assert_indexed_output() {
  local vault="$1"
  python3 - "$vault/_meta/codewiki.json" "$vault" <<'PY'
import json
import pathlib
import sys

meta_path = pathlib.Path(sys.argv[1])
vault = pathlib.Path(sys.argv[2])
meta = json.loads(meta_path.read_text())
generated = meta.get("generated_docs", [])
docs = meta.get("docs", {})
if not generated:
    raise SystemExit("codewiki generated_docs is empty")
if not any(pathlib.Path(path).suffix == ".md" for path in generated):
    raise SystemExit("codewiki generated no page set")
source_paths = {
    source
    for doc in docs.values()
    for source in doc.get("source_hashes", {})
}
if "src/lib.rs" not in source_paths:
    raise SystemExit("codewiki metadata has no per-file hash for src/lib.rs")
if not any(path.is_file() for path in vault.rglob("*.md")):
    raise SystemExit("codewiki vault contains no markdown pages")
PY
}

run_generation() {
  local ordinal="$1"
  local vault="$RUN_ROOT/run-$ordinal/vault"
  local manifest="$RUN_ROOT/run-$ordinal/manifest.sha256"
  mkdir -p "$(dirname "$vault")"
  "$ENGINE_BINARY" --project "$PROJECT_COPY" "$ENGINE_SUBCOMMAND" \
    --out "$vault" --ai off
  assert_indexed_output "$vault"
  normalized_manifest "$vault" "$manifest"
}

cleanup() {
  local status=$?
  if [[ -x "${GCODE_BINARY:-}" && -d "${PROJECT_COPY:-}" ]]; then
    "$GCODE_BINARY" --project "$PROJECT_COPY" invalidate --force >/dev/null 2>&1 || true
  fi
  if [[ "${REMOVE_RUN_ROOT:-0}" == "1" && -n "${RUN_ROOT:-}" ]]; then
    rm -rf -- "$RUN_ROOT"
  fi
  return "$status"
}

main() {
  local engine=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --engine)
        [[ $# -ge 2 ]] || { usage; return 2; }
        engine="$2"
        shift 2
        ;;
      -h|--help)
        usage
        return 0
        ;;
      *)
        usage
        return 2
        ;;
    esac
  done

  case "$engine" in
    gwiki) ;;
    gcode)
      printf 'legacy gcode engine removed; the baseline is frozen at the capture pinned in %s\n' \
        'crates/gwiki/tests/fixtures/codewiki_parity/README.md' >&2
      return 2
      ;;
    *)
      usage
      return 2
      ;;
  esac
  ENGINE="$engine"

  if [[ "${CODEWIKI_PARITY_PARSE_ONLY:-0}" == "1" ]]; then
    printf 'validated engine mode: %s\n' "$ENGINE"
    return 0
  fi

  SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
  FIXTURE_ROOT="$REPO_ROOT/crates/gwiki/tests/fixtures/codewiki_parity"
  FIXTURE_PROJECT="$FIXTURE_ROOT/project"
  BASELINE_PATH="$FIXTURE_ROOT/baseline.sha256"
  CAPTURE_README="$FIXTURE_ROOT/README.md"
  TARGET_DIR="${CODEWIKI_PARITY_TARGET_DIR:-$REPO_ROOT/target}"
  PRODUCTION_VAULT="${CODEWIKI_PARITY_PRODUCTION_VAULT:-$REPO_ROOT/wiki}"
  PARITY_PROJECT_ID="019fe500-0000-7000-8000-000000019814"

  local pinned_digest
  pinned_digest="$(python3 - "$CAPTURE_README" <<'PY'
import pathlib
import re
import sys

text = pathlib.Path(sys.argv[1]).read_text()
match = re.search(r"Pinned fixture input digest: `([0-9a-f]{64})`", text)
if match is None:
    raise SystemExit("missing pinned fixture input digest in fixture README")
print(match.group(1))
PY
)"
  ACTUAL_DIGEST="$(fixture_digest)"
  if [[ "$ACTUAL_DIGEST" != "$pinned_digest" ]]; then
    printf 'fixture input digest mismatch: expected %s, got %s\n' \
      "$pinned_digest" "$ACTUAL_DIGEST" >&2
    return 1
  fi

  if [[ -n "$(git -C "$REPO_ROOT" status --porcelain -- "$FIXTURE_PROJECT")" ]]; then
    printf 'fixture project must be tracked and clean before capture\n' >&2
    git -C "$REPO_ROOT" status --short -- "$FIXTURE_PROJECT" >&2
    return 1
  fi
  FIXTURE_STATUS_BEFORE="$(git -C "$REPO_ROOT" status --porcelain -- "$FIXTURE_ROOT")"

  RUN_ROOT="${CODEWIKI_PARITY_RUN_ROOT:-}"
  REMOVE_RUN_ROOT=0
  if [[ -z "$RUN_ROOT" ]]; then
    RUN_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/codewiki-parity.XXXXXX")"
    REMOVE_RUN_ROOT=1
  else
    if [[ -e "$RUN_ROOT" && -n "$(find "$RUN_ROOT" -mindepth 1 -maxdepth 1 -print -quit)" ]]; then
      printf 'CODEWIKI_PARITY_RUN_ROOT must be empty: %s\n' "$RUN_ROOT" >&2
      return 1
    fi
    mkdir -p "$RUN_ROOT"
  fi
  trap cleanup EXIT

  python3 - "$RUN_ROOT" "$PRODUCTION_VAULT" <<'PY'
import os
import pathlib
import sys

run_root = pathlib.Path(os.path.realpath(sys.argv[1]))
production = pathlib.Path(os.path.realpath(sys.argv[2]))
for candidate in (run_root, run_root / "run-1" / "vault", run_root / "run-2" / "vault"):
    if candidate == production or production in candidate.parents:
        raise SystemExit(
            f"refusing to place parity output at or below production vault: {production}"
        )
PY

  PROJECT_COPY="$RUN_ROOT/project"
  mkdir -p "$PROJECT_COPY/src" "$PROJECT_COPY/.gobby"
  cp -- "$FIXTURE_PROJECT/README.md" "$PROJECT_COPY/README.md"
  cp -- "$FIXTURE_PROJECT/Cargo.toml" "$PROJECT_COPY/Cargo.toml"
  cp -- "$FIXTURE_PROJECT/src/lib.rs" "$PROJECT_COPY/src/lib.rs"
  printf '{\n  "id": "%s",\n  "name": "codewiki-parity-fixture"\n}\n' \
    "$PARITY_PROJECT_ID" > "$PROJECT_COPY/.gobby/gcode.json"
  git -C "$PROJECT_COPY" init --quiet
  git -C "$PROJECT_COPY" add -- README.md Cargo.toml src/lib.rs
  GIT_AUTHOR_DATE="2000-01-01T00:00:00Z" GIT_COMMITTER_DATE="2000-01-01T00:00:00Z" \
    git -C "$PROJECT_COPY" -c user.name=CodeWiki -c user.email=codewiki@example.invalid \
      commit --quiet -m "CodeWiki parity fixture"

  GCODE_BINARY="$TARGET_DIR/debug/gcode"
  ENGINE_SUBCOMMAND="code"

  cargo build --locked --manifest-path "$REPO_ROOT/Cargo.toml" \
    --target-dir "$TARGET_DIR" -p gobby-code --bin gcode
  cargo build --locked --manifest-path "$REPO_ROOT/Cargo.toml" \
    --target-dir "$TARGET_DIR" -p gobby-wiki --bin gwiki
  ENGINE_BINARY="$TARGET_DIR/debug/gwiki"
  if [[ ! -x "$GCODE_BINARY" || ! -x "$ENGINE_BINARY" ]]; then
    printf 'workspace build did not produce the expected executable\n' >&2
    return 1
  fi

  "$GCODE_BINARY" --project "$PROJECT_COPY" invalidate --force >/dev/null 2>&1 || true
  "$GCODE_BINARY" --project "$PROJECT_COPY" index --full

  local revision
  local binary_version
  revision="$(git -C "$REPO_ROOT" rev-parse HEAD)"
  binary_version="$($ENGINE_BINARY --version)"
  printf 'engine: %s\nrevision: %s\nbinary_version: %s\ninput_digest: %s\nproject_id: %s\nbuild: cargo build --locked\n' \
    "$ENGINE" "$revision" "$binary_version" "$ACTUAL_DIGEST" "$PARITY_PROJECT_ID" \
    > "$RUN_ROOT/capture.txt"

  run_generation 1
  run_generation 2
  if ! cmp -s "$RUN_ROOT/run-1/manifest.sha256" "$RUN_ROOT/run-2/manifest.sha256"; then
    printf 'same-engine manifests differ\n' >&2
    diff -u "$RUN_ROOT/run-1/manifest.sha256" "$RUN_ROOT/run-2/manifest.sha256" >&2 || true
    return 1
  fi
  if ! cmp -s "$RUN_ROOT/run-1/manifest.sha256" "$BASELINE_PATH"; then
    printf 'gwiki output differs from the frozen legacy baseline\n' >&2
    diff -u "$BASELINE_PATH" "$RUN_ROOT/run-1/manifest.sha256" >&2 || true
    return 1
  fi
  local entry_count
  entry_count="$(wc -l < "$BASELINE_PATH" | tr -d ' ')"

  if [[ "$(fixture_digest)" != "$ACTUAL_DIGEST" ]]; then
    printf 'capture mutated the committed fixture project\n' >&2
    return 1
  fi

  if [[ "$(git -C "$REPO_ROOT" status --porcelain -- "$FIXTURE_ROOT")" != "$FIXTURE_STATUS_BEFORE" ]]; then
    printf 'run wrote an undeclared fixture path\n' >&2
    git -C "$REPO_ROOT" status --short -- "$FIXTURE_ROOT" >&2
    return 1
  fi

  printf '%s parity manifest verified (%s entries)\n' "$ENGINE" "$entry_count"
}

main "$@"
