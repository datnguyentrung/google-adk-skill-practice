from google.adk.skills import models

calculate_skill = models.Skill(
    frontmatter=models.Frontmatter(
        name="calculate",
        description=("Evaluates arithmetic expressions using the calculate tool."),
        metadata={
            "adk_additional_tools": [
                "prepare_calculation",
                "calculate",
            ],
        },
    ),
    instructions="""
Use this skill for binary arithmetic calculation requests.

Follow this workflow exactly:

1. Extract these three values from the user's current request:
   - `left_operand`
   - `operator_symbol`
   - `right_operand`

2. The supported operator symbols are:
   - `+`
   - `-`
   - `*`
   - `/`
   - `**`

3. Call the `prepare_calculation` tool with the three extracted values.

4. Inspect the response from `prepare_calculation`.

5. If `success` is false:
   - do not call the `calculate` tool;
   - briefly explain the returned error.

6. If `success` is true:
   - read the returned `dynamic_prompt`;
   - follow the runtime instructions inside `dynamic_prompt`;
   - use the returned `expression` as the input to the `calculate` tool.

7. Call the `calculate` tool exactly once with:
   `expression=<expression returned by prepare_calculation>`.

8. Treat the result from the `calculate` tool as the source of truth.

9. If the calculation succeeds, return the expression and result clearly.

10. If the calculation fails, briefly explain the tool error.

11. If the operands or operator cannot be determined, ask one concise
    clarification question.

12. Do not expose skill loading, routing, dynamic prompt construction,
    or internal tool execution in the final response.
""",
)
