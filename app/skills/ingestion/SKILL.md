---
name: ingestion
description: >
  Extract structured Product Sales Knowledge Graph data from business
  documents using the Product Sales ontology, validate the resulting
  GraphPatch, and optionally persist it to Neo4j.
metadata:
  adk_additional_tools:
    - prepare_extraction_context
    - validate_graph_patch
    - fill_graph_patch
---

# Product Sales Knowledge Graph Ingestion

Use this skill when the user wants to extract, map, ingest, validate,
or fill product-sales knowledge from a business document into the
Product Sales Knowledge Graph.

## Core principle

The root model performs all semantic reasoning.

Do NOT invoke another LLM, agent, or model to perform extraction.

The ontology file is read-only.

Never modify:

`app/data/ontology/product_sales_knowledge_graph_base_v3_1.ontology.json`

The ontology is the source of truth for:

- classes
- properties
- edges
- domain
- range
- datatype
- cardinality/rules

Operational ingestion policies may exist outside the ontology, but they
must never mutate the ontology file.

---

# Workflow

The ingestion workflow has two conceptual stages:

```text
Document
   ↓
EXTRACT
   ↓
GraphPatch
   ↓
FILL
   ↓
Neo4j
```

`EXTRACT` must never write to Neo4j.

`FILL` must never invent or infer missing knowledge.

---

# EXTRACT

## Goal

Convert the supplied source document into a `GraphPatch` grounded in:

1. evidence from the source document;
2. the Product Sales ontology.

The root model performs the semantic mapping.

## Step 1 — Read the document

Use the document preparation/reading capability to obtain document
chunks containing:

- source
- section
- content

Preserve section information because it is required for evidence.

Do not summarize away important numeric values, conditions, dates,
codes, statuses, thresholds, exceptions, or business rules before
extraction.

---

## Step 2 — Read ontology context

Obtain ontology context through the ingestion ontology services.

Use `technicalName` as the canonical identifier.

Examples:

```text
pskg:BankingProduct
pskg:BusinessRule
pskg:CustomerNeed

pskg:productCode
pskg:businessRuleCondition

pskg:hasEligibilityRule
pskg:satisfiesNeed
```

Do not use human-readable ontology names as GraphPatch identifiers.

For example:

Incorrect:

```json
{
  "className": "banking product"
}
```

Correct:

```json
{
  "className": "pskg:BankingProduct"
}
```

---

## Step 3 — Identify candidate entities

Read the document semantically.

Identify concepts that are explicitly supported by the document and
can be represented by ontology classes.

Never create an entity merely because such a class exists in the
ontology.

For each candidate entity determine:

- ontology class
- supported properties
- supporting evidence
- confidence

Prefer omission over fabrication.

---

## Step 4 — Extract properties

Only use properties allowed for the selected class.

Before using a property verify:

```text
property exists
AND
class is in property.domain
```

Do not create arbitrary properties.

Incorrect:

```json
{
  "pskg:annualInterestRate": 32
}
```

if `pskg:annualInterestRate` does not exist in the ontology.

If the document contains useful information that cannot be represented
by the ontology, preserve this fact in `warnings`.

---

## Step 5 — Normalize values conservatively

Normalization is allowed only when meaning is preserved.

Examples:

```text
01/08/2026
→ 2026-08-01
```

for an `xsd:date`.

Do not convert or infer values when the transformation changes business
meaning.

Do not invent missing codes, identifiers, dates, limits, versions, or
statuses.

---

## Step 6 — Extract relationships

Create an edge only when:

1. the edge exists in the ontology;
2. the source class is permitted by `domain`;
3. the target class is permitted by `range`;
4. the document provides sufficient semantic support for the
   relationship.

Every edge must reference existing node `tempId` values.

Example:

```text
BankingProduct
    └── pskg:hasEligibilityRule
            ↓
       BusinessRule
```

Do not generate Cypher during extraction.

---

## Step 7 — Handle repeated entities

When the same clearly identifiable entity appears in multiple document
sections, reuse one node.

Do not create duplicate nodes merely because an entity is mentioned
multiple times.

Use the strongest explicit identifiers available in the document when
deciding whether two mentions clearly refer to the same entity.

If identity is ambiguous, do not silently merge the entities.

Record uncertainty in `warnings`.

---

## Step 8 — Evidence

Every extracted node and edge must have evidence.

Evidence should contain:

```json
{
  "source": "...",
  "section": "...",
  "text": "..."
}
```

`text` should be a short source passage that directly supports the
extracted fact.

Do not place model reasoning in `evidence.text`.

Do not fabricate evidence.

---

## Step 9 — Confidence

Use confidence to represent extraction certainty, not ontology
validity.

Suggested interpretation:

```text
0.90–1.00
Explicit and unambiguous in the document.

0.75–0.89
Strongly supported but requires minor semantic mapping.

0.60–0.74
Plausible but somewhat ambiguous.

Below 0.60
Normally do not emit as a graph fact; place the issue in warnings.
```

Confidence does not override ontology validation.

---

# GraphPatch contract

Return extraction results using the GraphPatch contract.

Conceptually:

```json
{
  "nodes": [
    {
      "tempId": "product-1",
      "className": "pskg:BankingProduct",
      "properties": {},
      "evidence": [],
      "confidence": 0.95
    }
  ],
  "edges": [
    {
      "edgeName": "pskg:hasEligibilityRule",
      "sourceTempId": "product-1",
      "targetTempId": "rule-1",
      "evidence": [],
      "confidence": 0.93
    }
  ],
  "warnings": []
}
```

Do not add fields outside the GraphPatch schema unless the schema is
explicitly extended by the application.

---

# Important extraction rules

Never:

```text
document
→ keyword matching only
→ graph
```

Semantic interpretation must be performed by the root model.

For example, do not use brittle rules such as:

```text
if sentence contains "nhu cầu"
→ CustomerNeed
```

Instead determine the meaning of the passage and then ground that
meaning against the ontology.

Likewise, do not assume:

```text
every heading → node
every bullet → BusinessRule
every number → property
every product mention → new BankingProduct
```

---

# Validation

After constructing a GraphPatch, validate it using the ontology
validator.

Validation covers, where supported:

- unknown classes
- unknown properties
- property domain
- datatype
- class rules
- edge existence
- edge domain
- edge range
- required relationships
- supported semantic constraints

If validation fails during extraction:

1. inspect the failing facts;
2. correct mappings that are clearly wrong;
3. never fabricate missing source data merely to satisfy the ontology;
4. preserve unresolved problems in warnings where appropriate.

Do not write invalid data to Neo4j.

---

# FILL

## Goal

Persist an already-created GraphPatch safely.

FILL performs:

```text
GraphPatch
   ↓
strict validation
   ↓
identity resolution
   ↓
Neo4j transaction
   ↓
node upsert
   ↓
edge upsert
```

FILL must not perform semantic document extraction.

FILL must not invent missing properties or relationships.

---

## Identity handling

Some ontology entities have reliable operational identity policies.

Others may have unresolved identity.

If identity cannot be resolved safely:

```text
do not silently create a duplicate node
```

Return/report the unresolved identity instead.

Do not create arbitrary UUID-based identity merely to make persistence
succeed unless the ingestion policy explicitly permits it.

---

# Neo4j rules

Use ontology-derived mappings for:

```text
ontology class technicalName
→ ontology localName
→ Neo4j label

ontology property technicalName
→ ontology localName
→ Neo4j property

ontology edge technicalName
→ ontology localName
→ Neo4j relationship type
```

Do not accept arbitrary labels, relationship types, or property names
directly from document text.

Use transactions.

A GraphPatch should either be committed successfully or rolled back.

---

# Safety boundary

The ontology file is read-only.

Never modify the ontology to make a document pass validation.

If document content and ontology are incompatible:

```text
report the incompatibility
```

rather than changing the ontology.

---

# Output behavior

When the user asks only to extract:

```text
document
→ GraphPatch
```

Do not write to Neo4j.

When the user explicitly asks to ingest/fill/write:

```text
document
→ extract
→ validate
→ fill
```

When the user provides an existing GraphPatch and asks to fill it:

```text
GraphPatch
→ validate
→ fill
```

Do not re-extract the source document unnecessarily.

---

# Failure behavior

Stop FILL if any of the following occurs:

- GraphPatch validation fails
- source/target tempId is missing
- class/property/edge is outside ontology
- identity required for safe upsert cannot be resolved
- Neo4j transaction fails

Return actionable error information.

Never partially claim successful ingestion when the transaction was
rolled back.
