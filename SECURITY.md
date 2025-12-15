# Security Policy

## Supported Versions

| Version | Supported          |
| ------- | ------------------ |
| latest  | :white_check_mark: |

## Reporting a Vulnerability

If you discover a security vulnerability in Gobby, please report it responsibly:

1. **Do not** open a public GitHub issue for security vulnerabilities
2. Email security concerns to the maintainers directly
3. Include as much detail as possible:
   - Description of the vulnerability
   - Steps to reproduce
   - Potential impact
   - Suggested fix (if any)

We will acknowledge receipt within 48 hours and aim to provide a fix within 7 days for critical issues.

## Security Considerations

### Local-First Architecture

Gobby is designed as a local-first daemon:

- All data is stored locally in SQLite (`~/.gobby/gobby.db`)
- No data is sent to external servers unless explicitly configured
- MCP proxy connections are user-configured

### API Keys and Credentials

- API keys in `config.yaml` should have restricted file permissions
- Never commit `~/.gobby/config.yaml` to version control
- Use environment variables for sensitive values when possible

### Network Exposure

By default, Gobby binds to `localhost`:

- HTTP server: `127.0.0.1:8765`
- WebSocket server: `127.0.0.1:8766`

**Do not** expose these ports to the public internet without proper authentication.

### Hook Security

Hook dispatcher scripts execute with the permissions of the calling AI CLI. Review hook configurations before installation:

```bash
# Review what will be installed
cat src/install/claude/hooks/hook_dispatcher.py
```

## Best Practices

1. Keep Gobby updated to the latest version
2. Review MCP server configurations before adding them
3. Monitor logs at `~/.gobby/logs/` for suspicious activity
4. Use restrictive file permissions on the `~/.gobby/` directory
