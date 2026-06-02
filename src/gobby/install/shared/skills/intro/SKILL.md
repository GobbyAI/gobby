# Intro

Capture the user's global Gobby working profile once and save it to
`~/.gobby/personal/USER.md`.

## Goal

Create or update one personal profile file that can be injected into every fresh
interactive agent session. This profile is global and must not be copied into any
repository.

## Workflow

1. Read the existing file at `~/.gobby/personal/USER.md` if it exists.
2. Infer low-risk facts from local context:
   - Git identity from `git config --global user.name` and `git config --global user.email`.
   - Shell from `$SHELL`.
   - OS and machine context from standard local commands such as `uname -a`.
   - Editor or terminal preferences only when they are obvious from environment variables.
3. Ask only for missing facts that materially affect agent behavior. Keep the
   questions targeted and short. Do not ask for information already present in
   the file.
4. Write `~/.gobby/personal/USER.md` with exactly these sections, in this order:
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

- The path written.
- Which sections were created or updated.
- Any missing information the user chose not to provide.
