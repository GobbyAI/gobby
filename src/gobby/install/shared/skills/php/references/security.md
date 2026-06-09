# PHP Security

Use this reference for web input, authorization, authentication, sessions,
cookies, uploads, database queries, SSRF, command execution, templates,
serialization, secrets, and dependency changes.

## Input And Output

- Treat request bodies, query params, route params, headers, cookies, sessions,
  uploaded files, env vars, database rows, queues, webhooks, and decoded JSON as
  untrusted.
- Validate type, format, size, range, encoding, and authorization before use.
- Escape output in templates and responses according to context: HTML, attribute,
  JavaScript, CSS, URL, shell, SQL, or JSON.
- Avoid unserializing untrusted data; prefer JSON with explicit validation.

## Authorization And Sessions

- Check authorization at the use-case or policy boundary, not only in UI links.
- Keep authentication, authorization, validation, and not-found outcomes
  distinct.
- Protect CSRF-sensitive state changes using the framework's mechanism.
- Set secure cookie attributes and avoid logging session IDs or tokens.

## Files And Paths

- Validate uploads for size, MIME/content, extension, storage location, and scan
  policy where required.
- Prevent path traversal by resolving paths against allowed roots.
- Do not pass user-controlled paths, command args, SQL, XPath, LDAP, or template
  fragments into interpreters without safe binding or escaping.

## HTTP And SSRF

- Bound outbound HTTP timeouts, redirects, protocols, DNS/IP ranges, and response
  sizes when input influences the destination.
- Avoid sending secrets to user-controlled URLs.
- Validate webhook signatures, timestamps, replay windows, and idempotency.

## Dependencies And Secrets

- Keep Composer dependency changes minimal and review abandoned or vulnerable
  packages when the repo has security tooling.
- Never commit real secrets in config, tests, snapshots, fixtures, logs, or
  exception messages.
- Use framework secret stores or env handling consistently with local practice.
