from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class CommitStatus(StrEnum):
    COMMITTED = "committed"


class FillStatus(StrEnum):
    SUCCESS = "success"
    READBACK_MISMATCH = "readback_mismatch"


class _PersistenceModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )


class PersistedNode(_PersistenceModel):
    node_id: str = Field(alias="nodeId")
    labels: list[str]
    properties: dict


class PersistedRelationship(_PersistenceModel):
    relationship_id: str = Field(alias="relationshipId")
    type: str
    source_node_id: str = Field(alias="sourceNodeId")
    target_node_id: str = Field(alias="targetNodeId")
    properties: dict


class PersistedGraphReadback(_PersistenceModel):
    nodes: list[PersistedNode]
    relationships: list[PersistedRelationship]


class GraphWriteResult(_PersistenceModel):
    node_ids: dict[str, str] = Field(alias="nodeIds")
    relationship_ids: dict[str, str] = Field(alias="relationshipIds")
    expected_nodes: dict[str, PersistedNode] = Field(
        default_factory=dict,
        alias="expectedNodes",
    )
    expected_relationships: dict[str, PersistedRelationship] = Field(
        default_factory=dict,
        alias="expectedRelationships",
    )


class PersistedGraphReceipt(_PersistenceModel):
    version: Literal["1"] = "1"
    commit_status: CommitStatus = Field(
        default=CommitStatus.COMMITTED,
        alias="commitStatus",
    )
    verified: bool
    expected_node_count: int = Field(alias="expectedNodeCount", ge=0)
    expected_relationship_count: int = Field(
        alias="expectedRelationshipCount",
        ge=0,
    )
    node_ids: dict[str, str] = Field(alias="nodeIds")
    relationship_ids: dict[str, str] = Field(alias="relationshipIds")
    nodes: list[PersistedNode]
    relationships: list[PersistedRelationship]
    label_distribution: dict[str, int] = Field(alias="labelDistribution")
    relationship_type_distribution: dict[str, int] = Field(
        alias="relationshipTypeDistribution"
    )
    mismatches: list[str]


class FillResult(_PersistenceModel):
    status: FillStatus
    commit_status: CommitStatus = Field(alias="commitStatus")
    nodes: int = Field(ge=0)
    edges: int = Field(ge=0)
    node_ids: dict[str, str] = Field(alias="nodeIds")
    relationship_ids: dict[str, str] = Field(alias="relationshipIds")
    receipt: PersistedGraphReceipt
    partial_persistence: bool = Field(
        default=False,
        alias="partialPersistence",
    )
    persistence_mode: Literal["strict", "partial"] = Field(
        default="strict",
        alias="persistenceMode",
    )
    readiness_issues_ignored: list[dict[str, Any]] = Field(
        default_factory=list,
        alias="readinessIssuesIgnored",
    )


__all__ = [
    "CommitStatus",
    "FillResult",
    "FillStatus",
    "GraphWriteResult",
    "PersistedGraphReadback",
    "PersistedGraphReceipt",
    "PersistedNode",
    "PersistedRelationship",
]
