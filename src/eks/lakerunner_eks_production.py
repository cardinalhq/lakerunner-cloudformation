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

from troposphere import (
    Template, Parameter, Ref, Sub, GetAtt, Output, Tags
)
from troposphere.cloudformation import Stack

def create_production_template():
    """Create CloudFormation template for production EKS deployment with nested stacks"""

    t = Template()
    t.set_description("EKS Production Stack: Orchestrates VPC, Data Layer, and EKS Cluster substacks")

    # -----------------------
    # Parameters
    # -----------------------
    AvailabilityZone1 = t.add_parameter(Parameter(
        "AvailabilityZone1",
        Type="AWS::EC2::AvailabilityZone::Name",
        Description="First Availability Zone for subnets"
    ))

    AvailabilityZone2 = t.add_parameter(Parameter(
        "AvailabilityZone2",
        Type="AWS::EC2::AvailabilityZone::Name",
        Description="Second Availability Zone for subnets"
    ))

    VpcCidr = t.add_parameter(Parameter(
        "VpcCidr",
        Type="String",
        Default="10.0.0.0/16",
        Description="CIDR block for the VPC"
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

    # Configuration overrides
    ApiKeysOverride = t.add_parameter(Parameter(
        "ApiKeysOverride",
        Type="String",
        Default="",
        Description="OPTIONAL: Custom API keys configuration in YAML format"
    ))

    StorageProfilesOverride = t.add_parameter(Parameter(
        "StorageProfilesOverride",
        Type="String",
        Default="",
        Description="OPTIONAL: Custom storage profiles configuration in YAML format"
    ))

    # Template URLs - these would be replaced with actual S3 URLs in production
    VpcTemplateUrl = t.add_parameter(Parameter(
        "VpcTemplateUrl",
        Type="String",
        Default="https://s3.amazonaws.com/your-bucket/lakerunner-eks-vpc.yaml",
        Description="URL to VPC CloudFormation template"
    ))

    DataTemplateUrl = t.add_parameter(Parameter(
        "DataTemplateUrl",
        Type="String",
        Default="https://s3.amazonaws.com/your-bucket/lakerunner-eks-data.yaml",
        Description="URL to Data Layer CloudFormation template"
    ))

    ClusterTemplateUrl = t.add_parameter(Parameter(
        "ClusterTemplateUrl",
        Type="String",
        Default="https://s3.amazonaws.com/your-bucket/lakerunner-eks-cluster.yaml",
        Description="URL to EKS Cluster CloudFormation template"
    ))

    # Parameter groups for console
    t.set_metadata({
        "AWS::CloudFormation::Interface": {
            "ParameterGroups": [
                {
                    "Label": {"default": "Network Configuration"},
                    "Parameters": ["VpcCidr", "AvailabilityZone1", "AvailabilityZone2"]
                },
                {
                    "Label": {"default": "Node Group Configuration"},
                    "Parameters": ["NodeInstanceType", "NodeGroupMinSize", "NodeGroupMaxSize", "NodeGroupDesiredSize"]
                },
                {
                    "Label": {"default": "Configuration Overrides (Advanced)"},
                    "Parameters": ["ApiKeysOverride", "StorageProfilesOverride"]
                },
                {
                    "Label": {"default": "Template URLs (Advanced)"},
                    "Parameters": ["VpcTemplateUrl", "DataTemplateUrl", "ClusterTemplateUrl"]
                }
            ],
            "ParameterLabels": {
                "VpcCidr": {"default": "VPC CIDR Block"},
                "AvailabilityZone1": {"default": "Availability Zone 1"},
                "AvailabilityZone2": {"default": "Availability Zone 2"},
                "NodeInstanceType": {"default": "Node Instance Type"},
                "NodeGroupMinSize": {"default": "Node Group Min Size"},
                "NodeGroupMaxSize": {"default": "Node Group Max Size"},
                "NodeGroupDesiredSize": {"default": "Node Group Desired Size"},
                "ApiKeysOverride": {"default": "Custom API Keys (YAML)"},
                "StorageProfilesOverride": {"default": "Custom Storage Profiles (YAML)"},
                "VpcTemplateUrl": {"default": "VPC Template URL"},
                "DataTemplateUrl": {"default": "Data Template URL"},
                "ClusterTemplateUrl": {"default": "Cluster Template URL"}
            }
        }
    })

    # -----------------------
    # Nested Stacks
    # -----------------------

    # 1. VPC Stack
    vpc_stack = t.add_resource(Stack(
        "VpcStack",
        TemplateURL=Ref(VpcTemplateUrl),
        Parameters={
            "VpcCidr": Ref(VpcCidr),
            "AvailabilityZone1": Ref(AvailabilityZone1),
            "AvailabilityZone2": Ref(AvailabilityZone2)
        },
        Tags=Tags(
            Name=Sub("${AWS::StackName}-vpc"),
            Component="VPC"
        )
    ))

    # 2. Data Layer Stack
    data_stack = t.add_resource(Stack(
        "DataStack",
        TemplateURL=Ref(DataTemplateUrl),
        Parameters={
            "VpcId": GetAtt(vpc_stack, "Outputs.VpcId"),
            "PrivateSubnet1Id": GetAtt(vpc_stack, "Outputs.PrivateSubnet1Id"),
            "PrivateSubnet2Id": GetAtt(vpc_stack, "Outputs.PrivateSubnet2Id"),
            "NodeGroupSecurityGroupId": GetAtt(vpc_stack, "Outputs.NodeGroupSecurityGroupId"),
            "ApiKeysOverride": Ref(ApiKeysOverride),
            "StorageProfilesOverride": Ref(StorageProfilesOverride)
        },
        DependsOn=[vpc_stack.title],
        Tags=Tags(
            Name=Sub("${AWS::StackName}-data"),
            Component="DataLayer"
        )
    ))

    # 3. EKS Cluster Stack
    cluster_stack = t.add_resource(Stack(
        "ClusterStack",
        TemplateURL=Ref(ClusterTemplateUrl),
        Parameters={
            "VpcId": GetAtt(vpc_stack, "Outputs.VpcId"),
            "PrivateSubnet1Id": GetAtt(vpc_stack, "Outputs.PrivateSubnet1Id"),
            "PrivateSubnet2Id": GetAtt(vpc_stack, "Outputs.PrivateSubnet2Id"),
            "ControlPlaneSecurityGroupId": GetAtt(vpc_stack, "Outputs.ControlPlaneSecurityGroupId"),
            "NodeGroupSecurityGroupId": GetAtt(vpc_stack, "Outputs.NodeGroupSecurityGroupId"),
            "DbEndpoint": GetAtt(data_stack, "Outputs.DbEndpoint"),
            "DbPort": GetAtt(data_stack, "Outputs.DbPort"),
            "DbSecretArn": GetAtt(data_stack, "Outputs.DbSecretArn"),
            "BucketName": GetAtt(data_stack, "Outputs.BucketName"),
            "QueueUrl": GetAtt(data_stack, "Outputs.QueueUrl"),
            "NodeInstanceType": Ref(NodeInstanceType),
            "NodeGroupMinSize": Ref(NodeGroupMinSize),
            "NodeGroupMaxSize": Ref(NodeGroupMaxSize),
            "NodeGroupDesiredSize": Ref(NodeGroupDesiredSize)
        },
        DependsOn=[data_stack.title],
        Tags=Tags(
            Name=Sub("${AWS::StackName}-cluster"),
            Component="EKSCluster"
        )
    ))

    # -----------------------
    # Outputs
    # -----------------------
    t.add_output(Output(
        "VpcStackId",
        Value=Ref(vpc_stack),
        Description="VPC stack ID"
    ))

    t.add_output(Output(
        "VpcId",
        Value=GetAtt(vpc_stack, "Outputs.VpcId"),
        Description="VPC ID"
    ))

    t.add_output(Output(
        "DataStackId",
        Value=Ref(data_stack),
        Description="Data layer stack ID"
    ))

    t.add_output(Output(
        "DatabaseEndpoint",
        Value=GetAtt(data_stack, "Outputs.DbEndpoint"),
        Description="RDS database endpoint"
    ))

    t.add_output(Output(
        "S3BucketName",
        Value=GetAtt(data_stack, "Outputs.BucketName"),
        Description="S3 bucket name for data ingestion"
    ))

    t.add_output(Output(
        "ClusterStackId",
        Value=Ref(cluster_stack),
        Description="EKS cluster stack ID"
    ))

    t.add_output(Output(
        "ClusterRoleArn",
        Value=GetAtt(cluster_stack, "Outputs.ClusterRoleArn"),
        Description="EKS cluster service role ARN"
    ))

    t.add_output(Output(
        "NodeGroupRoleArn",
        Value=GetAtt(cluster_stack, "Outputs.NodeGroupRoleArn"),
        Description="EKS node group role ARN"
    ))

    # Deployment instructions
    t.add_output(Output(
        "DeploymentInstructions",
        Value=Sub("""
Production EKS deployment completed successfully!

Next steps:
1. Connect to the EKS cluster: aws eks update-kubeconfig --region ${AWS::Region} --name ${AWS::StackName}-cluster
2. Verify KEDA is installed: kubectl get pods -n keda
3. Verify Lakerunner is deployed: kubectl get pods -n lakerunner
4. Check service endpoints: kubectl get services -n lakerunner

Database endpoint: ${DbEndpoint}
S3 bucket: ${BucketName}
        """,
        DbEndpoint=GetAtt(data_stack, "Outputs.DbEndpoint"),
        BucketName=GetAtt(data_stack, "Outputs.BucketName")),
        Description="Deployment completion instructions"
    ))

    return t

# Generate template
if __name__ == "__main__":
    template = create_production_template()
    print(template.to_yaml())