"""CLI command: gobby compress -- <command>

Runs a command and compresses its output for LLM consumption.
Used by PreToolUse hook rewriting to reduce token usage.
"""

import subprocess
import sys

import click
import httpx


@click.command()
@click.option("--stats", is_flag=True, help="Show compression statistics to stderr")
@click.argument("command", nargs=-1, required=True)
def compress(command: tuple[str, ...], stats: bool) -> None:
    """Run a command and compress its output for LLM consumption.

    Usage: gobby compress -- git status
    """
    cmd = " ".join(command)

    result = subprocess.run(
        cmd,
        shell=True,
        capture_output=True,
        text=True,
    )

    raw_output = result.stdout
    if result.stderr:
        raw_output += result.stderr

    if not raw_output:
        sys.exit(result.returncode)

    from gobby.compression import OutputCompressor

    # Read compression config from daemon (best-effort, fall back to defaults)
    min_length = 1000
    excluded: list[str] = []
    try:
        from gobby.utils.daemon_client import DaemonClient

        resp = DaemonClient().call_http_api("/api/config/values", method="GET", timeout=1.0).json()
        cfg = resp.get("output_compression", {})
        min_length = cfg.get("min_output_length", min_length)
        excluded = cfg.get("excluded_commands", excluded)
        max_lines = cfg.get("max_compressed_lines", 100)
    except (httpx.HTTPError, ValueError, KeyError):
        max_lines = 100

    compressor = OutputCompressor(
        min_length=min_length,
        max_lines=max_lines,
        excluded_commands=excluded,
    )
    compressed = compressor.compress(cmd, raw_output)

    if stats:
        click.echo(
            f"[compress] strategy={compressed.strategy_name} "
            f"original={compressed.original_chars} "
            f"compressed={compressed.compressed_chars} "
            f"savings={compressed.savings_pct:.1f}%",
            err=True,
        )

    # Track savings via gobby-metrics (best-effort, non-blocking)
    if compressed.strategy_name not in ("passthrough", "excluded"):
        try:
            from gobby.utils.daemon_client import DaemonClient

            client = DaemonClient()
            client.call_http_api(
                "/api/metrics/counter",
                method="POST",
                json_data={
                    "name": "compression_chars_saved",
                    "value": compressed.original_chars - compressed.compressed_chars,
                    "labels": {"strategy": compressed.strategy_name},
                },
                timeout=1.0,
            )
        except httpx.HTTPError:
            pass  # Non-critical — don't fail the command

    output = compressed.compressed
    if compressed.strategy_name not in ("passthrough", "excluded"):
        output = f"[Output compressed by Gobby — {compressed.strategy_name}, {compressed.savings_pct:.0f}% reduction]\n{output}"
    click.echo(output, nl=False)
    sys.exit(result.returncode)
