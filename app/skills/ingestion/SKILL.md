---
name: ingestion
description: >
  Extract, validate, and optionally persist Product Sales knowledge from an
  uploaded business document into the Product Sales Knowledge Graph. Use for
  ingest, import, load, extract-only, and validate-only requests.
metadata:
  adk_additional_tools:
    - ingest_document_end_to_end
    - get_ingestion_status
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
- If persistence was requested but readiness fails, report the issues and stop before fill by default. If and only if the user explicitly asks for partial persistence despite readiness issues, call `ingest_document_end_to_end(..., allow_partial_persistence=true)`. This mode never bypasses extraction/source-grounding failures.

## Required workflow

For ingest/import/load/write requests on uploaded long documents, call
`ingest_document_end_to_end(artifact_name, persist=true)` first. This is the
user-facing path because it runs batching, extraction, validation, readiness,
and persistence to a real terminal state before returning. Do not use staged
manual tools for a normal user ingestion request unless the user explicitly asks
to debug or manually inspect batches. The root agent exposes only the end-to-end
long-document ingestion tool; staged begin/submit/finalize/fill helpers are
internal implementation/debug APIs and must not be emulated across model turns.
Do not fall back to a manual begin/submit loop after a retryable validation
error; the end-to-end tool owns retry pacing and retries.

For an uploaded long document, complete this sequence through a terminal state:

```text
ingest_document_end_to_end
  -> internal batching and retry
  -> internal validation and readiness gate
  -> optional fill when persistence-ready
  -> report verified committed readback when persistence is requested
```

Any response with `terminal: false`, `remainingBatches > 0`,
`stage: "batching"`, or `stage: "ready_to_finalize"` is an internal workflow
state and must not be described as complete. `validate_graph_patch` and
`fill_graph_patch` remain available for caller-supplied or genuinely small
patches. Do not use a legacy full-payload workflow for a long document. This
skill has no ingestion scripts.
Validation is invocation-scoped. Validation from an older turn never
authorizes a later fill. For an ingest/import/load/write request, completion
means a real Neo4j commit followed by verified readback. Never claim persistence
succeeded unless the terminal result has all of: `success: true`,
`stage: "completed"`, `commitStatus: "committed"`,
`verificationStatus: "verified"`, and `nodes > 0`. Candidate/extracted node
counts before fill are not persisted node counts.

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
a node of the same class if the later chunk adds a distinct persisted fact. But
if the later chunk is only a duplicate example or repeated explanation and adds
no distinct graph fact, `NOT_RELEVANT` is correct.

If validation reports `COVERAGE_MISSING` or `COVERAGE_NOT_EVIDENCED`, inspect
every affected chunk and extend a property/edge fact or provide a justified
`NOT_RELEVANT` decision. Adding generic node evidence is not a correction.

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

For Markdown tables, preserve the source row exactly, including leading and
trailing pipes and spacing. For example, cite
`| Tên tài liệu | Hướng dẫn nghiệp vụ sản phẩm Thẻ tín dụng Flexi Rewards |`,
not `Tên tài liệu | Hướng dẫn nghiệp vụ sản phẩm Thẻ tín dụng Flexi Rewards`.

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
  text that actually occurs in that chunk exactly. Do not change case, collapse
  whitespace, remove Markdown table delimiters, or combine non-contiguous lines.
- Never invent `Published` or another lifecycle status to satisfy ontology.
- Never invent product codes, version numbers, dates, fees, limits, conditions,
  or relationships.
- Every edge evidence excerpt must identify grounded facts for both endpoints
  and contain wording that states the relationship predicate (for example,
  "điều kiện", "hạn mức", "hồ sơ yêu cầu", or the ontology edge label).
  Endpoint co-occurrence without predicate wording is not edge evidence.
- Convert a source date to ISO `YYYY-MM-DD` only when its meaning is
  unambiguous. The compiler never guesses locale-sensitive dates.
- Preserve list order when order may carry domain meaning.
- Put genuine uncertainty in `warnings`; do not hide it in fabricated values.

Every property value is checked against each cited evidence excerpt, including
strings/lists, booleans, numbers, fees, limits, conditions, codes, statuses,
versions, and dates. If validation returns
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
metadata, or unresolved identity are readiness issues. Do not fabricate facts to clear readiness. A user may explicitly request partial persistence to inspect the extracted values; this writes the extraction-valid incomplete graph and returns `partialPersistence: true` plus the ignored readiness issues.

## Correction loop

When validation fails:

1. Inspect issue `code`, `location`, `nodeTempId`, `propertyName`, and `edgeName`.
2. For coverage issues, revisit the missing/conflicting chunk. If
   `errorSummary.coverageNotEvidencedChunkIndexes` is non-empty, the corrected
   fragment must not keep the same unsupported `MAPPED` coverage: for every
   listed chunk, either add a grounded property/edge fact citing that exact
   chunk or change that chunk to `NOT_RELEVANT` with a source-based reason.
3. For grounding issues, inspect the exact cited chunk and replace unsupported
   facts only when source evidence exists.
4. For ontology issues, use the supplied ontology context; never modify the
   ontology.
5. Submit the entire corrected draft to validation again. Never resubmit an
   unchanged fragment after `COVERAGE_NOT_EVIDENCED`.

Never call fill with a changed draft that has not been finalized again.

## Progressive disclosure

Use `load_skill_resource` when needed:

- `references/graph-patch-contract.md` for exact draft/evidence/coverage shape;
- `references/validation-policy.md` for coverage, grounding, readiness, identity,
  and persistence behavior;
- `references/examples.md` for correction examples.

## Final response

State what actually completed. For extraction/validation, report both validation
flags and unresolved issues. For persistence, report `commitStatus`, node/edge
counts, label/type distributions, verification status, and receipt artifact
name/version. Say that the graph was written to Neo4j only when `commitStatus`
is `committed`, readback is `verified`, and at least one node was persisted.
Candidate nodes in the workspace are never described as created/persisted nodes.
A readback mismatch means `success=false`, `stage=readback`, and
`commitStatus=committed`; never claim rollback after commit. If a document had
many prepared chunks, do not present a tiny graph as complete unless every
chunk passed the fact-level coverage gate.
