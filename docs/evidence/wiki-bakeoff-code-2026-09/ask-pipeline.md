# Native Ask frozen-cohort evidence

Status: **UNRUN**. This preparation artifact contains no native Ask invocation,
provider launch, measurement, or success claim.

## Execution gates

| Gate | Required evidence | Status |
|---|---|---|
| #22018 | Final accepted integration evidence and SHA-256 | UNRUN |
| #22019 | Final accepted integration evidence and SHA-256 | UNRUN |
| Normal-loader runtime admission | Accepted evidence and SHA-256 | UNRUN |
| Installed CLI acceptance | Accepted evidence and SHA-256 | UNRUN |
| Contained runtime isolation | Private home/bootstrap, loopback endpoint, private DB schema, service receipts | UNRUN |
| `ask-investigator` profile snapshot | Definition metadata, full effective profile, and content hash | UNRUN |
| `ask-reviewer` profile snapshot | Definition metadata, full effective profile, and content hash | UNRUN |
| Installed `gcode` | Path, binary SHA-256, version, and CLI contract v10 | UNRUN |

The runtime receipt must name exactly these tool identities:
`gobby-ask:query_evidence`, `gobby-ask:read_evidence`,
`gobby-ask:submit_answer`, `gobby-ask:submit_review`, and
`gobby-agents:end_agent_run`.

## Contained runtime receipt

The accepted runtime identity must include a non-secret `isolation` object. The
contained supervisor creates the service and DB; the cohort runner does not create a
second daemon framework. Paths must be absolute, owner-only, non-symlink artifacts.

```json
{
  "mode": "contained",
  "daemon_url": "http://127.0.0.1:REPLACE_PRIVATE_PORT",
  "gobby_home": "/REPLACE/private-runtime/gobby",
  "bootstrap": {"path": "/REPLACE/private-runtime/gobby/bootstrap.yaml", "sha256": "REPLACE"},
  "database": {
    "host": "127.0.0.1", "port": 60892, "name": "gobby_test",
    "schema": "gobby_test_REPLACE_UNIQUE", "receipt": {"path": "/REPLACE/database.json", "sha256": "REPLACE"}
  },
  "service": {
    "identity": "REPLACE_16_HEX_DEPLOYMENT_TOKEN", "daemon_url": "http://127.0.0.1:REPLACE_PRIVATE_PORT",
    "receipt": {"path": "/REPLACE/service.json", "sha256": "REPLACE"}
  }
}
```

The database receipt JSON contains exactly `host`, `port`, `name`, and `schema`.
The service receipt JSON contains exactly the daemon deployment token as `identity` and
the `daemon_url`. No credentials or database URL enter the cohort manifest. The runner
rejects duplicate bootstrap identity keys and requires the database URL query to contain
exactly one canonically encoded `options=-csearch_path=<schema>` value; libpq destination
overrides and additional settings fail closed. Before preparation, every primary, and
every export, it revalidates private ownership and all three receipt hashes. Immediately
before each Ask and export, a read-only `gcode status` materializes the interactive grant.
The runner then presents that grant and the contained operator token to the authenticated
daemon `/api/runtime/config` route at the same sealed base URL used by Ask/export, and
reads `/api/projects/<id>`. The accepted deployment token, grant-derived effective
database/search-path identity and config revision, project id, and checkout root must all
match their receipts; an absent or foreign daemon fails before the mutating request.
The runner supplies `GOBBY_DAEMON_URL`, `GOBBY_HOME`, and `GOBBY_TEST_PROTECT=1`
explicitly to each subprocess. Only a small allowlist of non-secret OS process variables
is inherited; ambient DB, daemon-port, session, task, provider-token, cloud-credential,
and managed-execution variables are excluded.

## Prepared commands

These commands are documentation only and have not been run. Replace the absolute
placeholder paths only after all gates above are accepted.

```bash
UV_NO_SYNC=1 UV_PROJECT_ENVIRONMENT=/Users/josh/Projects/gobby/.venv PYTHONPATH=src uv run --no-sync python docs/evidence/wiki-bakeoff-code-2026-09/ask_cohort.py prepare --runtime-identity /REPLACE/accepted-contained-runtime-identity.json --gcode-bin /REPLACE/installed/gcode --project-root /REPLACE/game-goblins --output-root /REPLACE/ask-cohort-output
UV_NO_SYNC=1 UV_PROJECT_ENVIRONMENT=/Users/josh/Projects/gobby/.venv PYTHONPATH=src uv run --no-sync python docs/evidence/wiki-bakeoff-code-2026-09/ask_cohort.py run-primary --manifest /REPLACE/ask-cohort-output/cohort-manifest.json
UV_NO_SYNC=1 UV_PROJECT_ENVIRONMENT=/Users/josh/Projects/gobby/.venv PYTHONPATH=src uv run --no-sync python docs/evidence/wiki-bakeoff-code-2026-09/ask_scoring.py review --manifest /REPLACE/ask-cohort-output/cohort-manifest.json --output /REPLACE/ask-cohort-output/review-packet.json
UV_NO_SYNC=1 UV_PROJECT_ENVIRONMENT=/Users/josh/Projects/gobby/.venv PYTHONPATH=src uv run --no-sync python docs/evidence/wiki-bakeoff-code-2026-09/ask_scoring.py score --packet /REPLACE/ask-cohort-output/review-packet.json --judgments /REPLACE/parent-reviewed-judgments.json --output-json /REPLACE/ask-cohort-output/scored.json --output-report /REPLACE/ask-cohort-output/report.md
```

Primary runs are serial, deterministic-retrieval runs with a 600-second service
deadline and a 630-second bounded client wait. A retry or hybrid diagnostic uses the
separate `supplement` command, requires an operator reason, and never replaces the
primary attempt in cohort scoring.

Before `run-primary`, verify the manifest hash, contained receipts, source commit/tree
identities, and Q14 first-parent name-status inventory. After it returns, freeze all
Q01-Q14 primary outcomes and hashes before considering a supplement. Review and score
once against those primaries; `report.md` materializes per-question retrieval and
answer metrics, raw hashes, runtime/usage, unsupported statements, and missing exact
values. Copy the reviewed result into this acceptance artifact without replacing any
primary failure.

## Frozen cohort

| Q | Commit | Primary | Retrieval | Answer review | Raw publication |
|---|---|---|---|---|---|
| Q01 | `0216f1e33f05962d49467d95fe84609041c6dba8` | UNRUN | UNRUN | UNRUN | UNRUN |
| Q02 | `0216f1e33f05962d49467d95fe84609041c6dba8` | UNRUN | UNRUN | UNRUN | UNRUN |
| Q03 | `0216f1e33f05962d49467d95fe84609041c6dba8` | UNRUN | UNRUN | UNRUN | UNRUN |
| Q04 | `0216f1e33f05962d49467d95fe84609041c6dba8` | UNRUN | UNRUN | UNRUN | UNRUN |
| Q05 | `0216f1e33f05962d49467d95fe84609041c6dba8` | UNRUN | UNRUN | UNRUN | UNRUN |
| Q06 | `0216f1e33f05962d49467d95fe84609041c6dba8` | UNRUN | UNRUN | UNRUN | UNRUN |
| Q07 | `0216f1e33f05962d49467d95fe84609041c6dba8` | UNRUN | UNRUN | UNRUN | UNRUN |
| Q08 | `0216f1e33f05962d49467d95fe84609041c6dba8` | UNRUN | UNRUN | UNRUN | UNRUN |
| Q09 | `0216f1e33f05962d49467d95fe84609041c6dba8` | UNRUN | UNRUN | UNRUN | UNRUN |
| Q10 | `0216f1e33f05962d49467d95fe84609041c6dba8` | UNRUN | UNRUN | UNRUN | UNRUN |
| Q11 | `0216f1e33f05962d49467d95fe84609041c6dba8` | UNRUN | UNRUN | UNRUN | UNRUN |
| Q12 | `0216f1e33f05962d49467d95fe84609041c6dba8` | UNRUN | UNRUN | UNRUN | UNRUN |
| Q13 | `0216f1e33f05962d49467d95fe84609041c6dba8` | UNRUN | UNRUN | UNRUN | UNRUN |
| Q14 | `8b24ac26699aac8b24254a647aa70b208287b492` | UNRUN | UNRUN | UNRUN | UNRUN |

The runner embeds the exact original prompt string and the commit's resolved tree OID
in each immutable primary attempt before launching `gcode ask`. Preparation also
seals Q14's first parent and complete first-parent `name-status` inventory without
copying the external gold answer into prompts or the source corpus.

## Scoring contract

The historical gcode gold-span retrieval hits are Q01, Q07, Q10, Q11, Q12, and
Q13 (6/14). This is a frozen comparison datum, not a native Ask result. Native
retrieval records first-query and cumulative-after-follow-up gold-span overlap,
query count, first supporting query/rank, reciprocal rank, recall at 5/10/20,
citation precision at 10, wrong-domain collisions, and completeness/continuation.
Filenames or hashes without the exact source-line overlap do not count as retrieval
support.

Answer quality is a distinct reviewed score: gold-component coverage,
correctness, source-supported claim precision, unsupported statements, citation
identity integrity, and direct/inferred/unknown accuracy. Q08 requires an honest
unknown and ambiguity rationale. Q14 requires the exact nine changed paths,
case-sensitive longest-prefix rules, automatic non-excluded scope, Hobby Supplies
minimum 2, and `Sleeves: ` minimum 4. The scorer never treats the historical 6/14
retrieval result as answer correctness and never uses an LLM answer key.

Every direct or inferred claim requires at least one valid citation; an uncited claim
fails citation integrity even when another claim has a valid citation. Unknown claims
may remain uncited, including the honest Q08 unknown.

## Accounting and conclusion

Every primary is authoritative as completed, failed, contract-error, interrupted, or
UNRUN. Raw prompt, stdout, stderr, result, publication manifest, answer, evidence,
source excerpts, hashes, wall time, and usage remain reviewable. Retry and hybrid
artifacts are separately labelled.

Errors after the primary process returns are also authoritative. Export failures are
typed `export_error`; output-persistence failures are typed `artifact_error`; an
operator interrupt during export is persisted as `interrupted/operator_interrupt`.
Isolation drift is a typed contract error and stops further execution. A resumed run
skips every existing primary attempt and never replaces it.

The accepted installed-binary hash is rechecked immediately before every primary,
retry, and hybrid invocation. A mismatch is recorded as an immutable contract-error
attempt and never replaced by a resumed run.

Conclusion: **BLOCKED / UNRUN** pending #22018, #22019, final runtime admission,
installed CLI acceptance, fourteen native primary attempts, and parent/validation-agent review.
