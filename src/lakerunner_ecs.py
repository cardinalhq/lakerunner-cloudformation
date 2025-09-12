#!/usr/bin/env python3
"""ECS infrastructure stack for Lakerunner."""

from troposphere import Template, Parameter, Ref, Sub, Export, Output
from troposphere.ec2 import SecurityGroup, SecurityGroupIngress
from troposphere.ecs import Cluster


t = Template()
t.set_description("ECS infrastructure stack for Lakerunner.")

# -----------------------
# Parameters
# -----------------------
VpcId = t.add_parameter(Parameter(
    "VpcId",
    Type="AWS::EC2::VPC::Id",
    Description="REQUIRED: VPC where the ECS cluster and security group will be created.",
))

# -----------------------
# Security Group for tasks and database
# -----------------------
TaskSG = t.add_resource(SecurityGroup(
    "TaskSG",
    GroupDescription="Security group for ECS tasks and database",
    VpcId=Ref(VpcId),
    SecurityGroupEgress=[{
        "IpProtocol": "-1",
        "CidrIp": "0.0.0.0/0",
        "Description": "Allow all outbound",
    }],
))

# task-to-task communication
t.add_resource(SecurityGroupIngress(
    "TaskSG7101Self",
    GroupId=Ref(TaskSG),
    IpProtocol="tcp",
    FromPort=7101,
    ToPort=7101,
    SourceSecurityGroupId=Ref(TaskSG),
    Description="task-to-task 7101",
))

# Allow tasks to connect to PostgreSQL database
t.add_resource(SecurityGroupIngress(
    "TaskSGDbSelf",
    GroupId=Ref(TaskSG),
    IpProtocol="tcp",
    FromPort=5432,
    ToPort=5432,
    SourceSecurityGroupId=Ref(TaskSG),
    Description="task-to-database PostgreSQL",
))

# -----------------------
# ECS cluster
# -----------------------
ClusterRes = t.add_resource(Cluster(
    "Cluster",
    ClusterName=Sub("${AWS::StackName}-cluster"),
))

# -----------------------
# Outputs
# -----------------------
t.add_output(Output(
    "ClusterArn",
    Value=Sub("arn:aws:ecs:${AWS::Region}:${AWS::AccountId}:cluster/${Cluster}", Cluster=Ref(ClusterRes)),
    Export=Export(name=Sub("${AWS::StackName}-ClusterArn")),
))

t.add_output(Output(
    "TaskSGId",
    Value=Ref(TaskSG),
    Export=Export(name=Sub("${AWS::StackName}-TaskSGId")),
))

print(t.to_yaml())
