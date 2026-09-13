# Reference library coverage policy

The capability catalog owns names, descriptions, loading conditions and exact
overview/topic paths. Each capability has one audit JSON file in this directory.
An audit records implementation and verification evidence; it does not make a
template active or authorize an operation.

## Public operation inventory

The focused library tests construct the real internal registry manager with
inert service dependencies and enabled default configuration. Registry factories
and tool registrations execute; public operations do not. Prompt storage is
replaced only for registration-time tool descriptions. No database connection,
running daemon, user configuration or network service is used for this inventory.
Membership comes from the registered tools, not a fixed service count.

Every returned service/tool pair must map to at least one exact catalog reference.
Shared ownership can produce multiple mappings. Dynamic `pipeline:<name>` tools
are covered by the pipeline execution reference and its dynamic audit record;
their installed names are runtime data, not bundled operations. Top-level proxy
tools are inventoried from both real daemon and stdio registration carriers,
without invoking their handlers, and retain explicit audit entries.

The Click tree supplies every visible command leaf, including operator-only
commands and equivalents of MCP operations. Independently invocable group
callbacks are included. Commands hidden by Click (including descendants of hidden
groups), private Python helpers and callbacks not registered as commands are
excluded. This is a visibility rule, not a list of exceptions for failing checks.
Native CLI inventory reads checkout declarations: the vendored gcode contract
and its Rust command declarations must agree, ghook's declared flags and schema
command are covered, gclient's argument-only launcher flags are covered, and
gdaemon schema diagnostics are covered. Native binaries are not executed by the
inventory test. The existing CLI contract parity suite additionally checks the
generated gcode contract against native implementations.

Two gdaemon schema commands are excluded explicitly: `apply` is the subordinate
migration executor owned by Python cutover/schema administration, and
`sweep-test-schemas` is isolated-test maintenance. Their recovery entrypoints are
covered in admin references. gterm's daemon-owned host transport is an internal
protocol; agent terminal operations are covered through sessions MCP and operator
workspace access through gclient. Help aliases and alternate spellings of an
already covered flag do not constitute separate operations.

## Audit and link contract

Audit version 1 records `capability`, `source_skills`, `guides`, `operations` and
`evidence`. Operation records identify their surface, exact reference path,
implementation path/symbol and verification evidence IDs. Additional fields
preserve surface-specific details and limitations. Evidence may be source/schema
inspection, isolated execution, installed-state reads or independent review;
keep the original command/result or description/status, including failures and
subsequent corrections. A claimed successful command is not a substitute for its
transcript evidence.

Each guide record names real rendered heading anchors and explicit evidence IDs.
Cross-capability guide links may rely on the owning capability's audit. Markdown
code-block comments are not headings. Reference-style links, duplicate headings
and inline formatting are parsed as Markdown. Catalog files and audit mappings
must agree; adding an unlisted reference does not satisfy coverage.

Python implementation symbols may be qualified nested names or unique unqualified
names from the original audits. Paths and symbols must still exist. Audits preserve
normative obligations; implementation disagreement with a contract must be
recorded as a discrepancy and resolved, not silently endorsed in prose.

## Verification

Run `tests/skills/test_reference_library.py` for the executable inventory, catalog,
guide and negative coverage checks. Keep the existing reference-loading and router
tests in the focused verification set: they exercise schema-gated directives,
pagination completion, stale cursors, independent tracking and context resets.
Router or menu loading must never satisfy an operation reference requirement.

Representative service verification uses the existing isolated task, plan/build,
agent/handoff, pipeline, worktree, config revision-conflict and oversized-result
fixtures. Record the exact selected commands and results in task evidence. Do not
replace those behavioral checks with prose or tool-name membership assertions.

Use the isolated test hub and temporary state for mutations. Coordinate shared
test-hub windows and native schema pins before running storage tests. Never use
the user's live state for examples, and do not run the full pytest suite without
an explicit request.
