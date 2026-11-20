# Node enrollment and node install spec corpus

`corpus.json` holds spec cases written from the acceptance lines of
`.gobby/plans/node-install-enrollment.md`. They are not replayed, because no Python
implementation of these verbs exists to record. `test_node_enrollment_corpus.py` checks
the corpus against the plan, the declared auth routes and the hub-node command contract
(`docs/contracts/node-commands.md`).

Each case carries:

- `id`: the plan's acceptance anchor.
- `kind`: `enrollment` or `node_install`.
- `command`: the node verb the anchor runs.
- `plan_line`: the plan line of the anchor.
- `anchor_text`: that line, verbatim, including the test it names. Most named tests do
  not exist in this tree: the Rust port adds them, and the Python golden
  `tests/install/test_node_setup_golden.py` with them. Of the named tests, only
  `crates/gdaemon/tests/nodes.rs::node_startup_requires_a_pinned_hub` and
  `tests/e2e/test_hub_node_pair.py::test_node_enrolls_and_relays` exist here. What this
  directory runs is `test_node_enrollment_corpus.py`, which checks the corpus, not the verbs.
- `http`: present only where the anchor's outcome rests on the hub's answer to a route.
- `description`: optional. It says in prose what a case covers beyond its single `command`.

## Entry points

- 2.1.1 runs `gdaemon node release-info`.
- 2.2.1 and 2.2.2 cover `gclient`'s node setup wrappers. Their behaviour is shared by
  both `gclient node install` and `gclient node enroll`, and `gclient node install` is the
  representative `command` for both verbs.
  - 2.2.1: with no daemon URL or token, either verb execs the sibling `gdaemon node <verb>`
    with its arguments unchanged and returns its exit status.
  - 2.2.2: with no sibling `gdaemon`, either verb exits 2 and names the expected path.

  Each case's `description` names both verbs. The plan anchors state install and enroll
  together, so there is no separate case for enroll.
- 2.1.2 and 2.2.3 are excluded. They are version bumps, which move to the cutover that
  promotes a binary.
