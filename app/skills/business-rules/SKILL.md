---
name: business-rules
description: >
  Use this skill whenever the ingestion batch contains eligibility criteria,
  policy rules, sales conditions, qualification requirements, required
  documents, document types, document validity, or rule priorities that must
  be mapped to the Product Sales Knowledge Graph. Always invoke before
  extracting business-rule or required-document facts so that
  load_business_rules_schema is called first and the correct ontology
  contract (BusinessRule, RequiredDocument classes, properties, edges) is
  loaded into context.
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
5. Do not create rule relationships unless both the source evidence and ontology schema support them.
