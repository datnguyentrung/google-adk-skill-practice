---
name: campaign-targeting
description: >
  Use this skill whenever an ingestion batch contains campaigns, customer segments,
  customer needs, campaign targeting, campaign benefits, or campaign validity.
  Always invoke before extracting campaign facts so load_campaign_targeting_schema is called first.
metadata:
  adk_additional_tools:
    - load_campaign_targeting_schema
---

# Campaign Targeting Schema Skill

Use this skill when the current ingestion batch contains facts about:

- campaigns
- campaign names, objectives, status, budget, or validity periods
- customer segments
- customer needs
- campaigns targeting segments or needs
- offers or products participating in campaigns
- campaign-specific benefits, rules, or scripts

## Workflow

1. Call `load_campaign_targeting_schema` before extracting graph facts from the batch.
2. Use only ontology elements returned by the tool.
3. Extract only facts explicitly supported by the current source batch.
4. If product, business-rule, or sales-script facts are also present, load the corresponding skill as needed.
5. Do not infer unsupported campaign membership, targeting, or relationships.
