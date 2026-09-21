---
name: ingestion

description: >
  Extract, validate, incrementally stage, and optionally persist Product Sales
  knowledge from uploaded business documents into the Product Sales Knowledge
  Graph. This skill owns the ingestion workflow and coordinates batch processing,
  dynamic schema loading, semantic extraction, repair, finalization, and persistence.

metadata:
  adk_additional_tools:
    - begin_ingestion
    - get_ingestion_batch
    - submit_ingestion_batch
    - finalize_ingestion
    - fill_ingestion
    - get_ingestion_status

    - load_product_catalog_schema
    - load_business_rules_schema
    - load_campaign_targeting_schema
    - load_customer_recommendation_schema
    - load_sales_enablement_schema
    - load_governance_versioning_schema

    - delete_document
    - validate_graph_patch
    - fill_graph_patch
---

# Product Sales Knowledge Graph ingestion

You own the ingestion workflow.

You are responsible for deciding:

- which ingestion step runs next;
- which Schema Skills are needed for the current batch;
- how to extract the current batch semantically;
- when the current batch must be repaired and retried;
- when all batches are ready to finalize;
- when persistence may begin.

Python tools provide deterministic capabilities such as document preparation,
workspace management, validation, identity handling, staging, lifecycle management,
and Neo4j persistence.

Do not reproduce deterministic persistence, staging, identity, or validation logic
in reasoning.

You are also the semantic mapper.

Do not invoke another extraction agent or a separate schema-routing model.

Schema selection, schema loading, and graph extraction for one batch are one
continuous semantic workflow owned by this skill.

The ontology is read-only and is the source of truth for:

- technical names;
- classes and properties;
- relationship domains and ranges;
- datatypes;
- identity rules;
- persistence constraints.

Never modify the ontology.

Never invent missing business facts to satisfy ontology requirements.

A graph that covers only a convenient subset of the source is not a complete
ingestion.


## Requested outcomes

For a new ingest/import/load/write request:

- execute the staged batch ingestion workflow defined below;
- persist only when the user requested persistence.

For extract-only:

- execute the same batch workflow through finalization;
- do not call `fill_ingestion`.

For validate-only on a caller-supplied small graph patch:

- use `validate_graph_patch`;
- report structured validation issues;
- do not run the document ingestion workflow unless a source document also needs
  to be ingested.

For delete:

- use `delete_document`;
- never simulate deletion by ingesting an empty graph.

For caller-supplied small graph patches:

- `validate_graph_patch` and `fill_graph_patch` may still be used directly.

Do not call or emulate deprecated one-shot ingestion runners.
The workflow in this skill is the source of truth for document ingestion control
flow.


## Required ingestion workflow

For a new document ingestion, follow this workflow exactly.


### 1. Begin ingestion

Call:

`begin_ingestion(artifact_name)`

This prepares the source, creates chunks and batches, initializes the ingestion
workspace, and returns the first `nextBatch`.

If an active uncommitted ingestion workspace already exists for the same document,
`begin_ingestion` will return the existing workspace status with `"resumed": true`
instead of re-partitioning or re-creating the workspace.

Keep the returned `ingestionId` for the entire ingestion run.

If begin fails, stop and report the returned error.


### 2. Process the active batch
Read all chunks contained in `nextBatch`.

Treat the entire batch as one semantic extraction unit.

Do not inspect only the first chunk and assume the remaining chunks belong to the
same schema domain.

If `nextBatch` contains `canonicalGraphContext`, use it as context about relevant
entities already staged by previous batches.

Canonical graph context is advisory semantic context. It does not replace
deterministic identity handling performed by the staging/persistence layer.


### 3. Select and load Schema Skills dynamically

Schema selection is strictly batch-scoped.

For every new batch:

- inspect only the active batch chunks;
- select the minimum required schema set again;
- normally load 1-2 schemas;
- load 3 only when the batch clearly spans three domains;
- loading more than 3 schemas is not allowed unless required by a validation repair;
- do not load a schema merely because another entity refers to that domain;
- do not rely on a previous batch's schema selection as evidence that the same
  schema is required for the current batch;
- load only the selected Schema Skills using their `load_<domain>_schema` tools;
- use the loaded ontology contracts immediately to extract the current batch.

Do not create a separate schema-routing phase or invoke a second model merely to
choose Schema Skills.

Schema selection, schema loading, and extraction belong to the same semantic flow
for the active batch.

Available Schema Skills include:

- `product-catalog`
  Banking products, offers, bundles, product attributes, fees, rates, benefits,
  limits, and product configuration.

- `business-rules`
  Eligibility rules, policies, qualification criteria, business constraints,
  required documents, and sales/product conditions.

- `campaign-targeting`
  Campaigns, promotions, targeting, customer segments, incentives, and campaign
  applicability.

- `customer-recommendation`
  Customer needs, customer context, current product usage, suitability, and
  recommendations.

- `sales-enablement`
  Sales knowledge, scripts, scenarios, FAQs, objection handling, and advisory
  content.

- `governance-versioning`
  Version lifecycle, approvals, effective periods, publication state, audit/change
  history, and governance metadata.

Do not load all Schema Skills unless the batch genuinely requires them.


### 4. Extract one batch fragment

Using:

- the current batch chunks;
- the loaded Schema Skills;
- `canonicalGraphContext` when provided;
- the source-grounding rules in this skill;

produce exactly one `GraphPatchFragment` for the current batch.

The fragment may contain:

- nodes;
- edges;
- properties on those nodes;
- coverage for every chunk in the active batch;
- warnings when genuine uncertainty exists.

Do not construct or retain a full-document graph in model context.

Previously completed batch fragments are already accumulated in ingestion staging.
Do not reconstruct previous fragments merely to process the next batch.


### 5. Submit the batch

Call:

```text
submit_ingestion_batch(
    ingestion_id,
    batch_index,
    graph_fragment
)
```

The tool performs deterministic work such as:

- fragment validation;
- batch-scope validation;
- identity handling;
- decomposition;
- incremental staging;
- pending-edge handling;
- batch-state updates.

Submitting a fragment is not a new semantic extraction phase.

If submission succeeds:

- treat the batch as staged;
- discard the completed fragment from working reasoning;
- inspect the returned state;
- if another `nextBatch` is returned, repeat steps 2 through 5 for that batch.

Never move to the next batch while the current batch remains unresolved.


### 6. Repair a failed batch

If extraction or submission reports a retryable issue:

- remain on the same batch;
- inspect the structured error;
- repair only the invalid or incomplete facts;
- preserve unrelated valid facts;
- keep already relevant Schema Skills loaded;
- load an additional Schema Skill only when the error indicates that another
  ontology domain is actually required;
- do not automatically load every remaining Schema Skill;
- submit the corrected fragment again.

Prefer minimal repair over remapping the batch from scratch when the prior fragment
is substantially valid.


### 7. Finalize ingestion

When no further batch remains, call:

`finalize_ingestion(ingestion_id)`

Finalization validates the accumulated staging state, including:

- all batches staged;
- document-level chunk coverage complete;
- unresolved pending relationships;
- unresolved conflicts;
- persistence readiness.

Do not reconstruct or merge all previous batch fragments in model context.

Staging is the accumulated source of truth for the ingestion run.


### 8. Repair final coverage when necessary

If finalization returns `"stage": "repair_required"` with `"ready": false` and a list of `repairBatchIndexes`:

**NEVER call `begin_ingestion()` during repair.** Calling `begin_ingestion()` in a repair flow is strictly forbidden.

Follow this exact repair loop for each index `idx` in `repairBatchIndexes`:

1. Call `get_ingestion_batch(ingestion_id, idx)` to retrieve the payload for batch `idx`.
2. Inspect the active batch chunks and load only the minimum required Schema Skills.
3. Extract grounded facts or provide justified `NOT_RELEVANT` decisions for the missing/invalid chunks.
4. Submit the corrected batch fragment using `submit_ingestion_batch(ingestion_id, idx, fragment)`.

When all `repairBatchIndexes` have been re-extracted and submitted, call `finalize_ingestion(ingestion_id)` again.

When a previously staged batch is resubmitted, the staging implementation refreshes and replaces that batch contribution rather than blindly retaining superseded facts from the earlier submission.

Do not fabricate ontology-required facts merely to clear readiness.


### 9. Persist when requested

If persistence was requested and finalization reports that the ingestion is ready
to fill, call:

`fill_ingestion(ingestion_id)`

This promotes validated staged facts into the domain graph.

Do not call fill before successful finalization.

If readiness is not satisfied, report the unresolved readiness issues rather than
inventing facts to force persistence.


### 10. Completion criteria

For extract-only:

- completion means every batch has been successfully staged;
- finalization has run;
- document-level extraction/coverage checks have completed.

For persisted ingestion:

- completion means the persistence operation reports a committed result;
- if the tool reports verification/readback status, it must indicate success.

Never report an intermediate `batching`, retry, `ready_to_finalize`, or readiness
state as a completed ingestion.


## Coverage is mandatory

Each `GraphPatchFragment.coverage` must contain exactly one coverage entry for
every chunk index in the active batch.

Missing coverage for any chunk in the active batch is an extraction error.

Document-level coverage is accumulated in ingestion staging and verified by
`finalize_ingestion` after all batches have been staged.

Example:

```json
"coverage": [
  {
    "chunkIndex": 10,
    "decision": "MAPPED",
    "reason": "Product attributes and pricing"
  },
  {
    "chunkIndex": 11,
    "decision": "MAPPED",
    "reason": "Eligibility conditions"
  },
  {
    "chunkIndex": 12,
    "decision": "NOT_RELEVANT",
    "reason": "Repeated explanatory text with no additional persisted fact"
  }
]
```

Do not mark a chunk `NOT_RELEVANT` merely because another chunk already created a
node of the same class if the later chunk adds a distinct persisted fact.

If a later chunk is only a duplicate example or repeated explanation and adds no
distinct graph fact, `NOT_RELEVANT` is correct.

If validation reports `COVERAGE_MISSING` or `COVERAGE_NOT_EVIDENCED`, inspect every
affected chunk and either:

- add a grounded property/edge fact citing that exact chunk; or
- provide a justified `NOT_RELEVANT` decision.

Adding generic node evidence is not a correction.


## Prefer specific ontology concepts over generic compression

Do not collapse independently queryable facts into one short summary node.

Use the most specific class/property/edge allowed by the loaded ontology.

When source and ontology support them, distinguish concepts such as:

- base product facts, normal/base rates, fees, terms, and ordinary product conditions -> `pskg:BankingProduct` properties or the appropriate `pskg:BusinessRule`; do not create a `pskg:ProductOffer` merely to hold the product's normal/base rate schedule;
- a named promotion/program that changes the commercial terms of one banking product (for example an additional rate, discount, benefit, promotional eligibility window, or promotional conditions) -> `pskg:ProductOffer`;
- a time-bound marketing/sales initiative that promotes products/offers and targets customer segments or needs -> `pskg:Campaign`;
- customer groups -> `pskg:CustomerSegment`;
- needs -> `pskg:CustomerNeed`;
- eligibility/sales conditions -> `pskg:BusinessRule`;
- required application artifacts -> `pskg:RequiredDocument`;
- scripted scenarios or objection handling -> `pskg:SalesScript`;
- explanatory material with no more specific class -> `pskg:SalesKnowledge`.

ProductOffer must be linked from exactly one BankingProduct via `pskg:hasOffer`.
Campaign/rule/segment/need edges such as `pskg:offerInCampaign`,
`pskg:offerHasRule`, `pskg:offerTargetsSegment`, and
`pskg:offerAddressesNeed` add optional context unless the loaded ontology says
otherwise; they do not replace `pskg:hasOffer`.

When the current batch introduces a node and the source directly states its
relationship to an entity already present in `canonicalGraphContext`, emit that
relationship in the current fragment using the canonical reference. Do not omit a
newly evidenced edge merely because one endpoint was staged by an earlier batch.
For example, a newly extracted `BusinessRule`, `CustomerNeed`,
`SalesKnowledge`, `SalesScript`, `Campaign`, or `ProductOffer` should be
connected to the relevant existing product/offer/campaign whenever the active
batch explicitly supports that relationship.

`SalesKnowledge` is a fallback for genuine knowledge content, not a bucket used to
avoid creating more specific ontology nodes.

For repeated independently queryable items, preserve useful granularity.

For example, distinct required document types should normally remain distinct
`RequiredDocument` nodes; distinct named customer segments should remain distinct
segments.

Do not create one node per sentence mechanically. Group only when the facts share
one semantic identity.


## Batch fragment contract

Every evidence item identifies the exact prepared chunk and contains a verbatim
source excerpt.

Do not paraphrase inside `evidence.text`.

For Markdown tables, preserve the source row exactly, including leading/trailing
pipes and spacing.

For example, cite:

`| Tên tài liệu | Hướng dẫn nghiệp vụ sản phẩm Thẻ tín dụng Flexi Rewards |`

not:

`Tên tài liệu | Hướng dẫn nghiệp vụ sản phẩm Thẻ tín dụng Flexi Rewards`

Example fragment shape:

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
          "evidence": [
            {
              "source": "example.md",
              "chunkIndex": 1,
              "section": "Product information",
              "text": "Product code: CC-FLEXI-001"
            }
          ]
        },
        {
          "propertyName": "pskg:bankingProductEffectiveFrom",
          "value": "2026-08-01",
          "evidence": [
            {
              "source": "example.md",
              "chunkIndex": 1,
              "section": "Product information",
              "text": "Effective date: 01/08/2026"
            }
          ]
        }
      ],
      "evidence": [
        {
          "source": "example.md",
          "chunkIndex": 1,
          "section": "Product information",
          "text": "Flexi Rewards credit card product"
        }
      ],
      "confidence": 0.98
    }
  ],
  "edges": [],
  "coverage": [
    {
      "chunkIndex": 1,
      "decision": "MAPPED",
      "reason": "Product metadata"
    }
  ],
  "warnings": []
}
```

Every `className`, `propertyName`, and `edgeName` is a complete ontology technical
name in `prefix:localName` form.

Invalid:

- `pskg`
- `productCode`
- `pskg:`

Valid:

- `pskg:BankingProduct`
- `pskg:productCode`
- `pskg:hasEligibilityRule`

Each property has its own evidence.

Node evidence does not automatically prove all of that node's properties.


## Source-grounding rules

- Emit only facts stated or unambiguously entailed by the source.

- Evidence must point to a real `chunkIndex`, match its source/section, and use
  text that actually occurs in that chunk exactly.

- Do not change case, collapse whitespace, remove Markdown table delimiters, or
  combine non-contiguous lines in evidence text.

- Never invent `Published` or another lifecycle status to satisfy ontology.

- Never invent product codes, version numbers, dates, fees, limits, conditions,
  statuses, or relationships.

- Every edge evidence excerpt must ground the relationship, not merely show that
  both endpoint entities occur in the same chunk.

- Endpoint co-occurrence without relationship evidence is not sufficient edge
  evidence.

- Convert a source date to ISO `YYYY-MM-DD` only when its meaning is unambiguous.

- Never guess locale-sensitive dates.

- Preserve list order when order may carry domain meaning.

- Put genuine uncertainty in `warnings`; do not hide it in fabricated values.

Every property value must be supported by its cited evidence, including:

- strings/lists;
- booleans;
- numbers;
- fees;
- limits;
- conditions;
- codes;
- statuses;
- versions;
- dates.

If validation returns `PROPERTY_VALUE_NOT_GROUNDED`, remove or correct the
unsupported value.

Never invent a quote to support it.

Do not emit `pskg:ruleType` solely because it is absent.

If the compiler derives a property deterministically, do not duplicate or
contradict that deterministic derivation.


## Identity and canonical reuse

Canonical graph context helps semantic extraction reuse entities already staged
by previous batches.

Do not create a new entity merely because the same real-world entity appears in a
new batch.

Reuse a canonical entity reference when the provided context clearly identifies
the same entity.

However, do not force two entities to merge when source identity indicates they
are distinct.

The final identity decision is enforced by deterministic identity/staging logic,
not by semantic context alone.


## Interpret validation correctly

`validForExtraction: false` means the emitted extraction is invalid or incomplete.

Typical issues include:

- malformed schema/technical names;
- unknown ontology terms;
- datatype/domain/range errors;
- duplicate/conflicting facts;
- dangling references;
- source-evidence mismatches;
- unsupported literal values;
- incomplete batch/document coverage.

`validForExtraction: true` with `validForPersistence: false` means the
source-grounded extraction is acceptable but cannot yet be persisted under the
ontology/readiness rules.

Possible readiness issues include:

- missing required governance metadata;
- required relationships not yet available;
- unresolved identity;
- unresolved pending edges;
- unresolved conflicts.

Do not fabricate facts merely to clear readiness.


## Correction loop

When the current batch fails validation:

1. Inspect structured issue fields such as:
   `code`, `location`, `nodeTempId`, `propertyName`, and `edgeName`.

2. Repair only the affected facts in the current batch.

3. For coverage issues:
   - revisit the affected chunk;
   - add a grounded property/edge fact citing that chunk; or
   - mark it `NOT_RELEVANT` with a source-based reason.

4. For grounding issues:
   - inspect the exact cited chunk;
   - correct or remove unsupported facts.

5. For ontology issues:
   - use the loaded Schema Skill contracts;
   - load an additional Schema Skill only when another ontology domain is actually
     required.

6. Preserve unrelated valid facts.

7. Resubmit the corrected batch through `submit_ingestion_batch`.

8. Do not move to another batch until the current batch is staged successfully.

After all batches are staged, use `finalize_ingestion` to validate the accumulated
ingestion state.


## Progressive disclosure

Use `load_skill_resource` when needed:

- `references/graph-patch-contract.md`
  for exact fragment/evidence/coverage shapes;

- `references/validation-policy.md`
  for coverage, grounding, readiness, identity, and persistence behavior;

- `references/examples.md`
  for extraction and correction examples.

Do not load all reference material unless needed.


## Final response

State only what actually completed.

For extraction/finalization, report:

- whether all batches were staged;
- extraction/validation status returned by finalization;
- unresolved coverage/readiness issues, if any.

For persistence, report fields actually returned by `fill_ingestion`, such as:

- `commitStatus`;
- persisted node count;
- persisted edge count;
- verification/readback status, when returned.

Say that the graph was written to Neo4j only when the fill result confirms a
successful committed persistence operation.

Do not describe candidate/staged nodes as persisted domain nodes before fill.

If readback/verification fails after a commit, report that state accurately and
do not claim rollback unless the persistence layer explicitly confirms one.

If a document had many prepared chunks, do not present a tiny graph as complete
unless every chunk passed the fact-level coverage gate.
