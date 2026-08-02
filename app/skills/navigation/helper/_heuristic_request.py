"""Heuristic fallback for navigation request extraction."""



import re

from app.core.schemas.graph_navigation import LocationRef, NavigationRequest
from app.core.schemas.graph_navigation import NavigationSessionState

NODE_HINTS = (
    "ngã ba",
    "ngã tư",
    "ngã năm",
    "ngõ cụt",
    "cổng",
    "bãi đỗ",
    "lối vào",
    "vòng xuyến",
    "hầm",
    "cầu",
)

ROAD_HINTS = ("đường", "phố", "ngõ", "hẻm", "tuyến")


def _heuristic_request(
    message: str, previous: NavigationSessionState | None
) -> NavigationRequest:
    """Fallback only; the extraction LLM is configured for normal ADK execution."""
    lower = message.casefold()
    start, destination = _extract_start_destination(message)
    return NavigationRequest(
        userMessage=message,
        start=start,
        destination=destination or (previous.destination if previous else None),
        currentPosition=start or (previous.current_position if previous else None),
        access=previous.access if previous else None,
        optimization=previous.optimization if previous else None,
        isWrongOrLost=any(
            word in lower
            for word in ("lạc", "sai đường", "wrong turn", "lost", "missed turn")
        ),
        asksNextStep=any(
            word in lower
            for word in ("bước tiếp", "tiếp theo", "next step", "continue")
        ),
    )


def _extract_start_destination(message: str) -> tuple[LocationRef | None, LocationRef | None]:
    match = re.search(
        r"\btừ\s+(?P<start>.+?)\s+(?:đến|tới|qua)\s+(?P<destination>.+)$",
        message,
        flags=re.IGNORECASE,
    )
    if not match:
        return None, None
    start = _clean_location(match.group("start"))
    destination = _clean_location(match.group("destination"))
    return _location_ref(start), _location_ref(destination)


def _clean_location(value: str) -> str:
    return re.sub(r"[.?!,;:]+$", "", value.strip())


def _location_ref(value: str) -> LocationRef | None:
    if not value:
        return None
    target_type = _infer_target_type(value)
    if target_type == "road":
        return LocationRef(roadName=value, rawText=value, targetType="road")
    if target_type == "node":
        return LocationRef(name=value, rawText=value, targetType="node")
    return LocationRef(name=value, roadName=value, rawText=value, targetType="auto")


def _infer_target_type(value: str) -> str:
    lower = value.casefold()
    if any(hint in lower for hint in NODE_HINTS):
        return "node"
    if any(hint in lower for hint in ROAD_HINTS):
        return "road"
    return "auto"
