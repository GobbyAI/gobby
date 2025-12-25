"""
Transcript parsers for different CLI tools.

Each CLI has its own transcript format. This package provides parsers
for extracting conversation data from each format.
"""

from gobby.sessions.transcripts.claude import ClaudeTranscriptParser

__all__ = ["ClaudeTranscriptParser"]
