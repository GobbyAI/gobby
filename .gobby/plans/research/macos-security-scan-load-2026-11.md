# macOS security-scan load on the Gobby development host

Task #25820; researcher gobby#20642; 2026-11-07 America/Chicago.
Research only, main `0.5.0`. No settings, signatures, processes or source code
changed. All linked sources accessed 2026-11-07; dates below are publication or
update dates where available. This is research evidence, not an approved plan.

## Research Findings

### Research Questions

- Which permission, process identity and signing practices reduce launch checks?
- Which filesystem and test practices reduce churn without disabling protection?
- What should this machine and Gobby change, and what remains unverified?

### Domain Context

Verify the existing Developer Tools mechanism before introducing a signing
runner. Reduce avoidable executable churn independently. Stable signing of the
installed host is an identity-maintenance proposal; blanket signing of ephemeral
tests has no demonstrated benefit here.

Local checks in this session:

| Read-only check | Result | Interpretation |
| --- | --- | --- |
| `sw_vers` | macOS 27.0.1, build 26A434 | Older-version observations require verification here. |
| `codesign -d -r- --verbose=4 /Users/josh/.gobby/bin/gterm` | `Signature=adhoc`, `TeamIdentifier=not set`; DR `cdhash H"6bf5e22a963ca69b476e6199d2a7159839d6e8f0"` | Current identity is hash-bound; effective permission remains unverified. |
| `spctl --assess --type execute --verbose=4 /Users/josh/.gobby/bin/gterm` | Exit 1: `internal error in Code Signing subsystem` | Inconclusive, not an ordinary assessment rejection. |
| Bounded `log show --last 1m --info` for DeveloperTool and gterm/XProtect | Exit 64: `Cannot run while sandboxed` | No launch-policy attribution available. |
| `ps -axo pid,ppid,pcpu,comm` | Exit 127: `[rtk: Operation not permitted (os error 1)]` | No fresh CPU snapshot. No bypass attempted. |

#25653 lead gobby#19758 reported load 23 on 18 cores initially, 46 at admission
check and 51 during #25733 gterm runs. Separate compile wall times were 15m24s,
27m04s, 6m22s and 10m38s. One three-crate nextest run took 1304 seconds; another
was interrupted at 3915 seconds with 1817/1837 tests run. No TCC attribution or
security-scan before/after experiment was captured. Source: durable message
`e02795cd-e492-4292-be6c-cdf57f5c5e76`, received 2026-11-07. Its Darwin 27.0.0
observation and this session's product/build observation above are recorded
separately. These timings demonstrate contention, not its cause.

The task brief's fseventsd ~100%, XprotectService ~93%, syspolicyd ~86%, tccd ~43%
are historical operator observations, not reproduced samples. A request for the
developer's existing snapshots timed out; delivery is unconfirmed. Memory search
also timed out; that does not establish absent project knowledge.

### Evidence & Sources

## Findings

### 1. Developer Tools registration, attribution and identity

Apple describes Developer Tools as allowing selected apps to run software that
does not meet system security policy [S1]. Nextest recommends enabling terminals
and detached multiplexers separately, says this affects Gatekeeper and XProtect,
and calls for restarting the terminal. It acknowledges unresolved performance
reports after permission is enabled [S2]. This expands trust in the selected
development launcher and code it executes.

Cargo's upstream discussion identifies the **responsible process**, usually the
terminal application, as the exception's subject [S3]. Immediate PPID is therefore
insufficient evidence. The brief's launchd-parented gterm host warrants checking
gterm's responsibility attribution; it does not prove gterm's registration alone
is sufficient. Granting the outer terminal or gclient is likewise unproven for
detached gterm-hosted shells. **Effective attribution here remains unverified.**

Nextest documents re-adding ad-hoc-signed multiplexers after updates [S2]. Apple
explains that the designated requirement identifies versions and that each
subsystem has its own policy [S4]. The observed hash-only gterm requirement makes
stale approval after a changed build a concrete hypothesis. Identical copies do
not inherently change signing identity: file identity/path and code identity are
separate variables. A stable Developer ID signer and signing identifier can
provide update continuity [S4,S5]; this host's permission persistence still needs
measurement. Developer ID signing and notarization are distinct operations.

Developer Tools does not establish exemption from all TCC services, remediation
or filesystem events; any claim that it eliminates all tccd CPU is unverified.
TN2206's older Developer Tools Access/debugger discussion describes a different
policy from the present privacy list [S1,S4]. Verification must distinguish
signature validity, explicit assessment, and actual child-launch policy. Apple
shows matching syspolicyd CDHashes to `codesign` output [S6]. `spctl` alone does
not prove a child inherited an effective exception.

### 2. Developer ID signing of gterm and tests

Apple positions Developer ID and notarization as distribution trust; notarization
itself scans submitted software [S5]. Neither establishes a blanket runtime scan
exemption. Stable signing of **installed gterm** is worth evaluating for identity
continuity, with CPU benefit explicitly unverified. Preserve the repository's
coherent promotion/new-inode installation contract.

Cargo's `target.<triple>.runner` wraps run/test/bench target execution [S7]. A
signing wrapper is technically possible, but does not intercept every independent
child executable or generally intercept build scripts. Signing a test harness
does not sign gterm images it launches. Per-execution signing adds mutations and
concurrency/keychain coordination; its net benefit is unverified. Do not make it
the default solution.

If tested later, sign an immutable completed artifact once before concurrent
execution. Compare valid ad-hoc versus Developer-ID-signed artifacts with matching
inputs, architecture, responsible host and cache conditions. A warm second run is
not evidence of signing benefit. No signing experiment was run here.

### 3. Supported scan reduction and boundaries

Apple documents first-launch/change-triggered checks and malware remediation
beyond launch assessment [S8]. Targeted Developer Tools permission is the supported
development mechanism found here [S1,S2]; avoiding needless executable changes
addresses the documented triggers. It does not promise zero security activity.

Howard Oakley's fresh-VM tests on Tahoe 26.6.1 found that allowing apps from
Anywhere still performed Gatekeeper and XProtect checks, and Developer Tools did
not universally admit quarantined ad-hoc apps [S9]. This is primary evidence on a
different OS version, not a macOS 27 result. No supported per-project XProtect
content exclusion was established in the reviewed sources. Do not disable SIP or
Gatekeeper, stop security services, modify protected XProtect data, or recursively
strip quarantine as a performance fix. Keep security data updates enabled.

### 4. Target placement, Spotlight, filesystem activity and test churn

Chromium recommends excluding its large checkout from Spotlight through Search
Privacy [S10]. Narrow exclusion to generated target/cache trees is reasonable if
source search is useful. **Spotlight exclusion is not an established XProtect or
fseventsd exemption.** A `.noindex` name is not antivirus policy. Any improvement
to this host's fseventsd CPU remains unverified; no supported per-folder fseventsd
suppression was established here.

Cargo issue #17467 reproduces no-op builds changing the top-level executable's
inode and adding relaunch latency, and links macOS copy/hardlink SIGKILL history
[S11]. Preserve reusable artifacts and avoid routine cleans or gratuitous copies.
Use stable target locations per compatible checkout/configuration [S7]; do not
combine unrelated concurrent worktrees into one output tree just to save scans.
Nextest groups can limit expensive tests' concurrency, but do not merge binaries
or remove assessment [S12]. A local throughput benefit needs measurement.

Repository evidence, read with gcode on 2026-11-07:

- `crates/gterminal/tests/host_support/mod.rs:109-145`: `private_gterm` copies to
  `.gterm.staged` and renames over `gterm` on every invocation. Its comment records
  macOS SIGKILL after in-place overwrite of an executed signed inode.
- Lines 162-169 call `private_gterm` from `spawn_host_with_env`: copying is per
  helper invocation, potentially more than once per test.
- `host_image.rs:254` copies gterm for an image test; `host_handover.rs:865` copies
  `/bin/sh` as an impostor. Specialized identity cases require separate treatment.

### 5. What large teams actually document

Nextest publishes terminal/multiplexer permission guidance [S2]; Cargo upstream
discusses responsibility and launch latency [S3,S11]. Nicholas Nethercote measured
shorter build-script launches after enabling Developer Tools and cites a rustc UI
suite improvement from 9m42s to 3m33s [S13]. These support plausibility, not a Gobby
speedup forecast. Chromium documents Spotlight exclusion [S10] and terminal
attribution of permission requests [S14]. Apple recommends incremental Xcode builds
with correct input/output/dependency declarations [S15].

No primary evidence found in this pass establishes that Mozilla or Bazel teams
Developer-ID-sign all ephemeral tests or use supported XProtect folder exclusions.
Do not infer an industry convention from isolated advice. Recommendations here
rest on the identified Rust, Chromium and Apple practices.

## Recommended setup

Proposals for the authorized owner, not actions performed by this researcher.
Compare equivalent workloads at comparable host load. Coordinate any host restart
through the team lead; project quiet hours prohibit restart/cutover.

1. **Capture attribution first.** Expected effect: diagnostic confidence. In an
   authorized unsandboxed terminal, verify using
   `codesign -d -r- --verbose=4 /Users/josh/.gobby/bin/gterm` and
   `spctl --assess --type execute --verbose=4 /Users/josh/.gobby/bin/gterm`.
   Capture one focused launch with
   `/usr/bin/log stream --timeout 20 --style compact --info --predicate 'process == "tccd" OR process == "syspolicyd" OR process == "XprotectService"'`.
   Inspect DeveloperTool service decisions, responsible identity and binary hashes
   [S3,S6]. Record `ps -axo pid,ppid,pcpu,comm` and `/usr/bin/uptime` in the same
   interval. Redacted or absent events do not prove exemption.

2. **Repair Developer Tools approval if identity/attribution shows it ineffective.**
   Expected effect: less local-development launch assessment [S1,S2]. Add/re-add
   the current responsible host in Privacy & Security > Developer Tools. Check
   gterm independently of gclient and the outer terminal. Coordinate restart;
   verify with step 1's logs, unchanged `codesign -d -r-` identity and the same
   bounded workload. UI presence alone is insufficient. Do not grant speculative
   broad exceptions to launchd, every agent or cargo.

3. **Reduce unnecessary test-host copies.** Expected effect: fewer executable
   instances and writes; CPU improvement is a hypothesis [S8,S11]. Reuse immutable
   artifacts for ordinary tests while keeping socket/token/state directories
   private. Preserve private mutable images where tested behavior requires them.
   Verify with `gcode grep -F 'private_gterm' crates/gterminal/tests -m 50`, an
   instrumented copy/spawn count and focused wall-time/CPU comparison. Explicitly
   verify image/handover isolation and the recorded overwrite/SIGKILL regression.

4. **Reuse builds and bound host-heavy bursts.** Expected effect: fewer redundant
   links/copies and controlled concurrency [S7,S11,S12]; net throughput needs a
   trial. Keep per-checkout target directories stable, use focused package runs
   and trial a Nextest host-heavy group. Verify with
   `cargo nextest show-config test-groups`, repeated-run build output, and
   `stat -f '%i %m %N' <executable>` plus `shasum -a 256 <executable>`.
   Compiler jobs and test concurrency are separate controls.

5. **Exclude generated trees from Spotlight when indexing is material.** Expected
   effect: less metadata indexing [S10], not a security exclusion. Use Search
   Privacy for actual target/cache paths. Verify the selected paths in that UI
   and compare indexing-process CPU with `ps -axo pid,ppid,pcpu,comm` during the
   same workload. `mdutil -s /` only reports volume status; it does not verify a
   folder exclusion. Do not repeatedly rebuild the index or disable event logging.

6. **Evaluate stable signing for installed gterm.** Expected effect: update-stable
   signing identity [S4,S5], with TCC persistence and CPU benefit unverified here.
   Requires an operator-provided signing identity and a promotion design preserving
   binary coherence. Verify across an update using `codesign -d -r- --verbose=4
   /Users/josh/.gobby/bin/gterm`, `codesign --verify --strict --verbose=2
   /Users/josh/.gobby/bin/gterm` and step 1's policy logs. Compare designated
   requirements, not TeamIdentifier alone. A signing runner requires a separate
   demonstrated net benefit and coverage review.

## Gobby code changes

Proposals only; this seat implements no code and authors no test/review records.

- Ordinary host fixtures should reuse an immutable executable for a bounded run;
  upgrade/corruption/substitution tests retain private mutable images. Determine
  lifetime from identity assumptions. A process-local `OnceLock` does not share
  artifacts across Nextest test processes. Avoid hardlink shortcuts [S11] and never
  overwrite a previously executed signed inode in place.
- Measure copies, launches and startup latency in the focused experiment. Fewer
  copies is a direct outcome; security-daemon CPU reduction needs separate evidence.
  Preserve teardown and socket/token/process isolation.
- If stable signing is approved, extend the existing promotion/signing contract.
  Canonical repository guidance names `src/gobby/install/bin_set_coherence.py` and
  release identity stamps. Do not manually re-sign installed binaries.
- Document responsible-process identity, Developer Tools approval, explicit
  assessment and Spotlight as separate diagnostic concerns, with sandbox limits.

### Risks

- Effective grant and responsibility on macOS 27.0.1 remain unverified. Restricted
  diagnostics prevent claiming a local fix or causal attribution.
- Developer Tools expands trust in the chosen launcher [S1,S2], including agent
  dependencies and build scripts; approval belongs to the operator.
- Fixture reuse can invalidate image/lifecycle isolation; blanket copy removal is
  unsafe. High historical load mixes several workloads and cannot isolate scans.

## Sources

All accessed 2026-11-07; undated means no publication date established here.

- **S1, Apple Support, undated:** [Developer Tools security policy control](https://support.apple.com/en-mt/guide/mac-help/mchlc5fb7f9c/27/mac/27).
- **S2, cargo-nextest, updated 2026-08-11:** [Nextest on macOS](https://nexte.st/docs/installation/macos/).
- **S3, Cargo PR #15908, discussion 2025-09-28:** [Detect initial binary launch latency](https://github.com/rust-lang/cargo/pull/15908).
- **S4, Apple TN2206, archived/undated:** [macOS Code Signing In Depth](https://developer.apple.com/library/archive/technotes/tn2206/_index.html).
- **S5, Apple Developer, undated:** [Signing Mac Software with Developer ID](https://developer.apple.com/developer-id/).
- **S6, Apple Developer Forums, log example 2022-05-11:** [Resolving Gatekeeper Problems](https://developer.apple.com/forums/thread/706379).
- **S7, Cargo Book, live/undated:** [Configuration: target directory and runner](https://doc.rust-lang.org/cargo/reference/config.html).
- **S8, Apple Platform Security, live/undated:** [Protecting against malware](https://support.apple.com/en-ie/guide/security/sec469d47bd8/web). Search extraction supplied first-launch/change behavior; direct fetches of other regional URLs returned HTTP 429.
- **S9, Howard Oakley, 2026-08-11, primary VM experiment:** [Running unnotarised code](https://eclecticlight.co/2026/08/11/how-can-you-run-code-that-hasnt-been-notarised/).
- **S10, Chromium source docs, live main:** [Mac build instructions: Spotlight exclusion](https://chromium.googlesource.com/chromium/src/+/main/docs/mac_build_instructions.md).
- **S11, Cargo issue #17467, opened 2026-09-13, reporter reproduction:** [No-op build and relaunch latency](https://github.com/rust-lang/cargo/issues/17467). Not a resolved upstream guarantee.
- **S12, cargo-nextest, updated 2026-08-17:** [Mutual exclusion and rate-limiting](https://nexte.st/docs/configuration/test-groups/).
- **S13, Nicholas Nethercote, 2025-09-04, primary measurements:** [Faster Rust builds on Mac](https://nnethercote.github.io/2025/09/04/faster-rust-builds-on-mac.html).
- **S14, Chromium source docs, live main:** [Debugging on macOS](https://github.com/chromium/chromium/blob/main/docs/mac/debugging.md).
- **S15, Apple Developer, undated:** [Improving incremental build speed](https://developer.apple.com/documentation/Xcode/improving-the-speed-of-incremental-builds).

Criteria review: required Findings areas 1–5, Recommended setup, Gobby code changes
and Sources are present. Claims cite sources or identify uncertainty; local checks
and diagnostic failures are explicit. Each numbered recommendation includes effect
and verification. The report-only commit and routed candidate are recorded on
#25820 after commit creation. No build/test suite was run for this report.
