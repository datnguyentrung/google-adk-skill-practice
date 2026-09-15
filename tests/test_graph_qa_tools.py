import pytest
from neo4j import RoutingControl

from app.tools.graph_qa import cypher_query


class FakeRecord:
    def __init__(self, data):
        self._data = data

    def data(self):
        return dict(self._data)


class FakeDriver:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def execute_query(self, cypher, *, parameters_=None, parameters=None, database_, routing_):
        params = parameters_ if parameters_ is not None else parameters
        self.calls.append(
            {
                "cypher": cypher,
                "parameters": params,
                "database": database_,
                "routing": routing_,
            }
        )
        response = self.responses.pop(0)
        records = [FakeRecord(item) for item in response["records"]]
        return records, object(), response["keys"]


class FakeEmbeddingProvider:
    def embed(self, text):
        if "online" in text.lower() or "OnlineCard" in text:
            return [1.0, 0.0]
        if "saving" in text.lower() or "SavingAccount" in text:
            return [0.0, 1.0]
        return [0.5, 0.5]


def test_validate_read_only_cypher_allows_match_return():
    cypher_query.validate_read_only_cypher(
        "MATCH (p:Product) WHERE p.name = $name RETURN p"
    )


@pytest.mark.parametrize(
    "cypher",
    [
        "MATCH (n) SET n.name = 'x' RETURN n",
        "MATCH (n) DETACH DELETE n",
        "MATCH (n) RETURN n; MATCH (m) RETURN m",
    ],
)
def test_validate_read_only_cypher_blocks_writes_and_multiple_statements(cypher):
    with pytest.raises(ValueError):
        cypher_query.validate_read_only_cypher(cypher)


def test_execute_read_cypher_uses_read_routing(monkeypatch):
    driver = FakeDriver(
        [
            {
                "records": [{"name": "Flexi Rewards"}],
                "keys": ["name"],
            }
        ]
    )
    monkeypatch.setattr(cypher_query, "get_driver", lambda: driver)
    monkeypatch.setattr(cypher_query.Neo4jClient, "database_name", "neo4j")

    result = cypher_query.execute_read_cypher(
        "MATCH (p:Product {name: $name}) RETURN p.name AS name",
        {"name": "Flexi Rewards"},
    )

    assert result == {
        "records": [{"name": "Flexi Rewards"}],
        "columns": ["name"],
        "count": 1,
        "strategy": "cypher",
    }
    assert driver.calls[0]["parameters"] == {"name": "Flexi Rewards"}
    assert driver.calls[0]["database"] == "neo4j"
    assert driver.calls[0]["routing"] == RoutingControl.READ


def test_vector_search_ranks_candidates_and_filters_labels(monkeypatch):
    driver = FakeDriver(
        [
            {
                "records": [
                    {
                        "node_id": "1",
                        "labels": ["Product"],
                        "properties": {"name": "OnlineCard", "benefit": "online shopping"},
                    },
                    {
                        "node_id": "2",
                        "labels": ["Product"],
                        "properties": {"name": "SavingAccount", "benefit": "saving"},
                    },
                ],
                "keys": ["node_id", "labels", "properties"],
            }
        ]
    )
    monkeypatch.setattr(cypher_query, "get_driver", lambda: driver)
    monkeypatch.setattr(cypher_query.Neo4jClient, "database_name", "neo4j")
    monkeypatch.setattr(
        cypher_query, "_embedding_provider", lambda: FakeEmbeddingProvider()
    )

    result = cypher_query.vector_search(
        "Tôi hay mua sắm online",
        labels=["Product"],
        top_k=1,
        candidate_limit=10,
    )

    assert result["strategy"] == "vector"
    assert result["count"] == 1
    assert result["records"][0]["node_id"] == "1"
    assert "WHERE any(label IN labels(n) WHERE label IN $labels)" in driver.calls[0][
        "cypher"
    ]
    assert driver.calls[0]["parameters"]["labels"] == ["Product"]


def test_hybrid_search_expands_candidate_ids_with_default_cypher(monkeypatch):
    driver = FakeDriver(
        [
            {
                "records": [
                    {
                        "node_id": "1",
                        "labels": ["Product"],
                        "properties": {"name": "OnlineCard"},
                    }
                ],
                "keys": ["node_id", "labels", "properties"],
            },
            {
                "records": [
                    {
                        "candidate_id": "1",
                        "candidate_properties": {"name": "OnlineCard"},
                        "outgoing_relationships": [],
                        "incoming_relationships": [],
                    }
                ],
                "keys": [
                    "candidate_id",
                    "candidate_properties",
                    "outgoing_relationships",
                    "incoming_relationships",
                ],
            },
        ]
    )
    monkeypatch.setattr(cypher_query, "get_driver", lambda: driver)
    monkeypatch.setattr(cypher_query.Neo4jClient, "database_name", "neo4j")
    monkeypatch.setattr(
        cypher_query, "_embedding_provider", lambda: FakeEmbeddingProvider()
    )

    result = cypher_query.hybrid_search("online card", labels=["Product"])

    assert result["strategy"] == "hybrid"
    assert result["semantic_hits"][0]["node_id"] == "1"
    assert result["graph_records"][0]["candidate_id"] == "1"
    assert driver.calls[1]["parameters"]["candidate_ids"] == ["1"]
    assert "OPTIONAL MATCH (candidate)-[outgoing]->(target)" in driver.calls[1][
        "cypher"
    ]


def test_hybrid_search_blocks_write_expansion_cypher(monkeypatch):
    monkeypatch.setattr(
        cypher_query,
        "vector_search",
        lambda **_: {
            "records": [{"node_id": "1"}],
            "count": 1,
            "strategy": "vector",
            "labels": [],
        },
    )

    with pytest.raises(ValueError):
        cypher_query.hybrid_search(
            "online card",
            expansion_cypher="MATCH (n) SET n.name = 'bad' RETURN n",
        )
