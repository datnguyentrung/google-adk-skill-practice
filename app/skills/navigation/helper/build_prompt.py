from app.core.schemas.graph_navigation import NavigationRequest, NavigationSessionState


def build_navigation_dynamic_prompt(
    request: NavigationRequest, state: NavigationSessionState, *, reroute: bool
) -> str:
    """Create the English runtime prompt consumed by the response renderer."""
    return "\n".join(
        (
            "You are rendering verified navigation guidance. Never invent route data.",
            f"User request: {request.user_message}",
            f"Current route: {state.route.route_id if state.route else 'none'}",
            f"Current position: {state.current_position.model_dump() if state.current_position else 'unknown'}",
            f"Destination: {state.destination.model_dump() if state.destination else 'unknown'}",
            f"Access mode: {state.access}",
            f"Optimization: {state.optimization}",
            f"Current step index: {state.current_step_index}",
            f"Warnings: {state.warnings}",
            f"Reroute required: {reroute}",
            f"Reroute status: {state.reroute_reason or 'not required'}",
            "Respond in Vietnamese with one concise, action-oriented instruction.",
        )
    )


def build_extraction_prompt(
    message: str,
    previous: NavigationSessionState | None,
) -> str:
    previous_state = (
        previous.model_dump_json(by_alias=True) if previous is not None else "null"
    )

    return f"""
Extract a structured navigation request from the latest user message.

Latest user message:
{message}

Previous navigation session state:
{previous_state}

Rules:
- Extract only information explicitly stated in the latest message.
- Do not invent locations.
- Do not invent access; the Python coordinator applies the default when omitted.
- Use previous state only when the latest message omits a value.
- Set location targetType to "node" for intersections, dead ends, gates,
  parking spots, building entrances, roundabouts, bridges, and tunnels.
- Set location targetType to "road" for road, street, lane, alley, or route
  names that should resolve through graph edge roadName values.
- Use roadName for road/edge targets and name for node targets. Preserve the
  original text in rawText.
- Do not generate navigation instructions.
- Return data matching the NavigationExtractionOutput schema.
""".strip()
