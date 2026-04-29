"""Tests for AWS::StackId-based install-id derivation."""

import json

from cardinal_cfn.install_id import install_id_short, install_id_long


def _to_dict(obj):
    """Render a troposphere intrinsic into a plain dict for comparison."""
    return json.loads(json.dumps(obj, default=lambda o: o.to_dict()))


def test_install_id_short_is_first_uuid_segment():
    """InstallIdShort is the first hex group of the StackId UUID (8 chars)."""
    expr = _to_dict(install_id_short())
    assert expr == {
        "Fn::Select": [
            0,
            {
                "Fn::Split": [
                    "-",
                    {
                        "Fn::Select": [
                            2,
                            {"Fn::Split": ["/", {"Ref": "AWS::StackId"}]},
                        ]
                    },
                ]
            },
        ]
    }


def test_install_id_long_joins_first_two_uuid_segments():
    """InstallIdLong is the first two hex groups of the StackId UUID joined (12 chars)."""
    expr = _to_dict(install_id_long())
    uuid_expr = {
        "Fn::Select": [2, {"Fn::Split": ["/", {"Ref": "AWS::StackId"}]}]
    }
    assert expr == {
        "Fn::Join": [
            "",
            [
                {"Fn::Select": [0, {"Fn::Split": ["-", uuid_expr]}]},
                {"Fn::Select": [1, {"Fn::Split": ["-", uuid_expr]}]},
            ],
        ]
    }
