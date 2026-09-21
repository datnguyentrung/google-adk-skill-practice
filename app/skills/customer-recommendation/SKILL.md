---
name: customer-recommendation
description: >
  Use this skill whenever an ingestion batch contains customer context, customer segmentation,
  customer needs, current product usage, or product recommendations. Always invoke
  before extracting customer recommendation facts so load_customer_recommendation_schema is called first.
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
5. When the source explicitly says that a product/offer addresses a need or targets a segment, emit the supported relationship even if the product/offer is already present in `canonicalGraphContext`.
6. Do not invent customer attributes, inferred preferences, segments, needs, or recommendations.
