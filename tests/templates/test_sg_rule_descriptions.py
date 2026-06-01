"""Regression guard: SG ingress/egress rule descriptions must match AWS's
allowed character set.

EC2 rejects SecurityGroup{Ingress,Egress} (and inline rule) descriptions that
contain characters outside ``a-zA-Z0-9. _-:/()#,@[]+=&;{}!$*``. Notably ``->``
fails because ``>`` is not in the set; we used to write ``"ALB -> query-api"``
which crashed the Security child stack at create time.
"""

import json
import re

import pytest

from cardinal_cfn import (
    lakerunner_infra_base,
    lakerunner_infra_rds,
    lrdev_baseinfra,
    lrdev_vpc,
    satellite_infra_base,
    satellite_services,
)
from cardinal_cfn.children import (
    alb, cert, maestro, migration,
    services_control, services_process, services_query,
)

_MODULES = [
    ("lrdev-vpc", lrdev_vpc),
    ("lrdev-baseinfra", lrdev_baseinfra),
    ("satellite-infra-base", satellite_infra_base),
    ("satellite-services", satellite_services),
    ("lakerunner-infra-base", lakerunner_infra_base),
    ("lakerunner-infra-rds", lakerunner_infra_rds),
    ("alb", alb),
    ("cert", cert),
    ("maestro", maestro),
    ("migration", migration),
    ("services-control", services_control),
    ("services-process", services_process),
    ("services-query", services_query),
]

# Per the EC2 API error: "Valid descriptions are strings less than 256
# characters from the following set:  a-zA-Z0-9. _-:/()#,@[]+=&;{}!$*"
_VALID = re.compile(r"^[A-Za-z0-9. _\-:/()#,@\[\]+=&;{}!$*]*$")


def _collect_descriptions(td: dict):
    """Yield (logical_id, where, description) for every SG rule description in td."""
    for logical_id, resource in td.get("Resources", {}).items():
        rtype = resource.get("Type", "")
        props = resource.get("Properties", {})
        if rtype in ("AWS::EC2::SecurityGroupIngress", "AWS::EC2::SecurityGroupEgress"):
            desc = props.get("Description")
            if isinstance(desc, str):
                yield logical_id, "Description", desc
        elif rtype == "AWS::EC2::SecurityGroup":
            for direction in ("SecurityGroupIngress", "SecurityGroupEgress"):
                for i, rule in enumerate(props.get(direction, []) or []):
                    desc = rule.get("Description") if isinstance(rule, dict) else None
                    if isinstance(desc, str):
                        yield logical_id, f"{direction}[{i}].Description", desc


@pytest.mark.parametrize("label,module", _MODULES)
def test_sg_rule_descriptions_use_only_aws_allowed_chars(label, module):
    td = json.loads(module.build().to_json())
    bad = []
    for logical_id, where, desc in _collect_descriptions(td):
        if not _VALID.match(desc) or len(desc) >= 256:
            bad.append(f"{logical_id}.{where}={desc!r}")
    assert not bad, (
        f"{label}: SG rule descriptions outside AWS's allowed character set "
        f"(a-zA-Z0-9. _-:/()#,@[]+=&;{{}}!$*):\n  " + "\n  ".join(bad)
    )
