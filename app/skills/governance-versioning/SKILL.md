---
name: governance-versioning
description: >
  Use this skill whenever an ingestion batch contains version records, version numbers,
  creation time, change history, published versions, approval tasks, or publication lifecycle info.
  Always invoke before extracting governance facts so load_governance_versioning_schema is called first.
metadata:
  adk_additional_tools:
    - load_governance_versioning_schema
---

# Governance Versioning Schema Skill

Use this skill when the current ingestion batch contains facts about:

- versions
- version numbers
- creation time or creator
- change descriptions or change history
- published versions
- approval tasks
- approval chains, approvers, steps, comments, or approval history
- governance or publication lifecycle information

## Workflow

1. Call `load_governance_versioning_schema` before extracting graph facts from the batch.
2. Use only ontology elements returned by the tool.
3. Extract version and approval facts only when explicitly supported by source evidence.
4. Load another schema skill when governance facts refer to domain objects whose schema is also required.
5. Do not infer approval, publication, version, or rollback state that is not present in the source.
