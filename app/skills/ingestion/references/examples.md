# Ingestion correction examples

## Property shape and evidence

Wrong:

```json
"properties": {"pskg:productCode": "CC-FLEXI-001"}
```

Also wrong: a property entry without evidence.

Correct:

```json
"properties": [{
  "propertyName": "pskg:productCode",
  "value": "CC-FLEXI-001",
  "evidence": [{
    "source": "example.md",
    "chunkIndex": 1,
    "section": "Product information",
    "text": "Product code: CC-FLEXI-001"
  }]
}]
```

Evidence text must be a verbatim excerpt from that exact chunk.

## Missing coverage

If prepare returns chunks `0..12` but coverage mentions only chunks 0 and 5, validation returns `COVERAGE_MISSING`.

Do not simply mark all missing chunks `NOT_RELEVANT`. Inspect each missing chunk and map any ontology-relevant business facts first.

A `MAPPED` chunk must be referenced by at least one node/property/edge evidence item.

## Hallucinated status

Source:

```text
Product code: CC-FLEXI-001
Effective date: 01/08/2026
```

Wrong:

```json
{"propertyName":"pskg:bankingProductStatus","value":"Published", "evidence":[...]}
```

Because the cited source does not state `Published`, do not emit it as a source fact. Omit the status; when the ontology marks that status as `runtime_managed`, the compiler supplies its configured safe value such as `Draft`.

## Granularity

If one document contains a named campaign, four distinct customer segments, several required document types, and multiple eligibility conditions, do not compress all of that into one generic `SalesKnowledge` node.

Prefer the most specific ontology classes and preserve independently queryable units. Use `SalesKnowledge` only for explanatory content that has no more specific representation.

## Date mapping

If source context makes `01/08/2026` unambiguously 1 August 2026, emit `2026-08-01`. If locale/meaning is ambiguous, warn and do not guess.

## Correction and revalidation

For `COVERAGE_MISSING`, revisit each missing chunk. For `EVIDENCE_TEXT_NOT_IN_SOURCE`, replace the paraphrase with a real excerpt. For `PROPERTY_VALUE_NOT_GROUNDED`, remove or correct the unsupported literal value.

After any change, submit the complete draft to validation again. Fill only after both validation flags are true in the same invocation.