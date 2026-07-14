# Testing And Tooling

## Validation Layers

Use complementary checks:

1. `bash -n` catches parse errors for Bash syntax.
2. ShellCheck catches quoting, status, portability, and data-flow mistakes.
3. shfmt checks the repository's formatting policy.
4. Runtime tests prove exit status, streams, filesystem effects, and cleanup.
5. Platform or version matrices cover environment-sensitive behavior.

Run repo wrappers when present because they carry pinned versions, exclusions,
directives, and formatting options.

## Focused Commands

For one script and one Bats file:

```bash
bash -n scripts/deploy.sh
shellcheck scripts/deploy.sh
shfmt -d scripts/deploy.sh
shfmt -d -ln=bats tests/deploy.bats
bats tests/deploy.bats
```

Use the actual interpreter for POSIX `sh` or another shell. A successful
`bash -n` does not prove portability.

## ShellCheck

Fix findings at the data or control-flow boundary. Add a targeted directive
only when the code intentionally violates the rule and a comment explains the
invariant ShellCheck cannot infer.

Check sourced files with the repo's source-path configuration. Pin the dialect
where file discovery or shebang inference is insufficient.

Standard Bats `@test` syntax is preprocessed and may be rejected by general
shell tools. Lint Bats through the repo's configured integration or use Bats'
valid-Bash `function test_name { #@test` form when direct ShellCheck support is
required.

## Formatting

Use the repo's shfmt flags. Formatting changes can alter heredoc indentation or
expose malformed continuations, so review the diff and rerun syntax checks after
formatting.

Avoid mixing an entire legacy-file reformat with a behavioral change unless the
repository explicitly requires it.

## Runtime Tests

Assert these separately:

- exact exit status;
- stdout and stderr ownership;
- created, modified, and removed paths;
- child-process status propagation;
- cleanup after command failure and termination;
- handling of spaces, newlines, glob characters, and leading dashes;
- absent optional tools and missing configuration;
- repeated or concurrent execution where supported.

Use a temporary HOME, working directory, config root, and PATH. Supply fake
executables through that PATH to control external command behavior without
mocking shell functions under test.

## Bats

Keep setup and teardown idempotent. Quote `$output` and inspect `$status`
explicitly. Separate tests for output, status, and side effects when a failure
would otherwise be ambiguous.

Use helper libraries already pinned by the repo. Avoid network access and real
user configuration in tests.

## Environment Matrix

Test every supported Bash major/minor version when using version-dependent
features. Add the operating systems or container images that differ in core
utilities, PATH layout, filesystem behavior, or signal handling.

Document any environment gap with the exact untested boundary and reason.
