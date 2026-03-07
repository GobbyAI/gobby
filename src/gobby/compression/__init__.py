"""Output compression for LLM token optimization.

Composable primitives (filter, group, truncate, dedup) applied per-command
to reduce verbose CLI output before it reaches the LLM context window.
"""

from gobby.compression.compressor import CompressionResult, OutputCompressor

__all__ = ["CompressionResult", "OutputCompressor"]
