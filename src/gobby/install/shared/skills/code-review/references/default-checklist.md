# Default Review Checklist

The built-in checklist open-code-review applies when no language rule matches
(its `default` rule group). Use it in the fallback path when `ocr` is unavailable,
or for a previewed file whose rule group is missing.

## Correctness

- Is the logic correct? Are there missing boundary conditions?
- Are exceptions handled properly?
- Is it safe under concurrency?

## Security

- Are there injection vulnerabilities such as SQL injection or XSS?
- Is sensitive information handled correctly?
- Is permission validation complete?

## Performance

- Are there obvious performance issues (N+1 queries, unnecessary loops)?
- Are resources properly released?

## Maintainability

- Is the code clear and easy to understand?
- Do names accurately express intent?
- Does it follow the project's existing code style and architecture patterns?

## Test Coverage

- Do critical logic paths have corresponding test cases?
- Do test cases cover boundary conditions?
