#!/usr/bin/env python3
"""Lakerunner Landscape CloudFormation Template - Part 1

Creates a "Lakerunner-compatible" VPC with all the networking components needed
for ECS and EKS deployments. This is the foundation layer that provides:
- VPC with public and private subnets across 2 AZs
- Internet Gateway and NAT Gateway for connectivity
- VPC Endpoints for AWS services (cost-optimized)
- Consistent naming and tagging

Deploy this first if you don't have an existing VPC, then use the outputs
in Part 2 (Common Infrastructure).
"""

import yaml
import os
from troposphere import (
    Template, Parameter, Output, Ref, Sub, GetAtt, Export, 
    Join, Select, Split, Condition, Equals, Not, If, GetAZs, And, Tags, Cidr
)
from troposphere.ec2 import (
    VPC, Subnet, RouteTable, Route, SubnetRouteTableAssociation,
    InternetGateway, VPCGatewayAttachment, NatGateway, EIP,
    SecurityGroup, SecurityGroupRule, VPCEndpoint
)


def load_defaults():
    """Load default configuration from YAML file."""
    config_path = os.path.join(os.path.dirname(__file__), '..', 'lakerunner-stack-defaults.yaml')
    with open(config_path, 'r') as f:
        return yaml.safe_load(f)


def standard_tags(resource_name, resource_type):
    """Generate standard tags for all resources."""
    return [
        {"Key": "Name", "Value": Sub(f"${{EnvironmentName}}-{resource_name}")},
        {"Key": "Component", "Value": "Landscape"},
        {"Key": "ResourceType", "Value": resource_type},
        {"Key": "ManagedBy", "Value": "Lakerunner"},
        {"Key": "Environment", "Value": Ref("EnvironmentName")},
    ]


# Initialize template
t = Template()
t.set_description("Lakerunner Landscape: Creates a complete VPC foundation for ECS/EKS deployments")

# Load defaults
defaults = load_defaults()

# =============================================================================
# Parameters
# =============================================================================

environment_name = t.add_parameter(Parameter(
    "EnvironmentName",
    Type="String",
    Default="lakerunner",
    Description="Environment name used for resource naming and tagging",
    AllowedPattern=r"^[a-zA-Z][a-zA-Z0-9-]*$",
    ConstraintDescription="Must start with a letter and contain only alphanumeric characters and hyphens"
))

vpc_cidr = t.add_parameter(Parameter(
    "VPCCidr",
    Type="String",
    Default="10.0.0.0/16",
    Description="CIDR block for the VPC (provides ~65k IPs)",
    AllowedPattern=r"^(\d{1,3}\.){3}\d{1,3}/\d{1,2}$",
    ConstraintDescription="Must be a valid CIDR block (e.g., 10.0.0.0/16)"
))

# =============================================================================
# Parameter Groups for CloudFormation Console
# =============================================================================

t.set_metadata({
    "AWS::CloudFormation::Interface": {
        "ParameterGroups": [
            {
                "Label": {"default": "Landscape Configuration"},
                "Parameters": ["EnvironmentName", "VPCCidr"]
            }
        ],
        "ParameterLabels": {
            "EnvironmentName": {"default": "Environment Name"},
            "VPCCidr": {"default": "VPC CIDR Block"}
        }
    }
})

# =============================================================================
# VPC and Core Networking
# =============================================================================

# VPC
vpc = t.add_resource(VPC(
    "VPC",
    CidrBlock=Ref(vpc_cidr),
    EnableDnsHostnames=True,
    EnableDnsSupport=True,
    Tags=standard_tags("vpc", "VPC")
))

# Internet Gateway
igw = t.add_resource(InternetGateway(
    "InternetGateway",
    Tags=standard_tags("igw", "InternetGateway")
))

# Attach Internet Gateway to VPC
vpc_gateway_attachment = t.add_resource(VPCGatewayAttachment(
    "VPCGatewayAttachment",
    VpcId=Ref(vpc),
    InternetGatewayId=Ref(igw)
))

# =============================================================================
# Subnets (Public and Private across 2 AZs)
# =============================================================================

# Public Subnets
public_subnet_1 = t.add_resource(Subnet(
    "PublicSubnet1",
    VpcId=Ref(vpc),
    AvailabilityZone=Select("0", GetAZs()),
    CidrBlock=Select("0", Cidr(Ref(vpc_cidr), "4", "8")),  # First /24 subnet
    MapPublicIpOnLaunch=True,
    Tags=standard_tags("public-1", "Subnet") + [
        {"Key": "Type", "Value": "Public"},
        {"Key": "kubernetes.io/role/elb", "Value": "1"}  # For EKS ALB
    ]
))

public_subnet_2 = t.add_resource(Subnet(
    "PublicSubnet2",
    VpcId=Ref(vpc),
    AvailabilityZone=Select("1", GetAZs()),
    CidrBlock=Select("1", Cidr(Ref(vpc_cidr), "4", "8")),  # Second /24 subnet
    MapPublicIpOnLaunch=True,
    Tags=standard_tags("public-2", "Subnet") + [
        {"Key": "Type", "Value": "Public"},
        {"Key": "kubernetes.io/role/elb", "Value": "1"}  # For EKS ALB
    ]
))

# Private Subnets
private_subnet_1 = t.add_resource(Subnet(
    "PrivateSubnet1",
    VpcId=Ref(vpc),
    AvailabilityZone=Select("0", GetAZs()),
    CidrBlock=Select("2", Cidr(Ref(vpc_cidr), "4", "8")),  # Third /24 subnet
    MapPublicIpOnLaunch=False,
    Tags=standard_tags("private-1", "Subnet") + [
        {"Key": "Type", "Value": "Private"},
        {"Key": "kubernetes.io/role/internal-elb", "Value": "1"}  # For EKS internal ALB
    ]
))

private_subnet_2 = t.add_resource(Subnet(
    "PrivateSubnet2",
    VpcId=Ref(vpc),
    AvailabilityZone=Select("1", GetAZs()),
    CidrBlock=Select("3", Cidr(Ref(vpc_cidr), "4", "8")),  # Fourth /24 subnet
    MapPublicIpOnLaunch=False,
    Tags=standard_tags("private-2", "Subnet") + [
        {"Key": "Type", "Value": "Private"},
        {"Key": "kubernetes.io/role/internal-elb", "Value": "1"}  # For EKS internal ALB
    ]
))

# =============================================================================
# Route Tables and Routes
# =============================================================================

# Public Route Table
public_route_table = t.add_resource(RouteTable(
    "PublicRouteTable",
    VpcId=Ref(vpc),
    Tags=standard_tags("public-rt", "RouteTable")
))

# Public Route (to Internet Gateway)
public_route = t.add_resource(Route(
    "PublicRoute",
    RouteTableId=Ref(public_route_table),
    DestinationCidrBlock="0.0.0.0/0",
    GatewayId=Ref(igw),
    DependsOn="VPCGatewayAttachment"
))

# Associate Public Subnets with Public Route Table
public_subnet_1_route_table_association = t.add_resource(SubnetRouteTableAssociation(
    "PublicSubnet1RouteTableAssociation",
    SubnetId=Ref(public_subnet_1),
    RouteTableId=Ref(public_route_table)
))

public_subnet_2_route_table_association = t.add_resource(SubnetRouteTableAssociation(
    "PublicSubnet2RouteTableAssociation",
    SubnetId=Ref(public_subnet_2),
    RouteTableId=Ref(public_route_table)
))

# NAT Gateway (single AZ for cost optimization)
nat_eip = t.add_resource(EIP(
    "NatEIP",
    Domain="vpc"
))

nat_gateway = t.add_resource(NatGateway(
    "NatGateway",
    AllocationId=GetAtt(nat_eip, "AllocationId"),
    SubnetId=Ref(public_subnet_1),
    Tags=standard_tags("nat-gw", "NatGateway")
))

# Private Route Table
private_route_table = t.add_resource(RouteTable(
    "PrivateRouteTable",
    VpcId=Ref(vpc),
    Tags=standard_tags("private-rt", "RouteTable")
))

# Private Route (to NAT Gateway for internet access)
private_route = t.add_resource(Route(
    "PrivateRoute",
    RouteTableId=Ref(private_route_table),
    DestinationCidrBlock="0.0.0.0/0",
    NatGatewayId=Ref(nat_gateway)
))

# Associate Private Subnets with Private Route Table
private_subnet_1_route_table_association = t.add_resource(SubnetRouteTableAssociation(
    "PrivateSubnet1RouteTableAssociation",
    SubnetId=Ref(private_subnet_1),
    RouteTableId=Ref(private_route_table)
))

private_subnet_2_route_table_association = t.add_resource(SubnetRouteTableAssociation(
    "PrivateSubnet2RouteTableAssociation",
    SubnetId=Ref(private_subnet_2),
    RouteTableId=Ref(private_route_table)
))

# =============================================================================
# VPC Endpoints (Cost-Optimized)
# =============================================================================

# Security Group for VPC Endpoints
vpc_endpoint_sg = t.add_resource(SecurityGroup(
    "VPCEndpointSecurityGroup",
    GroupDescription="Security group for VPC endpoints",
    VpcId=Ref(vpc),
    SecurityGroupIngress=[
        SecurityGroupRule(
            IpProtocol="tcp",
            FromPort=443,
            ToPort=443,
            CidrIp=Ref(vpc_cidr),
            Description="HTTPS from VPC"
        )
    ],
    SecurityGroupEgress=[
        SecurityGroupRule(
            IpProtocol="-1",
            CidrIp="0.0.0.0/0",
            Description="All outbound traffic"
        )
    ],
    Tags=standard_tags("vpce-sg", "SecurityGroup")
))

# S3 Gateway Endpoint (free, no data processing charges)
s3_gateway_endpoint = t.add_resource(VPCEndpoint(
    "S3GatewayEndpoint",
    VpcId=Ref(vpc),
    ServiceName=Sub("com.amazonaws.${AWS::Region}.s3"),
    VpcEndpointType="Gateway",
    RouteTableIds=[Ref(private_route_table)],
    PolicyDocument={
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": "*",
                "Action": [
                    "s3:GetObject",
                    "s3:PutObject",
                    "s3:DeleteObject",
                    "s3:ListBucket"
                ],
                "Resource": "*"
            }
        ]
    }
))

# Essential Interface Endpoints for ECS/EKS
# ECR API - required for pulling container images
ecr_api_endpoint = t.add_resource(VPCEndpoint(
    "EcrApiEndpoint",
    VpcId=Ref(vpc),
    ServiceName=Sub("com.amazonaws.${AWS::Region}.ecr.api"),
    VpcEndpointType="Interface",
    SubnetIds=[Ref(private_subnet_1), Ref(private_subnet_2)],
    SecurityGroupIds=[Ref(vpc_endpoint_sg)],
    PrivateDnsEnabled=True,
))

# ECR DKR - required for Docker image layers
ecr_dkr_endpoint = t.add_resource(VPCEndpoint(
    "EcrDkrEndpoint",
    VpcId=Ref(vpc),
    ServiceName=Sub("com.amazonaws.${AWS::Region}.ecr.dkr"),
    VpcEndpointType="Interface",
    SubnetIds=[Ref(private_subnet_1), Ref(private_subnet_2)],
    SecurityGroupIds=[Ref(vpc_endpoint_sg)],
    PrivateDnsEnabled=True,
))

# CloudWatch Logs - required for ECS/EKS logging
logs_endpoint = t.add_resource(VPCEndpoint(
    "LogsEndpoint",
    VpcId=Ref(vpc),
    ServiceName=Sub("com.amazonaws.${AWS::Region}.logs"),
    VpcEndpointType="Interface",
    SubnetIds=[Ref(private_subnet_1), Ref(private_subnet_2)],
    SecurityGroupIds=[Ref(vpc_endpoint_sg)],
    PrivateDnsEnabled=True,
))

# =============================================================================
# Outputs for use by Part 2 (Common Infrastructure)
# =============================================================================

t.add_output(Output(
    "VPCId",
    Description="VPC ID for use in Common Infrastructure",
    Value=Ref(vpc),
    Export=Export(Sub("${AWS::StackName}-VPCId"))
))

t.add_output(Output(
    "VPCCidr",
    Description="VPC CIDR block",
    Value=Ref(vpc_cidr),
    Export=Export(Sub("${AWS::StackName}-VPCCidr"))
))

t.add_output(Output(
    "PublicSubnets",
    Description="Public subnet IDs (comma-separated)",
    Value=Join(",", [Ref(public_subnet_1), Ref(public_subnet_2)]),
    Export=Export(Sub("${AWS::StackName}-PublicSubnets"))
))

t.add_output(Output(
    "PrivateSubnets",
    Description="Private subnet IDs (comma-separated)",
    Value=Join(",", [Ref(private_subnet_1), Ref(private_subnet_2)]),
    Export=Export(Sub("${AWS::StackName}-PrivateSubnets"))
))

t.add_output(Output(
    "PublicSubnet1",
    Description="Public subnet 1 ID",
    Value=Ref(public_subnet_1),
    Export=Export(Sub("${AWS::StackName}-PublicSubnet1"))
))

t.add_output(Output(
    "PublicSubnet2",
    Description="Public subnet 2 ID",
    Value=Ref(public_subnet_2),
    Export=Export(Sub("${AWS::StackName}-PublicSubnet2"))
))

t.add_output(Output(
    "PrivateSubnet1",
    Description="Private subnet 1 ID",
    Value=Ref(private_subnet_1),
    Export=Export(Sub("${AWS::StackName}-PrivateSubnet1"))
))

t.add_output(Output(
    "PrivateSubnet2",
    Description="Private subnet 2 ID",
    Value=Ref(private_subnet_2),
    Export=Export(Sub("${AWS::StackName}-PrivateSubnet2"))
))

t.add_output(Output(
    "VPCEndpointSecurityGroupId",
    Description="Security Group ID for VPC Endpoints",
    Value=Ref(vpc_endpoint_sg),
    Export=Export(Sub("${AWS::StackName}-VPCEndpointSGId"))
))

t.add_output(Output(
    "EnvironmentName",
    Description="Environment name for consistent resource naming",
    Value=Ref(environment_name),
    Export=Export(Sub("${AWS::StackName}-EnvironmentName"))
))


if __name__ == "__main__":
    print(t.to_yaml())