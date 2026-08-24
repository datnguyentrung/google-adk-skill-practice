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
A graph that covers only a convenient subset of the document is not a complete
ingestion.

## Choose the requested outcome

- Extract-only: prepare, build a complete source-grounded `GraphPatchDraft`,
  validate it, and return it.
- Validate-only: validate the supplied draft and explain structured issues.
- Ingest/import/load/write: prepare, extract, validate, correct if grounded,
  then fill.
- If persistence was requested but source evidence cannot satisfy persistence
  requirements, report readiness issues and stop before fill.

## Required workflow

For an uploaded document, complete this sequence in one invocation:

```text
prepare artifact
  -> inspect ALL prepared chunks and ontology context
  -> build a coverage ledger for every chunk
  -> map each relevant semantic unit to the most specific ontology construct
  -> attach exact chunk evidence to nodes, properties, and edges
  -> validate
  -> correct source-grounded errors or missing coverage
  -> revalidate after every change
  -> fill only when both validation flags are true
```

1. Call `prepare_extraction_context(artifact_name)`.
2. Record `chunkCount` and inspect every returned chunk. Do not stop after the
   first product metadata or eligibility section.
3. For every chunk, decide `MAPPED` or `NOT_RELEVANT`. `NOT_RELEVANT` is
   exceptional: use it only when the chunk contains no business fact that can
   be represented by the ontology.
4. For every `MAPPED` chunk, emit at least one node/property/edge evidence item
   referring to that exact `chunkIndex`.
5. Build the complete graph draft before validation.
6. Call `validate_graph_patch(graph_patch)`.
7. Correct only issues supported by the source and ontology.
8. Revalidate the full draft after every change.
9. Call `fill_graph_patch(graph_patch)` only when `validForExtraction` and
   `validForPersistence` are both true in the current invocation.

Validation is invocation-scoped. Validation from an older turn never
authorizes a later fill. Never claim persistence succeeded unless fill returns
`success: true`.

## Coverage is mandatory

`GraphPatchDraft.coverage` must contain exactly one entry for every prepared
chunk index. Missing chunks are an extraction error.

```json
"coverage": [
  {"chunkIndex": 0, "decision": "MAPPED", "reason": "Document governance metadata"},
  {"chunkIndex": 1, "decision": "MAPPED", "reason": "Product code and effective date"},
  {"chunkIndex": 2, "decision": "MAPPED", "reason": "Product explanation"}
]
```

Do not mark a chunk `NOT_RELEVANT` merely because another chunk already created
a node of the same class. A later chunk may contribute new properties, rules,
documents, segments, scripts, campaigns, offers, or knowledge.

If validation reports `COVERAGE_MISSING`, inspect every missing chunk and extend
the graph or provide a justified `NOT_RELEVANT` decision.

## Prefer specific ontology concepts over generic compression

Do not collapse independently queryable facts into one short summary node.
Use the most specific class/property/edge allowed by the ontology. For example,
when source and ontology support them, distinguish concepts such as:

- product facts -> `pskg:BankingProduct` properties;
- named promotions/programs -> `pskg:Campaign` or `pskg:ProductOffer`;
- customer groups -> `pskg:CustomerSegment`;
- needs -> `pskg:CustomerNeed`;
- eligibility/sales conditions -> `pskg:BusinessRule`;
- required application artifacts -> `pskg:RequiredDocument`;
- scripted scenarios or objection handling -> `pskg:SalesScript`;
- explanatory material with no more specific class -> `pskg:SalesKnowledge`.

`SalesKnowledge` is a fallback for genuine knowledge content, not a bucket used
to avoid creating more specific ontology nodes.

For repeated independently queryable items, preserve useful granularity. For
example, distinct required document types should normally remain distinct
`RequiredDocument` nodes; distinct named customer segments should remain
distinct segments. Do not create one node per sentence mechanically: group only
when the facts share one semantic identity.

## Draft contract

Every evidence item identifies the exact prepared chunk and contains a verbatim
source excerpt. Do not paraphrase inside `evidence.text`.

```json
{
  "nodes": [
    {
      "tempId": "product-1",
      "className": "pskg:BankingProduct",
      "properties": [
        {
          "propertyName": "pskg:productCode",
          "value": "CC-FLEXI-001",
          "evidence": [{
            "source": "example.md",
            "chunkIndex": 1,
            "section": "Product information",
            "text": "Product code: CC-FLEXI-001"
          }]
        },
        {
          "propertyName": "pskg:bankingProductEffectiveFrom",
          "value": "2026-08-01",
          "evidence": [{
            "source": "example.md",
            "chunkIndex": 1,
            "section": "Product information",
            "text": "Effective date: 01/08/2026"
          }]
        }
      ],
      "evidence": [{
        "source": "example.md",
        "chunkIndex": 1,
        "section": "Product information",
        "text": "Flexi Rewards credit card product"
      }],
      "confidence": 0.98
    }
  ],
  "edges": [],
  "coverage": [
    {"chunkIndex": 0, "decision": "MAPPED", "reason": "Document metadata"},
    {"chunkIndex": 1, "decision": "MAPPED", "reason": "Product metadata"}
  ],
  "warnings": []
}
```

Every `className`, `propertyName`, and `edgeName` is a complete ontology
technical name in `prefix:localName` form.

Invalid: `pskg`, `productCode`, `pskg:`.
Valid: `pskg:BankingProduct`, `pskg:productCode`,
`pskg:hasEligibilityRule`.

Each property has its own evidence. Node evidence does not automatically prove
all of that node's properties.

## Source-grounding rules

- Emit only facts stated or unambiguously entailed by the source.
- Evidence must point to a real `chunkIndex`, match its source/section, and use
  text that actually occurs in that chunk.
- Never invent `Published` or another lifecycle status to satisfy ontology.
- Never invent product codes, version numbers, dates, fees, limits, conditions,
  or relationships.
- Convert a source date to ISO `YYYY-MM-DD` only when its meaning is
  unambiguous. The compiler never guesses locale-sensitive dates.
- Preserve list order when order may carry domain meaning.
- Put genuine uncertainty in `warnings`; do not hide it in fabricated values.

Literal-sensitive values such as codes, statuses, versions, and dates are
checked against the cited source chunks. If validation returns
`PROPERTY_VALUE_NOT_GROUNDED`, remove or correct the unsupported value; never
invent a quote to support it.

Do not emit `pskg:ruleType` solely because it is absent. The compiler derives it
deterministically for `hasEligibilityRule`, `hasSalesConditionRule`, and
`governedByPolicy`. A conflicting supplied rule type is a semantic error.

## Interpret validation correctly

`validForExtraction: false` means the emitted extraction is invalid or
incomplete. Typical issues include malformed schema/technical names, unknown
ontology terms, datatype/domain/range errors, duplicate/conflicting facts,
dangling references, source-evidence mismatches, unsupported literal values,
and incomplete chunk coverage.

`validForExtraction: true` with `validForPersistence: false` means the complete
source-grounded extraction is acceptable but cannot yet be persisted under the
ontology. Missing required governance status, required relations, effective
metadata, or unresolved identity are readiness issues. Do not fabricate facts
to clear readiness.

## Correction loop

When validation fails:

1. Inspect issue `code`, `location`, `nodeTempId`, `propertyName`, and `edgeName`.
2. For coverage issues, revisit the missing/conflicting chunk.
3. For grounding issues, inspect the exact cited chunk and replace unsupported
   facts only when source evidence exists.
4. For ontology issues, use the supplied ontology context; never modify the
   ontology.
5. Submit the entire corrected draft to validation again.

Never call fill with a changed draft that has not been revalidated.

## Progressive disclosure

Use `load_skill_resource` when needed:

- `references/graph-patch-contract.md` for exact draft/evidence/coverage shape;
- `references/validation-policy.md` for coverage, grounding, readiness, identity,
  and persistence behavior;
- `references/examples.md` for correction examples.

## Final response

State what actually completed. For extraction/validation, report both validation
flags and unresolved issues. For persistence, report fill success/failure and
node/edge counts. If a document had many prepared chunks, do not present a tiny
graph as complete unless every chunk passed the coverage gate.
