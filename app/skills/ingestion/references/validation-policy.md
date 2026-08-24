# Validation and persistence policy

Validation returns two decisions: extraction correctness/completeness and
persistence readiness.

## Extraction validation

`validForExtraction` requires all of the following:

- schema and complete technical names are valid;
- emitted classes/properties/edges exist in the ontology;
- property domain/datatype and edge domain/range are valid;
- duplicates, dangling references, and deterministic semantic conflicts are absent;
- every prepared chunk has exactly one coverage decision;
- every `MAPPED` chunk is actually referenced by evidence;
- `NOT_RELEVANT` chunks are not used as evidence;
- evidence source/chunk/section/text matches the prepared source;
- literal-sensitive property values (codes, statuses, versions, dates) are
  supported by their cited source chunks.

This prevents a structurally valid three-node patch from being accepted as a
complete extraction when most of a long document was never considered.

## Persistence readiness

`validForPersistence` is evaluated only after extraction passes. It applies all
ontology cardinality/value/relationship rules plus identity preflight. Prepared
source context is also required before a patch can be authorized for write.

A missing ontology-required `Published` status is a readiness issue when the
source never states it. Adding `Published` without supporting source is instead
an extraction grounding error (`PROPERTY_VALUE_NOT_GROUNDED`).

`IDENTITY_UNRESOLVED` is emitted only after every permitted local identity
strategy fails. Natural identity takes precedence; source-scoped deterministic
identity may be used where policy allows.

## Invocation gate

Validation stores a fingerprint only when both flags are true. The fingerprint
binds the compiled patch, evidence and coverage, raw artifact digest, exact
ontology bytes, and compiler schema version. It exists only for the current
invocation.

Fill recompiles the current draft, compares the fingerprint, defensively
reassesses it against the same prepared source chunks, then writes all nodes and
edges in one Neo4j transaction. Missing/mismatched state returns
`VALIDATION_PRECONDITION`.

`NEO4J_WRITE_FAILED` is a persistence failure and never authorizes changing
business facts.
