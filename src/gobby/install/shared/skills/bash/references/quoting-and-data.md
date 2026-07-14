# Quoting And Data

## Expansions

Quote substitutions by default:

```bash
printf '%s\n' "$value"
output="$(generate_output)"
cp -- "$source" "$destination"
```

Leave expansions unquoted only for a specific shell operation such as glob or
pattern matching, and keep that intent local. Inside `[[ ... ]]`, quoting the
right side of `==` changes a glob pattern into a literal; inside `=~`, quoting
changes regex behavior. Test those expressions directly.

Use `printf` for data. `echo` varies across implementations and interprets some
values as options or escapes.

## Arrays And Commands

Represent a command as an array of arguments:

```bash
args=(deploy --environment "$environment")
if [[ -n "$tag" ]]; then
  args+=(--tag "$tag")
fi
tool "${args[@]}"
```

Each array element remains one argument. A string such as
`command="tool --tag $tag"` loses those boundaries and tempts `eval`.

Use `"${items[@]}"` to preserve elements. Use `"${items[*]}"` only when a
single joined string is the required output. Check array length before expanding
under `set -u` on Bash versions with empty-array edge cases.

## Positional Arguments And Options

Validate argument counts before accessing positions under `set -u`. Shift only
after consuming a known option. Reject unknown options and missing values with a
non-zero exit status.

Use `getopts` for portable short options. Use an explicit `while`/`case` parser
when long options are required. Preserve remaining operands with `"$@"`.

Pass `--` before user-controlled filenames for tools that implement the
end-of-options convention:

```bash
rm -- "$path"
```

## Reading Records And Filenames

Line-oriented input:

```bash
while IFS= read -r line || [[ -n "$line" ]]; do
  process_line "$line"
done < "$file"
```

Filename streams require NUL delimiters:

```bash
while IFS= read -r -d '' path; do
  process_file "$path"
done < <(find "$root" -type f -print0)
```

Avoid parsing `ls` output and avoid `for item in $(command)`. Both destroy record
boundaries.

## Status-Sensitive Assignment

Declaration builtins can hide substitution failure. Split the operations:

```bash
local output
if ! output="$(produce_output)"; then
  printf 'produce_output failed\n' >&2
  return 1
fi
```

Use parameter expansion for simple string operations. Switch to a structured
language or a format-aware tool for JSON, YAML, XML, or nested data.
