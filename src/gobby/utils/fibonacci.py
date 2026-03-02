"""Recursive fibonacci function implementation."""


def fibonacci(n: int) -> int:
    """
    Compute the nth Fibonacci number using recursion.

    Args:
        n: The position in the Fibonacci sequence (0-indexed)

    Returns:
        The nth Fibonacci number
    """
    if n <= 1:
        return n
    return fibonacci(n - 1) + fibonacci(n - 2)
