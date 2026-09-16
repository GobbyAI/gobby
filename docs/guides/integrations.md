# Integrations Guide

Gobby does not own GitHub or Linear issue, pull-request, or project identity.
Those products stay available as MCP servers. The generic MCP proxy discovers
and calls their tools. Gobby does not import issues into tasks, sync titles
or state, run GitHub issue triage, or open GitHub PRs.

Bundled templates `github.yaml` and `linear.yaml` remain. Instantiating them
does not restore Gobby-owned linkage fields.

## MCP is the integration surface

Run these from a Gobby project directory. Discover live servers and tools
before calling them:

```bash
gobby mcp-proxy list-servers
gobby mcp-proxy add-server github --template github
gobby mcp-proxy add-server linear --template linear
gobby mcp-proxy list-tools --server github
gobby mcp-proxy list-tools --server linear
```

Configure missing servers through the [MCP server guide](./mcp-tools.md#templates-and-instances).
Agents use progressive discovery on the generic proxy; listing these procedures
does not authorize an external write.

`import_mcp_server` can import a server definition from a GitHub repository URL.
That is MCP configuration import, not GitHub issue import.

## What Gobby no longer provides

- `gobby github` and `gobby linear` CLI groups
- `gobby init --github-url` / `gobby projects update --github-*`
- Task tools `import_github_issues`, `link_task_to_github_issue`,
  `close_linked_github_issue`, and `open_delivery_pr`
- GitHub issue triage webhooks and `/api/projects/{id}/github-triage`
- GitHub-MCP source-control HTTP for PRs, issues, and CI

Call the GitHub or Linear MCP server directly for issue and PR operations.
Local git merge, worktrees, clones, and git-local source-control HTTP remain.

## Related

- [MCP tools](./mcp-tools.md) — templates, instances, and progressive discovery
- [Webhooks and plugins](./webhooks-and-plugins.md) — comms and hook-extension
  delivery, not GitHub issue intake
- [CLI commands](./cli-commands.md) — operator MCP and hook commands
