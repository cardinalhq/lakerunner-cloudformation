"""cluster.yaml nested stack: ECS cluster, base task SG, execution role, base log group."""

from troposphere import (
    Template,
    Parameter,
    Ref,
    GetAtt,
    Output,
)
from troposphere.ecs import Cluster as ECSCluster, ClusterSetting
from troposphere.ec2 import SecurityGroup, SecurityGroupIngress
from troposphere.iam import Role
from troposphere.logs import LogGroup

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

    t.add_output(Output("ClusterArn", Value=GetAtt(cluster_res, "Arn")))
    t.add_output(Output("ClusterName", Value=Ref(cluster_res)))
    t.add_output(Output("TaskSecurityGroupId", Value=Ref(task_sg)))
    t.add_output(Output("ExecutionRoleArn", Value=GetAtt(exec_role, "Arn")))
    t.add_output(Output("BaseLogGroupName", Value=Ref(base_lg)))

    return t


if __name__ == "__main__":
    print(build().to_yaml())
