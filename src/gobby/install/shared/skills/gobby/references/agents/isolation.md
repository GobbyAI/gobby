# Bound a worker's workspace

Load when selecting isolation, reusing a checkout, granting external write roots,
or diagnosing sandbox failures. Inspect task/worktree/clone ownership and the
run's effective sandbox metadata before mutation. Use spawn dry-run and installed
definition inspection to understand inherited settings.

`none` uses the current repository context; `worktree` separates branch state;
`clone` separates the checkout further. These Git arrangements are independent
of OS sandbox policy. Reuse only the intended registered isolation, preserving
active writers and task ownership. Load workspace guidance before synchronization
or cleanup. Do not erase another session's edits to make reuse succeed.

Managed agents default to SRT; configured provider-native rendering is an explicit
operator choice and missing renderers fail closed. Preflight failure does not
permit fallback to an unconfined process. Enabled managed spawns add private
validation caches and scoped package-registry egress; Git network permission is
separate. Read the effective policy, since the base configuration alone does not
include per-run additions. SRT loopback host allowance is not exact port isolation.

External grants require absolute existing directories and a nonblank
`write_paths_reason`. Supply only authorized roots. Grants are canonicalized,
reject protected roots and escapes, and do not override denials. They are not
inherited automatically. A managed child can explicitly delegate only recorded
external roots or descendants, regardless of its declared parent. Resume
revalidates roots and symlink targets before allocating a successor.

On failure inspect the exact path/policy error, ownership, and violation metadata.
Repair through authorized configuration or installation; do not disable sandboxing
to hide the failure. Operator setup and host compatibility checks belong to the
guides, and mutating verification uses isolated state.

Guides: [Sandbox configuration](../../../../../../../../docs/guides/sandboxing.md)
and [compatibility](../../../../../../../../docs/guides/sandbox-compatibility.md).

_Last verified: 2026-09-12_
