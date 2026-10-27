# P3.4 fallback operator preparation and blind ratings

Retained as an inactive fallback for task #22604. Josh selected completing
item 4 first on 2026-10-26, followed by the full D/G/J and carry study.
Do not execute this fallback package under that selection. These commands
were checked offline; no live study or human ratings are claimed here.

The later [item-4 captures](../decisions/item-4-checklist-2026-10-26.md)
completed but did not satisfy the activation gate. No study ran as part of
that capture work. The old temporary directory named below no longer exists;
these commands are retained as historical preparation, not a runnable handoff.
Any selected study needs newly prepared output paths and confirmed C1 readiness.

## Choose the planned study branch

The prepared branch is `--no-decisions`, required by
`.gobby/plans/community-label-decisions.md:1062–1064` while OpenRouter's
item-4 checklist has an unmet item and this fallback is selected. The full
study will use a separate output directory and updated instructions after
the checklist is complete; do not remove the flag from this package silently.

The Orchestrator must first confirm C1 registration/indexing under the
existing ID `c8e071af-e1f7-434f-b3c0-f31a1b1299c9` and at least 15 eligible
communities with three or more members. The source folder is
`/Users/josh/Projects/wiki-bakeoff-code-2026-09/corpora/gcode/C1`.
The study also requires 15 eligible Gobby communities. Its loader reads
structural rows in a read-only transaction; label attempts and writes go
to an in-memory recording object. It uses the configured text-generation
service to generate names, so preparation performs inference. It does not
change live labels or configuration.

## Script identity and validation

The prepare and score CLI is the existing landed script:

`/Users/josh/Projects/gobby/scripts/community_label_study.py`

SHA256: `07a77db40dac6ac83cb388fc05a10e0aa81c64c0aab0f0a5acf7780079784552`.

Its shared flag/config module is
`/Users/josh/Projects/gobby/scripts/decisions_live_capture.py`, SHA256
`1df0ba09baf98b052842cbe186cc8c245d4ad66865631af4ce01d0be6accd0b7`.
The task checkout's files match landed `0.5.0` at
`c2f3f06f670e1dc33f996644da754fc26bb2b6fc`. Verify these hashes before the
operator run. Both prepare and score help were checked. Focused offline
validation passed all eight tests:

```sh
DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/scripts/test_community_label_study.py -q
```

The tests use synthetic communities and generation, stubbed decisions,
and isolated storage. They cover sampling, insufficient corpora, blinding,
shared generations, backend changes, scoring, all ship branches and the
read-only loader. Test ratings are not Josh's study ratings.

## Operator prepare command

Run only after the branch choice and C1 readiness are confirmed. The output
directory must be new: `prepare` can overwrite existing study files.

```sh
test ! -e /private/var/folders/5w/9cmg71vd2m108t5r_fb77l0h0000gn/T/gobby-lag7b7s1/label-study-no-decisions-20261026
```

Then run:

```sh
PYTHONPATH=/Users/josh/Projects/gobby/src uv run --project /Users/josh/Projects/gobby python /Users/josh/Projects/gobby/scripts/community_label_study.py prepare --wire-api openrouter-decisions --api-base https://openrouter.ai/api --model typesafe/jev-1.14-20261014 --identity-contract response_version --no-decisions --out /private/var/folders/5w/9cmg71vd2m108t5r_fb77l0h0000gn/T/gobby-lag7b7s1/label-study-no-decisions-20261026
```

The shared parser requires the decision endpoint/model flags, but this
branch does not construct a decision client, resolve a decision secret or
send decision requests. It needs no `--api-key-secret`. Any later full
study must pass only the stored secret name, never a key value.

The outputs in that directory are:

- `sheet.csv`: shuffled item IDs, member paths, candidate name and two
  empty rating columns. This is the only data file sent to Josh.
- `key.json`: corpus/community and arm mappings, kept from Josh until
  ratings are final.
- `run.json`: sampled communities, outcome events and run metadata, also
  kept from Josh until ratings are final.

The sample is fixed at 30 communities: 15 per corpus, five per size tercile,
seed 22604. Identical deterministic/generated names share one rating row.
This branch therefore produces **30–60 rows**, with two ratings per row.
The Assistant should count the actual CSV records after preparation and
state that exact number when delivering the sheet. Review the run's
generation failures before presenting it; failed generation can collapse
an item to the deterministic name and is not proof of generation quality.

The sheet hides model, arm, deterministic/generated origin, community IDs
and decision metadata. Member paths remain visible and can reveal the
corpus. This is a study of the current landed behavior; it contains no
old-versus-repaired arm. The full study, if selected later, can have up to
120 rows including carry items; this fallback has no J, carry or
sufficiency ratings.

## Instructions the Assistant sends Josh

“For each row, read the member paths and the proposed name. Fill the two
rating cells with 0, 1 or 2. For **accuracy**, use 0 if the name is misleading,
1 if it fits only partly or is too broad, and 2 if it describes the group
well. For **findability**, use 0 if the name would not help you find this
code, 1 if it gives a useful clue, and 2 if it is a clear term you would
search for. Rate each row on its own; please do not guess which method
produced the name. Keep all item IDs, paths and names unchanged. You can
pause and resume. Expect roughly 15–30 minutes for 30–60 rows; large groups
may take longer. Return the completed CSV to me in this same conversation.”

The scale explanations above are rating instructions for this run; the
plan specifies the two integer scales and the metrics derived from them.
Only Josh supplies the real ratings. There is no `rate` CLI command.
The Assistant saves his returned file as
`/private/var/folders/5w/9cmg71vd2m108t5r_fb77l0h0000gn/T/gobby-lag7b7s1/label-study-no-decisions-20261026/sheet.rated.csv`
and gives the developer the path and hash. Preserve the original sheet,
private key and run record.

## Developer score command after Josh returns ratings

Use the same script and SHA256 shown above:

```sh
PYTHONPATH=/Users/josh/Projects/gobby/src uv run --project /Users/josh/Projects/gobby python /Users/josh/Projects/gobby/scripts/community_label_study.py score --sheet /private/var/folders/5w/9cmg71vd2m108t5r_fb77l0h0000gn/T/gobby-lag7b7s1/label-study-no-decisions-20261026/sheet.rated.csv --key /private/var/folders/5w/9cmg71vd2m108t5r_fb77l0h0000gn/T/gobby-lag7b7s1/label-study-no-decisions-20261026/key.json --capture /dev/null --out /private/var/folders/5w/9cmg71vd2m108t5r_fb77l0h0000gn/T/gobby-lag7b7s1/label-study-no-decisions-20261026/label-quality-results.md
```

For a recorded `no_decisions` run, the required `--capture` argument is
unused: decision cost is zero and the file is not read. Do not use this
score command for a full study, which needs its genuine OpenRouter capture.

Scoring rejects missing, duplicate, unknown or out-of-range ratings. It
writes metrics and a proposed ship configuration without applying it.
Under this fallback all decision consumers remain off; G's regression
count chooses generated labeling or deterministic-only labeling. The
developer incorporates the output, sample and reported run provenance into
`label-quality.md`, then submits the evidence for review. Any later live
configuration action remains operator-side. After P3.4 lands, the
Orchestrator soft-deletes the C1 registration; its folder and marker stay.

Sources: `.gobby/plans/community-label-decisions.md:852–927,970–1027,1062–1077`;
`scripts/community_label_study.py` (`_load_communities`, `RecordingStorage`,
`_sample`, `_prepare`, `_ratings`, `_score`, `main`); and
`scripts/decisions_live_capture.py:add_decision_flags`. The scripts are
not visible in this worktree's code index, so these were read directly.

_Last verified: 2026-10-26_
