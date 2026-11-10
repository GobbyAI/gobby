"""Side-effect-free preflight of arbitrary rule definitions through engine primitives.

Unresolved runtime inputs are advisory. Grammar, schema, template and available
evaluation failures are errors. External effects and stateful helpers never run.
"""

import ast
import inspect
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, fields, replace
from typing import Any, Literal

from jinja2 import TemplateError, UndefinedError, nodes
from pydantic import ValidationError

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.workflows.definitions import RuleDefinitionBody
from gobby.workflows.evaluation_context import snapshot_evaluation_context
from gobby.workflows.regex_validation import iter_definition_regexes
from gobby.workflows.safe_evaluator import SafeExpressionEvaluator
from gobby.workflows.templates import create_workflow_template_environment


@dataclass(frozen=True)
class RuleValidationIssue:
    field: str
    code: str
    message: str
    level: Literal["error", "warning"] = "error"


@dataclass(frozen=True)
class RuleDryRunEffect:
    effect_type: str
    fields: dict[str, Any]
    executed: Literal[False] = False


@dataclass
class RuleValidation:
    name: str
    source: str | None = None
    issues: list[RuleValidationIssue] = field(default_factory=list)
    trace: list[RuleDryRunEffect] = field(default_factory=list)

    @property
    def errors(self) -> list[RuleValidationIssue]:
        return [item for item in self.issues if item.level == "error"]

    @property
    def warnings(self) -> list[RuleValidationIssue]:
        return [item for item in self.issues if item.level == "warning"]

    @property
    def valid(self) -> bool:
        return not self.errors

    def raise_for_errors(self) -> None:
        if self.errors:
            location = f"{self.source}: " if self.source else ""
            details = "; ".join(f"{item.field}: {item.message}" for item in self.errors)
            raise ValueError(f"{location}Rule {self.name!r}: {details}")


class RuntimeDataUnavailable(ValueError):
    """A dry run requires a live input or a stateful helper."""


class _UnavailableValue:
    def _unavailable(self, *args: Any, **kwargs: Any) -> Any:
        raise RuntimeDataUnavailable("Live objects are unavailable during dry run")

    __bool__ = __str__ = __iter__ = __getitem__ = __getattr__ = _unavailable


def _detached(value: Any) -> Any:
    """Admit data, never live callables, lazy predicates or foreign object methods."""
    if type(value) in {str, int, float, bool, type(None), HookEventType, SessionSource}:
        return value
    if type(value) is dict:
        return {key: _detached(item) for key, item in value.items() if type(key) is str}
    if type(value) in {list, tuple}:
        return [_detached(item) for item in value]
    if type(value) is HookEvent:
        return replace(
            value, **{item.name: _detached(getattr(value, item.name)) for item in fields(value)}
        )
    return _UnavailableValue()


class _DryRunEvaluator(SafeExpressionEvaluator):
    def visit(self, node: ast.AST) -> Any:
        result = super().visit(node)
        if isinstance(result, _UnavailableValue):
            raise RuntimeDataUnavailable("Live objects are unavailable during dry run")
        return result

    def visit_Name(self, node: ast.Name) -> Any:
        if node.id not in self.context and node.id not in self.allowed_funcs:
            if node.id not in {"true", "false", "none", "True", "False", "None"}:
                raise RuntimeDataUnavailable(f"Runtime variable {node.id!r} is unavailable")
        return super().visit_Name(node)


def _unavailable_helper(name: str, function: Callable[..., Any]) -> Callable[..., Any]:
    try:
        signature = inspect.signature(function)
    except (ValueError, TypeError):
        signature = None

    def unresolved(*args: Any, **kwargs: Any) -> Any:
        if signature is not None:
            signature.bind(*args, **kwargs)
        raise RuntimeDataUnavailable(f"Helper {name!r} requires runtime context")

    return unresolved


def _has_unavailable_cause(error: BaseException) -> bool:
    while True:
        if isinstance(error, RuntimeDataUnavailable | UndefinedError):
            return True
        if error.__cause__ is None:
            return False
        error = error.__cause__


def _condition_functions(event: str, context: dict[str, Any]) -> dict[str, Callable[..., Any]]:
    # These are the same registries used by the two runtime engines, rather than
    # a second inventory which can silently drift when helpers are introduced.
    if event in {"task_close", "task_land"}:
        from gobby.tasks.closure_policy import build_closure_condition_helpers

        functions = build_closure_condition_helpers(context)
    else:
        from gobby.workflows.engine.templating import build_rule_condition_helpers

        functions = build_rule_condition_helpers(context)
    pure = {len, bool, str, int, list, dict, any, all, isinstance}
    return {
        name: function if function in pure else _unavailable_helper(name, function)
        for name, function in functions.items()
    }


def validate_rule(
    definition: Mapping[str, Any] | str,
    *,
    name: str = "<rule>",
    source: str | None = None,
    context: Mapping[str, Any] | None = None,
) -> RuleValidation:
    """Validate and prepare a rule without DB, subprocess, dispatcher or handler access.

    Context is optional, copied, and follows the engine's evaluation context shape.
    All expressions and templates are checked even when a condition would skip them.
    Trace entries describe prepared effects; none claim an external effect executed.
    """
    report = RuleValidation(name=name, source=source)
    # Imported here: the llm package reaches the workflow engine, so a top-level import cycles.
    from gobby.llm.sdk_utils import MAX_CONTEXT_LABEL_CHARS

    label = f"rule:{name}"
    if len(label) > MAX_CONTEXT_LABEL_CHARS:
        report.issues.append(
            RuleValidationIssue(
                "name",
                "label",
                f"rule '{name}' makes a {len(label)}-character context label; "
                f"the limit is {MAX_CONTEXT_LABEL_CHARS}",
            )
        )
        return report
    try:
        data = json.loads(definition) if isinstance(definition, str) else dict(definition)
        body = RuleDefinitionBody.model_validate(data)
    except ValidationError as exc:
        for error in exc.errors():
            location = ".".join(map(str, error["loc"])) or "definition"
            report.issues.append(RuleValidationIssue(location, "schema", error["msg"]))
        return report
    except (ValueError, TypeError) as exc:
        report.issues.append(RuleValidationIssue("definition", "schema", str(exc)))
        return report

    ctx = snapshot_evaluation_context(_detached(dict(context or {})))
    if isinstance(ctx.get("variables"), dict):
        for key, value in ctx["variables"].items():
            ctx.setdefault(key, value)
    functions = _condition_functions(body.event.value, ctx)
    evaluator = _DryRunEvaluator(ctx, functions)
    environment = create_workflow_template_environment(strict_undefined=True)

    def failure(path: str, code: str, exc: BaseException) -> None:
        unresolved = _has_unavailable_cause(exc)
        report.issues.append(
            RuleValidationIssue(
                path,
                "unresolved" if unresolved else code,
                str(exc),
                "warning" if unresolved else "error",
            )
        )

    def expression(value: str, path: str, *, condition: bool = True) -> Any:
        try:
            SafeExpressionEvaluator.validate_syntax(value, set(functions))
        except (ValueError, SyntaxError) as exc:
            failure(path, "expression", exc)
            return None
        try:
            return evaluator.evaluate(value) if condition else evaluator.evaluate_value(value)
        except ValueError as exc:
            failure(path, "evaluation", exc)
            return None

    def prepare(value: Any, path: str) -> Any:
        if isinstance(value, dict):
            return {key: prepare(item, f"{path}.{key}") for key, item in value.items()}
        if isinstance(value, list):
            return [prepare(item, f"{path}.{index}") for index, item in enumerate(value)]
        if not isinstance(value, str) or ("{{" not in value and "{%" not in value):
            return value
        try:
            tree = environment.parse(value)
            local_names = {node.name for node in tree.find_all(nodes.Macro)}
            for call in tree.find_all(nodes.Call):
                if isinstance(call.node, nodes.Name):
                    function = call.node.name
                    if function not in functions and function not in environment.globals:
                        if function not in local_names:
                            raise ValueError(f"Function not allowed: {function}")
            template = environment.from_string(value)
            return template.render({**ctx, **functions})
        except (TemplateError, ValueError, TypeError, ArithmeticError) as exc:
            failure(path, "template", exc)
            return value

    for regex in iter_definition_regexes(data):
        try:
            re.compile(regex.pattern)
        except re.error as exc:
            path = re.sub(r"\[(\d+)\]", r".\1", regex.field)
            failure(path, "regex", exc)

    if body.when is not None:
        expression(body.when, "when")
    for index, effect in enumerate(body.resolved_effects):
        path = f"effects.{index}"
        if effect.when is not None:
            expression(effect.when, f"{path}.when")
        if effect.recovery_when is not None:
            expression(effect.recovery_when, f"{path}.recovery_when")
        fields = effect.model_dump(exclude_unset=True, exclude={"when", "recovery_when"})
        fields = prepare(fields, path)
        if effect.type == "set_variable" and isinstance(effect.value, str):
            from gobby.workflows.engine.effects import EffectsMixin

            if "{{" not in effect.value and EffectsMixin.is_expression(effect.value):
                fields["value"] = expression(effect.value, f"{path}.value", condition=False)
        report.trace.append(RuleDryRunEffect(effect.type, fields))
    return report
