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

Lifecycle/status attributes marked `ingestionPolicy.mode=runtime_managed` are not required from source documents. The compiler supplies the configured safe runtime value (for example `Draft`). If a source-emitted literal is unsupported, it is still an extraction grounding error; do not fabricate `Published`.

`IDENTITY_UNRESOLVED` is emitted only after every permitted local identity
strategy fails. Natural identity takes precedence; source-scoped deterministic
identity may be used where policy allows.

## Invocation gate

Strict validation stores a persistence gate fingerprint when both flags are true. The fingerprint binds the compiled patch, evidence and coverage, raw artifact digest, exact ontology bytes, and compiler schema version.

For an explicit user-requested partial persistence commit, `allow_partial_persistence=true` may proceed past readiness only after `validForExtraction=true`. The finalized patch is fingerprinted and reassessed against the same source chunks before write. Extraction, grounding, schema, domain/range, dangling-reference, and semantic errors remain blocking. Partial commits return `partialPersistence=true`, `persistenceMode=partial`, and the ignored readiness issues.

Fill writes the compiled nodes/edges in one Neo4j transaction and verifies readback. Missing/mismatched state returns `VALIDATION_PRECONDITION`.

`NEO4J_WRITE_FAILED` is a persistence failure and never authorizes changing
business facts.
