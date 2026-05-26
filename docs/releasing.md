# Releasing Gobby

Gobby releases are published by `.github/workflows/release.yml` on `v*` tags.

## CI Gate

The release workflow does not re-run the Python test suite. Before building and publishing, the
`verify-ci` job checks the tagged commit SHA against the `CI` workflow.

The gate considers only `push` and `workflow_dispatch` CI runs whose `headSha` exactly matches the
release SHA. It sorts those runs by `createdAt` and treats the newest considered run as the source
of truth:

- a newest completed run with `conclusion=success` passes
- a newest queued or in-progress run keeps polling until it completes or the timeout expires
- a newest completed run with any non-success conclusion fails
- no considered run fails after the polling timeout

This means a cancelled or flaky CI run can be recovered by manually dispatching `CI` for the exact
commit SHA and waiting for that newer run to pass.

## Release Flow

1. Confirm the commit to release has a green `CI` run from a `push` or `workflow_dispatch` event.
2. Create and push a `v*` tag for that commit.
3. The release workflow verifies CI, builds the package, publishes to PyPI, creates the GitHub
   release, and dispatches the Homebrew tap update.

## Gate-Only Dry Run

Use the release workflow's `workflow_dispatch` trigger with `gate_only=true` to exercise the CI
gate without building or publishing release artifacts. Publish jobs are restricted to tag push
events, so manual gate-only runs cannot publish.
