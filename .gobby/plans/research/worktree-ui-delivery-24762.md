# Worktree identity and removal delivery (#24762)

The production fixes are d302e982472ac8e45664ea274984e8a5216cc6b7 and
67a54b71fea7d0518903484a2b00a352c10d0ded. Their adjacent removal regressions
are in 436d8592ea89c12b60ca5978496cd78fd5ec09c5, attributed to the still-open
close-drain carrier #24761. All three commits are ancestors of local 0.5.0.
This record retains their shared delivery evidence without transferring that
carrier's live file ownership or claiming its separate work is complete.

- Branchless checkouts display their directory and detached state in the
  sidebar, picker and removal dialog. Ordinary branches and home project `~`
  keep their established identities.
- Removal refusal preserves the checkout entry and displays the actual Git
  refusal, wrapped recovery text and Cancel at both 100 and 48 columns.
- The original scratch registry row no longer exists. Its historical row-order
  mapping cannot be recovered; no historical screenshot mapping is asserted.

The reviewed release candidate is 436d8592ea89c12b60ca5978496cd78fd5ec09c5.
Code receipt: e8f76281-b18e-4a4b-84dd-cb7f30d6953f.
Security receipt: c761b6da-b34b-49d6-aaaf-cf2210a4bb15.
Landing receipt: 4c70ca9a-284a-4213-8f4e-fc826faffd15.
Activation receipt: a57a5be7-6982-4f53-bf7f-cfd1fc26c19b.

Supported signed, new-inode promotion installed gclient 0.1.65 with inode
768667987 and SHA-256
1171c279d0a216f3e80959c5d90d735e37e46822be3542e663d2685b37ff001d.
Josh relaunched it; PID 19878 started at 2026-10-20T15:26:43Z and mapped that
installed inode. No foreign-project worktree was removed for this proof.

Validation on the delivered client sources: 31 focused nextest cases passed
(sidebar rows, worktree daemon round trip, and dialog parity); 101 Python
version-pin and installer cases passed. Exact commands and native outcomes
are retained in Orchestrator #17164's task-linked transcript. The refusal
wrapping regression is committed in the candidate's ancestry rather than
only appearing in an uncommitted checkout.

Retain lane-7-close-drain-4 while #24761 still needs it. Task closure alone
does not authorize deleting this carrier workspace.

The first close review found a stale `~` expectation in the existing
`a_detached_worktree_row_does_not_latch_exit` integration test. Correction
a18827db82d15ba11af2a238484e51e70ad9d86a, owned by carrier #24761, updates
the expected directory label. Follow-up 59de9ab1aaa2230ecb57ab2362a18133e29a8e4d
uses the distinct directory `scratch` and corrects the stale test comment,
so the assertion distinguishes directory identity from the detached state.
The production behavior is unchanged.
After that correction, the complete gclient lib, client_loop and parity run
passed all 531 tests (one skipped). Formatting passed; the touched-file test
quality audit found zero issues across 132 tests; suppression audit found
zero new or stale entries. This replaces the narrower filtered Rust evidence
for closure. The correction changes tests only and needs no binary activation.
