"""Safe expression evaluation utilities.

Provides AST-based expression evaluation without using eval(),
and lazy boolean evaluation for deferred computation.
"""

from __future__ import annotations

import ast
import io
import logging
import operator
import re
import tokenize
from collections.abc import Callable, Iterator, Mapping
from typing import Any

__all__ = [
    "ASSISTANT_RESPONSE_CONTRASTIVE_PATTERNS",
    "ASSISTANT_RESPONSE_SCAN_LIMIT",
    "LazyBool",
    "SafeExpressionEvaluator",
    "build_condition_helpers",
]

logger = logging.getLogger(__name__)

ASSISTANT_RESPONSE_SCAN_LIMIT = 8000
ASSISTANT_RESPONSE_CONTRASTIVE_PATTERNS = (
    r"\bnot\b\s+[^.!?\n,;:]{1,80}?,\s*\bnot\b\s+[^.!?\n,;:]{1,80}?,\s*\bbut\b\s+[^.!?\n]{1,120}",
    r"\bnot\b\s+[^.!?\n,;:]{1,120}?,\s*\bbut\b\s+[^.!?\n]{1,120}",
    r"\b(?!not\b)[A-Z0-9][^.!?\n,;:]{1,120}?,\s*\bnot\b\s+[^.!?\n]{1,120}",
)
_ASSISTANT_RESPONSE_REGEX_ALLOWLIST = frozenset(ASSISTANT_RESPONSE_CONTRASTIVE_PATTERNS)
_ASSISTANT_RESPONSE_TEXT_KEYS = (
    "response",
    "assistant_response",
    "assistant_response_text",
    "prompt_response",
    "last_assistant_message",
    "last_assistant_content",
    "output",
    "message",
    "content",
    "log",
)
_NESTED_TEXT_KEYS = (
    "text",
    "content",
    "message",
    "response",
    "output",
)


class LazyBool:
    """Lazy boolean that defers computation until first access.

    Used to avoid expensive operations (git status, DB queries) when
    evaluating block_tools conditions that don't reference certain values.

    The computation is triggered when the value is used in a boolean context,
    compared, or hashed. The result is cached after the first computation.
    """

    __slots__ = ("_thunk", "_computed", "_value")

    def __init__(self, thunk: Callable[[], bool]) -> None:
        self._thunk = thunk
        self._computed = False
        self._value = False

    def __bool__(self) -> bool:
        if not self._computed:
            self._value = self._thunk()
            self._computed = True
        return self._value

    def __eq__(self, other: object) -> bool:
        if isinstance(other, LazyBool):
            other = bool(other)
        return bool(self) == other

    def __hash__(self) -> int:
        return hash(bool(self))

    def __repr__(self) -> str:
        if self._computed:
            return f"LazyBool({self._value})"
        return "LazyBool(<not computed>)"


class SafeExpressionEvaluator(ast.NodeVisitor):
    """Safe expression evaluator using AST.

    Evaluates simple Python expressions without using eval().
    Supports boolean operations, comparisons, attribute access, subscripts,
    and a limited set of allowed function calls.
    """

    # Comparison operators mapping
    CMP_OPS: dict[type[ast.cmpop], Callable[[Any, Any], bool]] = {
        ast.Eq: operator.eq,
        ast.NotEq: operator.ne,
        ast.Lt: operator.lt,
        ast.LtE: operator.le,
        ast.Gt: operator.gt,
        ast.GtE: operator.ge,
        ast.Is: operator.is_,
        ast.IsNot: operator.is_not,
        ast.In: lambda a, b: a in b,
        ast.NotIn: lambda a, b: a not in b,
    }

    def __init__(
        self, context: dict[str, Any], allowed_funcs: dict[str, Callable[..., Any]]
    ) -> None:
        self.context = context
        self.allowed_funcs = allowed_funcs

    @staticmethod
    def _normalize_expr(expr: str) -> str:
        """Collapse whitespace outside strings so YAML folding artefacts parse.

        YAML ``>`` folded scalars preserve newlines for lines with extra
        indentation, producing expressions like ``... )\\n  not in ...``
        which cause ``SyntaxError: unexpected indent`` in ``ast.parse``.
        String tokens are preserved verbatim so normalization cannot change
        the value being compared.
        """
        lines = expr.splitlines(keepends=True)
        line_offsets: list[int] = []
        offset = 0
        for line in lines:
            line_offsets.append(offset)
            offset += len(line)

        string_spans: list[tuple[int, int]] = []
        try:
            tokens = tokenize.generate_tokens(io.StringIO(expr).readline)
            for token in tokens:
                if token.type != tokenize.STRING:
                    continue
                start = line_offsets[token.start[0] - 1] + token.start[1]
                end = line_offsets[token.end[0] - 1] + token.end[1]
                string_spans.append((start, end))
        except tokenize.TokenError:
            return " ".join(expr.split())

        pieces: list[str] = []
        cursor = 0
        for start, end in string_spans:
            pieces.append(re.sub(r"\s+", " ", expr[cursor:start]))
            pieces.append(expr[start:end])
            cursor = end
        pieces.append(re.sub(r"\s+", " ", expr[cursor:]))
        return "".join(pieces).strip()

    def evaluate(self, expr: str) -> bool:
        """Evaluate expression and return boolean result."""
        try:
            tree = ast.parse(self._normalize_expr(expr), mode="eval")
            return bool(self.visit(tree.body))
        except Exception as e:
            raise ValueError(f"Invalid expression: {e}") from e

    def evaluate_value(self, expr: str) -> Any:
        """Evaluate expression and return the raw value (not coerced to bool)."""
        try:
            tree = ast.parse(self._normalize_expr(expr), mode="eval")
            return self.visit(tree.body)
        except Exception as e:
            raise ValueError(f"Invalid expression: {e}") from e

    def visit_BoolOp(self, node: ast.BoolOp) -> Any:
        """Handle 'and' / 'or' operations with Python semantics.

        Python's `and`/`or` return actual values, not just True/False:
        - `a and b` returns `a` if falsy, else `b`
        - `a or b` returns `a` if truthy, else `b`

        This matters for expressions like: `(dict.get('key') or {}).get('nested')`
        """
        if isinstance(node.op, ast.And):
            result: Any = True
            for v in node.values:
                result = self.visit(v)
                if not result:
                    return result
            return result
        elif isinstance(node.op, ast.Or):
            result = False
            for v in node.values:
                result = self.visit(v)
                if result:
                    return result
            return result
        raise ValueError(f"Unsupported boolean operator: {type(node.op).__name__}")

    def visit_Compare(self, node: ast.Compare) -> bool:
        """Handle comparison operations (==, !=, <, >, in, not in, etc.)."""
        left = self.visit(node.left)
        for op, comparator in zip(node.ops, node.comparators, strict=False):
            right = self.visit(comparator)
            op_func = self.CMP_OPS.get(type(op))
            if op_func is None:
                raise ValueError(f"Unsupported comparison: {type(op).__name__}")
            if not op_func(left, right):
                return False
            left = right
        return True

    def visit_UnaryOp(self, node: ast.UnaryOp) -> Any:
        """Handle unary operations (not, -, +)."""
        operand = self.visit(node.operand)
        if isinstance(node.op, ast.Not):
            return not operand
        elif isinstance(node.op, ast.USub):
            return -operand
        elif isinstance(node.op, ast.UAdd):
            return +operand
        raise ValueError(f"Unsupported unary operator: {type(node.op).__name__}")

    _SAFE_BIN_OPS: dict[type, Callable[..., Any]] = {
        ast.Add: operator.add,
        ast.Sub: operator.sub,
        ast.FloorDiv: operator.floordiv,
        ast.Mod: operator.mod,
    }

    def visit_BinOp(self, node: ast.BinOp) -> Any:
        """Handle binary arithmetic operations (+, -, //, %)."""
        op_func = self._SAFE_BIN_OPS.get(type(node.op))
        if op_func is None:
            raise ValueError(f"Unsupported binary operator: {type(node.op).__name__}")
        left = self.visit(node.left)
        right = self.visit(node.right)
        return op_func(left, right)

    def visit_Name(self, node: ast.Name) -> Any:
        """Handle variable names."""
        name = node.id
        # Built-in constants (support both Python and YAML/JSON conventions)
        if name in ("True", "true"):
            return True
        if name in ("False", "false"):
            return False
        if name in ("None", "none"):
            return None
        # Context variables
        if name in self.context:
            return self.context[name]
        # Allowed callables referenced by name (e.g. ``dict`` in
        # ``isinstance(x, dict)``); context bindings above take precedence.
        if name in self.allowed_funcs:
            return self.allowed_funcs[name]
        raise ValueError(f"Unknown variable: {name}")

    def visit_Constant(self, node: ast.Constant) -> Any:
        """Handle literal values (strings, numbers, booleans, None)."""
        return node.value

    # Safe method calls allowed on specific types
    SAFE_METHODS: dict[type, set[str]] = {
        dict: {"get", "keys", "values", "items"},
        str: {
            "strip",
            "lstrip",
            "rstrip",
            "startswith",
            "endswith",
            "lower",
            "upper",
            "split",
            "partition",
            "rpartition",
        },
        list: {"count", "index"},
    }

    def visit_Call(self, node: ast.Call) -> Any:
        """Handle function calls (only allowed functions and safe method calls)."""
        if any(keyword.arg is None for keyword in node.keywords):
            raise ValueError("Unsupported keyword unpacking")

        # Get function name
        if isinstance(node.func, ast.Name):
            func_name = node.func.id
        elif isinstance(node.func, ast.Attribute):
            # Handle method calls like obj.get('key'), s.strip(), s.startswith('/')
            obj = self.visit(node.func.value)
            method_name = node.func.attr
            args = [self.visit(arg) for arg in node.args]

            # Check if this is an allowed method call
            for allowed_type, allowed_methods in self.SAFE_METHODS.items():
                if isinstance(obj, allowed_type) and method_name in allowed_methods:
                    return getattr(obj, method_name)(*args)

            raise ValueError(f"Unsupported method call: {type(obj).__name__}.{method_name}")
        else:
            raise ValueError(f"Unsupported call type: {type(node.func).__name__}")

        # Check if function is allowed
        if func_name not in self.allowed_funcs:
            raise ValueError(f"Function not allowed: {func_name}")

        # Evaluate arguments
        args = [self.visit(arg) for arg in node.args]
        kwargs = {kw.arg: self.visit(kw.value) for kw in node.keywords if kw.arg is not None}

        return self.allowed_funcs[func_name](*args, **kwargs)

    def visit_Attribute(self, node: ast.Attribute) -> Any:
        """Handle attribute access (e.g., obj.attr)."""
        obj = self.visit(node.value)
        attr = node.attr
        if isinstance(obj, dict):
            # Allow dict-style attribute access for convenience
            if attr in obj:
                return obj[attr]
            raise ValueError(f"Key not found: {attr}")
        # Block dunder attributes to prevent sandbox escape
        # (e.g., __class__.__base__.__subclasses__())
        if attr.startswith("__"):
            raise ValueError(f"Access to dunder attribute '{attr}' is not allowed")
        if hasattr(obj, attr):
            return getattr(obj, attr)
        raise ValueError(f"Attribute not found: {attr}")

    def visit_Subscript(self, node: ast.Subscript) -> Any:
        """Handle subscript access (e.g., obj['key'] or obj[0])."""
        obj = self.visit(node.value)
        key = self.visit(node.slice)
        try:
            return obj[key]
        except (KeyError, IndexError, TypeError) as e:
            raise ValueError(f"Subscript access failed: {e}") from e

    def visit_List(self, node: ast.List) -> list[Any]:
        """Handle list literals (e.g., ['a', 'b', 'c'])."""
        return [self.visit(elt) for elt in node.elts]

    def visit_Dict(self, node: ast.Dict) -> dict[Any, Any]:
        """Handle dict literals (e.g., {'key': 'value'} or {})."""
        if any(key is None for key in node.keys):
            raise ValueError("Unsupported dictionary unpacking")

        return {
            self.visit(k): self.visit(v)
            for k, v in zip(node.keys, node.values, strict=True)
            if k is not None
        }

    def visit_Tuple(self, node: ast.Tuple) -> tuple[Any, ...]:
        """Handle tuple literals (e.g., ('a', 'b', 'c'))."""
        return tuple(self.visit(elt) for elt in node.elts)

    def visit_IfExp(self, node: ast.IfExp) -> Any:
        """Handle ternary expressions (e.g., x if condition else y)."""
        test = self.visit(node.test)
        if test:
            return self.visit(node.body)
        return self.visit(node.orelse)

    def _eval_comprehension(
        self, elt: ast.expr, generators: list[ast.comprehension]
    ) -> Iterator[Any]:
        """Evaluate comprehension generators, yielding evaluated elt for each iteration."""
        if not generators:
            yield self.visit(elt)
            return

        gen = generators[0]
        if not isinstance(gen.target, ast.Name):
            raise ValueError(f"Unsupported comprehension target: {type(gen.target).__name__}")
        target_name = gen.target.id
        iterable = self.visit(gen.iter)

        sentinel = object()
        old_value = self.context.get(target_name, sentinel)
        try:
            for item in iterable:
                self.context[target_name] = item
                if all(self.visit(if_clause) for if_clause in gen.ifs):
                    yield from self._eval_comprehension(elt, generators[1:])
        finally:
            if old_value is sentinel:
                self.context.pop(target_name, None)
            else:
                self.context[target_name] = old_value

    def visit_GeneratorExp(self, node: ast.GeneratorExp) -> Iterator[Any]:
        """Handle generator expressions (e.g., x for x in items if cond)."""
        return self._eval_comprehension(node.elt, node.generators)

    def visit_ListComp(self, node: ast.ListComp) -> list[Any]:
        """Handle list comprehensions (e.g., [x*2 for x in items])."""
        return list(self._eval_comprehension(node.elt, node.generators))

    def generic_visit(self, node: ast.AST) -> Any:
        """Reject any unsupported AST nodes."""
        raise ValueError(f"Unsupported expression type: {type(node).__name__}")


def _get_variables(context: dict[str, Any]) -> dict[str, Any]:
    """Extract variables dict from context, handling both dict and SimpleNamespace."""
    variables = context.get("variables", {})
    if isinstance(variables, dict):
        return variables
    # SimpleNamespace from workflow engine
    return getattr(variables, "__dict__", {})


def _coerce_response_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list | tuple):
        return "\n".join(text for item in value if (text := _coerce_response_text(item).strip()))
    if isinstance(value, dict):
        parts = [
            text
            for key in _NESTED_TEXT_KEYS
            if (text := _coerce_response_text(value.get(key)).strip())
        ]
        return "\n".join(parts)
    return ""


def _assistant_response_text(context: dict[str, Any]) -> str:
    cached = context.get("_assistant_response_text_cache")
    if isinstance(cached, str):
        return cached

    event = context.get("event")
    data = _event_field(event, "data", {})
    if not isinstance(data, dict):
        data = {}

    text = ""
    for key in _ASSISTANT_RESPONSE_TEXT_KEYS:
        text = _coerce_response_text(data.get(key)).strip()
        if text:
            break

    if not text:
        variables = _get_variables(context)
        for key in ("last_assistant_response", "last_assistant_message"):
            text = _coerce_response_text(variables.get(key)).strip()
            if text:
                break

    context["_assistant_response_text_cache"] = text
    return text


def _event_field(event: Any, field: str, default: Any) -> Any:
    if event is None:
        return default
    if isinstance(event, dict):
        return event.get(field, default)
    try:
        return getattr(event, field)
    except (AttributeError, TypeError) as exc:
        logger.debug(
            "Failed to read event field %s from %r: %s",
            field,
            event,
            exc,
        )
        return default


def build_condition_helpers(
    task_manager: Any = None,
    stop_registry: Any = None,
    plugin_conditions: dict[str, Any] | None = None,
    context: dict[str, Any] | None = None,
) -> dict[str, Callable[..., Any]]:
    """Build allowed_funcs dict with workflow condition helpers for SafeExpressionEvaluator.

    Creates closures that bind task_manager, stop_registry, and context
    so helper functions can be called from AST-based expressions.

    Args:
        task_manager: LocalTaskManager instance (enables task_tree_complete)
        stop_registry: StopRegistry instance (enables has_stop_signal)
        plugin_conditions: Dict of plugin condition name -> callable
        context: Evaluation context dict (needed for mcp_* helpers to access variables)

    Returns:
        Dict of function_name -> callable, ready to pass as allowed_funcs.
    """
    from .condition_helpers import (
        all_tasks_have_label,
        first_tdd_code_path,
        first_tdd_test_path,
        is_gobby_build_command,
        is_validation_command,
        shell_command_invokes_gcode,
        task_commit_project_path_allowlist_violation,
        task_needs_human_review,
        task_state_in,
        task_tree_complete,
        task_type_in,
        touches_claude_memory_path,
        touches_docker_policy_path,
        touches_ui_design_path,
    )
    from .memory_review_conditions import queue_memory_review_close
    from .monolith_guard import (
        outstanding_monolith_paths,
        projected_monolith_paths,
    )

    ctx = context or {}
    funcs: dict[str, Callable[..., Any]] = {
        "len": len,
        "bool": bool,
        "str": str,
        "int": int,
        "list": list,
        "dict": dict,
        "any": any,
        "all": all,
        "normalize_path": lambda p: p.replace("\\", "/"),
        "first_tdd_code_path": first_tdd_code_path,
        "first_tdd_test_path": first_tdd_test_path,
        "is_gobby_build_command": is_gobby_build_command,
        "is_validation_command": is_validation_command,
        "shell_command_invokes_gcode": shell_command_invokes_gcode,
        "task_commit_project_path_allowlist_violation": (
            task_commit_project_path_allowlist_violation
        ),
        "touches_claude_memory_path": touches_claude_memory_path,
        "touches_docker_policy_path": touches_docker_policy_path,
        "touches_ui_design_path": touches_ui_design_path,
    }

    # --- Task helpers ---

    if task_manager:
        funcs["all_tasks_have_label"] = lambda task_id_or_ids, label: all_tasks_have_label(
            task_manager, task_id_or_ids, label
        )
        funcs["task_tree_complete"] = lambda task_id: task_tree_complete(task_manager, task_id)
        funcs["task_needs_human_review"] = lambda task_id: task_needs_human_review(
            task_manager, task_id
        )
        funcs["task_state_in"] = lambda task_id, *states: task_state_in(
            task_manager, task_id, *states
        )
        funcs["task_type_in"] = lambda task_id_or_ids, *types: task_type_in(
            task_manager, task_id_or_ids, *types
        )
    else:
        funcs["all_tasks_have_label"] = lambda task_id_or_ids, label: False
        funcs["task_tree_complete"] = lambda task_id: False
        funcs["task_needs_human_review"] = lambda task_id: False
        funcs["task_state_in"] = lambda task_id, *states: False
        funcs["task_type_in"] = lambda task_id_or_ids, *types: False

    # --- Stop signal helper ---

    if stop_registry:
        funcs["has_stop_signal"] = lambda session_id: stop_registry.has_pending_signal(session_id)
    else:
        funcs["has_stop_signal"] = lambda session_id: False

    # --- MCP call tracking helpers ---

    def _mcp_called(server: str, tool: str | None = None) -> bool:
        """Check if MCP tool was called successfully."""
        variables = _get_variables(ctx)
        mcp_calls = variables.get("mcp_calls", {})
        if not isinstance(mcp_calls, dict):
            return False
        if tool:
            return tool in mcp_calls.get(server, [])
        return bool(mcp_calls.get(server))

    def _mcp_result_is_null(server: str, tool: str) -> bool:
        """Check if MCP tool result is null/missing."""
        variables = _get_variables(ctx)
        mcp_results = variables.get("mcp_results", {})
        if not isinstance(mcp_results, dict):
            return True
        server_results = mcp_results.get(server, {})
        if not isinstance(server_results, dict):
            return True
        return server_results.get(tool) is None

    def _mcp_failed(server: str, tool: str) -> bool:
        """Check if MCP tool call failed."""
        variables = _get_variables(ctx)
        mcp_results = variables.get("mcp_results", {})
        if not isinstance(mcp_results, dict):
            return False
        server_results = mcp_results.get(server, {})
        if not isinstance(server_results, dict):
            return False
        result = server_results.get(tool)
        if result is None:
            return False
        if isinstance(result, dict):
            if result.get("success") is False:
                return True
            if result.get("error"):
                return True
            if result.get("status") == "failed":
                return True
        return False

    def _mcp_result_has(server: str, tool: str, field: str, value: Any) -> bool:
        """Check if MCP tool result has a specific field value."""
        variables = _get_variables(ctx)
        mcp_results = variables.get("mcp_results", {})
        if not isinstance(mcp_results, dict):
            return False
        server_results = mcp_results.get(server, {})
        if not isinstance(server_results, dict):
            return False
        result = server_results.get(tool)
        if not isinstance(result, dict):
            return False
        return bool(result.get(field) == value)

    def _tool_call_succeeded() -> bool:
        """Check whether the current normalized after-tool event succeeded."""
        from gobby.hooks.tool_outcomes import normalize_tool_outcome

        event = ctx.get("event")
        data = _event_field(event, "data", None)
        if not isinstance(data, dict):
            return False

        metadata = _event_field(event, "metadata", {})
        is_failure = metadata.get("is_failure") if isinstance(metadata, dict) else None
        explicit_success = not is_failure if isinstance(is_failure, bool) else None
        outcome = normalize_tool_outcome(
            data,
            explicit_success=explicit_success,
            provenance="hook_event.metadata.is_failure" if explicit_success is not None else None,
        )
        return outcome.succeeded is True

    def _skill_loaded(name: str) -> bool:
        """Check the canonical skill ledger."""
        variables = _get_variables(ctx)
        loaded_skills = variables.get("loaded_skills", [])
        return isinstance(loaded_skills, list) and name in loaded_skills

    def _projected_monolith_paths(
        tool_input: Any = None,
        event_data: Mapping[str, Any] | None = None,
    ) -> list[str]:
        """Return threshold-crossing paths for the current structured write."""
        project = ctx.get("project")
        project_path = project.get("path") if isinstance(project, Mapping) else None
        event = ctx.get("event")
        data = event_data or _event_field(event, "data", {})
        return projected_monolith_paths(
            tool_input if tool_input is not None else ctx.get("tool_input"),
            project_path,
            data if isinstance(data, Mapping) else None,
        )

    def _outstanding_monolith_paths() -> list[str]:
        """Return over-budget files attributed to this session's task edits."""
        project = ctx.get("project")
        project_path = project.get("path") if isinstance(project, Mapping) else None
        return outstanding_monolith_paths(_get_variables(ctx), project_path)

    def _has_open_tool_error(tool: str, arguments: Mapping[str, Any]) -> bool:
        """Check normalized unresolved errors for one exact tool target."""
        from gobby.hooks.tool_error_tracker import (
            extract_target_key,
            normalize_open_tool_error_records,
        )

        if not isinstance(tool, str) or not isinstance(arguments, Mapping):
            return False
        variables = _get_variables(ctx)
        records = normalize_open_tool_error_records(variables.get("open_tool_errors"))
        target_key = extract_target_key(
            {"tool_name": tool.rpartition("/")[2] or tool},
            arguments,
        )
        return any(
            record["tool"] == tool and record["target_key"] == target_key for record in records
        )

    def _missing_claimed_task_required_skills(
        tool_input: Any = None,
        event_data: dict[str, Any] | None = None,
    ) -> list[str]:
        """Return claimed-task skills missing from the canonical skill ledger."""
        from gobby.workflows.claimed_task_skills import (
            missing_claimed_task_required_skills,
        )

        return missing_claimed_task_required_skills(
            _get_variables(ctx),
            tool_input,
            event_data,
        )

    def _assistant_response_matches_any(
        patterns: list[str],
        regex: bool = False,
    ) -> str | None:
        """Return the first matched assistant response text fragment."""
        if not isinstance(patterns, list | tuple):
            return None

        text = _assistant_response_text(ctx)
        if not text:
            return None
        haystack = text[:ASSISTANT_RESPONSE_SCAN_LIMIT]

        if not regex:
            lowered = haystack.lower()
            for pattern in patterns:
                if not isinstance(pattern, str) or pattern == "":
                    continue
                index = lowered.find(pattern.lower())
                if index >= 0:
                    return haystack[index : index + len(pattern)]
            return None

        for pattern in patterns:
            if not isinstance(pattern, str) or pattern not in _ASSISTANT_RESPONSE_REGEX_ALLOWLIST:
                continue
            try:
                match = re.search(pattern, haystack, re.IGNORECASE)
            except re.error:
                logger.warning("Invalid bundled assistant response regex skipped: %s", pattern)
                continue
            if match:
                return match.group(0)
        return None

    funcs["mcp_called"] = _mcp_called
    funcs["mcp_result_is_null"] = _mcp_result_is_null
    funcs["mcp_failed"] = _mcp_failed
    funcs["mcp_result_has"] = _mcp_result_has
    funcs["tool_call_succeeded"] = _tool_call_succeeded
    funcs["skill_loaded"] = _skill_loaded
    funcs["projected_monolith_paths"] = _projected_monolith_paths
    funcs["outstanding_monolith_paths"] = _outstanding_monolith_paths
    funcs["has_open_tool_error"] = _has_open_tool_error
    funcs["missing_claimed_task_required_skills"] = _missing_claimed_task_required_skills
    funcs["assistant_response_matches_any"] = _assistant_response_matches_any
    funcs["queue_memory_review_close"] = lambda event_data=None, tool_input=None: (
        queue_memory_review_close(
            task_manager,
            event_data
            if isinstance(event_data, Mapping)
            else _event_field(ctx.get("event"), "data", {}),
            tool_input if isinstance(tool_input, Mapping) else ctx.get("tool_input", {}),
            _get_variables(ctx),
        )
    )

    # --- Plugin conditions ---

    if plugin_conditions:
        funcs.update(plugin_conditions)

    return funcs
