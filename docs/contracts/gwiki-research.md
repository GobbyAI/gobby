# Gobby gwiki Research Boundary

The canonical `gwiki research` contract lives in the gobby-cli repository at
`docs/contracts/gwiki-research.md`. Gobby consumes that contract as an enhancer,
not as the owner of wiki mutation.

Gobby responsibilities:

- route `--ai daemon` model calls through the daemon frontier,
- schedule explicit per-project research and `--audit` cron jobs,
- run pipeline/build stages that invoke `gwiki research` or
  `gwiki research --audit`,
- review resulting notes or findings with agents after the CLI command exits.

Gobby must not be called from gwiki as an agent-spawn dependency. The old shape
where `gwiki research` posted to `/api/agents/spawn` with `--task-id`,
`--agent-count`, or `--resume` is retired by the gobby-cli contract.

Reference commits:

- Spec: `/Users/josh/Projects/gobby-cli@6b5c5ba8660624438d7416b412ec94ff86dec9a2`.
- First implementation slice:
  `/Users/josh/Projects/gobby-cli@f9bbde3fb836cac1bf99047558d5d2cca869310b`.

The implementation removes `gwiki research` daemon agent dispatch, drops
`--task-id`, `--agent-count`, and `--resume`, adds deterministic
`gwiki research --audit --ai off`, and updates the machine-readable gwiki
contract mirror.
