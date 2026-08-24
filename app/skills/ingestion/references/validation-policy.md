# Validation and persistence policy

Validation returns two independent decisions.

## Extraction validation

`validForExtraction` covers whether the emitted facts are structurally and
ontologically valid: schema, technical names, known ontology terms,
domain/range, datatypes, references, evidence, duplicates, and deterministic
semantic conflicts.

Ontology cardinality does not authorize invented facts. A source-grounded patch
can pass extraction while lacking data required for persistence.

## Persistence readiness

`validForPersistence` is evaluated only after extraction passes. It applies all
ontology cardinality, required value, and required relationship rules, plus
static identity preflight.

Missing status `Published`, required effective metadata, or required
relationships are readiness issues. Preserve the valid source extraction and
report the missing authority.

`IDENTITY_UNRESOLVED` is emitted only after the resolver tries every permitted
local policy. Natural identity takes precedence. Classes without a natural key
may use deterministic `_ingestionKey` scoped by evidence source.

## Gate and failures

Validation stores a fingerprint only when both decisions pass. The fingerprint
binds the compiled patch, raw artifact content digest, exact ontology bytes,
and compiler schema version. It exists only for the current invocation.

Fill recompiles the current draft and compares the fingerprint before creating
the Neo4j service. Missing or mismatched state returns
`VALIDATION_PRECONDITION`. Fill then validates defensively and writes all nodes
and edges in one transaction.

A `NEO4J_WRITE_FAILED` result is a persistence failure, not permission to alter
the extracted business facts.
