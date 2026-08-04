import ast
import operator
from typing import Union

Number = Union[int, float]

_OPERATORS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Pow: operator.pow,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}

OPERATOR_DESCRIPTIONS = {
    "+": "Add the right operand to the left operand.",
    "-": "Subtract the right operand from the left operand.",
    "*": "Multiply the left operand by the right operand.",
    "/": "Divide the left operand by the right operand.",
    "**": "Raise the left operand to the power of the right operand.",
}


def get_calculate_tools():
    return [
        prepare_calculation,
        calculate,
    ]


CALCULATE_PROMPT_TEMPLATE = """
You are handling an arithmetic calculation.

Expression: {expression}
Left operand: {left_operand}
Operator: {operator_symbol}
Right operand: {right_operand}
Operation description: {operator_description}

Instructions:

1. Call the `calculate` tool with expression="{expression}".
2. Treat the tool result as the source of truth.
3. If the tool succeeds, return the result clearly.
4. If it fails, return the tool error briefly.
5. Do not calculate the answer manually.
""".strip()


def prepare_calculation(
    left_operand: float, operator_symbol: str, right_operand: float
) -> dict:
    """
    Build runtime instructions for a binary arithmetic calculation.

    Args:
        left_operand: The number on the left side of the operator.
        operator_symbol: One of +, -, *, /, or **.
        right_operand: The number on the right side of the operator.

    Returns:
        A dictionary containing the normalized expression and dynamic prompt.
    """
    operator_description = OPERATOR_DESCRIPTIONS.get(operator_symbol)

    if operator_description is None:
        return {
            "success": False,
            "error": f"Unsupported operator: {operator_symbol}",
            "supported_operators": list(OPERATOR_DESCRIPTIONS.keys()),
        }

    expression = f"{left_operand} {operator_symbol} {right_operand}"

    dynamic_prompt = CALCULATE_PROMPT_TEMPLATE.format(
        expression=expression,
        left_operand=left_operand,
        operator_symbol=operator_symbol,
        right_operand=right_operand,
        operator_description=operator_description,
    )

    return {
        "success": True,
        "expression": expression,
        "left_operand": left_operand,
        "operator_symbol": operator_symbol,
        "right_operand": right_operand,
        "operator_description": operator_description,
        "dynamic_prompt": dynamic_prompt,
    }


def calculate(expression: str) -> dict:
    """
    Evaluate a basic arithmetic expression safely.

    Supported operations:
    addition, subtraction, multiplication, division,
    exponentiation, unary plus, unary minus, and parentheses.

    Args:
        expression: A mathematical expression, for example "8 + 5".

    Returns:
        A dictionary containing the original expression and its result.
    """
    try:
        parsed = ast.parse(expression, mode="eval")
        result = _evaluate_node(parsed.body)

        return {
            "success": True,
            "expression": expression,
            "result": result,
        }
    except ZeroDivisionError:
        return {
            "success": False,
            "expression": expression,
            "error": "Division by zero is not allowed.",
        }
    except (SyntaxError, TypeError, ValueError):
        return {
            "success": False,
            "expression": expression,
            "error": "The arithmetic expression is invalid or unsupported.",
        }


def _evaluate_node(node: ast.AST) -> Number:
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(
            node.value,
            (int, float),
        ):
            raise ValueError("Only numeric values are supported.")

        return node.value

    if isinstance(node, ast.BinOp):
        operation = _OPERATORS.get(type(node.op))

        if operation is None:
            raise ValueError("Unsupported binary operator.")

        left = _evaluate_node(node.left)
        right = _evaluate_node(node.right)

        return operation(left, right)

    if isinstance(node, ast.UnaryOp):
        operation = _OPERATORS.get(type(node.op))

        if operation is None:
            raise ValueError("Unsupported unary operator.")

        return operation(_evaluate_node(node.operand))

    raise ValueError("Unsupported expression.")
