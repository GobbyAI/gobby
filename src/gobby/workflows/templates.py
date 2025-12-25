import logging
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

logger = logging.getLogger(__name__)


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
            autoescape=select_autoescape(["html", "xml"]),
            trim_blocks=True,
            lstrip_blocks=True,
        )

    def render(self, template_str: str, context: dict[str, Any]) -> str:
        """
        Render a template string with the given context.
        """
        try:
            template = self.env.from_string(template_str)
            return template.render(**context)
        except Exception as e:
            logger.error(f"Error rendering template: {e}", exc_info=True)
            # Fallback to original string or raise?
            # For workflows, it might be better to fail typically, but let's return error message in string for visibility if strict validation isn't on.
            # actually, better to raise so the action fails and handles it.
            raise e

    def render_file(self, template_name: str, context: dict[str, Any]) -> str:
        """
        Render a template file with the given context.
        """
        try:
            template = self.env.get_template(template_name)
            return template.render(**context)
        except Exception as e:
            logger.error(f"Error rendering template file '{template_name}': {e}", exc_info=True)
            raise e
