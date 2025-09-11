import json
from lakerunner_services import t as services_template

def test_services_parameters_exist():
    params = json.loads(services_template.to_json())["Parameters"]
    expected = {
        "ClusterArn", "DbSecretArn", "DbHost", "DbPort",
        "TaskSecurityGroupId", "VpcId", "PrivateSubnets",
        "PublicSubnets", "BucketArn", "EfsId"
    }
    assert expected.issubset(params.keys())
    assert params["EfsId"]["Default"] == ""
