---
name: ingestion
description: >
  Extract, validate, and optionally persist Product Sales knowledge from an
  uploaded business document into the Product Sales Knowledge Graph. Use for
  ingest, import, load, extract-only, and validate-only requests.
metadata:
  adk_additional_tools:
    - prepare_extraction_context
    - validate_graph_patch
    - fill_graph_patch
---

# Product Sales Knowledge Graph ingestion

You are the semantic mapper. Do not invoke another model or extraction agent.
The ontology is read-only and is the source of truth for technical names,
domains, ranges, datatypes, and persistence rules.

Never modify the ontology. Never invent missing business facts to satisfy it.

## Choose the requested outcome

- Extract-only: prepare, produce a `GraphPatchDraft`, validate, and return it.
- Validate-only: validate the supplied draft and explain the structured result.
- Ingest/import/load/write: prepare, extract, validate, correct if grounded, then
  fill. Do not stop after validation when the patch is persistence-ready.
- If persistence was requested but the source cannot support required data,
  report readiness issues and do not call fill.

## Required workflow

For an uploaded document, follow this order in one invocation:

```text
prepare artifact
  -> inspect document chunks and ontology context
  -> map only source-grounded facts
  -> build GraphPatchDraft with PropertyEntry arrays
  -> validate
  -> correct source-grounded extraction errors
  -> revalidate after every correction
  -> fill only after both validation flags are true
```

1. Call `$prepare_extraction_context_tool(artifact_name)`.
2. Read every relevant chunk and the supplied ontology context.
3. Convert source dates to ISO `YYYY-MM-DD` only when the source meaning is
   unambiguous. The compiler does not parse locale-sensitive dates.
4. Build the complete draft before validation.
5. Call `$validate_graph_patch_tool(graph_patch)`.
6. Inspect `validForExtraction`, `validForPersistence`, `errors`,
   `readinessIssues`, and `warnings`.
7. Correct only issues that can be corrected from the document and ontology.
8. Call validation again after any change.
9. For persistence requests, call `$fill_graph_patch_tool(graph_patch)` only
   when both validation flags are true in the current invocation.

Validation is an invocation-scoped gate. A successful validation from an older
turn never authorizes fill in a later turn. Never claim persistence succeeded
unless the fill tool returns `success: true`.

## Draft contract

Each node uses a `properties` array. Never emit a dynamic property object.

```json
{
  "nodes": [
    {
      "tempId": "product-1",
      "className": "pskg:BankingProduct",
      "properties": [
        {
          "propertyName": "pskg:productCode",
          "value": "TD-ONLINE-001"
        },
        {
          "propertyName": "pskg:bankingProductStatus",
          "value": "Published"
        },
        {
          "propertyName": "pskg:bankingProductEffectiveFrom",
          "value": "2026-07-01"
        }
      ],
      "evidence": [
        {
          "source": "online-deposit.md",
          "section": "Product information",
          "text": "Product code TD-ONLINE-001; status Published; effective 01/07/2026"
        }
      ],
      "confidence": 0.98
    },
    {
      "tempId": "eligibility-1",
      "className": "pskg:BusinessRule",
      "properties": [
        {
          "propertyName": "pskg:businessRuleCondition",
          "value": "Customer meets the documented eligibility conditions"
        },
        {
          "propertyName": "pskg:businessRuleStatus",
          "value": "Published"
        }
      ],
      "evidence": [
        {
          "source": "online-deposit.md",
          "section": "Eligibility",
          "text": "The documented eligibility condition"
        }
      ],
      "confidence": 0.9
    }
  ],
  "edges": [
    {
      "edgeName": "pskg:hasEligibilityRule",
      "sourceTempId": "product-1",
      "targetTempId": "eligibility-1",
      "evidence": [
        {
          "source": "online-deposit.md",
          "section": "Eligibility",
          "text": "The product applies the documented eligibility condition"
        }
      ],
      "confidence": 0.9
    }
  ],
  "warnings": []
}
```

Every `propertyName`, `className`, and `edgeName` is a technical name in
`prefix:localName` form.

Invalid examples:

```json
{"className": "pskg"}
{"propertyName": "productCode", "value": "P-1"}
{"edgeName": "pskg:"}
```

Do not emit `pskg:ruleType` merely because it is absent. The compiler derives
it deterministically for `hasEligibilityRule`, `hasSalesConditionRule`, and
`governedByPolicy`. If the document supplies a conflicting rule type,
validation fails instead of silently replacing it.

Use unique `tempId` values. Every edge source and target must refer to a node in
the same draft. Provide non-empty, source-grounded evidence for every node and
edge. Confidence is between 0 and 1.

If the same property appears twice with the same value, the compiler
deduplicates it. If the values differ, validation returns
`DUPLICATE_PROPERTY`; resolve the conflict from evidence or report it.

## Source-grounding rules

- Emit only facts stated or unambiguously entailed by the document.
- Do not infer `Published`, another status, a product code, an effective date,
  price, fee, eligibility condition, or relationship merely because ontology
  persistence requires it.
- Preserve list order when it can carry domain meaning.
- Do not copy ontology labels or examples as document facts.
- Put uncertainty in `warnings`; do not hide it in invented values.
- Evidence text must support the exact node or relationship it accompanies.

## Interpret validation correctly

`validForExtraction: false` means the emitted draft itself is invalid. Examples:

- malformed schema or technical name;
- unknown class, property, or edge;
- wrong property datatype/domain or edge domain/range;
- duplicate/conflicting properties or temp IDs;
- dangling references or invalid evidence;
- deterministic semantic conflict.

Fix these only from the source and ontology, then revalidate.

`validForExtraction: true` with `validForPersistence: false` means the
source-grounded extraction is acceptable but not safe to persist. Examples:

- a required `Published` status is absent;
- a required ontology relationship or effective value is absent;
- identity is unresolved after all permitted identity policies.

Do not fabricate missing facts to clear readiness. Explain what authority is
missing and stop before fill.

An `IDENTITY_UNRESOLVED` readiness issue is not the same as a schema error. A
deterministic source-scoped identity may make a node ready even when it has no
natural key.

A persistence failure happens after a valid gate and is reported by fill. Do
not rewrite the draft to conceal a Neo4j or transaction failure.

## Correction loop

When validation returns correctable extraction errors:

1. Locate the issue by `location`, `nodeTempId`, `propertyName`, or `edgeName`.
2. Re-check the exact document evidence and ontology context.
3. Make the smallest supported correction.
4. Re-submit the full current draft to validation.
5. Repeat until valid or until evidence cannot resolve the issue.

Never call fill with a modified draft that has not been revalidated. Fill also
recompiles and revalidates defensively, but that is not a substitute for the
workflow.

## Progressive disclosure

Use `load_skill_resource` when details are needed:

- Load `references/graph-patch-contract.md` before constructing an unfamiliar
  class/property/edge shape or diagnosing schema/compiler errors.
- Load `references/validation-policy.md` when interpreting extraction versus
  readiness, identity, or persistence failures.
- Load `references/examples.md` for positive and negative correction examples.

Do not load all references by default when the main contract is sufficient.

## Final response

State the requested outcome and what actually completed. For extraction or
validation, summarize both validation flags and unresolved issues. For
persistence, include fill success/failure and counts returned by the tool.
Never represent a not-ready draft as persisted.
