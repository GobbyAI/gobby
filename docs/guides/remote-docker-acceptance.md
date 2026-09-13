# Remote Docker Stack Live-Test Runbook

## Current status

This checklist preserves the nine physical M0 acceptance obligations for
#19600. It is not a recipe for the future thin-node deployment.

[ROADMAP.md](../../ROADMAP.md) decisions 11, 13, and 17 are authoritative:
the single-active Python bridge is transitional; thin nodes perform
machine-local duties and datastores stay on the hub. The architecture direction
is settled. Physical acceptance and implementation completion remain separate.

The earlier executable recipe predates the current files-owner contract. It
copied a local bootstrap into remote mode while retaining `files_home` and
omitting `hub_daemon_url`; current validation rejects that combination.
Remote installation also requires an authenticated owner-profile response
before datastore probes. A standby exposes health, status, and lease control and cannot
provide that owner endpoint. A sole active daemon on machine B therefore does
not establish a simultaneously available files owner on machine A.

The obsolete copy-and-launch commands have been removed. Do not bypass these
checks or treat a local transport test as physical acceptance. The existing
#19600 human stability and implementation gates still apply. Use the
[hub install contract](hub-install-contract.md) for current bootstrap and
credential rules; follow ROADMAP.md for delivery order.

## Safety contract

This is an operator-coordinated physical campaign. A documentation audit does
not authorize changing the user's running services, credentials, network
policy, or volumes.

- Use the same exact Gobby commit and coherent native binary identity on both
  machines.
- Use dedicated acceptance homes, files roots, ports, and datastore targets.
  Verify the generated Compose project and volume identities; a different
  `GOBBY_HOME` alone is not proof of isolated Docker resources.
- Preserve each machine's identity. Do not copy `machine_id` to a new machine.
- Never remove containers or volumes or run Docker prune during the campaign.
- Before every container stop or start, verify its full immutable ID, exact
  Compose working-directory label, and service label. Require exactly one
  each of PostgreSQL, Qdrant, and FalkorDB in the captured stack.
- Coordinate every affected session before a maintenance window. Restore the
  original runtime after any failure once production has been stopped.
- Retain the stopped acceptance stack and evidence until review completes.
  Later deletion is a separate operator action.

## Topology and variables

The historical M0 acceptance places the managed datastores on physical
machine A and the active Python daemon on machine B. Machine C is a private
network device denied access to the acceptance datastore ports. This
transitional acceptance shape must not be presented as the destination node
architecture.

Record a UTC run ID, exact repository commits, native binary/schema identity,
machine IDs, Gobby homes, files roots, daemon ports, private network addresses,
datastore endpoints, provider endpoints, and the evidence directory. Keep
credential values out of the record. Confirm the files-owner limitation in
Current status is addressed by the implementation being tested before any
disruptive phase.

Use an operator-selected private evidence directory. Run names and example
paths are not authorization to touch a similarly named existing resource.

## Safe container identity helpers

The original shell helpers have been replaced by this verification contract.
Implement or use reviewed tooling for the exact test environment:

1. Enumerate containers by the expected Compose working-directory label.
2. Capture full immutable IDs, and require exactly the three intended services.
3. Verify each ID still resolves to itself and retains the expected directory
   and service labels immediately before its stop or start.
4. Refuse missing, duplicated, replaced, or foreign containers.
5. Record state before and after each operation without deleting anything.

Do not substitute a broad container-name match, all-container stop, or Compose
teardown. Keep the captured identities for Phase 9.

## Phase 1: protect and inventory the local installation

Establish healthy source state with `gobby health` and `gobby status`. Create
a restore-verified hub backup through `gobby hub-backup`, following the
[backup recovery guide](cli-commands.md#hub-backup-disaster-recovery).

Record the published manifest, store verification results, original bootstrap
checksum, current singleton/lease owner, affected sessions, and local container
identities. A manifest path alone does not prove that every store restored.
Do not continue after a failed backup or identity check.

## Phase 2: provision the isolated stack on machine A

Provision the reviewed acceptance stack using the exact tested commit.
Local installation requires an existing absolute files-home directory.
Verify installation results, managed-service health, distinct Docker
resources, and native/schema identity before using the stack.

The old M0 experiment included private datastore-port exposure. Current
`gobby datastores expose` code is a transitional operator surface, not the
destination network architecture. Any physical use must remain within the
specific acceptance campaign and its reviewed network policy. Do not expose
production datastores or widen network access as part of installing this
reference library.

Capture the actual bind addresses and container identities. The acceptance
policy must exclude wildcard, public, and unintended LAN binds.

## Phase 3: prepare the isolated daemon home on machine B

Validate the remote bootstrap against current code. Remote mode requires
`hub_daemon_url`, rejects `files_home`, and requires the hub's existing
authentication material. The owner URL must not point to the remote process
itself. Preserve a distinct machine identity and register the local checkout.

The owner-profile probe must succeed before datastore probes. A reachable
PostgreSQL port or copied bootstrap is not sufficient. Stop here if the
implementation cannot provide the owner endpoint while honoring the
single-active M0 contract. Do not disable validation or claim thin-node
support to get through this phase.

## Phase 4: stop the local production runtime without removing it

Notify affected sessions and wait for the agreed quiet window. Record the
session references to resume, then stop only the intended runtime and
captured local containers. Honor protected cron, handoff, and maintenance
gates.

Prove the original datastore ports are closed and captured containers remain
inspectable in a stopped state. From this point, Phase 9 is mandatory recovery
after any failure; preserve the original data and bootstrap.

## Phase 5: start the active daemon on machine B

Use a reviewed isolated launcher that cannot select the production OS service
or home. Record its process identity, selected bootstrap, endpoint, and native
schema identity. Wait for completed startup readiness and healthy required
services, not merely an HTTP 200 response.

Verify that machine B is the active lease owner and remote mode performs no
local Compose lifecycle. Exercise a second isolated daemon as standby and
verify that it exposes only health, status, and lease control. Keep this single-active test
distinct from future node behavior.

Configure the acceptance embedding and generation providers through their
current supported interfaces. Verify actual readiness and selected model
identity before continuity testing; a disappeared operation journal alone is
not proof of successful indexing or model activation.

## Phase 6: network and ACL acceptance

Record successful access from the authorized acceptance machine to each
intended endpoint. From machine C, prove rejection for every restricted
datastore endpoint. A successful unauthorized connection fails acceptance.

Record actual network-policy and bind evidence. This campaign's historical
datastore checks do not establish the destination HTTP/WS front-door boundary;
ROADMAP.md owns that later implementation and acceptance.

## Phase 7: task, session, vector, and graph continuity

Create clearly identified records in the isolated acceptance project through
the supported domain interfaces. Agents use task lifecycle MCP tools.
Record task, session, and memory IDs and the selected project/machine scope.

Verify semantic recall returns the created memory and inspect the graph
projection after its supported rebuild. A vector or graph count alone is
insufficient; tie the evidence to the created record.

Perform a coordinated clean handoff/restart of the acceptance daemon, then
read the same records again. Confirm the same IDs, content, project scope,
and active owner. Preserve failures and uncertain mutation outcomes before
retrying; do not broaden the project filter to make the check pass.

## Phase 8: PostgreSQL connection capacity

From the isolated acceptance hub, measure configured connection capacity and
observed connections grouped by application after representative CLI churn.
Record the scope of the measurement and reserve at least 20 unused connections,
as required by the historical acceptance criteria.

Do not print a credential-bearing DSN or count only one application's
connections as the server's total usage. Preserve enough evidence to distinguish
configured capacity, current occupancy, and the required headroom.

## Phase 9: return the Docker workload to machine B

Cleanly stop the acceptance daemon and verify its process has exited.
Remove only the acceptance network grants and prove the previously authorized
restricted connections are denied again.

Stop the exact captured remote containers without deleting them. Copy the
acceptance evidence, then start the original local containers by verified
immutable identity. Compare original bootstrap checksum and container IDs,
start the original daemon, and verify health, readiness, schema identity,
and lease ownership.

Observe the restored runtime for five continuous minutes. Resolve newly
encountered warnings or errors attributable to the campaign or restored runtime;
restart that observation window after a repair. Resume the sessions recorded
before the maintenance window through supported coordination messaging.

## Completion record

Complete the M0 checklist in [shared-stack.md](shared-stack.md) only after
the physical campaign passes. Record:

- UTC run ID and exact commit/native version on both machines.
- Machine IDs, roles, endpoint and network-policy evidence.
- Datastore health, schema identity, and readiness.
- Authorized access and machine C rejection.
- Exactly one active lease owner and a standby limited to health, status, and lease control.
- Task, session, memory, vector, and graph continuity tied to record IDs.
- Connection capacity, occupancy, and at least 20 connections of headroom.
- Local and remote immutable container identities.
- Original bootstrap and local runtime restoration.
- Five-minute clean observation and affected-session resumption.

Leave #19600 open until its physical criteria and existing gates are satisfied.
An audit of this guide, an isolated unit test, or a future architecture decision
does not supply that evidence.

_Last verified: 2026-09-13_
