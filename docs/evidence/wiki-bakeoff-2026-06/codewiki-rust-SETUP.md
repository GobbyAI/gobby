# CodeWiki - Bake-off SETUP & Teardown

Tool: CodeWiki (FSoft-AI4Code, arXiv:2510.24428). Repo commit 972dba9.
Target repo: gobby-cli (Rust workspace: gcode, gwiki, gsqz, gloc, ghook, gcore).
Model: google/gemma-4-26b-a4b-qat via LM Studio (OpenAI-compatible, host port 1234).
Date: 2026-06-14. All secrets redacted as ***REDACTED***.

## RESULT (headline)
CodeWiki structurally cannot document this repo. gobby-cli is pure Rust; CodeWiki's CLI
repository validator only recognizes Python/Java/JS/TS/C/C++/C#/PHP/Kotlin. ".rs" is NOT in
codewiki/cli/utils/repo_validator.py SUPPORTED_EXTENSIONS, so "codewiki generate" aborts at
step [2/4] "Validating repository..." with RepositoryError: No supported code files found
BEFORE any documentation LLM call. The documented --include "*.rs" flag does not help because
validation runs before include patterns are applied. No docs were produced. The LM Studio
model WAS reached (config validate connectivity test passed); the incompatibility is in
CodeWiki's language gating, not the model or transport.

## Environment
- Working dir: /Users/josh/Projects/wiki-bakeoff/CodeWiki
- Docker Desktop on macOS. host.docker.internal reaches host LM Studio.
- LM Studio at host :1234, OpenAI-compatible, Bearer-token auth REQUIRED (401 without).
- Key (read programmatically, never printed): OPENAI_API_KEY in /Users/josh/Projects/gobby/.env (mode 600).

## Why Docker CLI instead of docker-compose
The shipped docker/docker-compose.yml runs the WEB APP (run_web_app.py, port 8000,
GitHub-URL driven), NOT the local-repo CLI generator. So I built the image and ran the
codewiki CLI as one-off containers. NOTE: the Dockerfile installs requirements.txt but never
"pip install .", so the codewiki console-script is absent -> invoke via "python -m codewiki"
with PYTHONPATH=/app.

## Clean repo copy (avoid 209 GB target/)
    mkdir -p /Users/josh/Projects/wiki-bakeoff/CodeWiki/repo-clean/gobby-cli
    git -C /Users/josh/Projects/gobby-cli archive --format=tar HEAD \
      | tar -x -C /Users/josh/Projects/wiki-bakeoff/CodeWiki/repo-clean/gobby-cli
    # -> 1030 files, 19 MB, 421 .rs files, NO target/ dir. Mounted READ-ONLY at /repo.

## Build the image
    cd /Users/josh/Projects/wiki-bakeoff/CodeWiki
    docker build -f docker/Dockerfile -t codewiki:0.0.1 .
Bundled tree-sitter grammars: python, c-sharp, cpp, c, java, javascript, typescript,
kotlin, yaml. NO Rust grammar present.

## Configure (ONLY url/key/models per fairness rule)
Config persisted to mounted home dir (cw-home -> /cwhome/.codewiki). Keychain unavailable
in-container, so CODEWIKI_NO_KEYRING=1 forces file creds (~/.codewiki/credentials.json,
mode 600; key NOT in config.json).

    mkdir -p /Users/josh/Projects/wiki-bakeoff/CodeWiki/cw-home
    KEY=$(grep -E '^OPENAI_API_KEY=' /Users/josh/Projects/gobby/.env | head -1 | cut -d= -f2- | tr -d '\"' | tr -d "'")
    docker run --rm -e PYTHONPATH=/app -e CODEWIKI_NO_KEYRING=1 -e HOME=/cwhome \
      -e CW_KEY="$KEY" \
      -v /Users/josh/Projects/wiki-bakeoff/CodeWiki/cw-home:/cwhome \
      --add-host=host.docker.internal:host-gateway \
      --entrypoint python codewiki:0.0.1 -m codewiki config set \
        --provider openai-compatible --api-key "$CW_KEY" \
        --base-url http://host.docker.internal:1234/v1 \
        --main-model google/gemma-4-26b-a4b-qat \
        --cluster-model google/gemma-4-26b-a4b-qat \
        --fallback-model google/gemma-4-26b-a4b-qat

Base URL needs the /v1 suffix (pydantic-ai OpenAIProvider does not auto-append it). For
openai-compatible the model string is forwarded unchanged. Final config: see
config.snapshot.json; all token/depth values are CodeWiki DEFAULTS (max_tokens 32768,
per_module 36369, per_leaf 16000, max_depth 2).

## Validate connectivity (DID make a live LLM call)
    docker run --rm -e PYTHONPATH=/app -e CODEWIKI_NO_KEYRING=1 -e HOME=/cwhome \
      -v /Users/josh/Projects/wiki-bakeoff/CodeWiki/cw-home:/cwhome \
      --add-host=host.docker.internal:host-gateway \
      --entrypoint python codewiki:0.0.1 -m codewiki config validate
    # -> "API connectivity test successful" / "Configuration is valid!"

## Generate (both attempts ABORTED at validation; see RUN_LOG.txt)
    # Default config:
    docker run --rm -e PYTHONPATH=/app -e CODEWIKI_NO_KEYRING=1 -e HOME=/cwhome \
      -v /Users/josh/Projects/wiki-bakeoff/CodeWiki/cw-home:/cwhome \
      -v /Users/josh/Projects/wiki-bakeoff/CodeWiki/repo-clean/gobby-cli:/repo:ro \
      -v /Users/josh/Projects/wiki-bakeoff/outputs/CodeWiki/docs-default-run:/out \
      -w /repo --add-host=host.docker.internal:host-gateway \
      --entrypoint python codewiki:0.0.1 -m codewiki generate --output /out --verbose
    # -> RepositoryError: No supported code files found. CLI EXIT 0, no docs.

    # Force-include Rust (still aborts; validation precedes include logic):
    ... generate --output /out --include "*.rs" --verbose   # -> same RepositoryError.

codewiki generate documents Path.cwd(), hence -w /repo. Output /out stayed EMPTY (0 files).

## Artifacts
- RUN_LOG.txt          full console (redacted, with timestamps)
- config.snapshot.json final config, API key redacted
- SETUP.md             this file
- docs/ , docs-default-run/   -> EMPTY (no documentation generated)

## Docker inventory (LEFT IN PLACE per instructions)
Images:
  - codewiki:0.0.1          (built here; the CodeWiki app image)
  - python:3.12-slim        (base layer for the build; also used to write artifacts)
  - curlimages/curl:latest  (used to probe LM Studio; may pre-exist)
Containers: ALL CodeWiki runs were "docker run --rm" one-offs (auto-removed on exit).
NO long-lived CodeWiki container, NO codewiki-network, NO compose stack created.
Persistent HOST dirs created (not containers): cw-home/, repo-clean/ under the CodeWiki repo.

DO NOT TOUCH (daemon infra): gobby-postgres, gobby-postgres-test-1, services-falkordb-1,
services-qdrant-1.

## Teardown (executed)
No CodeWiki containers to stop (all --rm one-offs already gone). Verified with:
    docker ps -a --filter name=codewiki   # (empty)
Images intentionally LEFT: codewiki:0.0.1, python:3.12-slim, curlimages/curl:latest.
Optional later cleanup (NOT performed): "docker image rm codewiki:0.0.1" and deleting the
host dirs cw-home/ repo-clean/ output/ under the CodeWiki repo.
