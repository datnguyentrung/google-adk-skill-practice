from app.core.schemas.ingestion.graph_patch import GraphPatchFragment
from app.core.schemas.ingestion.validation import ValidationIssue
from app.core.schemas.ingestion.workspace import IngestionWorkspace
from app.services.ingestion.validate_graph_patch import GraphPatchValidationService

BUSINESS_RULE_CLASS = "pskg:BusinessRule"
RULE_TYPE_PROPERTY = "pskg:ruleType"


def fragments_with_replacement(
    workspace: IngestionWorkspace,
    batch_index: int,
    fragment: GraphPatchFragment,
) -> list[GraphPatchFragment]:
    fragments: list[GraphPatchFragment] = []
    for batch in workspace.batches:
        if batch.index == batch_index:
            fragments.append(fragment)
        elif batch.fragment is not None:
            fragments.append(batch.fragment)
    return fragments


def business_rule_edge_issues(
    fragment: GraphPatchFragment,
    validation_service: GraphPatchValidationService,
    *,
    existing_fragments: list[GraphPatchFragment] | None = None,
) -> list[ValidationIssue]:
    """Catch rule nodes that cannot receive compiler-derived pskg:ruleType."""
    rule_type_edges = _rule_type_edge_names(validation_service)
    if not rule_type_edges:
        return []

    context_edges = [
        edge
        for item in (existing_fragments or [])
        for edge in item.edges
        if edge.edge_name in rule_type_edges
    ]
    context_edges.extend(
        edge for edge in fragment.edges if edge.edge_name in rule_type_edges
    )
    derived_targets = {edge.target_temp_id for edge in context_edges}

    issues: list[ValidationIssue] = []
    for node_index, node in enumerate(fragment.nodes):
        if node.class_name != BUSINESS_RULE_CLASS:
            continue
        if node.temp_id in derived_targets:
            continue
        issues.append(
            ValidationIssue(
                code="DERIVED_PROPERTY_REQUIRES_EDGE_EVIDENCE",
                message=(
                    f"{BUSINESS_RULE_CLASS} node {node.temp_id} requires an "
                    f"incoming relationship edge that derives {RULE_TYPE_PROPERTY}"
                ),
                location=f"nodes.{node_index}.properties.{RULE_TYPE_PROPERTY}",
                node_temp_id=node.temp_id,
                property_name=RULE_TYPE_PROPERTY,
            )
        )
    return issues


def _rule_type_edge_names(validation_service: GraphPatchValidationService) -> set[str]:
    return validation_service.validator.registry.edge_names_deriving_property(
        RULE_TYPE_PROPERTY
    )
