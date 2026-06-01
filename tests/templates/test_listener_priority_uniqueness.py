"""Cross-template check: no two ListenerRules share a Priority.

The src/cardinal_cfn/listener_priorities.py registry already guards against
collisions within the registry. This test catches the case where a service
sets a Priority directly (bypassing the registry) and accidentally collides
with another service in a different child template.
"""

import json

from cardinal_cfn.children import (
    services_query,
    services_control,
    maestro,
)


_TIER_MODULES = {
    "services-query": services_query,
    "services-control": services_control,
    "maestro": maestro,
}


def test_no_listener_priority_collides_across_templates():
    by_priority: dict[int, list[str]] = {}
    for tier_name, module in _TIER_MODULES.items():
        td = json.loads(module.build().to_json())
        for logical_id, res in td["Resources"].items():
            if res["Type"] != "AWS::ElasticLoadBalancingV2::ListenerRule":
                continue
            priority = res["Properties"].get("Priority")
            # OTEL's listener rule is conditional via Fn::If; only count concrete priorities.
            if not isinstance(priority, int):
                continue
            by_priority.setdefault(priority, []).append(f"{tier_name}:{logical_id}")

    collisions = {p: ids for p, ids in by_priority.items() if len(ids) > 1}
    assert not collisions, f"ListenerRule priority collisions across templates: {collisions}"
