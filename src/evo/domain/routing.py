"""Execution routes for a root issue."""

from enum import Enum


class RoutingMode(str, Enum):
    SQUAD = "squad"
    DIRECT = "direct"


def issue_route(assignee_type):
    if assignee_type == "agent":
        return RoutingMode.DIRECT
    if assignee_type == "squad":
        return RoutingMode.SQUAD
    raise ValueError("Choose one agent or Squad")
