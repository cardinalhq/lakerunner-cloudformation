#!/usr/bin/env python3
"""
Lakerunner VPC CloudFormation Template

Creates a cost-effective VPC with essential VPC endpoints for private connectivity
to AWS services, avoiding internet egress charges for ECS, RDS, and other services.

Features:
- Public and private subnets across 2 AZs
- NAT Gateway for private subnet internet access (single AZ for cost optimization)
- S3 Gateway Endpoint (free)
- Interface endpoints for essential services:
  - Secrets Manager (for RDS passwords)
  - CloudWatch Logs (for ECS logging)
  - ECS (for container management)
- Security groups configured for VPC endpoints
"""

from troposphere import (
    Template, Parameter, Output, Ref, Sub, GetAtt, Export,
    Join, Select, Equals, If, GetAZs, And, Cidr
)
from troposphere.ec2 import (
    VPC, Subnet, RouteTable, Route, SubnetRouteTableAssociation,
    InternetGateway, VPCGatewayAttachment, NatGateway, EIP,
    SecurityGroup, SecurityGroupRule, VPCEndpoint
)

# Initialize template
t = Template()

t.set_description("Lakerunner VPC: Cost-optimized VPC with essential VPC endpoints for private AWS service access")


def standard_tags(resource_name, resource_type):
    """Generate standard tags for all resources."""
    return [
        {"Key": "Name", "Value": Sub(f"${{EnvironmentName}}-{resource_name}")},
        {"Key": "Component", "Value": "VPC"},
        {"Key": "ResourceType", "Value": resource_type},
        {"Key": "ManagedBy", "Value": "Lakerunner"},
        {"Key": "Environment", "Value": Ref(environment_name)},
    ]

# Parameters
vpc_cidr = t.add_parameter(Parameter(
    "VPCCidr",
    Type="String",
    Default="10.0.0.0/16",
    Description="CIDR block for the VPC",
    AllowedPattern=r"^(\d{1,3}\.){3}\d{1,3}/\d{1,2}$"
))

create_public_subnets = t.add_parameter(Parameter(
    "CreatePublicSubnets",
    Type="String",
    Default="Yes",
    AllowedValues=["Yes", "No"],
    Description="Create public subnets and internet gateway (No = private subnets only)"
))

create_nat = t.add_parameter(Parameter(
    "CreateNatGateway",
    Type="String",
    Default="Yes",
    AllowedValues=["Yes", "No"],
    Description="Create NAT Gateway for private subnet internet access (No = isolated private subnets, requires CreatePublicSubnets=Yes)"
))

environment_name = t.add_parameter(Parameter(
    "EnvironmentName",
    Type="String",
    Default="lakerunner",
    Description="Environment name for resource naming (typically the root stack name)",
    AllowedPattern=r"^[a-zA-Z][a-zA-Z0-9-]*$"
))

# Conditions
has_public_subnets = t.add_condition("CreatePublicSubnets", Equals(Ref(create_public_subnets), "Yes"))
has_nat = t.add_condition("CreateNatGateway", And(
    Equals(Ref(create_nat), "Yes"),
    Equals(Ref(create_public_subnets), "Yes")
))

# VPC
vpc = t.add_resource(VPC(
    "VPC",
    CidrBlock=Ref(vpc_cidr),
    EnableDnsHostnames=True,
    EnableDnsSupport=True,
    Tags=standard_tags("vpc", "VPC")
))

# Internet Gateway (conditional)
igw = t.add_resource(InternetGateway(
    "InternetGateway",
    Condition="CreatePublicSubnets",
    Tags=standard_tags("igw", "InternetGateway")
))

# Attach Internet Gateway to VPC (conditional)
vpc_gateway_attachment = t.add_resource(VPCGatewayAttachment(
    "VPCGatewayAttachment",
    Condition="CreatePublicSubnets",
    VpcId=Ref(vpc),
    InternetGatewayId=Ref(igw)
))

# Availability Zones (using first 2 AZs)
# Public Subnets (conditional)
public_subnet_1 = t.add_resource(Subnet(
    "PublicSubnet1",
    Condition="CreatePublicSubnets",
    VpcId=Ref(vpc),
    AvailabilityZone=Select("0", GetAZs()),
    CidrBlock=Select("0", Cidr(Ref(vpc_cidr), "4", "8")),
    MapPublicIpOnLaunch=True,
    Tags=standard_tags("public-1", "Subnet") + [
        {"Key": "Type", "Value": "Public"},
    ]
))

public_subnet_2 = t.add_resource(Subnet(
    "PublicSubnet2",
    Condition="CreatePublicSubnets",
    VpcId=Ref(vpc),
    AvailabilityZone=Select("1", GetAZs()),
    CidrBlock=Select("1", Cidr(Ref(vpc_cidr), "4", "8")),
    MapPublicIpOnLaunch=True,
    Tags=standard_tags("public-2", "Subnet") + [
        {"Key": "Type", "Value": "Public"},
    ]
))

# Private Subnets
private_subnet_1 = t.add_resource(Subnet(
    "PrivateSubnet1",
    VpcId=Ref(vpc),
    AvailabilityZone=Select("0", GetAZs()),
    CidrBlock=Select("2", Cidr(Ref(vpc_cidr), "4", "8")),
    MapPublicIpOnLaunch=False,
    Tags=standard_tags("private-1", "Subnet") + [
        {"Key": "Type", "Value": "Private"},
    ]
))

private_subnet_2 = t.add_resource(Subnet(
    "PrivateSubnet2",
    VpcId=Ref(vpc),
    AvailabilityZone=Select("1", GetAZs()),
    CidrBlock=Select("3", Cidr(Ref(vpc_cidr), "4", "8")),
    MapPublicIpOnLaunch=False,
    Tags=standard_tags("private-2", "Subnet") + [
        {"Key": "Type", "Value": "Private"},
    ]
))

# Public Route Table (conditional)
public_route_table = t.add_resource(RouteTable(
    "PublicRouteTable",
    Condition="CreatePublicSubnets",
    VpcId=Ref(vpc),
    Tags=standard_tags("public-rt", "RouteTable")
))

# Public Route (to Internet Gateway, conditional)
public_route = t.add_resource(Route(
    "PublicRoute",
    Condition="CreatePublicSubnets",
    RouteTableId=Ref(public_route_table),
    DestinationCidrBlock="0.0.0.0/0",
    GatewayId=Ref(igw),
    DependsOn="VPCGatewayAttachment"
))

# Associate Public Subnets with Public Route Table (conditional)
public_subnet_1_route_table_association = t.add_resource(SubnetRouteTableAssociation(
    "PublicSubnet1RouteTableAssociation",
    Condition="CreatePublicSubnets",
    SubnetId=Ref(public_subnet_1),
    RouteTableId=Ref(public_route_table)
))

public_subnet_2_route_table_association = t.add_resource(SubnetRouteTableAssociation(
    "PublicSubnet2RouteTableAssociation",
    Condition="CreatePublicSubnets",
    SubnetId=Ref(public_subnet_2),
    RouteTableId=Ref(public_route_table)
))

# NAT Gateway (conditional, single AZ for cost optimization)
nat_eip = t.add_resource(EIP(
    "NatEIP",
    Domain="vpc",
    Condition="CreateNatGateway"
))

nat_gateway = t.add_resource(NatGateway(
    "NatGateway",
    AllocationId=GetAtt(nat_eip, "AllocationId"),
    SubnetId=Ref(public_subnet_1),
    Condition="CreateNatGateway",
    Tags=standard_tags("nat-gw", "NatGateway")
))

# Private Route Table
private_route_table = t.add_resource(RouteTable(
    "PrivateRouteTable",
    VpcId=Ref(vpc),
    Tags=standard_tags("private-rt", "RouteTable")
))

# Private Route (to NAT Gateway, conditional)
private_route = t.add_resource(Route(
    "PrivateRoute",
    RouteTableId=Ref(private_route_table),
    DestinationCidrBlock="0.0.0.0/0",
    NatGatewayId=Ref(nat_gateway),
    Condition="CreateNatGateway"
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
    RouteTableIds=[Ref(private_route_table)]
))

# Interface Endpoints (essential services only for cost optimization)
# Secrets Manager - required for RDS password access
secrets_manager_endpoint = t.add_resource(VPCEndpoint(
    "SecretsManagerEndpoint",
    VpcId=Ref(vpc),
    ServiceName=Sub("com.amazonaws.${AWS::Region}.secretsmanager"),
    VpcEndpointType="Interface",
    SubnetIds=[Ref(private_subnet_1), Ref(private_subnet_2)],
    SecurityGroupIds=[Ref(vpc_endpoint_sg)],
    PrivateDnsEnabled=True,
))

# CloudWatch Logs - required for ECS logging
logs_endpoint = t.add_resource(VPCEndpoint(
    "LogsEndpoint",
    VpcId=Ref(vpc),
    ServiceName=Sub("com.amazonaws.${AWS::Region}.logs"),
    VpcEndpointType="Interface",
    SubnetIds=[Ref(private_subnet_1), Ref(private_subnet_2)],
    SecurityGroupIds=[Ref(vpc_endpoint_sg)],
    PrivateDnsEnabled=True,
))

# ECS - required for container management
ecs_endpoint = t.add_resource(VPCEndpoint(
    "EcsEndpoint",
    VpcId=Ref(vpc),
    ServiceName=Sub("com.amazonaws.${AWS::Region}.ecs"),
    VpcEndpointType="Interface",
    SubnetIds=[Ref(private_subnet_1), Ref(private_subnet_2)],
    SecurityGroupIds=[Ref(vpc_endpoint_sg)],
    PrivateDnsEnabled=True,
))

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

# ECR DKR - required for pulling Docker images
ecr_dkr_endpoint = t.add_resource(VPCEndpoint(
    "EcrDkrEndpoint",
    VpcId=Ref(vpc),
    ServiceName=Sub("com.amazonaws.${AWS::Region}.ecr.dkr"),
    VpcEndpointType="Interface",
    SubnetIds=[Ref(private_subnet_1), Ref(private_subnet_2)],
    SecurityGroupIds=[Ref(vpc_endpoint_sg)],
    PrivateDnsEnabled=True,
))

# Outputs for use by other stacks
t.add_output(Output(
    "VpcId",
    Description="VPC ID",
    Value=Ref(vpc),
    Export=Export(Sub("${AWS::StackName}-VpcId"))
))

t.add_output(Output(
    "VpcCidr",
    Description="VPC CIDR block",
    Value=Ref(vpc_cidr),
    Export=Export(Sub("${AWS::StackName}-VpcCidr"))
))

t.add_output(Output(
    "PublicSubnets",
    Condition="CreatePublicSubnets",
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
    Condition="CreatePublicSubnets",
    Description="Public subnet 1 ID",
    Value=Ref(public_subnet_1),
    Export=Export(Sub("${AWS::StackName}-PublicSubnet1"))
))

t.add_output(Output(
    "PublicSubnet2",
    Condition="CreatePublicSubnets",
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
    "InternetGatewayId",
    Condition="CreatePublicSubnets",
    Description="Internet Gateway ID",
    Value=Ref(igw),
    Export=Export(Sub("${AWS::StackName}-InternetGatewayId"))
))

t.add_output(Output(
    "NatGatewayId",
    Description="NAT Gateway ID (if created)",
    Value=If("CreateNatGateway", Ref(nat_gateway), "None"),
    Export=Export(Sub("${AWS::StackName}-NatGatewayId"))
))

t.add_output(Output(
    "VPCEndpointSecurityGroupId",
    Description="Security Group ID for VPC Endpoints",
    Value=Ref(vpc_endpoint_sg),
    Export=Export(Sub("${AWS::StackName}-VPCEndpointSGId"))
))

if __name__ == "__main__":
    print(t.to_yaml())
