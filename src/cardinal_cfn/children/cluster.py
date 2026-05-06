"""cluster.yaml nested stack: ECS cluster + Cloud Map namespace + base log group.

Per the Phase 2 prereqs-split refactor, this stack no longer creates the task
security group or the execution role; the customer pre-creates both and passes
their IDs/ARNs as parameters.
"""

from troposphere import (
    Template,
    Parameter,
    Ref,
    GetAtt,
    Output,
    Sub,
)
from troposphere.ecs import Cluster as ECSCluster, ClusterSetting
from troposphere.logs import LogGroup
from troposphere.servicediscovery import PrivateDnsNamespace

from cardinal_cfn.naming import cardinal_tags
from cardinal_cfn.parameters import add_install_id_parameters
from cardinal_cfn.policies import apply_policy


def build() -> Template:
    t = Template()
    t.set_description("Cardinal cluster: ECS cluster, Cloud Map namespace, base log group.")

    add_install_id_parameters(t)
    t.add_parameter(
        Parameter(
            "VpcId",
            Type="AWS::EC2::VPC::Id",
            Description="VPC ID (forwarded from root).",
        )
    )
    t.add_parameter(
        Parameter(
            "ExecutionRoleArn",
            Type="String",
            Description="ECS task execution role ARN (customer-supplied).",
        )
    )
    t.add_parameter(
        Parameter(
            "TaskSgId",
            Type="AWS::EC2::SecurityGroup::Id",
            Description="ECS task security group ID (customer-supplied).",
        )
    )

    cluster_res = t.add_resource(
        ECSCluster(
            "Cluster",
            ClusterSettings=[ClusterSetting(Name="containerInsights", Value="enabled")],
            Tags=cardinal_tags(component="compute", role="ecs-cluster"),
        )
    )
    apply_policy(cluster_res, "ecs-cluster")

    base_lg = t.add_resource(
        LogGroup(
            "BaseLogGroup",
            RetentionInDays=14,
        )
    )
    apply_policy(base_lg, "log-group")

    # Cloud Map private DNS namespace for in-cluster service-to-service routing
    # (e.g. alert-evaluator -> query-api). Services can register and resolve
    # each other via DNS without going through the ALB.
    sd_namespace = t.add_resource(
        PrivateDnsNamespace(
            "ServiceNamespace",
            Name=Sub("cardinal-${InstallIdShort}.local"),
            Vpc=Ref("VpcId"),
            Description="Internal service-to-service DNS for the Cardinal cluster.",
        )
    )

    t.add_output(Output("ClusterArn", Value=GetAtt(cluster_res, "Arn")))
    t.add_output(Output("ClusterName", Value=Ref(cluster_res)))
    t.add_output(Output("BaseLogGroupName", Value=Ref(base_lg)))
    t.add_output(Output("ServiceNamespaceId", Value=Ref(sd_namespace)))
    t.add_output(Output("ServiceNamespaceName", Value=Sub("cardinal-${InstallIdShort}.local")))

    return t


if __name__ == "__main__":
    print(build().to_yaml())
