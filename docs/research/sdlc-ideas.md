# Gobby SDLC Ideas

**Status:** Post-0.5.0 research backlog  
**Reviewed:** 2026-07-22

## Executive assessment

Gobby is already a strong implementation and validation engine. Its largest software
development lifecycle opportunities are at the handoffs between phases: deciding when an ad
hoc task needs managed delivery, transferring ownership, interpreting validation, obtaining
independent approval, verifying a published release, and feeding production outcomes back into
planning.

Gobby intentionally supports two execution lanes:

1. **Ad hoc tasks** remain lightweight: claim, edit, run focused validation, commit, and close.
2. **Gobby Plan and Gobby Build** are opt-in for work that benefits from staged planning,
   development, QA, review, and delivery.

The goal is to preserve that lightweight default while recognizing when work has grown beyond
it.

## Evidence from a recent task

The audit traced task **#18690**, "Stop phantom ACP sessions and stabilize Sessions UI
WebSocket," through sessions **#9401** and **#9407**.

The implementation and verification were strong:

- 61 focused protected backend tests passed.
- 54 targeted frontend tests passed.
- Ruff, strict mypy across 1,556 source files, frontend lint/type/build, and
  `git diff --check` passed.
- Manual browser and authenticated WebSocket verification passed.
- The commit was linked to the task.

The workflow also exposed coordination costs:

- The work spanned two sessions, about 119 turns, and 627 tool calls.
- The sessions encountered task-ownership contention.
- The task had no lifecycle stages, affected-file record, artifacts, or delivery state because
  it remained in the ad hoc lane.
- It closed as `completed` while `validation_status` remained `invalid` under an override.

This is a useful example of work that began as an ad hoc bug fix and eventually displayed
signals associated with managed delivery.

## Lifecycle promotion

Post-0.5.0 task **#18707** under epic **#18498** tracks a lifecycle-promotion feature. It
depends on architecture task **#15005**, which covers the extensible task taxonomy and lifecycle
model.

### Proposed behavior

When an ad hoc task materially grows in scope, risk, or coordination cost, a rule should emit a
one-time, non-blocking recommendation:

> This started as ad hoc work, but it now spans backend and frontend changes, requires
> integration testing, and contains three independent work items. Promote it to a Gobby Plan
> and execute the remaining work through Gobby Build?

Actions:

- **Preview plan**
- **Promote**
- **Continue ad hoc**

### Trigger signals

Hard signals should be sufficient on their own:

- Multiple dependent work items
- Multiple agents or an ownership transfer
- Security, authentication, migrations, or release infrastructure
- Required manual or end-to-end acceptance

Accumulated signals should use a threshold:

- Scope growth after implementation begins
- Changes across multiple subsystems
- Repeated rework
- Work spanning several sessions
- Validation overrides
- Independently executable work discovered during implementation

Raw duration, file count, or tool-call count should be supporting evidence rather than the sole
reason for promotion.

### UX and state requirements

- Explain the concrete signals that triggered the recommendation.
- Recommend once without blocking or repeatedly interrupting work.
- Resurface only when a materially stronger signal appears after dismissal.
- Preview the generated plan before mutating task state.
- Promote the existing task atomically and idempotently.
- Preserve task identity, claim, commits, verification receipts, session history, dependencies,
  and completed work.
- Map completed work into the generated stages so promotion does not restart the task.
- Record accepted and dismissed recommendations to tune thresholds over time.

## Remaining findings

### 1. Separate validation verdicts from acceptance decisions

A task can close as `completed` while retaining `validation_status=invalid`. Downstream tools
cannot reliably distinguish a failed task from accepted risk or an evidence-classification
problem.

Recommended model:

- Preserve the immutable validator verdict and its evidence.
- Record the terminal acceptance decision separately.
- Represent outcomes such as `accepted`, `accepted_with_override`, and `rejected`.
- Record who accepted an override, why, and its expiration or follow-up when appropriate.

Tasks **#18701** and **#18702** improved task-scoped evidence handling. Task **#18703** is
addressing provider-independent verification outcome projection, so this finding should be
reassessed after that work lands.

### 2. Add safe ownership transfer

Storage claims are atomic, but forced takeover can replace a live owner without an acknowledged
handoff.

Add an atomic transfer operation with:

- Expected current owner and task revision
- Receiving session
- Transfer reason
- Notification to both sessions
- Revocation of the former owner's mutation authority
- An audited administrative recovery path for abandoned sessions

### 3. Improve affected-file scope and overlap detection

Affected files are optional on ad hoc tasks. That weakens early collision detection when several
agents or sessions work nearby.

Recommended behavior:

- Preserve declared expected files when known.
- Derive observed files from linked commits.
- Warn about overlapping task scopes before mutation.
- Require acknowledgement when an agent works outside declared scope.

Post-0.5.0 task **#18580** already covers recorded acknowledgement for edits outside declared
task scope.

### 4. Require independent approval for high-risk changes

Main-branch protection requires pull requests, resolved conversations, and the Merge Gate, but
currently requires zero approving reviews. Automated review is useful evidence but does not
provide independent human accountability.

Use risk-tiered approval for:

- Authentication and authorization
- Secrets and credential handling
- Agent command execution
- Installers and hooks
- Database migrations
- Release and CI infrastructure

For a single-maintainer project, an explicit high-risk approval checkpoint, separate review
channel, or time-delayed confirmation can provide a practical interim control.

### 5. Put critical browser paths in CI

Playwright and browser specifications exist, but pull-request CI currently relies on Vitest for
the frontend. The sampled WebSocket task therefore needed manual browser validation.

Add a small isolated Playwright smoke suite covering:

- Authentication
- WebSocket connection and reconnection
- Session-list updates
- Phantom-session regression
- One critical user workflow from creation through visible UI confirmation

Keep broader exploratory browser testing manual.

### 6. Detect CI and contributor-configuration drift

Branch filters, `.gobby/project.json`, hooks, contributor documentation, and active development
conventions have diverged. Examples include stale branch targets and documentation that describes
validation different from what hooks actually run.

Recommended changes:

- Establish one machine-readable source for active branches and validation commands.
- Generate or verify CI and hooks from that source.
- Add a repository consistency test for branches, project commands, hooks, and documented
  commands.

### 7. Move security analysis into planning

Gobby already has strong commit- and CI-time controls: CodeQL, Bandit, dependency auditing,
Dependabot, secret scanning, and push protection. The missing layer is a systematic security
design trigger before implementation.

For high-risk work, require lightweight abuse cases or a threat model covering applicable trust
boundaries, including:

- Agent command execution
- MCP servers and remote configuration
- Credentials and secrets
- Authentication and WebSockets
- Installers and hooks
- Database migrations
- Untrusted repository content

The risk classifier can share signals with lifecycle promotion.

### 8. Extend release integrity through consumer verification

The release workflow already gates tagged source on successful CI and uses PyPI trusted
publishing. The official PyPI action also produces publication attestations.

Add:

- GitHub artifact attestations for wheels and source distributions
- An SBOM for released artifacts
- Clean installation tests from PyPI and Homebrew on Linux and macOS
- Hash comparison across built, PyPI, and GitHub-hosted artifacts
- Consumer-side attestation verification
- A documented rollback, yank, and hotfix procedure

If a release rerun uses `skip-existing`, treat an existing remote artifact as success only when
its hash matches the locally built artifact.

### 9. Measure delivery and workflow outcomes

Gobby measures tool usage, tokens, validation, rules, builds, and agent activity. Add a value
stream record from task creation through release and recovery.

Useful measures include:

- Change lead time and release frequency
- Stage wait time and review latency
- Claim conflict and ownership-transfer rate
- Lifecycle-promotion recommendation and acceptance rate
- Validation override rate
- Rework and escaped-defect rate
- Failed-release recovery time
- Task success, user intervention, and developer friction

Use these measures diagnostically. Avoid turning them into individual or team performance
targets.

### 10. Detect documentation and installed-state drift

Contributor documentation contains stale storage, testing, commit, and pre-push guidance.
Installed database lifecycle definitions also retain wording from older workflow designs. Because
installed definitions are the source of truth, template inspection alone cannot determine live
behavior.

Add a semantic drift audit across:

- Bundled templates
- Installed database definitions
- Project configuration
- Hooks and CI
- Contributor and architecture documentation

The audit should distinguish intentional project overrides from accidental drift.

## Suggested sequencing

1. Finish verification-outcome work and define coherent terminal acceptance semantics.
2. Add safe ownership transfer and improve task-scope overlap detection.
3. Complete the extensible lifecycle architecture and implement lifecycle promotion.
4. Add risk-tiered approval, planning-stage security evidence, and critical Playwright CI.
5. Harden release verification and recovery.
6. Add delivery metrics and continuous configuration/documentation drift checks.

## Reference practices

- [NIST Secure Software Development Framework](https://csrc.nist.gov/pubs/sp/800/218/final)
- [DORA software delivery metrics](https://dora.dev/guides/dora-metrics/)
- [SLSA source requirements](https://slsa.dev/spec/v1.2/source-requirements)
- [SLSA build track basics](https://slsa.dev/spec/v1.2/build-track-basics)
- [GitHub artifact attestations](https://docs.github.com/en/actions/concepts/security/artifact-attestations)
- [PyPI attestation consumption](https://docs.pypi.org/attestations/consuming-attestations/)
