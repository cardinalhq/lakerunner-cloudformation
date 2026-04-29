"""cluster.yaml nested stack: ECS cluster, base task SG, execution role, base log group."""

from troposphere import (
    Template,
    Parameter,
    Ref,
    GetAtt,
    Output,
    Sub,
)
from troposphere.ecs import Cluster as ECSCluster, ClusterSetting
from troposphere.ec2 import SecurityGroup, SecurityGroupIngress
from troposphere.iam import Policy, Role
from troposphere.logs import LogGroup
from troposphere.servicediscovery import PrivateDnsNamespace

from cardinal_cfn.naming import cardinal_tags
from cardinal_cfn.parameters import add_install_id_parameters
from cardinal_cfn.policies import apply_policy


def build() -> Template:
    t = Template()
    t.set_description("Cardinal cluster: ECS cluster, base task SG, execution role.")

    add_install_id_parameters(t)
    t.add_parameter(
        Parameter(
            "VpcId",
            Type="AWS::EC2::VPC::Id",
            Description="VPC ID (forwarded from root).",
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

    task_sg = t.add_resource(
        SecurityGroup(
            "TaskSG",
            GroupDescription="Cardinal ECS task security group",
            VpcId=Ref("VpcId"),
            SecurityGroupEgress=[
                {
                    "IpProtocol": "-1",
                    "CidrIp": "0.0.0.0/0",
                    "Description": "Allow all outbound",
                }
            ],
            Tags=cardinal_tags(component="compute", role="task-sg"),
        )
    )

    t.add_resource(
        SecurityGroupIngress(
            "TaskSGAllSelf",
            GroupId=Ref(task_sg),
            IpProtocol="tcp",
            FromPort=0,
            ToPort=65535,
            SourceSecurityGroupId=Ref(task_sg),
            Description="task-to-task all ports",
        )
    )

    exec_role = t.add_resource(
        Role(
            "ExecutionRole",
            AssumeRolePolicyDocument={
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Principal": {"Service": "ecs-tasks.amazonaws.com"},
                        "Action": "sts:AssumeRole",
                    }
                ],
            },
            ManagedPolicyArns=[
                "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
            ],
            Policies=[
                Policy(
                    PolicyName="cardinal-execution-secrets",
                    # ECS uses ExecutionRole to fetch values for the
                    # `secrets` block at task launch (Secrets Manager + SSM).
                    # AmazonECSTaskExecutionRolePolicy does not include either,
                    # so we add tightly-scoped inline grants here. Patterns
                    # cover the named cardinal/* secrets and the
                    # logical-ID-derived auto-generated ones.
                    PolicyDocument={
                        "Version": "2012-10-17",
                        "Statement": [
                            {
                                "Effect": "Allow",
                                "Action": ["secretsmanager:GetSecretValue"],
                                "Resource": [
                                    Sub(
                                        "arn:${AWS::Partition}:secretsmanager:"
                                        "${AWS::Region}:${AWS::AccountId}:"
                                        "secret:cardinal/${InstallIdLong}/*"
                                    ),
                                    Sub(
                                        "arn:${AWS::Partition}:secretsmanager:"
                                        "${AWS::Region}:${AWS::AccountId}:"
                                        "secret:DbMasterSecret-*"
                                    ),
                                    Sub(
                                        "arn:${AWS::Partition}:secretsmanager:"
                                        "${AWS::Region}:${AWS::AccountId}:"
                                        "secret:MaestroDbSecret-*"
                                    ),
                                    Sub(
                                        "arn:${AWS::Partition}:secretsmanager:"
                                        "${AWS::Region}:${AWS::AccountId}:"
                                        "secret:InternalServiceKeysSecret-*"
                                    ),
                                ],
                            },
                            {
                                "Effect": "Allow",
                                "Action": [
                                    "ssm:GetParameter",
                                    "ssm:GetParameters",
                                ],
                                "Resource": [
                                    Sub(
                                        "arn:${AWS::Partition}:ssm:"
                                        "${AWS::Region}:${AWS::AccountId}:"
                                        "parameter/cardinal/${InstallIdLong}/*"
                                    ),
                                ],
                            },
                        ],
                    },
                )
            ],
            Tags=cardinal_tags(component="compute", role="exec-role"),
        )
    )

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
    t.add_output(Output("TaskSecurityGroupId", Value=Ref(task_sg)))
    t.add_output(Output("ExecutionRoleArn", Value=GetAtt(exec_role, "Arn")))
    t.add_output(Output("BaseLogGroupName", Value=Ref(base_lg)))
    t.add_output(Output("ServiceNamespaceId", Value=Ref(sd_namespace)))
    t.add_output(Output("ServiceNamespaceName", Value=Sub("cardinal-${InstallIdShort}.local")))

    return t


if __name__ == "__main__":
    print(build().to_yaml())
