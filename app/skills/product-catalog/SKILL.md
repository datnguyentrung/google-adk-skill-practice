---
name: product-catalog
description: >
  Use this skill whenever an ingestion batch contains banking products, product
  offers, product bundles, product codes, prices, fees, benefits, product attributes,
  or product relationships (cross-sell, upsell, substitution, exclusion). Always
  invoke before extracting product catalog facts so load_product_catalog_schema is called first.
metadata:
  adk_additional_tools:
    - load_product_catalog_schema
---

# Product Catalog Schema Skill

Use this skill when the current ingestion batch contains facts about:

- banking products
- product names, codes, versions, status, or effective dates
- prices, fees, benefits, or product attributes
- product offers or product bundles
- product categories or classifications
- cross-sell, upsell, substitution, complement, or exclusion relationships

## Workflow

1. Call `load_product_catalog_schema` before extracting graph facts from the batch.
2. Use the returned ontology classes, properties, edges, and rules as the source of truth.
3. Extract only facts supported by both the source batch and the loaded schema.
4. If the batch also contains facts from another ontology domain, load the corresponding skill as well.
5. Do not invent classes, properties, edges, rules, or enum-like values that are not present in the loaded schema.
