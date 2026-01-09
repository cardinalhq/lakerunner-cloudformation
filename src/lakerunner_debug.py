#!/usr/bin/env python3
# Copyright (C) 2025 CardinalHQ, Inc
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, version 3.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>.

"""
Debug task for network troubleshooting (DNS, connectivity, etc.)

Deploys a long-running task you can shell into via ECS Exec:
  aws ecs execute-command --cluster <cluster> --task <task-id> \
    --container Debug --interactive --command /bin/sh

The container includes: dig, nslookup, ping, traceroute, curl, netcat, etc.
"""

from troposphere import (
    Export,
    GetAtt,
    ImportValue,
    Output,
    Parameter,
    Ref,
    Split,
    Sub,
    Template,
)
from troposphere.logs import LogGroup
from troposphere.iam import Role, Policy
from troposphere.ecs import (
    ContainerDefinition,
    LogConfiguration,
    TaskDefinition,
    Service,
    NetworkConfiguration,
    AwsvpcConfiguration,
)


def create_debug_template():
    t = Template()
    t.set_description("Lakerunner debug task: long-running container for network troubleshooting via ECS Exec")

    # -----------------------
    # Parameters
    # -----------------------
    CommonInfraStackName = t.add_parameter(Parameter(
        "CommonInfraStackName", Type="String",
        Description="REQUIRED: Name of the CommonInfra stack to import values from."
    ))

    ContainerImage = t.add_parameter(Parameter(
        "ContainerImage", Type="String",
        Default="nicolaka/netshoot:latest",
        Description="Debug container image (default: nicolaka/netshoot with dig, nslookup, curl, etc.)"
    ))

    t.set_metadata({
        "AWS::CloudFormation::Interface": {
            "ParameterGroups": [
                {"Label": {"default": "Infrastructure"}, "Parameters": ["CommonInfraStackName"]},
                {"Label": {"default": "Container"}, "Parameters": ["ContainerImage"]},
            ],
            "ParameterLabels": {
                "CommonInfraStackName": {"default": "CommonInfra Stack Name"},
                "ContainerImage": {"default": "Debug Container Image"},
            }
        }
    })

    # Helper for imports
    def ci_export(suffix):
        return Sub("${CommonInfraStackName}-%s" % suffix)

    ClusterArnValue = ImportValue(ci_export("ClusterArn"))
    SecurityGroupsValue = ImportValue(ci_export("TaskSGId"))
    PrivateSubnetsValue = Split(",", ImportValue(ci_export("PrivateSubnets")))

    # -----------------------
    # CloudWatch Logs
    # -----------------------
    LogGroupRes = t.add_resource(LogGroup(
        "DebugLogGroup",
        LogGroupName=Sub("/lakerunner/debug/${AWS::StackName}"),
        RetentionInDays=7
    ))

    # -----------------------
    # Task Role (needs SSM for ECS Exec)
    # -----------------------
    TaskRole = t.add_resource(Role(
        "DebugTaskRole",
        AssumeRolePolicyDocument={
            "Version": "2012-10-17",
            "Statement": [{
                "Effect": "Allow",
                "Principal": {"Service": "ecs-tasks.amazonaws.com"},
                "Action": "sts:AssumeRole"
            }]
        },
        Policies=[
            Policy(
                "EcsExecPolicy",
                PolicyName="EcsExecPolicy",
                PolicyDocument={
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Action": [
                                "ssmmessages:CreateControlChannel",
                                "ssmmessages:CreateDataChannel",
                                "ssmmessages:OpenControlChannel",
                                "ssmmessages:OpenDataChannel"
                            ],
                            "Resource": "*"
                        }
                    ]
                }
            )
        ]
    ))

    # -----------------------
    # Execution Role
    # -----------------------
    ExecutionRole = t.add_resource(Role(
        "DebugExecutionRole",
        AssumeRolePolicyDocument={
            "Version": "2012-10-17",
            "Statement": [{
                "Effect": "Allow",
                "Principal": {"Service": "ecs-tasks.amazonaws.com"},
                "Action": "sts:AssumeRole"
            }]
        },
        ManagedPolicyArns=[
            "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
        ]
    ))

    # -----------------------
    # Task Definition
    # -----------------------
    TaskDef = t.add_resource(TaskDefinition(
        "DebugTaskDef",
        Family="lakerunner-debug",
        Cpu="256",
        Memory="512",
        NetworkMode="awsvpc",
        RequiresCompatibilities=["FARGATE"],
        ExecutionRoleArn=GetAtt(ExecutionRole, "Arn"),
        TaskRoleArn=GetAtt(TaskRole, "Arn"),
        ContainerDefinitions=[
            ContainerDefinition(
                Name="Debug",
                Image=Ref(ContainerImage),
                Command=["sleep", "infinity"],
                Essential=True,
                LogConfiguration=LogConfiguration(
                    LogDriver="awslogs",
                    Options={
                        "awslogs-group": Ref(LogGroupRes),
                        "awslogs-region": Sub("${AWS::Region}"),
                        "awslogs-stream-prefix": "debug"
                    }
                )
            )
        ]
    ))

    # -----------------------
    # ECS Service (single task, ECS Exec enabled)
    # -----------------------
    DebugService = t.add_resource(Service(
        "DebugService",
        ServiceName=Sub("${AWS::StackName}-debug"),
        Cluster=ClusterArnValue,
        TaskDefinition=Ref(TaskDef),
        LaunchType="FARGATE",
        DesiredCount=1,
        EnableExecuteCommand=True,
        NetworkConfiguration=NetworkConfiguration(
            AwsvpcConfiguration=AwsvpcConfiguration(
                Subnets=PrivateSubnetsValue,
                SecurityGroups=[SecurityGroupsValue],
                AssignPublicIp="DISABLED"
            )
        )
    ))

    # -----------------------
    # Outputs
    # -----------------------
    t.add_output(Output(
        "ClusterArn",
        Description="ECS Cluster ARN (for ecs execute-command)",
        Value=ClusterArnValue
    ))

    t.add_output(Output(
        "ServiceName",
        Description="Debug service name",
        Value=Ref(DebugService)
    ))

    t.add_output(Output(
        "ExecCommand",
        Description="Command to get a shell (replace TASK_ID with actual task ID)",
        Value=Sub(
            "aws ecs execute-command --cluster ${Cluster} --task TASK_ID --container Debug --interactive --command /bin/sh",
            Cluster=ClusterArnValue
        )
    ))

    t.add_output(Output(
        "ListTasksCommand",
        Description="Command to list running debug tasks",
        Value=Sub(
            "aws ecs list-tasks --cluster ${Cluster} --service-name ${Service}",
            Cluster=ClusterArnValue,
            Service=Ref(DebugService)
        )
    ))

    return t


if __name__ == "__main__":
    template = create_debug_template()
    print(template.to_yaml())
