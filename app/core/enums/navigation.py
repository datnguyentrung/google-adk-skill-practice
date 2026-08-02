from enum import StrEnum


class NodeType(StrEnum):
    INTERSECTION = "intersection"
    DEAD_END = "dead_end"
    ROUNDABOUT = "roundabout"
    ENTRANCE = "entrance"
    PARKING = "parking"
    BRIDGE = "bridge"
    TUNNEL = "tunnel"
    U_TURN = "u_turn"
    BUILDING = "building"


class RoadType(StrEnum):
    MAIN = "main"
    RESIDENTIAL = "residential"
    ALLEY = "alley"
    ROUNDABOUT = "roundabout"
    BRIDGE = "bridge"
    TUNNEL = "tunnel"
    PEDESTRIAN = "pedestrian"
    SERVICE = "service"


class EdgeStatus(StrEnum):
    OPEN = "open"
    CLOSED = "closed"
    RESTRICTED = "restricted"


class AccessMode(StrEnum):
    CAR = "car"
    MOTORBIKE = "motorbike"
    BICYCLE = "bicycle"
    PEDESTRIAN = "pedestrian"
    SERVICE_VEHICLE = "service_vehicle"


class OptimizationMode(StrEnum):
    SHORTEST_DISTANCE = "shortest_distance"
    FASTEST_TIME = "fastest_time"


class TurnDirection(StrEnum):
    START = "start"
    STRAIGHT = "straight"
    LEFT = "left"
    RIGHT = "right"
    U_TURN = "u_turn"
    ROUNDABOUT = "roundabout"
    ARRIVE = "arrive"
    UNKNOWN = "unknown"


class NavigationScenario(StrEnum):
    INITIAL_ROUTE = "initial_route"
    CONTINUE_GUIDANCE = "continue_guidance"
    OFF_ROUTE_RECOVERY = "off_route_recovery"
    WRONG_TURN_REROUTE = "wrong_turn_reroute"
    FORGOTTEN_ROUTE = "forgotten_route"
    ROUTE_ERROR = "route_error"