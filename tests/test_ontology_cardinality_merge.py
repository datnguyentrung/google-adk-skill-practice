import json
import pytest
from app.services.ingestion.ontology.loader import OntologyLoader
from app.services.ingestion.ontology.registry import OntologyRegistry
from app.services.ingestion.incremental.staging_store import IngestionStagingStore


def test_ontology_cardinality_registry_api():
    ontology = OntologyLoader.load(
        "app/data/ontology/product_sales_knowledge_graph_base_v3_1.ontology.json"
    )
    registry = OntologyRegistry(ontology)

    # SINGLE properties (exactlyQualified 1)
    assert registry.property_max_occurrences("pskg:BankingProduct", "pskg:productCode") == 1
    assert registry.property_allows_multiple_values("pskg:BankingProduct", "pskg:productCode") is False

    assert registry.property_max_occurrences("pskg:BankingProduct", "pskg:bankingProductStatus") == 1
    assert registry.property_allows_multiple_values("pskg:BankingProduct", "pskg:bankingProductStatus") is False

    # MULTI properties (minQualified 0, no max=1)
    assert registry.property_max_occurrences("pskg:BankingProduct", "pskg:productAttributes") is None
    assert registry.property_allows_multiple_values("pskg:BankingProduct", "pskg:productAttributes") is True


def test_staging_store_single_value_merge(monkeypatch):
    """
    Test SINGLE property merge:
    A + A -> A, no conflict
    A + B -> conflict
    """
    store = IngestionStagingStore()
    ingestion_id = "test_single_merge"

    # Batch 0: A
    res0 = store.stage_batch_facts(
        ingestion_id=ingestion_id,
        source_version_id="v1",
        batch_index=0,
        entities=[{"entityKey": "pskg:BankingProduct|pskg:productCode|p1", "className": "pskg:BankingProduct", "confidence": 1.0, "tempId": "n1"}],
        properties=[{
            "entityKey": "pskg:BankingProduct|pskg:productCode|p1",
            "className": "pskg:BankingProduct",
            "propertyName": "pskg:productCode",
            "valueJson": json.dumps("CODE_A"),
            "valueHash": "hash_a",
            "isList": False,
            "evidence": []
        }],
        edges=[],
        coverage=[],
    )
    assert res0["conflicts"] == 0

    # Batch 1: A (same value) -> A, no conflict
    res1 = store.stage_batch_facts(
        ingestion_id=ingestion_id,
        source_version_id="v1",
        batch_index=1,
        entities=[{"entityKey": "pskg:BankingProduct|pskg:productCode|p1", "className": "pskg:BankingProduct", "confidence": 1.0, "tempId": "n1"}],
        properties=[{
            "entityKey": "pskg:BankingProduct|pskg:productCode|p1",
            "className": "pskg:BankingProduct",
            "propertyName": "pskg:productCode",
            "valueJson": json.dumps("CODE_A"),
            "valueHash": "hash_a",
            "isList": False,
            "evidence": []
        }],
        edges=[],
        coverage=[],
    )
    assert res1["conflicts"] == 0

    # Batch 2: B (different value for single-value property) -> conflict
    res2 = store.stage_batch_facts(
        ingestion_id=ingestion_id,
        source_version_id="v1",
        batch_index=2,
        entities=[{"entityKey": "pskg:BankingProduct|pskg:productCode|p1", "className": "pskg:BankingProduct", "confidence": 1.0, "tempId": "n1"}],
        properties=[{
            "entityKey": "pskg:BankingProduct|pskg:productCode|p1",
            "className": "pskg:BankingProduct",
            "propertyName": "pskg:productCode",
            "valueJson": json.dumps("CODE_B"),
            "valueHash": "hash_b",
            "isList": False,
            "evidence": []
        }],
        edges=[],
        coverage=[],
    )
    assert res2["conflicts"] == 1


def test_staging_store_multi_value_merge():
    """
    Test MULTI property merge:
    A + B -> [A, B]
    [A, B] + C -> [A, B, C]
    [A, B] + B -> [A, B]
    A + [B, C] -> [A, B, C]
    """
    store = IngestionStagingStore()
    ingestion_id = "test_multi_merge"

    # 1. A + B -> [A, B]
    store.stage_batch_facts(
        ingestion_id=ingestion_id,
        source_version_id="v1",
        batch_index=0,
        entities=[{"entityKey": "pskg:BankingProduct|pskg:productCode|p2", "className": "pskg:BankingProduct", "confidence": 1.0, "tempId": "n2"}],
        properties=[{
            "entityKey": "pskg:BankingProduct|pskg:productCode|p2",
            "className": "pskg:BankingProduct",
            "propertyName": "pskg:productAttributes",
            "valueJson": json.dumps("A"),
            "valueHash": "hash_a",
            "isList": False,
            "evidence": []
        }],
        edges=[],
        coverage=[],
    )

    res1 = store.stage_batch_facts(
        ingestion_id=ingestion_id,
        source_version_id="v1",
        batch_index=1,
        entities=[{"entityKey": "pskg:BankingProduct|pskg:productCode|p2", "className": "pskg:BankingProduct", "confidence": 1.0, "tempId": "n2"}],
        properties=[{
            "entityKey": "pskg:BankingProduct|pskg:productCode|p2",
            "className": "pskg:BankingProduct",
            "propertyName": "pskg:productAttributes",
            "valueJson": json.dumps("B"),
            "valueHash": "hash_b",
            "isList": False,
            "evidence": []
        }],
        edges=[],
        coverage=[],
    )
    assert res1["conflicts"] == 0

    # 2. [A, B] + C -> [A, B, C]
    res2 = store.stage_batch_facts(
        ingestion_id=ingestion_id,
        source_version_id="v1",
        batch_index=2,
        entities=[{"entityKey": "pskg:BankingProduct|pskg:productCode|p2", "className": "pskg:BankingProduct", "confidence": 1.0, "tempId": "n2"}],
        properties=[{
            "entityKey": "pskg:BankingProduct|pskg:productCode|p2",
            "className": "pskg:BankingProduct",
            "propertyName": "pskg:productAttributes",
            "valueJson": json.dumps("C"),
            "valueHash": "hash_c",
            "isList": False,
            "evidence": []
        }],
        edges=[],
        coverage=[],
    )
    assert res2["conflicts"] == 0

    # 3. [A, B, C] + B -> [A, B, C] (deduplicated)
    res3 = store.stage_batch_facts(
        ingestion_id=ingestion_id,
        source_version_id="v1",
        batch_index=3,
        entities=[{"entityKey": "pskg:BankingProduct|pskg:productCode|p2", "className": "pskg:BankingProduct", "confidence": 1.0, "tempId": "n2"}],
        properties=[{
            "entityKey": "pskg:BankingProduct|pskg:productCode|p2",
            "className": "pskg:BankingProduct",
            "propertyName": "pskg:productAttributes",
            "valueJson": json.dumps("B"),
            "valueHash": "hash_b",
            "isList": False,
            "evidence": []
        }],
        edges=[],
        coverage=[],
    )
    assert res3["conflicts"] == 0

    # 4. Single list merge: A + [B, C] -> [A, B, C]
    ingestion_id_list = "test_multi_merge_list"
    store.stage_batch_facts(
        ingestion_id=ingestion_id_list,
        source_version_id="v1",
        batch_index=0,
        entities=[{"entityKey": "pskg:BankingProduct|pskg:productCode|p3", "className": "pskg:BankingProduct", "confidence": 1.0, "tempId": "n3"}],
        properties=[{
            "entityKey": "pskg:BankingProduct|pskg:productCode|p3",
            "className": "pskg:BankingProduct",
            "propertyName": "pskg:productAttributes",
            "valueJson": json.dumps("A"),
            "valueHash": "hash_a",
            "isList": False,
            "evidence": []
        }],
        edges=[],
        coverage=[],
    )
    res4 = store.stage_batch_facts(
        ingestion_id=ingestion_id_list,
        source_version_id="v1",
        batch_index=1,
        entities=[{"entityKey": "pskg:BankingProduct|pskg:productCode|p3", "className": "pskg:BankingProduct", "confidence": 1.0, "tempId": "n3"}],
        properties=[{
            "entityKey": "pskg:BankingProduct|pskg:productCode|p3",
            "className": "pskg:BankingProduct",
            "propertyName": "pskg:productAttributes",
            "valueJson": json.dumps(["B", "C"]),
            "valueHash": "hash_bc",
            "isList": True,
            "evidence": []
        }],
        edges=[],
        coverage=[],
    )
    assert res4["conflicts"] == 0


def test_real_world_case_product_attributes_conflict_resolution():
    """
    Test real-world bug case:
    BankingProduct pskg:productAttributes

    batch 0: "Currency: VND, Minimum Deposit..., Tenors..."
    batch 1: "Maturity Options..., Fees..., Early withdrawal..."

    Expected:
    - no IngestionConflict
    - isList = true
    - both values preserved
    """
    store = IngestionStagingStore()
    ingestion_id = "test_real_product_attributes_merge"

    val_batch0 = "Currency: VND, Minimum Deposit Amount: 1,000,000 VND per deposit, Eligible Customer Type: Individual customers, Product Type: Term deposit, Tenors: 1, 3, 6, 9, 12 months"
    val_batch1 = "Maturity Options: Option 1 - Pay Out Both Principal and Interest, Option 2 - Renew Principal Only, Option 3 - Renew Both Principal and Interest. Fees: No fees for opening, maintaining, withdrawal at maturity, or early withdrawal through the application. Early withdrawal recalculates interest using the non-term interest rate."

    entity_key = "pskg:BankingProduct|pskg:productCode|td-online-001"

    # Batch 0
    res0 = store.stage_batch_facts(
        ingestion_id=ingestion_id,
        source_version_id="v1",
        batch_index=0,
        entities=[{"entityKey": entity_key, "className": "pskg:BankingProduct", "confidence": 1.0, "tempId": "n1"}],
        properties=[{
            "entityKey": entity_key,
            "className": "pskg:BankingProduct",
            "propertyName": "pskg:productAttributes",
            "valueJson": json.dumps(val_batch0),
            "valueHash": "h0",
            "isList": False,
            "evidence": []
        }],
        edges=[],
        coverage=[],
    )
    assert res0["conflicts"] == 0

    # Batch 1
    res1 = store.stage_batch_facts(
        ingestion_id=ingestion_id,
        source_version_id="v1",
        batch_index=1,
        entities=[{"entityKey": entity_key, "className": "pskg:BankingProduct", "confidence": 1.0, "tempId": "n1"}],
        properties=[{
            "entityKey": entity_key,
            "className": "pskg:BankingProduct",
            "propertyName": "pskg:productAttributes",
            "valueJson": json.dumps(val_batch1),
            "valueHash": "h1",
            "isList": False,
            "evidence": []
        }],
        edges=[],
        coverage=[],
    )
    assert res1["conflicts"] == 0

    # Verify staging summary and Neo4j node content
    summary = store.get_staging_summary(ingestion_id)
    assert summary["conflictCount"] == 0

    driver = store.client.get_driver()
    with driver.session(database=store.client.database_name) as session:
        record = session.run(
            """
            MATCH (p:IngestionStagedProperty {ingestionId: $ingestion_id, entityKey: $entity_key, propertyName: 'pskg:productAttributes'})
            RETURN p.valueJson AS val, p.isList AS isList
            """,
            ingestion_id=ingestion_id,
            entity_key=entity_key,
        ).single()

        assert record is not None
        assert record["isList"] is True
        staged_list = json.loads(record["val"])
        assert isinstance(staged_list, list)
        assert len(staged_list) == 2
        assert val_batch0 in staged_list
        assert val_batch1 in staged_list
