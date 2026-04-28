"""Tests for shared parameter helpers."""

import json

from troposphere import Template

from cardinal_cfn.parameters import (
    add_install_id_parameters,
    add_no_echo_parameter,
    add_parameter_group_metadata,
)


def _to_dict(t):
    return json.loads(t.to_json())


def test_add_install_id_parameters_adds_two_string_parameters():
    t = Template()
    add_install_id_parameters(t)
    rendered = _to_dict(t)
    assert "InstallIdShort" in rendered["Parameters"]
    assert "InstallIdLong" in rendered["Parameters"]
    assert rendered["Parameters"]["InstallIdShort"]["Type"] == "String"
    assert rendered["Parameters"]["InstallIdLong"]["Type"] == "String"


def test_add_no_echo_parameter_marks_no_echo_true():
    t = Template()
    add_no_echo_parameter(t, "LicenseData", description="License JSON")
    rendered = _to_dict(t)
    assert rendered["Parameters"]["LicenseData"]["NoEcho"] is True


def test_add_parameter_group_metadata_creates_console_grouping():
    t = Template()
    add_parameter_group_metadata(
        t,
        groups=[
            {"label": "Networking", "parameters": ["VpcId", "PrivateSubnets"]},
            {"label": "Sizing", "parameters": ["RdsInstanceClass"]},
        ],
        labels={"VpcId": "VPC Id"},
    )
    rendered = _to_dict(t)
    interface = rendered["Metadata"]["AWS::CloudFormation::Interface"]
    groups = interface["ParameterGroups"]
    assert groups[0]["Label"]["default"] == "Networking"
    assert groups[0]["Parameters"] == ["VpcId", "PrivateSubnets"]
    assert interface["ParameterLabels"]["VpcId"]["default"] == "VPC Id"
