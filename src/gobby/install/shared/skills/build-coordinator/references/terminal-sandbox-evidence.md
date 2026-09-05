## Terminal and sandbox evidence

For terminal work, require the worker's OS, provider, run/session IDs, source
checkout and commit, generated sandbox policy and canonical run temp path,
exact commands, pass/fail/skip counts, and fixture-process sets before and after.
Use `docs/guides/gterminal-development-guide.md` for the affected Guard groups.
Guard H groups 2, 3, and 6 and the runtime-contract/external-attach tests may bind
Unix sockets; skips or policy denials remain **UNVALIDATED** until the required
platform run passes. A parent-shell run does not replace a required spawned-agent
run.

macOS managed SRT agents receive a socket grant only for their current run's
canonical temp directory, with `allowAllUnixSockets=false`; preserve any separate
operator grants. Linux and WSL2 retain socket restrictions while permitting run
directory writes. Require real bind/listen/connect and boundary-denial evidence
when changing this policy. Web-chat socket permissions are outside that grant.
