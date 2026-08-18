#!/bin/bash
# Requires macOS system Bash 3.2 or newer.
set -Eeuo pipefail
umask 077

readonly CODEX_HOME_PATH="$HOME/.codex"
readonly RESET_BACKUP="$HOME/.codex.pre-sidecar-reset-2026-08-17"
readonly OLD_CODEX_LINK="$HOME/.local/bin/codex"
readonly EXPECTED_OLD_TARGET="$CODEX_HOME_PATH/packages/standalone/current/bin/codex"
readonly BREW_BIN="/opt/homebrew/bin/brew"
readonly CODEX_BIN="/opt/homebrew/bin/codex"
readonly GOBBY_REPO="/Users/josh/Projects/gobby"
readonly INDEX_DIR="$HOME/.gobby/cache/transcript-indexes"

declare -a TEMPORARY_FILES=()

die() {
    printf 'ERROR: %s\n' "$*" >&2
    exit 1
}

cleanup() {
    local status=$?
    local path

    for path in "${TEMPORARY_FILES[@]}"; do
        [[ -n "$path" ]] && rm -f -- "$path"
    done

    return "$status"
}

check_doctor() {
    local report_path="$1"

    if ! "$CODEX_BIN" doctor --json | tee "$report_path"; then
        die "Codex Doctor failed to produce a report."
    fi

    /usr/bin/jq -e '
        .checks["state.paths"] as $paths
        | .checks["state.rollout_db_parity"] as $parity
        | (
            $paths.details
            | [to_entries[]
               | select(.key | endswith("DB integrity"))
               | .value]
          ) as $db_integrity
        | (($db_integrity | length) > 0)
          and ($db_integrity | all(. == "ok"))
          and ($paths.status == "ok")
          and ($parity.status == "ok")
          and (
              ($paths.details["active rollout files"]
               | capture("^(?<count>[0-9]+)").count
               | tonumber) < 10000
          )
          and ($parity.details["rollout DB scan cap reached"] == "false")
          and ($parity.details["rollout DB stale rows"] == "0")
    ' "$report_path" >/dev/null || {
        /usr/bin/jq '{
            overallStatus,
            state_paths: .checks["state.paths"],
            rollout_parity: .checks["state.rollout_db_parity"]
        }' "$report_path"
        die "Codex Doctor acceptance checks failed."
    }
}

main() {
    local confirmation
    local resolved_codex
    local filename
    local skill_path
    local skill_name
    local plugin_state
    local plugin_id
    local gobby_status
    local service_name
    local doctor_before
    local events_file
    local thread_id
    local rollout_path
    local rollout_dir
    local normalized_rollout
    local rollout_hash
    local sidecar_path
    local doctor_after
    local attempt
    local -a plugins=(
        "documents@openai-primary-runtime"
        "pdf@openai-primary-runtime"
        "spreadsheets@openai-primary-runtime"
        "presentations@openai-primary-runtime"
        "template-creator@openai-primary-runtime"
        "sites@openai-bundled"
        "browser@openai-bundled"
        "visualize@openai-bundled"
    )

    trap cleanup EXIT

    [[ "$EUID" -ne 0 ]] || die "Run as your normal user, without sudo."
    [[ -d "$GOBBY_REPO/.git" ]] || die "Gobby repository is missing."
    [[ -d "$CODEX_HOME_PATH" ]] || die "$CODEX_HOME_PATH is missing."
    [[ ! -e "$RESET_BACKUP" ]] || die "Backup already exists: $RESET_BACKUP"
    [[ -L "$OLD_CODEX_LINK" ]] || die "$OLD_CODEX_LINK is not a symlink."
    [[ "$(readlink "$OLD_CODEX_LINK")" == "$EXPECTED_OLD_TARGET" ]] ||
        die "Unexpected Codex symlink target."
    [[ -x "$BREW_BIN" ]] || die "Homebrew is unavailable."
    "$BREW_BIN" list --cask codex >/dev/null ||
        die "Homebrew Codex cask is not installed."

    printf '\nThis will move:\n  %s\ninto:\n  %s\n\n' \
        "$CODEX_HOME_PATH" "$RESET_BACKUP"
    printf 'Type RESET CODEX to continue: '
    if ! IFS= read -r confirmation; then
        die "Confirmation input closed."
    fi
    [[ "$confirmation" == "RESET CODEX" ]] || die "Confirmation declined."

    cd "$GOBBY_REPO" || die "Could not enter the Gobby repository."

    printf '\nStopping Gobby while leaving its databases running...\n'
    uv run gobby stop || die "Gobby did not stop cleanly."

    local codex_process_pattern='(^|/)(codex|codex-code-mode-host)([[:space:]]|$)'
    if pgrep -f "$codex_process_pattern" >/dev/null; then
        printf '\nCodex processes remain:\n' >&2
        pgrep -fal "$codex_process_pattern" >&2 || true
        uv run gobby start || true
        die "Close every listed Codex process and rerun the script."
    fi

    printf '\nCreating recoverable backup...\n'
    mv "$CODEX_HOME_PATH" "$RESET_BACKUP" || die "Could not create the Codex backup."

    [[ -L "$OLD_CODEX_LINK" ]] ||
        die "Expected Codex symlink disappeared unexpectedly."
    unlink "$OLD_CODEX_LINK" || die "Could not remove the standalone Codex symlink."

    printf '\nReinstalling Homebrew Codex...\n'
    "$BREW_BIN" reinstall --cask codex || die "Homebrew Codex reinstall failed."
    hash -r

    [[ -x "$CODEX_BIN" ]] || die "Homebrew Codex binary is missing."
    resolved_codex="$(command -v codex)"
    [[ "$resolved_codex" == "$CODEX_BIN" ]] ||
        die "codex resolves to $resolved_codex, expected $CODEX_BIN"

    "$CODEX_BIN" --version

    printf '\nAuthenticate through the browser window...\n'
    "$CODEX_BIN" login || die "Codex login failed."
    "$CODEX_BIN" login status || die "Codex login status is unhealthy."

    printf '\nRestoring selected user-authored configuration...\n'
    for filename in config.toml AGENTS.md; do
        if [[ -f "$RESET_BACKUP/$filename" ]]; then
            cp -p "$RESET_BACKUP/$filename" "$CODEX_HOME_PATH/$filename"
        fi
    done

    if [[ -d "$RESET_BACKUP/rules" ]]; then
        /usr/bin/ditto "$RESET_BACKUP/rules" "$CODEX_HOME_PATH/rules"
    fi

    if [[ -d "$RESET_BACKUP/skills" ]]; then
        mkdir -p "$CODEX_HOME_PATH/skills"
        shopt -s nullglob dotglob

        for skill_path in "$RESET_BACKUP/skills"/*; do
            skill_name="${skill_path##*/}"
            [[ "$skill_name" == ".system" ]] && continue
            /usr/bin/ditto \
                "$skill_path" \
                "$CODEX_HOME_PATH/skills/$skill_name"
        done

        shopt -u nullglob dotglob
    fi

    printf '\nReinstalling previously installed plugins...\n'
    if ! plugin_state="$("$CODEX_BIN" plugin list --json)"; then
        die "Could not list installed Codex plugins."
    fi

    for plugin_id in "${plugins[@]}"; do
        if /usr/bin/jq -e --arg id "$plugin_id" \
            '.installed[]? | select(.pluginId == $id)' \
            <<<"$plugin_state" >/dev/null; then
            printf 'Already installed: %s\n' "$plugin_id"
        else
            "$CODEX_BIN" plugin add "$plugin_id" ||
                die "Could not install plugin $plugin_id."
            if ! plugin_state="$("$CODEX_BIN" plugin list --json)"; then
                die "Could not refresh installed Codex plugins."
            fi
        fi
    done

    printf '\nInstalling Gobby integration and restarting Gobby...\n'
    uv run gobby install --codex || die "Gobby Codex integration install failed."
    uv run gobby start || die "Gobby did not start cleanly."

    if ! gobby_status="$(uv run gobby status)"; then
        die "Gobby status failed."
    fi
    printf '%s\n' "$gobby_status"

    for service_name in PostgreSQL Qdrant FalkorDB; do
        if ! printf '%s\n' "$gobby_status" |
            /usr/bin/grep -Eq \
                "^[[:space:]]*${service_name}:[[:space:]]+healthy([[:space:](]|$)"; then
            die "$service_name is unhealthy."
        fi
    done

    doctor_before="$(mktemp "${TMPDIR:-/tmp}/codex-doctor-before.XXXXXX")"
    TEMPORARY_FILES+=("$doctor_before")
    check_doctor "$doctor_before"

    printf '\nCreating disposable Codex session through Gobby...\n'
    events_file="$(mktemp "${TMPDIR:-/tmp}/codex-reset-events.XXXXXX")"
    TEMPORARY_FILES+=("$events_file")

    if ! "$CODEX_BIN" exec \
        --json \
        --sandbox read-only \
        -C "$GOBBY_REPO" \
        "This is a disposable reset verification. Reply exactly RESET_OK and use no tools." |
        tee "$events_file"; then
        die "Disposable Codex session failed."
    fi

    thread_id="$(
        /usr/bin/jq -r \
            'select(.type == "thread.started") | .thread_id // empty' \
            "$events_file" |
            /usr/bin/head -n 1
    )"

    [[ -n "$thread_id" ]] || die "Disposable Codex thread ID was not reported."
    printf 'Disposable thread: %s\n' "$thread_id"

    rollout_path=""
    for ((attempt = 1; attempt <= 60; attempt++)); do
        rollout_path="$(
            find "$CODEX_HOME_PATH/sessions" \
                -type f \
                -name "rollout-*${thread_id}.jsonl" \
                -print -quit 2>/dev/null || true
        )"
        [[ -n "$rollout_path" ]] && break
        sleep 1
    done

    [[ -n "$rollout_path" ]] || die "Disposable rollout file was not found."

    rollout_dir="$(cd "$(dirname "$rollout_path")" && pwd -P)"
    normalized_rollout="$rollout_dir/$(basename "$rollout_path")"
    rollout_hash="$(
        printf '%s' "$normalized_rollout" |
            /usr/bin/shasum -a 256 |
            /usr/bin/cut -d ' ' -f 1
    )"
    sidecar_path="$INDEX_DIR/$rollout_hash.gobby-index.json"

    for ((attempt = 1; attempt <= 60; attempt++)); do
        [[ -f "$sidecar_path" ]] && break
        sleep 1
    done

    [[ -f "$sidecar_path" ]] ||
        die "Gobby transcript index was not created: $sidecar_path"
    [[ ! -e "${rollout_path}.gobby-index.json" ]] ||
        die "An index was incorrectly written beside the rollout."

    printf 'Verified isolated index: %s\n' "$sidecar_path"

    "$CODEX_BIN" archive "$thread_id" || die "Could not archive $thread_id."

    doctor_after="$(mktemp "${TMPDIR:-/tmp}/codex-doctor-after.XXXXXX")"
    TEMPORARY_FILES+=("$doctor_after")
    check_doctor "$doctor_after"

    printf '\nRESET VERIFIED\n'
    printf 'Backup retained at: %s\n' "$RESET_BACKUP"
    printf 'Doctor acceptance passed before and after disposable-session archival.\n'
}

main "$@"
