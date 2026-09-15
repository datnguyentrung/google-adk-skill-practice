---
name: graph-qa
description: >
  Use this skill when the user asks a natural-language question
  that should be answered using knowledge stored in Neo4j.

metadata:
  adk_additional_tools:
    - execute_read_cypher
    - vector_search
    - hybrid_search
---

# Graph QA

Answer natural-language questions using evidence stored in Neo4j.

## Graph QA workflow

1. Understand the user's question and preserve the original language and named entities.

2. Determine which ontology/schema group is required.

3. Load only the minimum schema required for the question.
   - Prefer one schema skill/group per user turn.
   - Do not load unrelated ontology groups.
   - If the required schema is already available in the current turn/context,
     do not load it again.

4. Plan the answer requirements before querying Neo4j.
   - Identify the entity/entities being asked about.
   - Identify the exact properties, relationships, and evidence required.
   - Map ontology technical names to canonical Neo4j local names before
     generating Cypher.

5. Choose exactly one primary retrieval strategy:
   - `execute_read_cypher`
   - `vector_search`
   - `hybrid_search`

6. Build one focused primary retrieval request that aims to collect all
   evidence required to answer the question.

   For Cypher:
   - Prefer explicit property projection over `RETURN n` or whole-node returns.
   - Return only fields needed for the answer.
   - Use parameters for user/entity values.
   - Use `OPTIONAL MATCH` only where actually required.
   - Avoid broad graph expansion and Cartesian products.
   - Always apply an appropriate `LIMIT` unless the query is guaranteed
     to return a bounded result.

7. Execute the primary retrieval exactly once.

8. Evaluate the result without automatically querying for more detail.

   - If sufficient evidence was returned:
     answer immediately.
   - If the graph legitimately contains no matching evidence:
     state that the requested information was not found.
   - Do not run another query merely to enrich, elaborate, or improve
     an already answerable result.

9. A second retrieval call is allowed only as a controlled fallback when:
   - the first query failed because of a recoverable schema/query mismatch; or
   - the first query returned zero results and there is a clear alternative
     lookup strategy for the same entity.

   The fallback must not be used for exploratory "get more details" loops.

10. After the primary retrieval (or one permitted fallback), answer using
    only the retrieved graph evidence.

## Tool-call budget per user turn

- Schema loading:
  - Target: 0–1 calls.
  - Load only the smallest relevant schema group.

- Graph retrieval:
  - Target: 1 primary retrieval call.
  - Hard maximum: 2 retrieval calls.
  - The second call is fallback-only.

- Do not repeatedly alternate:
  LLM → Cypher → LLM → Cypher → LLM → Cypher.

- Prefer:
  LLM planning → one focused retrieval → final answer.

## Result-size budget

Neo4j results are sent back into the LLM context and therefore consume
Gemini input tokens.

For that reason:

- Never use `RETURN n`, `RETURN p, o, r...` unless the complete node is
  specifically required.
- Return explicit scalar properties.
- Avoid `properties(n)` for normal QA.
- Avoid unrestricted relationship expansion.
- Use `LIMIT`.
- Keep result sets compact and directly relevant to the user's question.

Example:

Bad:

    RETURN p, o, r

Preferred:

    RETURN
        p.productCode AS productCode,
        p.bankingProductName AS productName,
        o.benefit AS benefit,
        r.businessRuleCondition AS condition
    LIMIT 20

## Failure behavior

If the available schema or retrieved evidence is insufficient after the
allowed retrieval budget:

- state what information is missing;
- do not continue an open-ended query loop;
- do not hallucinate missing graph facts.

## Query-generation rules

- Use only physical Neo4j labels (`neo4jLabel`), relationship types (`relationshipType`), and property keys (`neo4jPropertyKey`) supplied by the selected schema.
- For relationships in Cypher, ALWAYS use the physical `relationshipType` (which is in `UPPER_SNAKE_CASE`), such as `[:HAS_ELIGIBILITY_RULE]`, `[:HAS_OFFER]`, `[:HAS_SALES_CONDITION_RULE]`, `[:GOVERNED_BY_POLICY]`, `[:HAS_KNOWLEDGE]`. Never use camelCase for relationship types (e.g., `[:hasEligibilityRule]` is INCORRECT).
- For Node labels, use `neo4jLabel` (e.g., `:BankingProduct`, `:BusinessRule`).
- For Node properties, use `neo4jPropertyKey` (e.g., `p.bankingProductName`).
- Never use ontology-prefixed properties such as `p.pskg:bankingProductName`.
- Preserve product names and other user-provided named entities.
- Parameterize user/entity values using `$parameter`.

## Evidence completeness

Before making a definitive conclusion such as:
- "eligible"
- "qualified"
- "applicable"
- "definitely"
- "fully satisfies"

check whether all required conditions are supported by retrieved evidence.

If only some conditions are known, do not infer the remaining conditions.

Use wording such as:
- "The available evidence confirms..."
- "This satisfies the amount and tenor requirements, but the remaining conditions still need to be checked."
- "The graph does not contain enough evidence to conclude eligibility."

For rules containing multiple mandatory conditions, retrieve and evaluate
all relevant conditions before giving a definitive eligibility result.
