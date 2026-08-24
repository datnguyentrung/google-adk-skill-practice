# GraphPatchDraft contract

`GraphPatchDraft` is model-visible. `CompiledGraphPatch` is internal and must
never be produced or requested by the model.

## Top level

- `nodes`: array of extracted nodes.
- `edges`: array of extracted edges.
- `warnings`: array of plain-language source uncertainties.
- Extra fields are rejected everywhere.

## Node

- `tempId`: non-empty unique identifier within the draft.
- `className`: ontology technical name in `prefix:localName` form.
- `properties`: array of `PropertyEntry` objects.
- `evidence`: non-empty array.
- `confidence`: number from 0 through 1.

## PropertyEntry

```json
{"propertyName": "pskg:productCode", "value": "CC-FLEXI-001"}
```

`propertyName` must be a technical name. `value` may be a JSON scalar, object,
or array allowed by the ontology datatype. Array order is preserved because it
may be meaningful.

An exact duplicate entry is harmless and is deduplicated. Two entries with the
same name and different values are ambiguous and return `DUPLICATE_PROPERTY`.

## Edge

- `edgeName`: ontology technical name.
- `sourceTempId` and `targetTempId`: existing node IDs in the same draft.
- `evidence`: non-empty array supporting the relationship.
- `confidence`: number from 0 through 1.

## Evidence

- `source`: non-empty source identifier, normally the artifact name.
- `section`: optional section or heading.
- `text`: non-empty source-grounded excerpt or concise supporting statement.

Evidence is not a place to add unsupported facts.

## Compiler behavior

The compiler converts the property array into an internal map, rejects
duplicate temp IDs and dangling references, and derives the target
`pskg:ruleType` for these edges:

- `pskg:hasEligibilityRule` -> `ELIGIBILITY`
- `pskg:hasSalesConditionRule` -> `SALES_CONDITION`
- `pskg:governedByPolicy` -> `POLICY`

A contradictory supplied value returns `SEMANTIC_CONFLICT`.

The compiler never parses `01/08/2026` or another locale-sensitive date. Map a
source date to ISO only when its meaning is clear from the document context.
