---
name: business-rules
description: >
  Use this skill whenever an ingestion batch contains eligibility criteria,
  policy rules, sales conditions, qualification requirements, required documents,
  document validity, or rule priorities. Always invoke before extracting business-rule
  facts so load_business_rules_schema is called first.
metadata:
  adk_additional_tools:
    - load_business_rules_schema
---

# Business Rules Schema Skill

Use this skill when the current ingestion batch contains facts about:

- eligibility criteria
- policy or governance conditions
- sales conditions
- qualification requirements
- rule priority or applicability
- required documents
- document type, copy count, validity, or document conditions
- products, offers, campaigns, or documents governed by rules

## Workflow

1. Call `load_business_rules_schema` before extracting graph facts from the batch.
2. Use only classes, properties, edges, and rules returned by the tool.
3. Preserve the source meaning of conditions and constraints; do not strengthen or weaken them.
4. Load another schema skill if the rule connects to product, campaign, customer, or sales content outside this domain.
5. When a newly extracted rule is explicitly stated to govern a product, offer, or campaign, emit the corresponding supported relationship even if that subject already exists in `canonicalGraphContext`; do not leave the rule orphan solely because its subject was staged in an earlier batch.
6. Do not create rule relationships unless both the source evidence and ontology schema support them.
