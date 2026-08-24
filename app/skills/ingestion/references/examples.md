# Ingestion correction examples

## Property shape

Wrong:

```json
"properties": {"pskg:productCode": "CC-FLEXI-001"}
```

Correct:

```json
"properties": [
  {"propertyName": "pskg:productCode", "value": "CC-FLEXI-001"}
]
```

## Technical names

Wrong: `pskg`, `productCode`, or `pskg:`.

Correct: `pskg:BankingProduct`, `pskg:productCode`, and
`pskg:hasEligibilityRule`.

## Date mapping

If a Vietnamese source clearly states an effective date `01/08/2026`, emit
`2026-08-01`. If the locale or meaning is ambiguous, warn and do not guess.

## Missing status

If the document supports product code and effective date but never states a
status, omit status. Expected validation shape:

```json
{
  "validForExtraction": true,
  "validForPersistence": false,
  "errors": [],
  "readinessIssues": [
    {"code": "ONTOLOGY_RULE_UNSATISFIED"}
  ]
}
```

Do not add `Published` to make fill possible.

## Correction and revalidation

If validation reports `PROPERTY_DATATYPE_MISMATCH` for an emitted date:

1. Re-read the date and locale from the source.
2. Correct it to ISO only if unambiguous.
3. Submit the entire corrected draft to validation.
4. Fill only after both validation flags return true.
