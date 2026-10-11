# Rollback rehearsal at the cutover head

## Break-glass admitted after revert

The isolated rehearsal for #23522 (Rollback rehearsal keeps the operator a way in)
passed on 2026-10-10, 22:57:33–23:10:01 CDT. Orchestrator gobby#15977 ran the
reviewed launcher outside the developer seat sandbox. Launcher exit: **0**.
The single test passed in **110.61 seconds**.

The tip accepted the bootstrap bearer key with HTTP 200. The reviewed inverse
started on the same isolated home, schema and port pair, accepted the original
break-glass credential with HTTP 200, and refused the same request without a
header with HTTP 401. Both promoted sets reported head schema **466**;
this value came from the tip's identity stamp. The break-glass file's SHA-256
remained `2beadf5627dc7d0826ce4656a5b8dfee01072505e5de4986184c6a3d22bb8057`.

This establishes reverted Python startup and protected-route admission on the
retained schema objects. It does not extend the proof to other routes or native
agent launches. Non-loopback refusal belongs to the plan's earlier authentication
checks. The inverse is rehearsal-only and must not land on `0.5.0`.

Authority: [key-cutover plan, section 1.6](../../.gobby/plans/gdaemon-key-cutover.md)
as amended at `a3c3e68ab4`; task #23522's option A criteria.

## Source, isolation and review

- Recorded tip: `db3bb9bf7db90e69bfcdc8e4974da43d0f1e7fd3`.
- Reviewed inverse: `b112a9e5f3ad28a8253b1173a76b38fcc7b859b6`.
- Inverse parent: `db3bb9bf7db90e69bfcdc8e4974da43d0f1e7fd3`, verified before the run and after checkout.
- Managed standalone clone: `/Users/josh/.gobby/clones/rehearsal-23522-15999`, ID
  `a53ff32b-9477-4c21-afbd-270b5f6652b9`.
- Scratch directory: `/private/var/folders/5w/9cmg71vd2m108t5r_fb77l0h0000gn/T/gobby-8v6ntq4b/lane2-23522-option-a.dzfkPU`.
- Test SHA-256: `6c80ec2bbea8c86ddbf6496c9b9f43f1ce6a2261183d39b8755824a80b4a38f2`.
- Python interpreter: the clone's own `.venv/bin/python`.
- Isolated test hub: `postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test`.
- `GOBBY_TEST_PROTECT=1`, `GOBBY_TEST_GDAEMON=installed`,
  `GOBBY_NATIVE_BIN_DIR=<scratch>/bin`, `CARGO_TARGET_DIR=<scratch>/target`.
- The launcher removed inherited `PYTHONPATH`, `VIRTUAL_ENV`,
  `UV_PROJECT_ENVIRONMENT`, `GOBBY_HOME` and `GOBBY_CONFIG`.
  The daemon fixture then set its isolated home and clone source path.

The Orchestrator authorized the managed clone root after the clone tool rejected
a temporary clone location. A sandbox denial prevented writing the inverse in
that clone, so the Orchestrator directed preparation in the lane worktree and
execution outside the seat sandbox. The launcher fetched the reviewed inverse
into the clone, verified its parent, and checked out the recorded tip before
starting pytest.

Lane 2 reviewer gobby#16000 bounced inverse `d2ebdf672e28bc3fdc2e4aad5c361804e87d58c6`
in verdict `8a59623d-64ae-44c9-a37d-124f098d48af`. The correction preserved later
private-envelope issuance and renewal, daemon-owned managed grant persistence,
and fail-closed handling of stored retired terminal configuration. It also
restored the local-token warning and recorded actual merge resolution diffs.
The reviewer then **APPROVED** `b112a9e5f3ad28a8253b1173a76b38fcc7b859b6` before execution:
“All bounce 8a59623d findings fixed (HIGH-1, MEDIUM-2, MEDIUM-3, LOW-4, LOW-5).
Line sweep has no restored or lost non-cutover lines.”
Approval was for rehearsal only, with no landing receipt or `land_commit`.

## Decision 9 breach and forward-only schema

#23519 (Shared-token cutover: gdaemon validates keys and Python trusts only the
front door) introduced migration 464 in `5596ae13948f77356bb853f76b93d870926379bd`,
breaching Decision 9's rollback premise. Migrations 460–466 stayed forward-only.

The inverse retained the tip's bytes for `crates/gcore/assets/schema/`,
`crates/gcore/src/schema/assets.rs`, `crates/gcore/src/grant/bundle.rs`,
`src/gobby/storage/schema_expected_identity.json`, `Cargo.lock` and
`src/gobby/install/version_pins.py`. Current crate version/dependency metadata
was retained for `--locked` coherence. Root `tests/`, `crates/*/tests/`, `docs/`
and Markdown instructions retained tip bytes. Six in-crate unit-test modules
reverted with their production code as clean reverse patches:

- `crates/gcode/src/graph/code_graph/tests.rs`
- `crates/gcore/src/ai/daemon/tests.rs`
- `crates/gcore/src/ai/effective_config/tests.rs`
- `crates/gcore/src/grant/tests.rs`
- `crates/gterminal/src/host/native_ops/tests.rs`
- `crates/gterminal/src/host/tests.rs`

These modules lie outside the retained test globs. The actual health and
200/401 checks below establish tolerance of the retained objects.

## Linked commits and inverse selection

The inventory contains 40 linked records from #23518
(bootstrap-derived managed capability signing), #23519 (shared-token cutover),
and #23520 (auth key commands). The inverse selected 19
non-merge commits, newest first:

```text
bf2bc90f3f7c5033c35cd87a54a0e526bd1a5405
9d596467f92a2dff64f3edd0bb26325e288c1828
bb79dc39074c213ebb28ff039fcb96a8cf0eae3d
d345f74ac9d012ed2185ae2e3262b4f506c0cda2
587ca70335ee72a5ce8318387192289eb7ed5981
0e185234d479fcc396add4109bd399ed8c7b6231
574205b306c95affd5ffeea6abe0eca40643018e
0f2f36b48cb2969e6dd5720ba40e78673e025753
5596ae13948f77356bb853f76b93d870926379bd
102d7daf4313921060c51d9566c69d83628242e1
0ab31d3b801bf557d603b2289f01153dc90fa434
14fcd4e3649abb19cc99561c3f60abeeb739f3a5
0368119634d60e2b458717438b28a69b3feafbba
23991aaa94ab6a3b890353406a78046017f5febe
1731d268f38663df5566a46e1b09b486cc9f46f0
eb485cd1d2885ecf1adcca7e9b0001b3c9c2d4e6
eace4f902e319059579b2d13059a18f4de64e1ae
23328c739215275210cb44ef988e78618aa1cdf6
11549daad4ac68ca0653c03f435787fc38aa07f6
```

Every linked record and its disposition:

| Section | Linked SHA | Disposition |
| --- | --- | --- |
| 1.2 | `11549daad4ac68ca0653c03f435787fc38aa07f6` | revert |
| 1.3 | `eb485cd1d2885ecf1adcca7e9b0001b3c9c2d4e6` | revert |
| 1.3 | `0368119634d60e2b458717438b28a69b3feafbba` | revert |
| 1.3 | `23328c739215275210cb44ef988e78618aa1cdf6` | revert |
| 1.3 | `eace4f902e319059579b2d13059a18f4de64e1ae` | revert |
| 1.3 | `cdd5927502998d7bbaf9a317fa0cf6b17bcd90ab` | exclude merge wrapper/base sync per Orchestrator |
| 1.3 | `1731d268f38663df5566a46e1b09b486cc9f46f0` | revert |
| 1.3 | `23991aaa94ab6a3b890353406a78046017f5febe` | revert |
| 1.3 | `ea9147a41bb5a48a2fc837e81dcb4dfaa850a572` | exclude merge wrapper/base sync per Orchestrator |
| 1.3 | `14fcd4e3649abb19cc99561c3f60abeeb739f3a5` | revert |
| 1.3 | `a37e2f84beb61b8db8e66bab61f7facf8333cf70` | exclude merge wrapper/base sync per Orchestrator |
| 1.3 | `6efee47029e963c6f551b7f2391b04392fb7edb5` | exclude merge wrapper/base sync per Orchestrator |
| 1.3 | `0ab31d3b801bf557d603b2289f01153dc90fa434` | revert |
| 1.3 | `0646a6a550933ada45ebcc4baa58c85dd6a54899` | exclude merge wrapper/base sync per Orchestrator |
| 1.3 | `32658b9baf38461a0b513163569b9ad5d364e743` | not ancestor |
| 1.3 | `102d7daf4313921060c51d9566c69d83628242e1` | revert |
| 1.3 | `5596ae13948f77356bb853f76b93d870926379bd` | revert |
| 1.3 | `e1e7d0276eef17f2bf13a561b668a4acfcf0407c` | exclude merge wrapper/base sync per Orchestrator |
| 1.3 | `67fce0692c0be32ac24c9a7b0f7e26167dc20765` | exclude generated schema identity refresh; preserve head schema per Orchestrator |
| 1.3 | `4f00c80eb76bba14b9675c46f69e542900b35fcc` | exclude merge wrapper/base sync per Orchestrator |
| 1.3 | `0f2f36b48cb2969e6dd5720ba40e78673e025753` | revert |
| 1.3 | `920147d3efb504d6b15e3310ba9ef3a892b72120` | exclude merge wrapper/base sync per Orchestrator |
| 1.3 | `574205b306c95affd5ffeea6abe0eca40643018e` | revert |
| 1.3 | `0e185234d479fcc396add4109bd399ed8c7b6231` | revert |
| 1.3 | `e2d81a65f8cad8276ced51121f0c49f8bfe8c051` | exclude merge wrapper/base sync per Orchestrator |
| 1.3 | `8594953482b1438254d20b0cc2395da89066a2e2` | not ancestor |
| 1.3 | `3f6dc72463883705f8384a0115522088082e424d` | exclude instruction-only change per Orchestrator; retain tip text |
| 1.3 | `47af2e01a51c8948a7b86e69914f52ae3305cf3c` | not ancestor |
| 1.3 | `6a837fa6a27e2fac1f8a07749dfbb2c51a99c623` | not ancestor |
| 1.3 | `ac566c5e81159cc1a6d66f1984bd3a912aa69d0e` | not ancestor |
| 1.3 | `587ca70335ee72a5ce8318387192289eb7ed5981` | revert |
| 1.3 | `522ae283a979eedbb2eafc26de9f4a18af84fdee` | exclude instruction-only change per Orchestrator; retain tip text |
| 1.3 | `d345f74ac9d012ed2185ae2e3262b4f506c0cda2` | revert |
| 1.3 | `cc5581cd4f46a77503e2bc93b0356069eac14e72` | withdrawn |
| 1.3 | `bb79dc39074c213ebb28ff039fcb96a8cf0eae3d` | revert |
| 1.3 | `e7e707e54e57c97be39b10221f362ca8a9c26675` | exclude merge wrapper/base sync per Orchestrator |
| 1.3 | `f33e0ed77b16d784ec2901aba783007c26630f65` | exclude instruction-only change per Orchestrator; retain tip text |
| 1.3 | `95ce7e76e38866f14728678025ee67c32ef23cb1` | exclude merge wrapper/base sync per Orchestrator |
| 1.4 | `9d596467f92a2dff64f3edd0bb26325e288c1828` | revert |
| 1.4 | `bf2bc90f3f7c5033c35cd87a54a0e526bd1a5405` | revert |

## Every observed conflict resolution

The preparation encountered 59 conflict occurrences
across 53 paths. The non-writer
review covered the following corrected rules and the excluded merges.

| Inverted commit | Path | Resolution |
| --- | --- | --- |
| `9d596467f92a2dff64f3edd0bb26325e288c1828` | `tests/cli/test_auth_login.py` | Take tip test bytes. |
| `0f2f36b48cb2969e6dd5720ba40e78673e025753` | `Cargo.lock` | Keep tip release lockfile byte-identical. |
| `0f2f36b48cb2969e6dd5720ba40e78673e025753` | `crates/gclient/Cargo.toml` | Keep current release version 0.1.33 to match retained Cargo.lock; this conflict changes version metadata only. |
| `0f2f36b48cb2969e6dd5720ba40e78673e025753` | `src/gobby/install/version_pins.py` | Keep tip managed binary pins byte-identical. |
| `5596ae13948f77356bb853f76b93d870926379bd` | `Cargo.lock` | Keep tip release lockfile byte-identical. |
| `5596ae13948f77356bb853f76b93d870926379bd` | `crates/gcore/assets/schema/catalog.manifest.json` | Keep tip forward schema/identity bytes. |
| `5596ae13948f77356bb853f76b93d870926379bd` | `crates/gcore/src/grant/bundle.rs` | Keep tip forward schema/identity bytes. |
| `5596ae13948f77356bb853f76b93d870926379bd` | `crates/gcore/src/schema/assets.rs` | Keep tip forward schema/identity bytes. |
| `5596ae13948f77356bb853f76b93d870926379bd` | `crates/gcore/tests/schema_contract.rs` | Take tip test bytes. |
| `5596ae13948f77356bb853f76b93d870926379bd` | `crates/gdaemon/Cargo.toml` | Keep current release version and dependency metadata to match retained Cargo.lock; remove cutover use sites in runtime code. |
| `5596ae13948f77356bb853f76b93d870926379bd` | `crates/gdaemon/tests/cli_contract.rs` | Take tip test bytes. |
| `5596ae13948f77356bb853f76b93d870926379bd` | `src/gobby/install/version_pins.py` | Keep tip managed binary pins byte-identical. |
| `5596ae13948f77356bb853f76b93d870926379bd` | `src/gobby/storage/schema_expected_identity.json` | Keep tip forward schema/identity bytes. |
| `5596ae13948f77356bb853f76b93d870926379bd` | `tests/contracts/http/runtime_handshake.json` | Take tip test bytes. |
| `5596ae13948f77356bb853f76b93d870926379bd` | `tests/runtime_grants/golden/brokered_datastores.json` | Take tip test bytes. |
| `5596ae13948f77356bb853f76b93d870926379bd` | `tests/runtime_grants/golden/direct_datastores.json` | Take tip test bytes. |
| `5596ae13948f77356bb853f76b93d870926379bd` | `tests/runtime_grants/golden/old_client_new_grant.json` | Take tip test bytes. |
| `5596ae13948f77356bb853f76b93d870926379bd` | `tests/runtime_grants/golden/payload_skew_unknown_field.json` | Take tip test bytes. |
| `5596ae13948f77356bb853f76b93d870926379bd` | `tests/runtime_grants/golden/unavailable_datastores.json` | Take tip test bytes. |
| `23328c739215275210cb44ef988e78618aa1cdf6` | `crates/gcode/tests/evidence.rs` | Take tip test bytes. |
| `23328c739215275210cb44ef988e78618aa1cdf6` | `crates/gcore/src/grant/acquisition/managed.rs` | Retain existing managed module boundary and pre-cutover parsing; keep later private-envelope refresh and daemon-owned grant persistence fixes (c36ed6ce55, 1f19833390). |
| `23328c739215275210cb44ef988e78618aa1cdf6` | `crates/gcore/src/local_token.rs` | Restore local_cli_token file readers; retain later managed envelope identity checks and fail-closed precedence. |
| `23328c739215275210cb44ef988e78618aa1cdf6` | `crates/gdaemon/src/front_door/auth.rs` | Delete cutover-added key validator; Python resumes bearer and break-glass authentication. |
| `23328c739215275210cb44ef988e78618aa1cdf6` | `crates/gdaemon/src/front_door/challenge.rs` | Delete cutover-added challenge interceptor; Python resumes challenge handling. |
| `23328c739215275210cb44ef988e78618aa1cdf6` | `crates/gdaemon/src/front_door/mod.rs` | Remove key validator and challenge dispatch; retain current routing, proxying and listener structure. |
| `23328c739215275210cb44ef988e78618aa1cdf6` | `crates/gdaemon/src/serve.rs` | Remove front-door secret/resolver/auth parameter; retain terminal drain and retention-task cleanup added after cutover. |
| `23328c739215275210cb44ef988e78618aa1cdf6` | `crates/gdaemon/tests/front_door.rs` | Take tip test bytes. |
| `23328c739215275210cb44ef988e78618aa1cdf6` | `crates/gterminal/src/host/backpressure/tests.rs` | Take tip test body, including virtual-clock stalled-reply check. |
| `23328c739215275210cb44ef988e78618aa1cdf6` | `src/gobby/agents/code_index.py` | Restore local operator-token signing and imports; retain private managed envelope transport and current caller shape. |
| `23328c739215275210cb44ef988e78618aa1cdf6` | `src/gobby/config/registry.py` | Restore auth.api_token_hash registry admission; preserve #23212 fail-closed behavior for stored terminals.default_backend (3b34620a1d). |
| `23328c739215275210cb44ef988e78618aa1cdf6` | `src/gobby/hooks/inbox.py` | Keep current decomposition and replay concurrency; restore pre-cutover local-token missing warning to match read_local_api_token. |
| `23328c739215275210cb44ef988e78618aa1cdf6` | `src/gobby/install/shared/skills/gobby/references/admin/authentication.md` | Keep tip authentication documentation. |
| `23328c739215275210cb44ef988e78618aa1cdf6` | `tests/agents/test_agent_constants.py` | Take tip test bytes. |
| `23328c739215275210cb44ef988e78618aa1cdf6` | `tests/cli/test_install_coverage.py` | Take tip test bytes. |
| `23328c739215275210cb44ef988e78618aa1cdf6` | `tests/e2e/conftest.py` | Take tip test bytes. |
| `23328c739215275210cb44ef988e78618aa1cdf6` | `tests/e2e/test_single_active_daemon.py` | Take tip test bytes. |
| `23328c739215275210cb44ef988e78618aa1cdf6` | `tests/hooks/test_envelope_marker_retention.py` | Take tip test bytes. |
| `23328c739215275210cb44ef988e78618aa1cdf6` | `tests/hooks/test_inbox.py` | Take tip test bytes. |
| `23328c739215275210cb44ef988e78618aa1cdf6` | `tests/servers/routes/test_runtime_config.py` | Take tip test bytes. |
| `23328c739215275210cb44ef988e78618aa1cdf6` | `tests/servers/test_mcp_routes.py` | Take tip test bytes. |
| `23328c739215275210cb44ef988e78618aa1cdf6` | `tests/terminals/acceptance/conftest.py` | Take tip test bytes. |
| `23328c739215275210cb44ef988e78618aa1cdf6` | `tests/utils/test_local_token.py` | Take tip test bytes. |
| `11549daad4ac68ca0653c03f435787fc38aa07f6` | `src/gobby/agents/code_index.py` | Restore local operator-token signing and imports; retain private managed envelope transport and current caller shape. |
| `11549daad4ac68ca0653c03f435787fc38aa07f6` | `src/gobby/agents/constants.py` | Keep tip bytes; retain #23923 private envelopes without environment capability issuance (1ea8e1eccc). |
| `11549daad4ac68ca0653c03f435787fc38aa07f6` | `src/gobby/agents/spawn.py` | Restore local operator-token signing for private envelopes; keep #23923 removal of env capability issuance and current title lifecycle. |
| `11549daad4ac68ca0653c03f435787fc38aa07f6` | `tests/agents/test_agent_constants.py` | Take tip test bytes. |
| `11549daad4ac68ca0653c03f435787fc38aa07f6` | `tests/agents/test_isolation.py` | Take tip test bytes. |
| `11549daad4ac68ca0653c03f435787fc38aa07f6` | `tests/agents/test_spawn_executor.py` | Take tip test bytes. |
| `11549daad4ac68ca0653c03f435787fc38aa07f6` | `tests/ai/test_managed_tool_chat_lease.py` | Take tip test bytes. |
| `11549daad4ac68ca0653c03f435787fc38aa07f6` | `tests/mcp_proxy/test_workspaces_registry.py` | Take tip test bytes. |
| `11549daad4ac68ca0653c03f435787fc38aa07f6` | `tests/runtime_grants/test_maintenance_launch.py` | Take tip test bytes. |
| `11549daad4ac68ca0653c03f435787fc38aa07f6` | `tests/servers/conftest.py` | Take tip test bytes. |
| `11549daad4ac68ca0653c03f435787fc38aa07f6` | `tests/servers/routes/test_agents_routes.py` | Take tip test bytes. |
| `11549daad4ac68ca0653c03f435787fc38aa07f6` | `tests/servers/routes/test_api_key_routes.py` | Take tip test bytes. |
| `11549daad4ac68ca0653c03f435787fc38aa07f6` | `tests/servers/routes/test_runtime_config.py` | Take tip test bytes. |
| `11549daad4ac68ca0653c03f435787fc38aa07f6` | `tests/servers/routes/test_runtime_handshake.py` | Take tip test bytes. |
| `11549daad4ac68ca0653c03f435787fc38aa07f6` | `tests/servers/test_auth_service.py` | Take tip test bytes. |
| `11549daad4ac68ca0653c03f435787fc38aa07f6` | `tests/servers/test_mcp_programmatic_boundary.py` | Take tip test bytes. |
| `11549daad4ac68ca0653c03f435787fc38aa07f6` | `tests/utils/test_local_token.py` | Take tip test bytes. |

Clean reverse patches were also checked against the retention rules. Existing
hook inbox and grant-acquisition module boundaries were preserved. The inverse's
`acquisition.rs` is 820 lines and `acquisition/managed.rs` is 198 lines.
This repaired an intermediate inverse that had crossed the production monolith
ceiling. Later private-envelope renewal and daemon-owned cache publication
remain in place; terminal environment capability issuance remains removed.

## Excluded merge resolution results

Merges were excluded from `git revert`; using `-m 1` would undo unrelated
integration changes. Actual `git show --remerge-diff --format=fuller` results
were captured for every linked merge.

| Merge | Resolution rule |
| --- | --- |
| `cdd5927502998d7bbaf9a317fa0cf6b17bcd90ab` | Test-only resolution hunks: retain tip tests. |
| `ea9147a41bb5a48a2fc837e81dcb4dfaa850a572` | Cutover auth.api_token_hash retirement: inverse restores registry admission. |
| `a37e2f84beb61b8db8e66bab61f7facf8333cf70` | No resolution hunks. |
| `6efee47029e963c6f551b7f2391b04392fb7edb5` | No resolution hunks. |
| `0646a6a550933ada45ebcc4baa58c85dd6a54899` | No resolution hunks. |
| `32658b9baf38461a0b513163569b9ad5d364e743` | No resolution hunks. |
| `e1e7d0276eef17f2bf13a561b668a4acfcf0407c` | Release pin resolution hunks: retain tip release metadata. |
| `4f00c80eb76bba14b9675c46f69e542900b35fcc` | Release pin resolution hunks: retain tip release metadata. |
| `920147d3efb504d6b15e3310ba9ef3a892b72120` | No resolution hunks. |
| `e2d81a65f8cad8276ced51121f0c49f8bfe8c051` | No resolution hunks. |
| `e7e707e54e57c97be39b10221f362ca8a9c26675` | No resolution hunks. |
| `95ce7e76e38866f14728678025ee67c32ef23cb1` | No resolution hunks. |

The four merges with resolution hunks are recorded verbatim below. The other
listed merges produced commit metadata without resolution hunks.

### cdd5927502998d7bbaf9a317fa0cf6b17bcd90ab

```diff
commit cdd5927502998d7bbaf9a317fa0cf6b17bcd90ab
Merge: eb485cd1d2 1d4d04bc61
Author:     Josh Wilhelmi <josh@gobby.ai>
AuthorDate: Wed Oct 7 01:20:32 2026 -0500
Commit:     Josh Wilhelmi <josh@gobby.ai>
CommitDate: Wed Oct 7 01:20:32 2026 -0500

    [gobby-#23519] chore: merge current 0.5.0 into front-door cutover

diff --git a/crates/gcode/tests/evidence.rs b/crates/gcode/tests/evidence.rs
remerge CONFLICT (content): Merge conflict in crates/gcode/tests/evidence.rs
index fb86d5caaa..aa319142f3 100644
--- a/crates/gcode/tests/evidence.rs
+++ b/crates/gcode/tests/evidence.rs
@@ -1111,17 +1111,11 @@ fn attach_audited_hybrid_grant(
 ) -> anyhow::Result<()> {
     use std::collections::BTreeMap;

-<<<<<<< eb485cd1d2 ([gobby-#23519] fix: verify front door secret in launch fixtures)
     let mut grant = evidence_managed_grant(home, project_id, connections)?;
-=======
-    let machine = std::fs::read_to_string(home.join("machine_id"))?;
-    let mut grant =
-        gobby_core::grant::managed_direct_grant(project_id, machine.trim(), connections);
     grant.principal.kind = gobby_core::grant::PrincipalKind::Interactive;
     grant.principal.execution_id = None;
     grant.principal.session_id = None;
     grant.deployment.token = gobby_core::grant::deployment_token(home);
->>>>>>> 1d4d04bc61 (chore: land reviewed f21e1ae299 for #23697)
     grant.capabilities.embed = gobby_core::grant::AiCapability::Daemon {};
     grant = grant.with_checksum();
     let settings = gobby_core::grant::CachedSettings {
@@ -1267,13 +1261,7 @@ fn attach_managed_grant(
     project_id: &str,
     connections: &gobby_core::grant::DirectConnections,
 ) -> anyhow::Result<()> {
-<<<<<<< eb485cd1d2 ([gobby-#23519] fix: verify front door secret in launch fixtures)
-    let grant = evidence_managed_grant(home, project_id, connections)?;
-    let path = gobby_core::grant::write_managed_bootstrap(&home.join("grants"), &grant)?;
-=======
-    let machine = std::fs::read_to_string(home.join("machine_id"))?;
-    let mut grant =
-        gobby_core::grant::managed_direct_grant(project_id, machine.trim(), connections);
+    let mut grant = evidence_managed_grant(home, project_id, connections)?;
     grant.principal.kind = gobby_core::grant::PrincipalKind::Interactive;
     grant.principal.execution_id = None;
     grant.principal.session_id = None;
@@ -1297,7 +1285,6 @@ fn attach_managed_grant(
             settings: std::collections::BTreeMap::new(),
         },
     )?;
->>>>>>> 1d4d04bc61 (chore: land reviewed f21e1ae299 for #23697)
     command
         .env("GOBBY_HOME", home)
         .env("GOBBY_DAEMON_URL", daemon_url)
diff --git a/tests/cli/test_install_coverage.py b/tests/cli/test_install_coverage.py
remerge CONFLICT (content): Merge conflict in tests/cli/test_install_coverage.py
index c31979503c..86ddb55fe5 100644
--- a/tests/cli/test_install_coverage.py
+++ b/tests/cli/test_install_coverage.py
@@ -67,13 +67,7 @@ def runner(monkeypatch: pytest.MonkeyPatch) -> CliRunner:
     )
     monkeypatch.setattr(install_module, "SecretStore", MagicMock())
     monkeypatch.setattr(install_module, "ConfigStore", MagicMock())
-<<<<<<< eb485cd1d2 ([gobby-#23519] fix: verify front door secret in launch fixtures)
     monkeypatch.setattr(install_module, "ensure_local_api_key", MagicMock())
-    monkeypatch.setattr(install_module, "_provision_gdaemon_for_services", MagicMock())
-=======
-    monkeypatch.setattr(install_module, "AuthStore", MagicMock())
-    monkeypatch.setattr(install_module, "_provision_local_api_token", MagicMock())
->>>>>>> 1d4d04bc61 (chore: land reviewed f21e1ae299 for #23697)
     monkeypatch.setattr(install_module, "require_installed", lambda: None)
     monkeypatch.setattr(
         importlib.import_module("gobby.cli.install_components"),
```

### ea9147a41bb5a48a2fc837e81dcb4dfaa850a572

```diff
commit ea9147a41bb5a48a2fc837e81dcb4dfaa850a572
Merge: 23991aaa94 ea7b69ed77
Author:     Josh Wilhelmi <josh@gobby.ai>
AuthorDate: Wed Oct 7 06:48:10 2026 -0500
Commit:     Josh Wilhelmi <josh@gobby.ai>
CommitDate: Wed Oct 7 06:48:10 2026 -0500

    [gobby-#23519] chore: merge current base and preserve auth retirement

diff --git a/src/gobby/config/registry.py b/src/gobby/config/registry.py
remerge CONFLICT (content): Merge conflict in src/gobby/config/registry.py
index 3cb4756427..e57601d2dc 100644
--- a/src/gobby/config/registry.py
+++ b/src/gobby/config/registry.py
@@ -595,10 +595,7 @@ _REMOVED_TMUX_SPAWN_KEYS = (
 _REMOVED_STORED_KEYS = frozenset(
     {f"memory.{key}" for key in REMOVED_MEMORY_CONFIG_KEYS}
     | {f"tmux.{key}" for key in _REMOVED_TMUX_SPAWN_KEYS}
-<<<<<<< 23991aaa94 ([gobby-#23519] fix: migrate proxy promotion fixture to bootstrap key)
-    | {"terminals.default_backend", "auth.api_token_hash"}
-=======
->>>>>>> ea7b69ed77 (chore: land reviewed 841699d52b for #23688)
+    | {"auth.api_token_hash"}
 )
 _REMOVED_STORED_SECTIONS = ("memory_usefulness",)
```

### e1e7d0276eef17f2bf13a561b668a4acfcf0407c

```diff
commit e1e7d0276eef17f2bf13a561b668a4acfcf0407c
Merge: 5596ae1394 487db55c86
Author:     Josh Wilhelmi <josh@gobby.ai>
AuthorDate: Thu Oct 8 11:39:43 2026 -0500
Commit:     Josh Wilhelmi <josh@gobby.ai>
CommitDate: Thu Oct 8 11:39:43 2026 -0500

    [gobby-#23519] chore: refresh front-door validation base from 0.5.0

diff --git a/Cargo.lock b/Cargo.lock
remerge CONFLICT (content): Merge conflict in Cargo.lock
index b9cc1964d9..f08855adbc 100644
--- a/Cargo.lock
+++ b/Cargo.lock
@@ -2504,11 +2504,7 @@ dependencies = [

 [[package]]
 name = "gobby-daemon"
-<<<<<<< 5596ae1394 ([gobby-#23519] fix: preserve rotated credential drain during reconciliation)
 version = "0.4.9"
-=======
-version = "0.4.8"
->>>>>>> 487db55c86 ([gobby-#23799] docs: add Lane 3 sandboxed researcher pilot gobby#15848 to the roster)
 dependencies = [
  "anyhow",
  "assert_cmd",
diff --git a/crates/gdaemon/Cargo.toml b/crates/gdaemon/Cargo.toml
remerge CONFLICT (content): Merge conflict in crates/gdaemon/Cargo.toml
index c22c4fa58b..04afeb07ed 100644
--- a/crates/gdaemon/Cargo.toml
+++ b/crates/gdaemon/Cargo.toml
@@ -1,10 +1,6 @@
 [package]
 name = "gobby-daemon"
-<<<<<<< 5596ae1394 ([gobby-#23519] fix: preserve rotated credential drain during reconciliation)
 version = "0.4.9"
-=======
-version = "0.4.8"
->>>>>>> 487db55c86 ([gobby-#23799] docs: add Lane 3 sandboxed researcher pilot gobby#15848 to the roster)
 edition = "2024"
 rust-version = "1.88"
 authors = ["Josh Wilhelmi <hello@gobby.ai>"]
diff --git a/src/gobby/install/version_pins.py b/src/gobby/install/version_pins.py
remerge CONFLICT (content): Merge conflict in src/gobby/install/version_pins.py
index 3ef78a64a3..90c74a66cf 100644
--- a/src/gobby/install/version_pins.py
+++ b/src/gobby/install/version_pins.py
@@ -6,11 +6,7 @@ MANAGED_BIN_VERSION_PINS: dict[str, str] = {
     # Floors track published helper release tags.
     "ghook": "0.7.3",
     "gcode": "1.5.0",
-<<<<<<< 5596ae1394 ([gobby-#23519] fix: preserve rotated credential drain during reconciliation)
     "gdaemon": "0.4.9",
-=======
-    "gdaemon": "0.4.8",
->>>>>>> 487db55c86 ([gobby-#23799] docs: add Lane 3 sandboxed researcher pilot gobby#15848 to the roster)
     "gterm": "0.1.7",
     "gclient": "0.1.23",
 }
```

### 4f00c80eb76bba14b9675c46f69e542900b35fcc

```diff
commit 4f00c80eb76bba14b9675c46f69e542900b35fcc
Merge: 67fce0692c 87e91c4090
Author:     Josh Wilhelmi <josh@gobby.ai>
AuthorDate: Thu Oct 8 14:01:18 2026 -0500
Commit:     Josh Wilhelmi <josh@gobby.ai>
CommitDate: Thu Oct 8 14:01:18 2026 -0500

    [gobby-#23519] chore: merge idle TTL repair and preserve release pins

diff --git a/src/gobby/install/version_pins.py b/src/gobby/install/version_pins.py
remerge CONFLICT (content): Merge conflict in src/gobby/install/version_pins.py
index 94e3f563b2..5bba32f302 100644
--- a/src/gobby/install/version_pins.py
+++ b/src/gobby/install/version_pins.py
@@ -6,13 +6,8 @@ MANAGED_BIN_VERSION_PINS: dict[str, str] = {
     # Floors track published helper release tags.
     "ghook": "0.7.3",
     "gcode": "1.5.0",
-<<<<<<< 67fce0692c ([gobby-#23519] fix: align schema identity with committed migration bytes)
     "gdaemon": "0.4.9",
-    "gterm": "0.1.7",
-=======
-    "gdaemon": "0.4.8",
     "gterm": "0.1.8",
->>>>>>> 87e91c4090 (chore: land reviewed dfbecd99a3 for #23801)
     "gclient": "0.1.23",
 }
```

## Both phase records

Hashes below are from the scratch bin directory **after promotion and signing**.
The tip's promoted hashes in the launcher match the tip phase record.
Both phase records include the running gdaemon child's executable, clone Python
source, runner command, isolated home, public ports and installed identity.

```json
{
  "tip": {
    "python_source": "/Users/josh/.gobby/clones/rehearsal-23522-15999/src/gobby/__init__.py",
    "gdaemon": "/private/var/folders/5w/9cmg71vd2m108t5r_fb77l0h0000gn/T/gobby-8v6ntq4b/lane2-23522-option-a.dzfkPU/bin/gdaemon",
    "identity": {
      "runner_protocol": 1,
      "baseline_version": 420,
      "latest_version": 466,
      "baseline_checksum": "e5b08e67d3f5df97cfac96b9b12aac1f4f936a9465220f27b95d6e067e207e3a",
      "latest_checksum": "54b6d53ceb2c903e1089861d250138aecf9bc6b319fc35bc11c86494e3e884e4",
      "assets_root_hash": "fb5de6ac597d0f6f41a15a4d24480f3237db398f3a6f74325ad799693237d2e2"
    },
    "daemon_pythonpath": "/Users/josh/.gobby/clones/rehearsal-23522-15999/src",
    "source_head": "db3bb9bf7db90e69bfcdc8e4974da43d0f1e7fd3",
    "running_gdaemon_exe": [
      "/private/var/folders/5w/9cmg71vd2m108t5r_fb77l0h0000gn/T/gobby-8v6ntq4b/lane2-23522-option-a.dzfkPU/bin/gdaemon"
    ],
    "hashes": {
      "gcode": "307b9beb21c8a5bc347c96c6834fa0d325bbfba33c1d8e8ec8b970aa7f157b10",
      "gdaemon": "279ed9c65990d0a54329770b552a5367d0fa6a6016a6a613353f006a87f8ab52",
      "ghook": "fc698bf8627a8ce98fdd41dde39aca25e63f4f5e28629254f672958b2fcee75b"
    },
    "stamp": {
      "assets_root_hash": "fb5de6ac597d0f6f41a15a4d24480f3237db398f3a6f74325ad799693237d2e2",
      "baseline_checksum": "e5b08e67d3f5df97cfac96b9b12aac1f4f936a9465220f27b95d6e067e207e3a",
      "baseline_version": 420,
      "latest_checksum": "54b6d53ceb2c903e1089861d250138aecf9bc6b319fc35bc11c86494e3e884e4",
      "latest_version": 466,
      "runner_protocol": 1
    },
    "home": "/private/var/folders/5w/9cmg71vd2m108t5r_fb77l0h0000gn/T/gobby_e2e_al2p_fnf/.gobby-home",
    "ports": [
      36179,
      36890
    ],
    "runner_command": [
      "/Users/josh/.gobby/clones/rehearsal-23522-15999/.venv/bin/python",
      "/Users/josh/.gobby/clones/rehearsal-23522-15999/tests/e2e/readiness_bootstrap.py",
      "gobby.runner",
      "--config",
      "/private/var/folders/5w/9cmg71vd2m108t5r_fb77l0h0000gn/T/gobby_e2e_al2p_fnf/.gobby-home/config.yaml"
    ],
    "break_glass_sha256": "2beadf5627dc7d0826ce4656a5b8dfee01072505e5de4986184c6a3d22bb8057",
    "break_glass_mode": "0600",
    "admin_status_bearer": "200"
  },
  "reverted": {
    "python_source": "/Users/josh/.gobby/clones/rehearsal-23522-15999/src/gobby/__init__.py",
    "gdaemon": "/private/var/folders/5w/9cmg71vd2m108t5r_fb77l0h0000gn/T/gobby-8v6ntq4b/lane2-23522-option-a.dzfkPU/bin/gdaemon",
    "identity": {
      "runner_protocol": 1,
      "baseline_version": 420,
      "latest_version": 466,
      "baseline_checksum": "e5b08e67d3f5df97cfac96b9b12aac1f4f936a9465220f27b95d6e067e207e3a",
      "latest_checksum": "54b6d53ceb2c903e1089861d250138aecf9bc6b319fc35bc11c86494e3e884e4",
      "assets_root_hash": "fb5de6ac597d0f6f41a15a4d24480f3237db398f3a6f74325ad799693237d2e2"
    },
    "daemon_pythonpath": "/Users/josh/.gobby/clones/rehearsal-23522-15999/src",
    "source_head": "b112a9e5f3ad28a8253b1173a76b38fcc7b859b6",
    "running_gdaemon_exe": [
      "/private/var/folders/5w/9cmg71vd2m108t5r_fb77l0h0000gn/T/gobby-8v6ntq4b/lane2-23522-option-a.dzfkPU/bin/gdaemon"
    ],
    "hashes": {
      "gcode": "60319301afdec219f9563e134525179a97a7e09fb39e3380471a17dc79b723c7",
      "gdaemon": "31b6f6a397076bacddeb1dc85a56c561c63a7636bb8d6835fede4c33b95f4148",
      "ghook": "c7da523d9492f9062099431277fe2c0ac3b7bba60b4068fec5d9a7ffa0aab0bd"
    },
    "stamp": {
      "assets_root_hash": "fb5de6ac597d0f6f41a15a4d24480f3237db398f3a6f74325ad799693237d2e2",
      "baseline_checksum": "e5b08e67d3f5df97cfac96b9b12aac1f4f936a9465220f27b95d6e067e207e3a",
      "baseline_version": 420,
      "latest_checksum": "54b6d53ceb2c903e1089861d250138aecf9bc6b319fc35bc11c86494e3e884e4",
      "latest_version": 466,
      "runner_protocol": 1
    },
    "home": "/private/var/folders/5w/9cmg71vd2m108t5r_fb77l0h0000gn/T/gobby_e2e_al2p_fnf/.gobby-home",
    "ports": [
      36179,
      36890
    ],
    "runner_command": [
      "/Users/josh/.gobby/clones/rehearsal-23522-15999/.venv/bin/python",
      "/Users/josh/.gobby/clones/rehearsal-23522-15999/tests/e2e/readiness_bootstrap.py",
      "gobby.runner",
      "--config",
      "/private/var/folders/5w/9cmg71vd2m108t5r_fb77l0h0000gn/T/gobby_e2e_al2p_fnf/.gobby-home/config.yaml"
    ],
    "admin_status_break_glass": "200",
    "admin_status_without_header": "401",
    "break_glass_sha256": "2beadf5627dc7d0826ce4656a5b8dfee01072505e5de4986184c6a3d22bb8057"
  }
}
```

The unauthenticated `/api/auth/status` health probes returned 200:

```json
[
  {
    "port": 36179,
    "pid": 70633,
    "timeout_seconds": 180,
    "elapsed_seconds": 21.23521529196296,
    "auth_status_200": true
  },
  {
    "port": 36179,
    "pid": 74203,
    "timeout_seconds": 180,
    "elapsed_seconds": 14.482912709005177,
    "auth_status_200": true
  }
]
```

The reverted start reused `daemon_instance.command`, its environment and project
directory, with `start_new_session=True`. It used
`wait_for_daemon_health`, rather than the fixture's websocket-authenticated
restart method. The test's `finally` block terminated the reverted process tree.

## Commands and status codes

The launcher ran:

```bash
uv run python /private/var/folders/5w/9cmg71vd2m108t5r_fb77l0h0000gn/T/gobby-8v6ntq4b/lane2-23522-option-a.dzfkPU/run_rehearsal.py --clone /Users/josh/.gobby/clones/rehearsal-23522-15999 --lane /Users/josh/.gobby/worktrees/gobby/lane-2-front-door --scratch /private/var/folders/5w/9cmg71vd2m108t5r_fb77l0h0000gn/T/gobby-8v6ntq4b/lane2-23522-option-a.dzfkPU --tip db3bb9bf7db90e69bfcdc8e4974da43d0f1e7fd3 --inverse b112a9e5f3ad28a8253b1173a76b38fcc7b859b6
```

The launcher recorded eight commands, all exit 0. The test recorded its checkout,
build, promotion, provenance and HTTP probes, all exit 0. Credentials were
redacted in the command record. Native builds and pytest produced no warning
or error diagnostics.

| Origin | Command | Exit |
| --- | --- | --- |
| Launcher | `git status --porcelain` | 0 |
| Launcher | `git fetch /Users/josh/.gobby/worktrees/gobby/lane-2-front-door b112a9e5f3ad28a8253b1173a76b38fcc7b859b6` | 0 |
| Launcher | `git rev-parse 'b112a9e5f3ad28a8253b1173a76b38fcc7b859b6^'` | 0 |
| Launcher | `git checkout --detach db3bb9bf7db90e69bfcdc8e4974da43d0f1e7fd3` | 0 |
| Launcher | `git rev-parse HEAD` | 0 |
| Launcher | `cargo build --locked -p gobby-daemon -p gobby-code -p gobby-hooks` | 0 |
| Launcher | `uv run python -c 'import os; from pathlib import Path; from gobby.install.bin_set_coherence import promote_workspace_binary_set; s=Path(os.environ['\''GOBBY_REHEARSAL_SCRATCH'\'']); promote_workspace_binary_set({n:s/'\''target'\''/'\''debug'\''/n for n in ('\''gcode'\'','\''gdaemon'\'','\''ghook'\'')}, bin_dir=s/'\''bin'\'')'` | 0 |
| Launcher | `uv run pytest tests/e2e/test_key_rollback_rehearsal_scratch.py -q --basetemp /private/var/folders/5w/9cmg71vd2m108t5r_fb77l0h0000gn/T/gobby-8v6ntq4b/lane2-23522-option-a.dzfkPU/pytest` | 0 |
| Test | `git rev-parse HEAD` | 0 |
| Test | `git rev-parse 'b112a9e5f3ad28a8253b1173a76b38fcc7b859b6^'` | 0 |
| Test | `git rev-parse HEAD` | 0 |
| Test | `/Users/josh/.gobby/clones/rehearsal-23522-15999/.venv/bin/python -c 'import json,gobby; from gobby.utils.native_bin import local_native_bin_path; from gobby.storage.schema_contract import installed_schema_identity; print(json.dumps({'\''python_source'\'':gobby.__file__, '\''gdaemon'\'':str(local_native_bin_path('\''gdaemon'\'')), '\''identity'\'':installed_schema_identity()}))'` | 0 |
| Test | `curl -sS -o /dev/null -w '%{http_code}' -H 'Authorization: Bearer <redacted>' http://127.0.0.1:36179/api/admin/status` | 0 |
| Test | `git checkout --detach b112a9e5f3ad28a8253b1173a76b38fcc7b859b6` | 0 |
| Test | `git rev-parse 'HEAD^'` | 0 |
| Test | `git rev-parse HEAD` | 0 |
| Test | `cargo build --locked -p gobby-daemon -p gobby-code -p gobby-hooks` | 0 |
| Test | `/Users/josh/.gobby/clones/rehearsal-23522-15999/.venv/bin/python -c 'from pathlib import Path; from gobby.install.bin_set_coherence import promote_workspace_binary_set; root=Path('\''/private/var/folders/5w/9cmg71vd2m108t5r_fb77l0h0000gn/T/gobby-8v6ntq4b/lane2-23522-option-a.dzfkPU'\''); promote_workspace_binary_set({n:root/'\''target'\''/'\''debug'\''/n for n in ('\''gcode'\'','\''gdaemon'\'','\''ghook'\'')}, bin_dir=root/'\''bin'\'')'` | 0 |
| Test | `git rev-parse HEAD` | 0 |
| Test | `/Users/josh/.gobby/clones/rehearsal-23522-15999/.venv/bin/python -c 'import json,gobby; from gobby.utils.native_bin import local_native_bin_path; from gobby.storage.schema_contract import installed_schema_identity; print(json.dumps({'\''python_source'\'':gobby.__file__, '\''gdaemon'\'':str(local_native_bin_path('\''gdaemon'\'')), '\''identity'\'':installed_schema_identity()}))'` | 0 |
| Test | `curl -sS -o /dev/null -w '%{http_code}' -H 'X-Gobby-Break-Glass: <redacted>' http://127.0.0.1:36179/api/admin/status` | 0 |
| Test | `curl -sS -o /dev/null -w '%{http_code}' http://127.0.0.1:36179/api/admin/status` | 0 |
| Test | `git status --porcelain --untracked-files=no` | 0 |

| Phase | HTTP request | Status |
| --- | --- | --- |
| Tip | `GET /api/admin/status`, bootstrap bearer header | 200 |
| Reverted | `GET /api/admin/status`, original break-glass header | 200 |
| Reverted | Same request, no header | 401 |

```text
============================= test session starts ==============================
platform darwin -- Python 3.14.3, pytest-9.0.3, pluggy-1.6.0
rootdir: /Users/josh/.gobby/clones/rehearsal-23522-15999
configfile: pyproject.toml
plugins: mock-3.15.1, xdist-3.8.0, timeout-2.4.0, anyio-4.14.2, asyncio-1.3.0, cov-7.0.0
asyncio: mode=Mode.AUTO, debug=False, asyncio_default_fixture_loop_scope=None, asyncio_default_test_loop_scope=function
collected 1 item

tests/e2e/test_key_rollback_rehearsal_scratch.py .                       [100%]

======================== 1 passed in 110.61s (0:01:50) =========================
```

Static preparation validation passed: AST/Ruff check/format on 33 changed Python
files; focused mypy on nine files; Rust formatting on four packages; retention
byte comparison and diff checks. The scratch test passed quality and type audits.
The suppression ratchet reported 187 baseline entries, zero new and zero stale.
Native compile was established by both recorded `cargo build --locked` runs.

## Cleanup

The reverted process tree was stopped. Pytest fixture teardown released its
isolated project/home and schema. The clone's tracked status was clean.
After pytest, the launcher recorded:

```json
{
  "command": [
    "pgrep",
    "-f",
    "/private/var/folders/5w/9cmg71vd2m108t5r_fb77l0h0000gn/T/gobby-8v6ntq4b/lane2-23522-option-a.dzfkPU/bin/"
  ],
  "exit": 1,
  "stdout": "",
  "stderr": ""
}
```

Exit 1 with empty stdout proves no process matching the scratch native bin path
remained. After this evidence was preserved in commit
`bb10a5da560ed4c583723260971f7e9d450cb14c`, Orchestrator gobby#15977 confirmed
the managed clone's registry row and directory were deleted and the scratch
directory was removed. The developer independently verified that both recorded
paths no longer exist. The scratch test was archived before cleanup and removed
from the lane tree; the final deliverable changes only this evidence document.

_Last verified: 2026-10-11_
