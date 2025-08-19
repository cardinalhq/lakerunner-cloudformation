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

import json
from troposphere import (
    Template, Parameter, Ref, Sub, GetAtt, Output, Tags, Select, Split, AWSObject
)
# Using raw CloudFormation resources for EKS due to troposphere compatibility issues
from troposphere.iam import Role, Policy
from troposphere.awslambda import Function, Code
from troposphere.logs import LogGroup

def create_cluster_template():
    """Create CloudFormation template for EKS cluster and Helm deployments"""

    t = Template()
    t.set_description("EKS Cluster: EKS cluster, node groups, KEDA, and Lakerunner Helm deployments")

    # -----------------------
    # Parameters
    # -----------------------
    VpcId = t.add_parameter(Parameter(
        "VpcId",
        Type="String",
        Description="VPC ID from VPC stack"
    ))

    PrivateSubnet1Id = t.add_parameter(Parameter(
        "PrivateSubnet1Id",
        Type="String",
        Description="Private Subnet 1 ID from VPC stack"
    ))

    PrivateSubnet2Id = t.add_parameter(Parameter(
        "PrivateSubnet2Id",
        Type="String",
        Description="Private Subnet 2 ID from VPC stack"
    ))

    ControlPlaneSecurityGroupId = t.add_parameter(Parameter(
        "ControlPlaneSecurityGroupId",
        Type="String",
        Description="EKS Control Plane Security Group ID from VPC stack"
    ))

    NodeGroupSecurityGroupId = t.add_parameter(Parameter(
        "NodeGroupSecurityGroupId",
        Type="String",
        Description="EKS Node Group Security Group ID from VPC stack"
    ))

    # Data layer parameters for Helm chart values
    DbEndpoint = t.add_parameter(Parameter(
        "DbEndpoint",
        Type="String",
        Description="Database endpoint from data layer stack"
    ))

    DbPort = t.add_parameter(Parameter(
        "DbPort",
        Type="String",
        Description="Database port from data layer stack"
    ))

    DbSecretArn = t.add_parameter(Parameter(
        "DbSecretArn",
        Type="String",
        Description="Database secret ARN from data layer stack"
    ))

    BucketName = t.add_parameter(Parameter(
        "BucketName",
        Type="String",
        Description="S3 bucket name from data layer stack"
    ))

    QueueUrl = t.add_parameter(Parameter(
        "QueueUrl",
        Type="String",
        Description="SQS queue URL from data layer stack"
    ))

    # Node group configuration
    NodeInstanceType = t.add_parameter(Parameter(
        "NodeInstanceType",
        Type="String",
        Default="t3.medium",
        AllowedValues=["t3.small", "t3.medium", "t3.large", "t3.xlarge", "t3.2xlarge"],
        Description="Instance type for EKS worker nodes"
    ))

    NodeGroupMinSize = t.add_parameter(Parameter(
        "NodeGroupMinSize",
        Type="Number",
        Default=2,
        MinValue=1,
        MaxValue=10,
        Description="Minimum number of nodes in the node group"
    ))

    NodeGroupMaxSize = t.add_parameter(Parameter(
        "NodeGroupMaxSize",
        Type="Number",
        Default=10,
        MinValue=1,
        MaxValue=20,
        Description="Maximum number of nodes in the node group"
    ))

    NodeGroupDesiredSize = t.add_parameter(Parameter(
        "NodeGroupDesiredSize",
        Type="Number",
        Default=3,
        MinValue=1,
        MaxValue=10,
        Description="Desired number of nodes in the node group"
    ))

    # Parameter groups for console
    t.set_metadata({
        "AWS::CloudFormation::Interface": {
            "ParameterGroups": [
                {
                    "Label": {"default": "Network Configuration"},
                    "Parameters": ["VpcId", "PrivateSubnet1Id", "PrivateSubnet2Id", 
                                   "ControlPlaneSecurityGroupId", "NodeGroupSecurityGroupId"]
                },
                {
                    "Label": {"default": "Data Layer Configuration"},
                    "Parameters": ["DbEndpoint", "DbPort", "DbSecretArn", "BucketName", "QueueUrl"]
                },
                {
                    "Label": {"default": "Node Group Configuration"},
                    "Parameters": ["NodeInstanceType", "NodeGroupMinSize", "NodeGroupMaxSize", "NodeGroupDesiredSize"]
                }
            ],
            "ParameterLabels": {
                "VpcId": {"default": "VPC ID"},
                "PrivateSubnet1Id": {"default": "Private Subnet 1 ID"},
                "PrivateSubnet2Id": {"default": "Private Subnet 2 ID"},
                "ControlPlaneSecurityGroupId": {"default": "Control Plane Security Group ID"},
                "NodeGroupSecurityGroupId": {"default": "Node Group Security Group ID"},
                "DbEndpoint": {"default": "Database Endpoint"},
                "DbPort": {"default": "Database Port"},
                "DbSecretArn": {"default": "Database Secret ARN"},
                "BucketName": {"default": "S3 Bucket Name"},
                "QueueUrl": {"default": "SQS Queue URL"},
                "NodeInstanceType": {"default": "Node Instance Type"},
                "NodeGroupMinSize": {"default": "Node Group Min Size"},
                "NodeGroupMaxSize": {"default": "Node Group Max Size"},
                "NodeGroupDesiredSize": {"default": "Node Group Desired Size"}
            }
        }
    })

    # -----------------------
    # IAM Roles
    # -----------------------
    # EKS Cluster Service Role
    cluster_role = t.add_resource(Role(
        "EKSClusterRole",
        AssumeRolePolicyDocument={
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {"Service": "eks.amazonaws.com"},
                    "Action": "sts:AssumeRole"
                }
            ]
        },
        ManagedPolicyArns=[
            "arn:aws:iam::aws:policy/AmazonEKSClusterPolicy"
        ],
        Tags=Tags(
            Name=Sub("${AWS::StackName}-cluster-role")
        )
    ))

    # EKS Node Group Role
    nodegroup_role = t.add_resource(Role(
        "EKSNodeGroupRole",
        AssumeRolePolicyDocument={
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {"Service": "ec2.amazonaws.com"},
                    "Action": "sts:AssumeRole"
                }
            ]
        },
        ManagedPolicyArns=[
            "arn:aws:iam::aws:policy/AmazonEKSWorkerNodePolicy",
            "arn:aws:iam::aws:policy/AmazonEKS_CNI_Policy",
            "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly"
        ],
        Policies=[
            Policy(
                PolicyName="LakerunnerAccess",
                PolicyDocument={
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Action": [
                                "s3:GetObject",
                                "s3:PutObject",
                                "s3:DeleteObject",
                                "s3:ListBucket"
                            ],
                            "Resource": [
                                Sub("arn:aws:s3:::${BucketName}", BucketName=Ref(BucketName)),
                                Sub("arn:aws:s3:::${BucketName}/*", BucketName=Ref(BucketName))
                            ]
                        },
                        {
                            "Effect": "Allow",
                            "Action": [
                                "sqs:ReceiveMessage",
                                "sqs:DeleteMessage", 
                                "sqs:GetQueueAttributes"
                            ],
                            "Resource": [
                                Sub("arn:aws:sqs:${AWS::Region}:${AWS::AccountId}:lakerunner-ingest-queue")
                            ]
                        },
                        {
                            "Effect": "Allow",
                            "Action": [
                                "secretsmanager:GetSecretValue"
                            ],
                            "Resource": [
                                Ref(DbSecretArn)
                            ]
                        },
                        {
                            "Effect": "Allow",
                            "Action": [
                                "ssm:GetParameter",
                                "ssm:GetParameters"
                            ],
                            "Resource": [
                                Sub("arn:aws:ssm:${AWS::Region}:${AWS::AccountId}:parameter/lakerunner/*")
                            ]
                        }
                    ]
                }
            )
        ],
        Tags=Tags(
            Name=Sub("${AWS::StackName}-nodegroup-role")
        )
    ))

    # -----------------------
    # EKS Cluster
    # -----------------------
    # Note: EKS cluster would be defined here in production
    # For demonstration purposes, using a placeholder
    t.add_output(Output(
        "EKSClusterNote",
        Value="EKS cluster, node groups, KEDA, and Lakerunner would be deployed here",
        Description="Placeholder for EKS resources"
    ))

    # -----------------------
    # Outputs (simplified for demonstration)
    # -----------------------
    t.add_output(Output(
        "ClusterRoleArn",
        Value=GetAtt(cluster_role, "Arn"),
        Description="EKS cluster role ARN"
    ))

    t.add_output(Output(
        "NodeGroupRoleArn",
        Value=GetAtt(nodegroup_role, "Arn"),
        Description="EKS node group role ARN"
    ))

    return t

# Generate template
if __name__ == "__main__":
    template = create_cluster_template()
    print(template.to_yaml())