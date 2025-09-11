import json
from lakerunner_root import t as root_template

def test_nested_stack_parameters():
    resources = json.loads(root_template.to_json())["Resources"]
    ecs_params = resources["EcsStack"]["Properties"]["Parameters"]
    assert "VpcId" in ecs_params
    rds_params = resources["RdsStack"]["Properties"]["Parameters"]
    assert {"PrivateSubnets", "TaskSecurityGroupId"}.issubset(rds_params.keys())
    svc_params = resources["ServicesStack"]["Properties"]["Parameters"]
    expected = {
        "ClusterArn", "DbSecretArn", "DbHost", "DbPort",
        "TaskSecurityGroupId", "VpcId", "PrivateSubnets",
        "PublicSubnets", "BucketArn"
    }
    assert expected.issubset(svc_params.keys())
