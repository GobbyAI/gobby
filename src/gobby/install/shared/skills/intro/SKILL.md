---
name: intro
description: "Capture the user's global Gobby working profile once and save it to <files_home>/USER.md on the hub, or PUT /api/files/user-md on a remote."
version: "1.1.0"
category: core
triggers:
  - intro
  - onboarding
  - working profile
metadata:
  gobby:
    audience: all
---
# Intro

Capture the user's global Gobby working profile once. On the hub owner, save
it to `<files_home>/USER.md`. On a remote node, PUT the same text to
`/api/files/user-md` on `hub_daemon_url`. Never write
`~/.gobby/personal/USER.md`.

## Goal

Create or update one personal profile file that can be injected into every fresh
interactive agent session. This profile is global and must not be copied into any
repository.

## Operator prerequisites

Writers never create the `files_home` root. Provision that hub bind directory
first, then install, then migrate, then start.

1. On the hub host, create the existing absolute bind directory. Standalone
   and laptop hubs use `$GOBBY_HOME/files`. Do not use `$GOBBY_HOME` itself
   or `$GOBBY_HOME/personal`.
2. Local hub install: `gobby install --files-home <existing-abs-dir>`.
3. Remote install requires `hub_daemon_url` in the node bootstrap and a copy of
   the hub's existing `local_cli_token`. The remote installer does not generate
   or rotate that token.
4. Upgrade or stop every remote before migrate. Copy leftover node-local
   `USER.md`, personal tree, wiki, and project attachments onto the hub's
   legacy source locations (`$GOBBY_HOME/personal`, `~/wiki/topics`,
   `$GOBBY_HOME/projects/<id>/attachments`) first. This epic does not collect
   files from other machines.
5. Run `gobby files migrate` on the hub while the daemon is stopped, then
   `gobby start`. Do not start until migrate has finished.

## Workflow

1. Decide the write path from this process's bootstrap:
   - Local (`datastore_mode: local`): read and write
     `<files_home>/USER.md`. `files_home` is the absolute path in the local
     bootstrap. Do not fall back to `$GOBBY_HOME/personal/USER.md`.
   - Remote (`datastore_mode: remote`): GET then PUT
     `{hub_daemon_url}/api/files/user-md` with body `{"content": "..."}`.
     Authenticate with the hub `local_cli_token`. Do not mkdir
     `~/.gobby/personal` and do not write a node-local `USER.md`.
2. Infer low-risk facts from local context:
   - Git identity from `git config --global user.name` and `git config --global user.email`.
   - Shell from `$SHELL`.
   - OS and machine context from standard local commands such as `uname -a`.
   - Editor or terminal preferences only when they are obvious from environment variables.
3. Ask only for missing facts that materially affect agent behavior. Keep the
   questions targeted and short. Do not ask for information already present in
   the file.
4. Write the profile with exactly these sections, in this order:
   - `Identity`
   - `Working Style`
   - `Preferences`
   - `Autonomy & Boundaries`
   - `Never-Do`
5. Preserve useful existing content by moving it into the closest matching
   section. Remove duplicates and stale contradictions.
6. Keep the profile concise. Prefer durable preferences over task-specific
   details.

## Section Guidance

### Identity

Name, role, common project context, git identity, operating system, shell, and
other stable facts useful to an agent starting cold.

### Working Style

How the user likes work sequenced, how much planning they expect, how direct the
agent should be, and what quality bar matters.

### Preferences

Tooling, editor, terminal, validation, documentation, formatting, and communication
preferences.

### Autonomy & Boundaries

What the agent may do without asking, when it must pause for approval, and what
counts as an acceptable assumption.

### Never-Do

Hard prohibitions. Include destructive commands, privacy boundaries, security
boundaries, and any user-specific dealbreakers.

## Output

After writing the file, report:

- The path or URL written (`<files_home>/USER.md` or
  `PUT {hub_daemon_url}/api/files/user-md`).
- Which sections were created or updated.
- Any missing information the user chose not to provide.
