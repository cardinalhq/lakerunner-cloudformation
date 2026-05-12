"""Guard: the only Lambda in the whole product is the optional PEM cert importer.

No-Lambda target environments must be able to run everything except `cert.yaml`,
which is skipped entirely when an ACM CertificateArn is supplied.
"""

import json

import pytest

from cardinal_cfn import cardinal_infrastructure, cardinal_vpc, root
from cardinal_cfn.children import (
    alb, cert, migration,
    services_query, services_process, services_control, otel, maestro,
)

# (label, module, allows_lambda)
_TEMPLATES = [
    ("cardinal-vpc", cardinal_vpc, False),
    ("cardinal-infrastructure", cardinal_infrastructure, False),
    ("cardinal-lakerunner (root)", root, False),
    ("alb", alb, False),
    ("cert", cert, True),
    ("migration", migration, False),
    ("services-query", services_query, False),
    ("services-process", services_process, False),
    ("services-control", services_control, False),
    ("otel", otel, False),
    ("maestro", maestro, False),
]


@pytest.mark.parametrize("label,module,allows_lambda", _TEMPLATES)
def test_lambda_only_in_cert(label, module, allows_lambda):
    td = json.loads(module.build().to_json())
    has_lambda = any(r["Type"] == "AWS::Lambda::Function" for r in td["Resources"].values())
    has_custom = any(
        r["Type"] == "AWS::CloudFormation::CustomResource" or r["Type"].startswith("Custom::")
        for r in td["Resources"].values()
    )
    if allows_lambda:
        return  # cert.yaml legitimately has a (conditional) Lambda + custom resource
    assert not has_lambda, f"{label} must not create an AWS::Lambda::Function"
    assert not has_custom, f"{label} must not use a CloudFormation custom resource"
