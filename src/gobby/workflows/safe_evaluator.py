"""Safe expression evaluation utilities.

Provides AST-based expression evaluation without using eval(),
and lazy boolean evaluation for deferred computation.
"""

from __future__ import annotations

import ast
import logging
import operator
from collections.abc import Callable
from typing import Any

__all__ = ["LazyBool", "SafeExpressionEvaluator", "build_condition_helpers"]

logger = logging.getLogger(__name__)


class LazyBool:
    """Lazy boolean that defers computation until first access.

    Used to avoid expensive operations (git status, DB queries) when
    evaluating block_tools conditions that don't reference certain values.

    The computation is triggered when the value is used in a boolean context
    (e.g., `if lazy_val:` or `not lazy_val`), which happens during eval().
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
        """Collapse whitespace so YAML folding artefacts don't break ast.parse.

        YAML ``>`` folded scalars preserve newlines for lines with extra
        indentation, producing expressions like ``... )\\n  not in ...``
        which cause ``SyntaxError: unexpected indent`` in ``ast.parse``.
        Replacing interior newlines+whitespace with a single space is safe
        because ``when`` conditions are always single expressions.
        """
        return " ".join(expr.split())

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
        ast.Mult: operator.mul,
        ast.FloorDiv: operator.floordiv,
        ast.Mod: operator.mod,
    }

    def visit_BinOp(self, node: ast.BinOp) -> Any:
        """Handle binary arithmetic operations (+, -, *, //, %)."""
        op_func = self._SAFE_BIN_OPS.get(type(node.op))
        if op_func is None:
            raise ValueError(f"Unsupported binary operator: {type(node.op).__name__}")
        left = self.visit(node.left)
        right = self.visit(node.right)
        return op_func(left, right)

    def visit_Name(self, node: ast.Name) -> Any:
        """Handle variable names."""
        name = node.id
        # Built-in constants
        if name == "True":
            return True
        if name == "False":
            return False
        if name == "None":
            return None
        # Context variables
        if name in self.context:
            return self.context[name]
        raise ValueError(f"Unknown variable: {name}")

    def visit_Constant(self, node: ast.Constant) -> Any:
        """Handle literal values (strings, numbers, booleans, None)."""
        return node.value

    # Safe method calls allowed on specific types
    SAFE_METHODS: dict[type, set[str]] = {
        dict: {"get", "keys", "values", "items"},
        str: {"strip", "lstrip", "rstrip", "startswith", "endswith", "lower", "upper", "split"},
        list: {"count", "index"},
    }

    def visit_Call(self, node: ast.Call) -> Any:
        """Handle function calls (only allowed functions and safe method calls)."""
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
        kwargs = {kw.arg: self.visit(kw.value) for kw in node.keywords if kw.arg}

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

    def visit_Tuple(self, node: ast.Tuple) -> tuple[Any, ...]:
        """Handle tuple literals (e.g., ('a', 'b', 'c'))."""
        return tuple(self.visit(elt) for elt in node.elts)

    def visit_IfExp(self, node: ast.IfExp) -> Any:
        """Handle ternary expressions (e.g., x if condition else y)."""
        test = self.visit(node.test)
        if test:
            return self.visit(node.body)
        return self.visit(node.orelse)

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
        task_manager: LocalTaskManager instance (enables task_tree_complete, task_needs_user_review)
        stop_registry: StopRegistry instance (enables has_stop_signal)
        plugin_conditions: Dict of plugin condition name -> callable
        context: Evaluation context dict (needed for mcp_* helpers to access variables)

    Returns:
        Dict of function_name -> callable, ready to pass as allowed_funcs.
    """
    from .condition_helpers import task_needs_user_review, task_tree_complete

    ctx = context or {}
    funcs: dict[str, Callable[..., Any]] = {
        "len": len,
        "bool": bool,
        "str": str,
        "int": int,
        "list": list,
        "dict": dict,
    }

    # --- Task helpers ---

    if task_manager:
        funcs["task_tree_complete"] = lambda task_id: task_tree_complete(task_manager, task_id)
        funcs["task_needs_user_review"] = lambda task_id: task_needs_user_review(
            task_manager, task_id
        )
    else:
        funcs["task_tree_complete"] = lambda task_id: True
        funcs["task_needs_user_review"] = lambda task_id: False

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

    funcs["mcp_called"] = _mcp_called
    funcs["mcp_result_is_null"] = _mcp_result_is_null
    funcs["mcp_failed"] = _mcp_failed
    funcs["mcp_result_has"] = _mcp_result_has

    # --- Plugin conditions ---

    if plugin_conditions:
        funcs.update(plugin_conditions)

    return funcs
