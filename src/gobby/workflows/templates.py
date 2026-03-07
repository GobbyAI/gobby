import logging
import re
import shlex
from typing import Any, Protocol, runtime_checkable

from jinja2 import Environment, FileSystemLoader, select_autoescape

logger = logging.getLogger(__name__)


@runtime_checkable
class TemplateRenderer(Protocol):
    """Protocol for template rendering.

    Any object with a compatible ``render`` method satisfies this protocol,
    enabling static type checkers to validate callers without coupling them
    to the concrete :class:`TemplateEngine` implementation.
    """

    def render(self, template_str: str, context: dict[str, Any]) -> str: ...


def _regex_replace(value: str, pattern: str, replacement: str) -> str:
    """Jinja2 filter: regex substitution.

    Usage in templates:
        {{ text | regex_replace('pattern', 'replacement') }}
    """
    return re.sub(pattern, replacement, str(value))


def _regex_search(value: str, pattern: str, group: int = 1) -> str:
    """Jinja2 filter: extract a regex capture group from text.

    Usage in templates:
        {{ text | regex_search('library ID: (/\\S+)') }}
        {{ text | regex_search('version (\\d+\\.\\d+)', 1) }}

    Returns the captured group (default group 1), or empty string if no match.
    """
    match = re.search(pattern, str(value))
    if match:
        try:
            return match.group(group)
        except IndexError:
            return match.group(0)
    return ""


def _shlex_quote(value: str) -> str:
    """Jinja2 filter: shell-escape a string using shlex.quote.

    Usage in templates:
        {{ command | shlex_quote }}
    """
    return shlex.quote(str(value))


class TemplateEngine:
    """
    Engine for rendering Jinja2 templates in workflows.
    """

    def __init__(self, template_dirs: list[str] | None = None):
        if template_dirs:
            loader = FileSystemLoader(template_dirs)
        else:
            loader = None

        self.env = Environment(
            loader=loader,
            # Disable autoescape for inline templates (default_for_string=False)
            # We generate markdown, not HTML - escaping breaks apostrophes etc.
            autoescape=select_autoescape(["html", "xml"], default_for_string=False),
            trim_blocks=True,
            lstrip_blocks=True,
        )
        self.env.filters["regex_search"] = _regex_search
        self.env.filters["regex_replace"] = _regex_replace
        self.env.filters["shlex_quote"] = _shlex_quote

    def render(self, template_str: str, context: dict[str, Any]) -> str:
        """
        Render a template string with the given context.
        """
        try:
            template = self.env.from_string(template_str)
            return str(template.render(**context))
        except Exception as e:
            logger.error(f"Error rendering template: {e}", exc_info=True)
            # Fallback to original string or raise?
            # For workflows, it might be better to fail typically, but let's return error message in string for visibility if strict validation isn't on.
            # actually, better to raise so the action fails and handles it.
            raise

    def render_file(self, template_name: str, context: dict[str, Any]) -> str:
        """
        Render a template file with the given context.
        """
        try:
            template = self.env.get_template(template_name)
            return str(template.render(**context))
        except Exception as e:
            logger.error(f"Error rendering template file '{template_name}': {e}", exc_info=True)
            raise
