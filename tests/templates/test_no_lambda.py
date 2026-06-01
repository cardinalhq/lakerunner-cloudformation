"""Guard: nothing in the product creates a Lambda or a CloudFormation custom
resource. The whole stack must be deployable in environments where Lambda is
not available.
"""

import json

import pytest

from cardinal_cfn import (
    cardinal_cleanup,
    lakerunner_infra_base,
    lakerunner_infra_rds,
    lakerunner_services,
    lrdev_baseinfra,
    lrdev_vpc,
    satellite_infra_base,
    satellite_services,
)
from cardinal_cfn.children import (
    alb, cert, migration,
    services_query, services_process, services_control, maestro,
)

_TEMPLATES = [
    ("lrdev-vpc", lrdev_vpc),
    ("lrdev-baseinfra", lrdev_baseinfra),
    ("cardinal-cleanup", cardinal_cleanup),
    ("satellite-infra-base", satellite_infra_base),
    ("satellite-services", satellite_services),
    ("lakerunner-infra-base", lakerunner_infra_base),
    ("lakerunner-infra-rds", lakerunner_infra_rds),
    ("lakerunner-services", lakerunner_services),
    ("alb", alb),
    ("cert", cert),
    ("migration", migration),
    ("services-query", services_query),
    ("services-process", services_process),
    ("services-control", services_control),
    ("maestro", maestro),
]


@pytest.mark.parametrize("label,module", _TEMPLATES)
def test_no_lambda_or_custom_resource(label, module):
    td = json.loads(module.build().to_json())
    types = [r["Type"] for r in td["Resources"].values()]
    assert "AWS::Lambda::Function" not in types, f"{label} must not create an AWS::Lambda::Function"
    assert not any(t == "AWS::CloudFormation::CustomResource" or t.startswith("Custom::")
                   for t in types), f"{label} must not use a CloudFormation custom resource"
