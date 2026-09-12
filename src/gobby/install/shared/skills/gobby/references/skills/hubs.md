# Search and configure skill hubs

Load when installed guidance is insufficient or managing external sources.
Start with `list_hubs`, then `search_hub(query="<topic>")`; omit `hub_name` to
search configured hubs, or select one returned name. Inspect `hub_errors`
even when the overall response succeeds: partial results are not an exhaustive
negative search. Preserve the returned install identifier, especially GitHub
topic IDs that include repository and path. Search does not install or load.

Configuration defaults include anthropic-skills, gobby-topic, claude-plugins,
clawdhub and skillsmp; actual configured providers may differ. Inspect
`auth_required`, `auth_configured`, and secret-reference names without printing
secret values. A missing hub manager or unknown hub requires configuration,
not guesses at URLs or repeated failed searches.

Operators can use `gobby skills search`, `gobby skills hub list`, and
`gobby skills hub add`. The add command accepts `--type github` for a GitHub
collection (stored as `github-collection`); required URL/repo flags depend on
type. Adding writes revision-checked configuration. There is no `hub remove`
command: update configuration or choose a distinct name. Use configuration
operations for provider types not exposed by the add command.

Install only the selected result through the installation workflow and load its
instructions afterward. A successful download is not evidence the installed
skill exists, passed checks, or is active.

Guide: [Skills](../../../../../../../../docs/guides/skills.md#skill-hubs).

_Last verified: 2026-09-12_
