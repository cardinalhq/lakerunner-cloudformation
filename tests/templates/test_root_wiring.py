"""Integration test: root parameter passes match child parameter declarations."""

import json

import pytest

from cardinal_cfn import root
from cardinal_cfn.children import (
    alb, cert, migration,
    services_query, services_process, services_control, otel, maestro,
)


CHILDREN = {
    "AlbStack": alb,
    "CertStack": cert,
    "MigrationStack": migration,
    "ServicesQueryStack": services_query,
    "ServicesProcessStack": services_process,
    "ServicesControlStack": services_control,
    "OtelStack": otel,
    "MaestroStack": maestro,
}


@pytest.mark.parametrize("logical_id,module", list(CHILDREN.items()))
def test_root_passes_match_child_params(logical_id, module):
    root_td = json.loads(root.build().to_json())
    child_td = json.loads(module.build().to_json())

    nested = root_td["Resources"][logical_id]["Properties"]["Parameters"]
    declared = set(child_td["Parameters"].keys())
    passed = set(nested.keys())

    missing = declared - passed
    extra = passed - declared
    assert not missing, f"{logical_id}: root does not pass required params {missing}"
    assert not extra, f"{logical_id}: root passes unknown params {extra}"


_LIST_TYPES = {
    "CommaDelimitedList",
    "List<String>",
    "List<AWS::EC2::Subnet::Id>",
    "List<AWS::EC2::SecurityGroup::Id>",
    "List<AWS::EC2::AvailabilityZone::Name>",
}


def _passed_value_shape(value):
    """Categorize a root-passed value as 'list', 'string', or 'unknown'."""
    if isinstance(value, list):
        return "list"
    if isinstance(value, str):
        return "string"
    if isinstance(value, dict):
        # Intrinsic functions used by root.py:
        # - Ref / Fn::GetAtt / Fn::Sub / Fn::Join all yield strings at deploy time.
        # - The only way root produces a list value into a nested stack is if
        #   it directly passes a Python list (caught above).
        if any(k in value for k in ("Ref", "Fn::GetAtt", "Fn::Sub", "Fn::Join", "Fn::Select")):
            return "string"
    return "unknown"


@pytest.mark.parametrize("logical_id,module", list(CHILDREN.items()))
def test_root_passes_compatible_value_types(logical_id, module):
    """Ensure root-passed values are shape-compatible with declared child param types.

    Catches the regression where root passes Ref(<List<...>> param) directly
    (a list at deploy time) into a child parameter declared as plain String:
    CFN renders 'foo,bar' but the child expected 'foo' or vice versa.
    """
    root_td = json.loads(root.build().to_json())
    child_td = json.loads(module.build().to_json())

    nested = root_td["Resources"][logical_id]["Properties"]["Parameters"]
    declared = child_td["Parameters"]

    for name, value in nested.items():
        decl_type = declared[name]["Type"]
        shape = _passed_value_shape(value)
        if shape == "list":
            assert decl_type in _LIST_TYPES, (
                f"{logical_id}.{name}: root passes a list but child declares "
                f"Type={decl_type!r}; nested-stack list params must be passed "
                f"as CSV strings. Wrap with Fn::Join."
            )
        elif shape == "string":
            assert decl_type not in _LIST_TYPES, (
                f"{logical_id}.{name}: root passes a string-shaped value but "
                f"child declares list Type={decl_type!r}; either change child "
                f"to String + Fn::Split, or pass an actual list from root."
            )
