# gobby-mcp

`gmcp` is Gobby's native MCP wrapper. It connects provider CLIs to the local
daemon over HTTP and preserves the Python wrapper's caller identity, capability
refresh, protocol header, and error contracts.

This crate currently provides the reusable blocking daemon dialer and terminal
context capture. The stdio transport adds the `gmcp` executable in the next task;
CLI MCP configurations will launch `gmcp` directly rather than a Python process.
The dialer never starts a daemon. Its caller should run requests on a blocking
pool when serving asynchronous transports.

Server-coupled values are embedded from `contracts/mcp_wrapper.v1.json` and checked
against Python constants by `tests/mcp_proxy/test_gmcp_contracts.py`.
