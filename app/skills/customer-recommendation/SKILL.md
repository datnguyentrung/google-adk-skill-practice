---
name: customer-recommendation
description: >
  Load the Product Sales Knowledge Graph schema for customer context,
  customer segmentation, customer needs, current product usage, product
  recommendation, and related recommendation facts during ingestion extraction.
metadata:
  adk_additional_tools:
    - load_customer_recommendation_schema
---

# Customer Recommendation Schema Skill

Use this skill when the current ingestion batch contains facts about:

- customers
- customer identifiers or behavior context
- customer segments
- customer needs
- products currently used by a customer
- recommended products
- product matching or recommendation context

## Workflow

1. Call `load_customer_recommendation_schema` before extracting graph facts from the batch.
2. Use the returned schema as the only ontology contract for this domain.
3. Extract customer-related facts only when they are grounded in the source batch.
4. Load `product-catalog` or `business-rules` as additional skills when product details or eligibility conditions are also present.
5. Do not invent customer attributes, inferred preferences, segments, needs, or recommendations.
