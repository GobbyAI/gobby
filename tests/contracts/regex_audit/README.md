# Regex audit fixtures

These files hold the synthetic facts the generic regex portability audit in
`tests/contracts/test_regex_audit.py` checks. No file here names, counts or copies a bundled
definition: the audit enumerates whatever bundled YAML exists and checks one property for every
pattern it finds.

## The universe

The audit reads every `*.yaml` and `*.toml` file under the two scanned directories,
`src/gobby/install/shared` and `.gobby/workflows/rules`, through the production enumerator, plus
the production contrastive constants of the assistant-response matcher. Adding, renaming or
deleting a bundled definition fails no test unless one of its patterns fails the property.

## The property

Every found pattern gets a consumer class from a code constant in `regex_audit_tool.py`, and
`classify(pattern, consumer)` gives it one verdict:

- `as_written`: the pattern means the same on both engines under its consumer as written;
- `rewrite`: an exact family rewrite gives a Rust text with the same observable;
- `reported`: a report row for the operator (a proposal, a lookaround no family covers, an
  inert pattern, or a semantic decision); a reported row is never a failure;
- `fail`: the pattern cannot be ported and the cause is not a reportable shape. The reason names
  the cause: `unknown-construct:<text>` (the lexer does not know a construct), `python-invalid`
  (not valid Python on every supported minor), `parser-disagreement` (the lexer and Python's
  parser disagree on its structure), `rust-rejected:<id>` (a construct Rust does not compile and
  no translation covers), `rust-reinterpreted:<id>` (a construct Rust compiles with another
  meaning, such as a possessive quantifier or the verbose flag), `rewrite-mismatch:<input>` (a
  claimed family rewrite differs from the original on a probe) or `two-engine-mismatch:<input>`
  (a text called exact disagrees between the engines).

A verdict is exact only after the two-engine driver passes it: `verify` sends every exact text
to the Rust driver and compares each probe with the Python observable of the original. `verify`
compares only `as_written` and `rewrite` rows and leaves a `reported` row unchanged, with its
`classify` reason and `verified` false, so a proposal is never sent to the driver. Proposals and
semantic decisions are report rows, not passes.

A pattern is python-valid only when every supported minor (3.13 and 3.14) compiles it; a
pattern that one minor rejects is `fail` with the reason `python-invalid` on both legs. A minor
the gate does not run is known through the construct rows only.

## Consumer classes

| class | Python engine | external flags | driver mode |
|---|---|---|---|
| BOOL | Python engine `re` | none | search |
| INERT-EXEMPTION | Python engine `re` | none | search (no consumer call) |
| FULLMATCH | Python engine `re` | none | fullmatch |
| MATCH-GROUPS-FIRSTLINE | Python engine `re` | none | match, first line only |
| SEARCH-PKG-M | Python engine `regex` | multi_line | search |
| SPAN-ALLOWLIST | Python engine `re` | case_insensitive | search |
| FULLMATCH-PATH | Python engine `re` | none | fullmatch |
| SEARCH-GROUP | Python engine `re` | none | search |
| SUB | Python engine `re` | none | sub |
| UNKNOWN | Python engine `re` | none | search (no consumer call) |

`consumers.json` drives each class with a synthetic pattern, an input and a control through its
production consumer; the two observables differ and each equals its recorded value. A
`flag_guard` case adds `case_insensitive` or `multi_line` to the consumer's own regex call and
shows the recorded control changes, so the class's flagless entry is a fact of production. Two
(class, flag) pairs need no guard: MATCH-GROUPS-FIRSTLINE with `multi_line` is
`single-line-subject` (the consumer matches the first line only), and SPAN-ALLOWLIST with
`multi_line` is `no-anchor-in-allowlist` (the allowlisted constants hold no anchor).

## Observables

- Boolean classes compare truth; SPAN-ALLOWLIST compares the span; MATCH-GROUPS-FIRSTLINE and
  SEARCH-GROUP compare the match, its span and its group spans; FULLMATCH-PATH compares a
  fullmatch.
- Group spans: an absent group is None on the Python side and null in the driver; unmatched groups are
  normalized to None before any comparison, never compared as (-1, -1).
- SUB: the substitution observable is every match (span and groups, in order) and the replaced
  text. A SUB pattern that can match the empty string is reported as
  `sub-empty-match-iteration` (Python accepts an empty match directly after a non-empty one and
  the Rust iterator does not); a SUB pattern with no two-match witness and no single-match proof
  is reported as `no-multi-match-witness`. Replacement templates are not classified: the port's
  replace filter expands Python templates itself.

## Boundaries and membership

- `\b` and `\B` are checked on three subject groups (empty, nonword only, word edge) on every
  supported minor. `\B` on the empty subject differs between minors (3.13 no match, 3.14 match),
  so a pattern that can reach it is reported under an `-empty-subject` decision id, unless the
  alternative with the boundary must consume a character (`restricted:nonempty-alternative`).
- Membership is keyed by engine, minor and atom. An atom whose Python set differs between the
  supported minors, or that is outside the closed basis (the six shorthands and the ASCII block
  under ignore case), is reported under a `-version-difference` id.
- R5 independence under `i` reads version-stable fold sets only; a literal whose fold is not
  stable or not measured gives `lookaround-fold-not-version-stable` on both legs.
- A newline in a lookaround body is decided from the pattern text with no membership read
  (`class_matches_newline`).

## Record and replay

Replay (the default run):

    DATABASE_URL=<test hub> GOBBY_TEST_PROTECT=1 uv run --no-sync pytest tests/contracts/test_regex_audit.py

Record (re-records the `expect` blocks of `consumers.json` and regenerates the reports):

    GOBBY_CORPUS_RECORD=1 GOBBY_REGEX_QUESTIONS_PATH=<questions file> DATABASE_URL=<test hub> \
      GOBBY_TEST_PROTECT=1 uv run --no-sync pytest \
      tests/contracts/test_regex_audit.py::test_rerecord_regex_audit_report

`docs/port/regex-audit.json` and `docs/port/regex-audit.md` are generated by the record run and
never asserted. Any change to a consumer, a driver or a fixture is followed by a re-record, and
a second record run changes no byte.

## Conventions

- Fixture literals are short synthetic words (q, syn, synq, a, b, x, zzz, go, echo, agree,
  object, slow, fast, not and the like, in either letter case) plus the one code point U+00B7;
  definition names start with `synthetic-`.
- `rules_corpus.find_definition_references` guards every file here: a value at a definition
  reference position that does not start with `synthetic-`, or a path below a bundled root other
  than the two scanned directories named alone, is reported.
- A known-bad demonstration is synthetic by its `synthetic-` rule name, and its `when` reads
  every argument from `event.data` (for example the command from
  `event.data.get('tool_input', {}).get('command', '')`). No test compares a demonstration `when`
  with a bundled one; its non-vacuity comes from the input and control decisions differing.
