# GraphPatchDraft contract

`GraphPatchDraft` is model-visible. `CompiledGraphPatch` is internal and must not be emitted by the model.

## Top level

- `nodes`: extracted ontology nodes.
- `edges`: extracted ontology relationships.
- `coverage`: exactly one decision for every chunk returned by prepare.
- `warnings`: source uncertainties.
- Extra fields are rejected.

## Evidence

Every evidence object identifies an exact prepared chunk and uses a verbatim source excerpt:

```json
{
  "source": "example.md",
  "chunkIndex": 6,
  "section": "Product characteristics",
  "text": "Product name: Flexi Rewards"
}
```

`chunkIndex` must exist. `source` and optional `section` must match that chunk. `text` must occur in the chunk content; do not paraphrase evidence.

## Node and PropertyEntry

A node has `tempId`, `className`, `properties`, node-level `evidence`, and `confidence`.

Each property carries evidence specific to that fact:

```json
{
  "propertyName": "pskg:productCode",
  "value": "CC-FLEXI-001",
  "evidence": [{
    "source": "example.md",
    "chunkIndex": 1,
    "section": "Product information",
    "text": "Product code: CC-FLEXI-001"
  }]
}
```

Node evidence is not a substitute for property evidence. Array value order is preserved. Exact duplicate property/value pairs are deduplicated; conflicting values return `DUPLICATE_PROPERTY`.

## Edge

- `edgeName`: exact ontology technical name.
- `sourceTempId` / `targetTempId`: existing nodes in this draft.
- `evidence`: exact source excerpt supporting the relationship.
- `confidence`: 0 through 1.

## Coverage

Every prepared chunk appears exactly once:

```json
{"chunkIndex": 9, "decision": "MAPPED", "reason": "Credit-limit rules"}
```

Use `NOT_RELEVANT` only when the chunk truly contains no ontology-relevant business fact:

```json
{"chunkIndex": 10, "decision": "NOT_RELEVANT", "reason": "Formatting-only boilerplate"}
```

A `MAPPED` chunk must be referenced by node/property/edge evidence. A `NOT_RELEVANT` chunk must not be used as evidence.

## Compiler behavior

The compiler converts property entries to the internal map, preserves property-level evidence and coverage, rejects duplicate temp IDs and dangling references, and derives target `pskg:ruleType` for:

- `pskg:hasEligibilityRule` -> `ELIGIBILITY`
- `pskg:hasSalesConditionRule` -> `SALES_CONDITION`
- `pskg:governedByPolicy` -> `POLICY`

A contradictory supplied value returns `SEMANTIC_CONFLICT`.

The compiler never guesses locale-sensitive dates. Map a source date to ISO only when its meaning is unambiguous.

The canonical fingerprint includes:

- compiled node/property values;
- node/property/edge evidence including `chunkIndex`;
- coverage decisions;
- artifact content digest;
- ontology file digest;
- compiler schema version.

Changing any of these invalidates the validation gate.