import pytest
from jinja2 import Environment, FileSystemLoader, TemplateNotFound
from jinja2.exceptions import SecurityError
from jinja2.sandbox import SandboxedEnvironment

from gobby.workflows.templates import TemplateEngine

pytestmark = pytest.mark.unit


class TestTemplateEngine:
    def test_init_defaults(self) -> None:
        engine = TemplateEngine()
        assert isinstance(engine.env, SandboxedEnvironment)
        assert engine.env.loader is None
        assert engine.env.autoescape

    def test_init_with_dirs(self, tmp_path) -> None:
        template_dir = tmp_path / "templates"
        template_dir.mkdir()
        engine = TemplateEngine(template_dirs=[str(template_dir)])
        assert type(engine.file_env) is Environment
        assert isinstance(engine.file_env.loader, FileSystemLoader)
        assert str(template_dir) in engine.file_env.loader.searchpath

    def test_render_string_success(self) -> None:
        engine = TemplateEngine()
        template_str = "Hello {{ name }}!"
        result = engine.render(template_str, {"name": "World"})
        assert result == "Hello World!"

    def test_render_string_error(self) -> None:
        engine = TemplateEngine()
        # Create a template that raises an error during rendering
        # Using a variable that doesn't exist won't raise by default unless we set undefined
        # But we can force an error by doing an operation that fails
        template_str = "{{ x + 1 }}"

        with pytest.raises(TypeError):
            engine.render(template_str, {"x": "string"})

    def test_render_string_blocks_unsafe_attribute_access(self) -> None:
        engine = TemplateEngine()

        with pytest.raises(SecurityError):
            engine.render("{{ [].__class__.__mro__ }}", {})

    def test_render_file_success(self, tmp_path) -> None:
        template_dir = tmp_path / "templates"
        template_dir.mkdir()
        (template_dir / "test.html").write_text("Hello {{ name }}!")

        engine = TemplateEngine(template_dirs=[str(template_dir)])
        result = engine.render_file("test.html", {"name": "File"})
        assert result == "Hello File!"

    def test_render_file_not_found(self, tmp_path) -> None:
        engine = TemplateEngine(template_dirs=[str(tmp_path)])
        with pytest.raises(TemplateNotFound):
            engine.render_file("nonexistent.html", {})

    def test_render_file_render_error(self, tmp_path) -> None:
        template_dir = tmp_path / "templates"
        template_dir.mkdir()
        # Create a template that will cause a runtime error
        (template_dir / "error.html").write_text("{{ x + 1 }}")

        engine = TemplateEngine(template_dirs=[str(template_dir)])
        with pytest.raises(TypeError):
            engine.render_file("error.html", {"x": "string"})
