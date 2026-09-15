---
name: sales-enablement
description: >
  Use this skill whenever an ingestion batch contains sales scripts, sales scenarios,
  objection handling, sales guidance, FAQs, articles, or sales knowledge facts.
  Always invoke before extracting sales enablement facts so load_sales_enablement_schema is called first.
metadata:
  adk_additional_tools:
    - load_sales_enablement_schema
---

# Sales Enablement Schema Skill

Use this skill when the current ingestion batch contains facts about:

- sales scripts
- sales scenarios
- opening lines, objection handling, or closing lines
- sales skills or selling guidance
- sales knowledge
- FAQs, articles, documents, guides, or playbooks
- links between products, offers, campaigns, scripts, skills, or knowledge

## Workflow

1. Call `load_sales_enablement_schema` before extracting graph facts from the batch.
2. Use the returned ontology schema as the source of truth.
3. Keep extracted script and knowledge content grounded in the source text.
4. Load `product-catalog`, `campaign-targeting`, or another relevant skill when cross-domain relationships are present.
5. Do not invent scripts, advice, knowledge content, or relationships not supported by the source.
