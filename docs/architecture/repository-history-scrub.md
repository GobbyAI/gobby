# Repository History Scrub Decision and Runbook

Status: accepted. Owner task: `#19660`. Decision date: 2026-08-05.

## Decision

Rewrite the `GobbyAI/gobby` Git history to remove every historical copy of:

- `.gobby/tasks.jsonl`
- `.gobby/memories.jsonl`
- either filename under any nested `.gobby/` directory

The repository is already public. This is an active remediation rather than a
pre-publication precaution. PostgreSQL remains authoritative for tasks and memories;
machine-local JSONL backups remain outside Git.

The rewrite is a coordinated maintenance event. It must remap Git commit evidence in
PostgreSQL, replace every clone, and complete GitHub's server-side cleanup. A path-only
rewrite without the database remap corrupts task and checkpoint evidence.

## Security rationale

A metadata-only audit of the latest historical snapshots found:

| Path | Records | Absolute user paths | Email-like text | URLs |
|---|---:|---:|---:|---:|
| `.gobby/tasks.jsonl` | 13,474 | 439 | 7 | 171 |
| `.gobby/memories.jsonl` | 2,653 | 24 | — | 9 |

The records also contain task descriptions, validation evidence, source identifiers,
tags, and operational context. Their presence violates the current machine-local backup
boundary and unnecessarily publishes personal and internal metadata.

History inspection found 2,187 protected-path mentions across these historical locations:

```text
.gobby/tasks.jsonl
.gobby/memories.jsonl
web/.gobby/tasks.jsonl
web/.gobby/memories.jsonl
src/gobby/ui/web/.gobby/memories.jsonl
```

History removal does not revoke credentials. Rotate any credential discovered during the
content audit before rewriting history.

## Coupled PostgreSQL evidence

The following authoritative columns store Git SHAs and must move through
`git-filter-repo`'s `commit-map` in the same outage:

| Table | Column | Project scope |
|---|---|---|
| `checkpoints` | `commit_sha` | owning task |
| `checkpoints` | `parent_sha` | owning task |
| `task_artifacts` | `base_commit_sha` | owning task |
| `task_delivery_campaigns` | `merge_sha` | owning task |
| `task_stage_states` | `completed_commit_sha` | owning task |
| `tasks` | `closed_commit_sha` | direct |
| `tasks` | `commits` JSON array | direct |

Run `python -m gobby.cli.history_scrub` from the trusted pre-rewrite checkout. It:

- scopes every query to one project UUID and verifies `projects.name`;
- resolves short stored SHAs as unique prefixes of full old SHAs;
- preserves and counts valid SHAs absent from the commit map because they already refer
  outside the rewritten published ref set;
- clears nullable scalar evidence and removes JSON-array entries when the commit map marks
  the old commit as pruned;
- splits legacy comma-packed `tasks.commits` strings into ordered JSON-array entries and
  validates every component independently;
- aborts on malformed or ambiguous SHAs and on pruned non-null checkpoint evidence;
- verifies every replacement is a commit in the scrubbed mirror;
- rejects any remaining protected path at any history depth;
- locks all affected tables and updates all seven locations in one transaction;
- compares the committed plan with a post-update snapshot before commit;
- defaults to a read-only dry run and requires the project UUID twice for `--apply`.

No evidence is mapped to a guessed ancestor. A zero commit-map target means the old commit
contained no surviving tree change: nullable references are cleared and array entries are
removed. Required `checkpoints.commit_sha` and `checkpoints.parent_sha` values block the
campaign instead because those columns cannot safely become null. Valid unmatched SHAs stay
unchanged as pre-existing evidence outside the rewrite's published ref set.

## Operator runbook

### 1. Establish the maintenance boundary

Schedule an outage. Pause all agents and automation, and require every contributor to
push or archive useful work. Record every session that must be resumed. No old clone may
push after the rewrite.

From a clean trusted checkout, define the immutable campaign inputs:

```bash
set -euo pipefail

export PROJECT_ID=d45545c5-ded5-4335-b115-0245752edacf
export EXPECTED_PROJECT_NAME=gobby
export EXPECTED_GITHUB_REPO=GobbyAI/gobby
export REPO_URL=https://github.com/GobbyAI/gobby.git
export SCRUB_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/gobby-history-scrub.XXXXXX")"
export SCRUB_REPO="$SCRUB_ROOT/gobby.git"
export VERIFY_REPO="$SCRUB_ROOT/verify.git"
export SCRUB_STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
export HUB_BACKUP="$HOME/.gobby/backups/hub/history-scrub-$SCRUB_STAMP"
export SCRUB_EVIDENCE="$HOME/.gobby/backups/history-scrub/$SCRUB_STAMP"
mkdir -p "$SCRUB_EVIDENCE"

test "$(git status --porcelain)" = ""
test "$(gh repo view "$EXPECTED_GITHUB_REPO" --json visibility --jq .visibility)" = PUBLIC
uvx --from git-filter-repo git-filter-repo --version
```

Freeze protected branches only long enough to perform the coordinated force push. Record
their current settings before changing them, and restore those settings immediately after
the push.

### 2. Create and verify the hub backup

The backup command stops the daemon, verifies restores for every hub store, atomically
publishes the manifest, and restarts the daemon after success. Stop it again for the
rewrite outage.

```bash
uv run gobby hub-backup --output "$HUB_BACKUP" --json \
  | tee "$SCRUB_EVIDENCE/hub-backup.json"
test -s "$HUB_BACKUP/manifest.json"

uv run gobby stop
if uv run gobby status; then
  echo "daemon is still running" >&2
  exit 1
fi
```

Confirm every daemon sharing this PostgreSQL hub is stopped before continuing. Keep the
hub offline through local clone replacement and post-push verification.

### 3. Build the scrubbed mirror

Clone the public remote after the contributor freeze so its refs are the publication
source of truth.

```bash
git clone --mirror "$REPO_URL" "$SCRUB_REPO"
git -C "$SCRUB_REPO" show-ref > "$SCRUB_EVIDENCE/refs.before"
git -C "$SCRUB_REPO" log --all --name-only --format= \
  > "$SCRUB_EVIDENCE/paths.before"

(
  cd "$SCRUB_REPO"
  uvx --from git-filter-repo git-filter-repo \
    --sensitive-data-removal \
    --invert-paths \
    --path .gobby/tasks.jsonl \
    --path-glob '*/.gobby/tasks.jsonl' \
    --path .gobby/memories.jsonl \
    --path-glob '*/.gobby/memories.jsonl' \
    --force
)
```

`git-filter-repo` skips refs whose objects are trees or blobs. Codex turn-diff refs can
have that shape, so remove every remaining non-commit/non-tag ref before garbage
collection:

```bash
while IFS= read -r ref_name; do
  object_type="$(git -C "$SCRUB_REPO" cat-file -t "$ref_name")"
  case "$object_type" in
    commit|tag) ;;
    *) git -C "$SCRUB_REPO" update-ref -d "$ref_name" ;;
  esac
done < <(git -C "$SCRUB_REPO" for-each-ref --format='%(refname)')

git -C "$SCRUB_REPO" reflog expire --expire=now --all
git -C "$SCRUB_REPO" gc --prune=now
cp "$SCRUB_REPO/filter-repo/commit-map" "$SCRUB_EVIDENCE/commit-map"
git -C "$SCRUB_REPO" show-ref > "$SCRUB_EVIDENCE/refs.after"
```

### 4. Prove local history is clean

Both the commit-path walk and reachable-object walk must return no protected path. The
repository and commit map must pass structural checks.

```bash
git -C "$SCRUB_REPO" fsck --full
test -s "$SCRUB_REPO/filter-repo/commit-map"

if git -C "$SCRUB_REPO" log --all --name-only --format= \
  | grep -E '(^|/)\.gobby/(tasks|memories)\.jsonl$'; then
  echo "protected path remains in commit history" >&2
  exit 1
fi

if git -C "$SCRUB_REPO" rev-list --objects --all \
  | grep -E '(^|[[:space:]])([^[:space:]]*/)?\.gobby/(tasks|memories)\.jsonl$'; then
  echo "protected path remains reachable" >&2
  exit 1
fi
```

Review `refs.before` and `refs.after`. Published commit and tag ref names must be
preserved; only explicitly identified non-commit refs may disappear.

### 5. Preflight and apply the PostgreSQL SHA remap

Run the read-only preflight first. Its output contains counts only and must report no
error.

```bash
uv run python -m gobby.cli.history_scrub \
  --project-id "$PROJECT_ID" \
  --expected-project-name "$EXPECTED_PROJECT_NAME" \
  --scrubbed-repo "$SCRUB_REPO" \
  | tee "$SCRUB_EVIDENCE/database-remap-dry-run.txt"
```

Review `unmatched_references`, `pruned_references`, and
`normalized_task_commit_entries`. Unmatched references remain byte-for-byte unchanged. Pruned
references will be cleared from nullable scalar columns or removed from `tasks.commits`;
comma-packed task commit entries will become separate ordered array entries. The preflight
already rejects any pruned reference in a required checkpoint column. Retain these counts as
campaign evidence.

The dry run is the final abort-without-state-change boundary. Apply the same proven plan:

```bash
uv run python -m gobby.cli.history_scrub \
  --project-id "$PROJECT_ID" \
  --confirm-project-id "$PROJECT_ID" \
  --expected-project-name "$EXPECTED_PROJECT_NAME" \
  --scrubbed-repo "$SCRUB_REPO" \
  --apply \
  | tee "$SCRUB_EVIDENCE/database-remap-applied.txt"
```

After this transaction commits, keep the daemon stopped. Complete the force push or
restore the verified hub backup into an explicit recovery database and perform a
controlled database cutover.

### 6. Replace the public refs

`git-filter-repo` removes `origin`. Re-add the exact reviewed URL and push the complete
mirror only after the database transaction succeeds:

```bash
git -C "$SCRUB_REPO" remote add origin "$REPO_URL"
git -C "$SCRUB_REPO" remote -v
git -C "$SCRUB_REPO" push --force --mirror origin
```

Immediately restore the recorded branch-protection settings. Do not accept pushes from
old clones.

### 7. Verify the public remote independently

Use a fresh clone from GitHub rather than the working scrub mirror:

```bash
git clone --mirror "$REPO_URL" "$VERIFY_REPO"
git -C "$VERIFY_REPO" fsck --full

if git -C "$VERIFY_REPO" log --all --name-only --format= \
  | grep -E '(^|/)\.gobby/(tasks|memories)\.jsonl$'; then
  echo "protected path remains on the public remote" >&2
  exit 1
fi

if git -C "$VERIFY_REPO" rev-list --objects --all \
  | grep -E '(^|[[:space:]])([^[:space:]]*/)?\.gobby/(tasks|memories)\.jsonl$'; then
  echo "protected object remains on the public remote" >&2
  exit 1
fi

git -C "$VERIFY_REPO" show-ref > "$SCRUB_EVIDENCE/remote-refs.after"
```

Open a GitHub Support request for cached views, pull-request refs, and server-side garbage
collection. Include the affected paths and rewrite timestamp; omit historical record
contents. Contact fork owners because a fork can continue publishing the old objects.

### 8. Replace clones and restore service

Every clone, worktree, CI cache, deployment checkout, bundle, and archived bare mirror
created before the rewrite contains the old history. Quarantine it, create a fresh clone,
and securely dispose of the old copy after recovery evidence is sealed. Contributors must
cherry-pick reviewed patches onto fresh history rather than merge or force-push old refs.

For the machine hosting the daemon:

1. Replace the checkout at the registered project path with a fresh clone.
2. Run `uv sync` in that clone.
3. Start the daemon with `uv run gobby start --verbose`.
4. Verify `uv run gobby status` and the daemon health endpoints.
5. Resume the recorded sessions only after their working directories use fresh history.
6. Watch logs for five continuous clean minutes; restart the window after any warning or
   error attributable to this campaign.

## Recovery boundaries

| Boundary | Recovery |
|---|---|
| Before database `--apply` | Abandon the disposable mirror; hub and GitHub are unchanged. |
| After database `--apply`, before force push | Keep daemons stopped; complete the push or restore the verified hub backup into an explicit recovery target and cut over. |
| After force push | Prefer completing cleanup. Full rollback requires the sealed pre-rewrite mirror, the hub backup, another coordinated force push, and another clone replacement. |

Never start the daemon while PostgreSQL points at rewritten SHAs and the registered
checkout still contains only old history.

## Rehearsal evidence

The filter and verification procedure was tested against a disposable mirror cloned from
the actual public remote. Results:

```text
historical_path_mentions_before=2187
historical_path_mentions_after=0
reachable_path_objects_after=0
noncommit_refs_removed=0
published_ref_names_preserved=66
head_pyproject=present
```

A read-only remapper rehearsal against the live hub and that scrubbed mirror passed on
2026-08-05. It rolled back the database transaction and reported:

```text
distinct_stored_shas=14347
changed_references=22634
unmatched_references=2126
pruned_references=51
normalized_task_commit_entries=2
```

The SHA remapper has focused unit coverage for malformed, ambiguous, unmatched, pruned,
required-checkpoint, and nested-path cases plus an isolated PostgreSQL integration test that
normalizes comma-packed task commit evidence and updates every authoritative SHA location.
Test DSNs must point to `gobby_test`; the remapper's default CLI mode remains read-only.

## References

- [git-filter-repo manual](https://github.com/newren/git-filter-repo/blob/main/Documentation/git-filter-repo.txt)
- [GitHub: Removing sensitive data from a repository](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/removing-sensitive-data-from-a-repository)
