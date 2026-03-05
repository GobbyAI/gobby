# Webhook Actions and Plugin Development Guide

This guide covers two powerful workflow extension mechanisms in Gobby:
1. **Webhook Actions** - Make HTTP requests to external services during workflow execution
2. **Plugin Actions** - Create custom workflow actions with Python plugins

## Webhook Actions

Webhook actions allow workflows to send HTTP requests to external services like Slack, Discord, or custom APIs. They support variable interpolation, retry logic, and response capture.

### YAML Syntax Reference

```yaml
- action: webhook
  # Required: One of url OR webhook_id
  url: "https://example.com/api/endpoint"   # Direct URL
  # OR
  webhook_id: "slack_alerts"                 # Reference to registered webhook

  # Optional parameters
  method: POST                               # GET, POST, PUT, PATCH, DELETE (default: POST)
  timeout: 30                                # Request timeout in seconds (1-300, default: 30)

  # Request headers (supports ${secrets.VAR} interpolation)
  headers:
    Content-Type: "application/json"
    Authorization: "Bearer ${secrets.API_TOKEN}"
    X-Custom-Header: "value"

  # Request body (supports ${variable} interpolation)
  payload:
    message: "Hello from session ${session_id}"
    data: "${context.summary}"

  # Retry configuration
  retry:
    max_attempts: 3                          # Total attempts including first (1-10)
    backoff_seconds: 2                       # Base delay, doubles each retry
    retry_on_status:                         # HTTP codes to retry on
      - 429                                  # Rate limited
      - 500                                  # Server error
      - 502                                  # Bad gateway
      - 503                                  # Service unavailable
      - 504                                  # Gateway timeout

  # Response capture (store response data in workflow variables)
  capture_response:
    status_var: "webhook_status"             # HTTP status code
    body_var: "webhook_body"                 # Response body (auto-parsed as JSON if valid)
    headers_var: "webhook_headers"           # Response headers dict

  # Callbacks (action names to execute)
  on_success: "notify_success"               # Execute on 2xx response
  on_failure: "handle_failure"               # Execute after all retries exhausted
```

### Parameter Reference

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `url` | string | - | Target URL (required if no `webhook_id`) |
| `webhook_id` | string | - | Registered webhook reference (required if no `url`) |
| `method` | enum | `POST` | HTTP method: GET, POST, PUT, PATCH, DELETE |
| `headers` | dict | `{}` | Request headers (supports secret interpolation) |
| `payload` | dict/string | `null` | Request body (supports variable interpolation) |
| `timeout` | int | `30` | Request timeout in seconds (1-300) |
| `retry` | object | `null` | Retry configuration |
| `capture_response` | object | `null` | Variables to capture from response |
| `on_success` | string | `null` | Action to run on 2xx response |
| `on_failure` | string | `null` | Action to run after retries exhausted |

### Variable Interpolation

Webhooks support variable interpolation using `${...}` syntax:

| Syntax | Description | Example |
|--------|-------------|---------|
| `${var}` | Workflow variable | `${session_id}`, `${task_title}` |
| `${context.var}` | Context variable | `${context.summary}` |
| `${secrets.VAR}` | Secure secret | `${secrets.SLACK_TOKEN}` |
| `${env.VAR}` | Environment variable | `${env.API_URL}` |

**Security Notes:**
- `${secrets.*}` values are never logged in plaintext
- Secrets are redacted as `[REDACTED]` in error messages
- Secrets are not stored in workflow state

---

## Integration Examples

### Example 1: Slack Notifications

Send workflow events to a Slack channel:

```yaml
name: slack-notifications
type: lifecycle

triggers:
  on_session_start:
    - action: webhook
      url: "${env.SLACK_WEBHOOK_URL}"  # Set in environment
      method: POST
      headers:
        Content-Type: "application/json"
      payload:
        text: "Session started by ${context.cli_name}"
        blocks:
          - type: section
            text:
              type: mrkdwn
              text: "*Session Started*\n`${session_id}`"
          - type: context
            elements:
              - type: mrkdwn
                text: "Project: ${context.project_name}"

  on_session_end:
    - action: webhook
      url: "${env.SLACK_WEBHOOK_URL}"
      payload:
        text: "Session ended"
        attachments:
          - color: "good"
            title: "Session Summary"
            text: "${context.summary}"
```

**Slack Webhook Setup:**
1. Go to [api.slack.com/apps](https://api.slack.com/apps) and create an app
2. Enable "Incoming Webhooks" and add to your workspace
3. Copy the webhook URL

### Example 2: Discord Notifications

Send messages to a Discord channel:

```yaml
name: discord-notifications
type: lifecycle

triggers:
  on_session_end:
    - action: webhook
      url: "https://discord.com/api/webhooks/1234567890/abcdefghijklmnop"
      method: POST
      payload:
        username: "Gobby Bot"
        avatar_url: "https://example.com/bot-avatar.png"
        embeds:
          - title: "Session Complete"
            description: "${context.summary}"
            color: 5763719  # Green color
            fields:
              - name: "Session ID"
                value: "`${session_id}`"
                inline: true
              - name: "Duration"
                value: "${context.duration}"
                inline: true
            footer:
              text: "Gobby Workflow Engine"
            timestamp: "${context.timestamp}"

  on_error:
    - action: webhook
      url: "https://discord.com/api/webhooks/1234567890/abcdefghijklmnop"
      payload:
        username: "Gobby Bot"
        embeds:
          - title: "Workflow Error"
            description: "${error_message}"
            color: 15158332  # Red color
```

**Discord Webhook Setup:**
1. Open channel settings in Discord
2. Go to Integrations > Webhooks
3. Click "New Webhook" and copy the URL

### Example 3: Custom API Integration

Send data to your own API with authentication and response handling:

```yaml
name: custom-api-workflow
type: lifecycle

triggers:
  on_session_end:
    # Step 1: Create a ticket in your tracking system
    - action: webhook
      url: "https://api.yourcompany.com/v1/tickets"
      method: POST
      headers:
        Authorization: "Bearer ${secrets.API_TOKEN}"
        Content-Type: "application/json"
        X-Request-ID: "${session_id}"
      payload:
        title: "Session ${session_id} completed"
        description: "${context.summary}"
        tags: ["gobby", "automated"]
        metadata:
          session_id: "${session_id}"
          project: "${context.project_name}"
      timeout: 15
      retry:
        max_attempts: 3
        backoff_seconds: 2
        retry_on_status: [429, 500, 502, 503]
      capture_response:
        status_var: "ticket_status"
        body_var: "ticket_response"

    # Step 2: Send Slack notification with ticket link (only if ticket created)
    - action: webhook
      when: "${ticket_status} == 201"
      url: "${env.SLACK_WEBHOOK_URL}"
      payload:
        text: "Ticket created: ${ticket_response.url}"
```

### Example 4: Jira Integration with Chained Webhooks

Create Jira issues and notify on completion:

```yaml
name: jira-integration
type: lifecycle

triggers:
  on_session_end:
    # Create Jira issue
    - action: webhook
      url: "https://yourcompany.atlassian.net/rest/api/3/issue"
      method: POST
      headers:
        Authorization: "Basic ${secrets.JIRA_API_TOKEN}"
        Content-Type: "application/json"
      payload:
        fields:
          project:
            key: "DEV"
          summary: "Review: ${context.first_message}"
          description:
            type: "doc"
            version: 1
            content:
              - type: "paragraph"
                content:
                  - type: "text"
                    text: "${context.summary}"
          issuetype:
            name: "Task"
      capture_response:
        body_var: "jira_issue"
        status_var: "jira_status"

    # Notify team
    - action: webhook
      when: "${jira_status} == 201"
      url: "${env.SLACK_WEBHOOK_URL}"
      payload:
        text: "Created Jira issue: <${jira_issue.self}|${jira_issue.key}>"
```

### Example 5: Using Registered Webhooks

Pre-configure webhooks in `~/.gobby/config.yaml`:

```yaml
# ~/.gobby/config.yaml
webhooks:
  slack_general:
    url: "${env.SLACK_WEBHOOK_URL}"
    headers:
      Content-Type: "application/json"

  slack_alerts:
    url: "${env.SLACK_ALERTS_WEBHOOK_URL}"
    headers:
      Content-Type: "application/json"

  pagerduty:
    url: "https://events.pagerduty.com/v2/enqueue"
    headers:
      Content-Type: "application/json"
```

Then reference them in workflows:

```yaml
on_error:
  - action: webhook
    webhook_id: "slack_alerts"
    payload:
      text: "Error in workflow: ${error_message}"

  - action: webhook
    webhook_id: "pagerduty"
    payload:
      routing_key: "${secrets.PAGERDUTY_KEY}"
      event_action: "trigger"
      payload:
        summary: "Gobby workflow failed"
        source: "gobby"
        severity: "warning"
```

---

## Troubleshooting

### Common Webhook Failures

#### 1. Connection Timeout

**Symptom:** Webhook fails with "Timeout after 30s"

**Causes:**
- Target server is slow or unreachable
- Network connectivity issues
- Firewall blocking outbound requests

**Solutions:**
```yaml
# Increase timeout
- action: webhook
  url: "https://slow-api.example.com"
  timeout: 60  # Increase from default 30s

# Add retry for transient issues
  retry:
    max_attempts: 3
    backoff_seconds: 2
```

#### 2. Authentication Failures (401/403)

**Symptom:** HTTP 401 Unauthorized or 403 Forbidden

**Causes:**
- Invalid or expired API token
- Missing secret configuration
- Wrong authentication header format

**Solutions:**
```yaml
# Verify secret is configured
headers:
  Authorization: "Bearer ${secrets.API_TOKEN}"  # Check this secret exists

# For Basic auth, use base64 encoding
headers:
  Authorization: "Basic ${secrets.BASIC_AUTH}"  # Value: base64(user:pass)
```

Check secrets are set in environment or config:
```bash
export GOBBY_SECRET_API_TOKEN="your-token-here"
```

#### 3. Rate Limiting (429)

**Symptom:** HTTP 429 Too Many Requests

**Causes:**
- Sending too many requests to the API
- API rate limit exceeded

**Solutions:**
```yaml
# Add retry with backoff
- action: webhook
  url: "https://api.example.com"
  retry:
    max_attempts: 5
    backoff_seconds: 5  # Longer backoff for rate limits
    retry_on_status: [429]
```

#### 4. Invalid Payload (400)

**Symptom:** HTTP 400 Bad Request

**Causes:**
- Malformed JSON payload
- Missing required fields
- Invalid field types or values

**Solutions:**
```yaml
# Ensure payload matches API expectations
payload:
  text: "string value"        # Not: text: 123
  blocks: []                  # Empty array, not null

# Check variable interpolation produces valid JSON
payload:
  message: "${context.summary}"  # Ensure summary doesn't break JSON
```

#### 5. SSL/TLS Certificate Errors

**Symptom:** SSL certificate verification failed

**Causes:**
- Self-signed certificate
- Expired certificate
- Certificate chain issues

**Solutions:**
- Use valid SSL certificates in production
- For development, the daemon can be configured to skip verification (not recommended for production)

#### 6. Missing Secret Reference

**Symptom:** `ValueError: Missing secret 'SECRET_NAME' referenced in header`

**Causes:**
- Secret not configured in environment or config
- Typo in secret name

**Solutions:**
```bash
# Set the secret in environment
export GOBBY_SECRET_SLACK_TOKEN="xoxb-your-token"

# Or add to config.yaml
secrets:
  SLACK_TOKEN: "xoxb-your-token"
```

#### 7. Webhook ID Not Found

**Symptom:** `ValueError: webhook_id 'name' not found in registry`

**Causes:**
- Webhook not registered in config
- Typo in webhook_id

**Solutions:**
```yaml
# Add webhook to ~/.gobby/config.yaml
webhooks:
  my_webhook:  # This is the webhook_id
    url: "https://example.com"
```

### Debugging Webhooks

Enable debug logging to see request/response details:

```bash
# Start daemon with verbose logging
gobby start --verbose

# Or set in config
logging:
  level: DEBUG
```

Check webhook execution in logs:
```
DEBUG Webhook POST https://api.example.com -> 200 (0.45s)
DEBUG Webhook retry 2/3, backoff 2s
WARNING Webhook POST https://api.example.com failed: HTTP 500
```

### Plugin Troubleshooting

#### Plugin Not Loading

**Symptom:** Plugin actions not available

**Checks:**
1. Plugin file in correct location (`~/.gobby/plugins/`)
2. Plugin enabled in config
3. No syntax errors in plugin file
4. Plugin class has `name` attribute

```bash
# Check plugin status
gobby status --plugins
```

#### Action Validation Errors

**Symptom:** "Missing required field" or "invalid type"

**Solution:** Check your YAML matches the action schema:
```yaml
# Wrong
- action: plugin:example-notify:log_metric
  metric_name: "test"
  # Missing required 'value'

# Correct
- action: plugin:example-notify:log_metric
  metric_name: "test"
  value: 42.5
```

---

## Best Practices

### Security

1. **Never hardcode secrets** - Always use `${secrets.VAR}` interpolation
2. **Use HTTPS** - Avoid HTTP URLs for sensitive data
3. **Validate SSL certificates** - Don't disable verification in production
4. **Limit retry attempts** - Avoid overwhelming failing services
5. **Set reasonable timeouts** - Don't hang indefinitely

### Reliability

1. **Always configure retries** for critical webhooks
2. **Use `on_failure`** to handle errors gracefully
3. **Capture response** to debug issues
4. **Log webhook results** for auditing

### Performance

1. **Use registered webhooks** for frequently used endpoints
2. **Set appropriate timeouts** - Don't wait too long for slow services
3. **Avoid blocking workflows** - Use background processing for slow APIs

---

## See Also

- [Rules Guide](rules.md) - Rule engine documentation (events, effects, conditions)
- [Workflows Overview](workflows-overview.md) - How rules, agents, and pipelines fit together
- [Example Plugin](../../examples/plugins/example_notify.py) - Full plugin implementation
- [Code Guardian Plugin](../../examples/plugins/code_guardian.py) - Advanced plugin example
