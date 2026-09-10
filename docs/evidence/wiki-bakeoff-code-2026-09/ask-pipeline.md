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
| `ask-investigator` profile snapshot | Database-derived definition/provider/model/reasoning/content hash | UNRUN |
| `ask-reviewer` profile snapshot | Database-derived definition/provider/model/reasoning/content hash | UNRUN |
| Installed `gcode` | Path, binary SHA-256, version, and CLI contract v10 | UNRUN |

The runtime receipt must name exactly these tool identities:
`gobby-ask:query_evidence`, `gobby-ask:read_evidence`,
`gobby-ask:submit_answer`, `gobby-ask:submit_review`, and
`gobby-agents:end_agent_run`.

## Prepared commands

These commands are documentation only and have not been run. Replace the absolute
placeholder paths only after all gates above are accepted.

```bash
UV_PROJECT_ENVIRONMENT=/Users/josh/Projects/gobby/.venv PYTHONPATH=src uv run --no-sync python docs/evidence/wiki-bakeoff-code-2026-09/ask_cohort.py prepare --runtime-identity /REPLACE/accepted-runtime-identity.json --gcode-bin /REPLACE/installed/gcode --project-root /REPLACE/game-goblins --output-root /REPLACE/ask-cohort-output
UV_PROJECT_ENVIRONMENT=/Users/josh/Projects/gobby/.venv PYTHONPATH=src uv run --no-sync python docs/evidence/wiki-bakeoff-code-2026-09/ask_cohort.py run-primary --manifest /REPLACE/ask-cohort-output/cohort-manifest.json
UV_PROJECT_ENVIRONMENT=/Users/josh/Projects/gobby/.venv PYTHONPATH=src uv run --no-sync python docs/evidence/wiki-bakeoff-code-2026-09/ask_scoring.py review --manifest /REPLACE/ask-cohort-output/cohort-manifest.json --output /REPLACE/ask-cohort-output/review-packet.json
UV_PROJECT_ENVIRONMENT=/Users/josh/Projects/gobby/.venv PYTHONPATH=src uv run --no-sync python docs/evidence/wiki-bakeoff-code-2026-09/ask_scoring.py score --packet /REPLACE/ask-cohort-output/review-packet.json --judgments /REPLACE/parent-reviewed-judgments.json --output-json /REPLACE/ask-cohort-output/scored.json --output-report /REPLACE/ask-cohort-output/report.md
```

Primary runs are serial, deterministic-retrieval runs with a 600-second service
deadline and a 630-second bounded client wait. A retry or hybrid diagnostic uses the
separate `supplement` command, requires an operator reason, and never replaces the
primary attempt in cohort scoring.

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
in each immutable primary attempt before launching `gcode ask`.

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

## Accounting and conclusion

Every primary is authoritative as completed, failed, contract-error, interrupted, or
UNRUN. Raw prompt, stdout, stderr, result, publication manifest, answer, evidence,
source excerpts, hashes, wall time, and usage remain reviewable. Retry and hybrid
artifacts are separately labelled.

Conclusion: **BLOCKED / UNRUN** pending #22018, #22019, final runtime admission,
installed CLI acceptance, fourteen native primary attempts, and parent/validation-agent review.
