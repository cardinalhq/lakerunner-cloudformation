"""Pre-allocated ListenerRule priorities.

ListenerRule.Priority must be unique per Listener, and that constraint is
enforced across all stacks attached to that listener. Pre-allocating keeps
future per-service stack splits collision-free.

400-999 is reserved for new services.
"""


LISTENER_PRIORITIES: dict = {
    "query-api":     100,
    "admin-api":     110,
    "maestro-https": 200,
    "maestro-dex":   210,
    "otel-grpc":     300,
}


def priority_for(service_key: str) -> int:
    """Return the registered priority for a service. Raises KeyError if unknown."""
    return LISTENER_PRIORITIES[service_key]
