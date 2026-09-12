# Gobby-managed attachment

Gobby launches, connects, disconnects, and reconnects the ordinary stdio server
through its existing MCP connection manager. There is no annotation-specific
process manager, runtime hook, credential, or transport.

1. Build and globally install the npm package as described in [mcp.md](mcp.md).
   Check `command -v gobby-annotate-mcp` and invoke it without arguments: it should
   print the `--root` usage error to stderr, not silently exit. Ensure the npm bin
   directory is also in the daemon's subprocess PATH; a terminal shell's PATH
   alone does not establish that. Gobby's actual connection is the final check.
2. Sync bundled Gobby content through the normal installation/sync flow. Inspect
   the installed `gobby-annotate` template row: stdio, command
   `gobby-annotate-mcp`, required `capture_root`, appended with `--root`.
   Merely adding the source YAML does not install or connect a server.
3. Resolve the current project's `.gobby/annotations/` to an absolute directory
   and create it. Inspect the registry for an existing project instance. Reuse
   its root and configuration if present. Otherwise invoke Gobby's top-level tool:

   ```json
   {
     "name": "gobby-annotate",
     "template": "gobby-annotate",
     "scope": "project",
     "values": { "capture_root": "/absolute/project/.gobby/annotations" }
   }
   ```

   Pass this to `add_mcp_server`; do not create a custom launch command. Inspect
   the instantiated server row for the current project and resolved arguments.

4. Through Gobby's proxy, discover schemas and call `list_captures`. Add an export
   to the configured root, call `get_annotation`, then `get_screenshot` and verify
   image content. Use normal disconnect/connect operations and repeat the reads.
   Persisted configuration should remain available after a normal daemon restart.

Troubleshooting: `ENOENT`/command-not-found means the executable or its Node
interpreter cannot be resolved in the daemon environment. Install the package
in that environment or repair PATH, then use normal reconnect. An invalid-root
error means the configured directory is missing, inaccessible, or a symlink;
repair the directory or explicitly update the instance configuration. Invalid
bundles appear as discovery errors; fix or remove that export rather than
changing the MCP launch path. Duplicate export IDs require resolving conflicting
copies. Never hide these failures by starting another private subprocess.

The annotation skill uses this connection to create tasks; the server itself
does not mutate tasks or browser state. Captures moved from a mobile device must
reach this root explicitly; Safari staging is not synchronization.
