"""Sample module fixture for evals/test_testgen.py.

The test analyzes this file with TestGenerator.analyze_module() and expects
exactly these functions: add, greet, fetch_count, multiply (plus the private
_private_helper, only picked up with include_private=True).
"""


def add(a, b):
    """Cong 2 so."""
    return a + b


def greet(name, greeting="Hello"):
    return f"{greeting}, {name}!"


async def fetch_count(url):
    return 42


class Calculator:
    def multiply(self, x, y):
        return x * y


def _private_helper():
    return "private"
