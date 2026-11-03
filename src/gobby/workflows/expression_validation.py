"""Static checks for the safe evaluator, including unreachable branches."""

from __future__ import annotations

import ast
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .safe_evaluator import SafeExpressionEvaluator


def validate_expression_tree(
    tree: ast.Expression,
    evaluator: type[SafeExpressionEvaluator],
    function_names: set[str],
) -> None:
    """Derive accepted expression nodes and operators from the runtime evaluator."""
    methods = set().union(*evaluator.SAFE_METHODS.values())
    for node in ast.walk(tree.body):
        if isinstance(node, ast.expr) and not hasattr(evaluator, f"visit_{type(node).__name__}"):
            raise ValueError(f"Unsupported expression type: {type(node).__name__}")
        if isinstance(node, ast.BinOp) and type(node.op) not in evaluator._SAFE_BIN_OPS:
            raise ValueError(f"Unsupported binary operator: {type(node.op).__name__}")
        if isinstance(node, ast.UnaryOp) and not isinstance(node.op, ast.Not | ast.USub | ast.UAdd):
            raise ValueError(f"Unsupported unary operator: {type(node.op).__name__}")
        if isinstance(node, ast.Compare) and any(
            type(op) not in evaluator.CMP_OPS for op in node.ops
        ):
            raise ValueError("Unsupported comparison")
        if isinstance(node, ast.Attribute) and node.attr.startswith("__"):
            raise ValueError(f"Access to dunder attribute '{node.attr}' is not allowed")
        if isinstance(node, ast.Dict) and any(key is None for key in node.keys):
            raise ValueError("Unsupported dictionary unpacking")
        if isinstance(node, ast.Slice):
            raise ValueError("Unsupported expression type: Slice")
        if isinstance(node, ast.comprehension):
            if not isinstance(node.target, ast.Name) or node.is_async:
                raise ValueError("Unsupported comprehension target or async comprehension")
        if isinstance(node, ast.Call):
            if any(keyword.arg is None for keyword in node.keywords):
                raise ValueError("Unsupported keyword unpacking")
            if isinstance(node.func, ast.Name):
                if node.func.id not in function_names:
                    raise ValueError(f"Function not allowed: {node.func.id}")
            elif isinstance(node.func, ast.Attribute):
                if node.func.attr not in methods:
                    raise ValueError(f"Unsupported method call: {node.func.attr}")
            else:
                raise ValueError(f"Unsupported call type: {type(node.func).__name__}")
