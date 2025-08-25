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
    Join, Select, Split, Condition, Equals, Not, If, GetAZs
)
from troposphere.ec2 import (
    VPC, Subnet, RouteTable, Route, SubnetRouteTableAssociation,
    InternetGateway, VPCGatewayAttachment, NatGateway, EIP,
    SecurityGroup, SecurityGroupRule, VPCEndpoint
)

# Initialize template
t = Template()

t.set_description("Lakerunner VPC: Cost-optimized VPC with essential VPC endpoints for private AWS service access")

# Parameters
vpc_cidr = t.add_parameter(Parameter(
    "VpcCidr",
    Type="String",
    Default="10.0.0.0/16",
    Description="CIDR block for the VPC",
    AllowedPattern=r"^(\d{1,3}\.){3}\d{1,3}/\d{1,2}$"
))

create_nat = t.add_parameter(Parameter(
    "CreateNatGateway",
    Type="String",
    Default="Yes",
    AllowedValues=["Yes", "No"],
    Description="Create NAT Gateway for private subnet internet access (No = isolated private subnets)"
))

environment_name = t.add_parameter(Parameter(
    "EnvironmentName",
    Type="String",
    Default="lakerunner",
    Description="Environment name for resource naming",
    AllowedPattern=r"^[a-zA-Z][a-zA-Z0-9-]*$"
))

# Conditions
has_nat = t.add_condition("CreateNatGateway", Equals(Ref(create_nat), "Yes"))

# VPC
vpc = t.add_resource(VPC(
    "VPC",
    CidrBlock=Ref(vpc_cidr),
    EnableDnsHostnames=True,
    EnableDnsSupport=True,
    Tags=[
        {"Key": "Name", "Value": Sub("${EnvironmentName}-vpc")},
    ]
))

# Internet Gateway
igw = t.add_resource(InternetGateway(
    "InternetGateway",
    Tags=[
        {"Key": "Name", "Value": Sub("${EnvironmentName}-igw")},
    ]
))

# Attach Internet Gateway to VPC
vpc_gateway_attachment = t.add_resource(VPCGatewayAttachment(
    "VPCGatewayAttachment",
    VpcId=Ref(vpc),
    InternetGatewayId=Ref(igw)
))

# Availability Zones (using first 2 AZs)
# Public Subnets
public_subnet_1 = t.add_resource(Subnet(
    "PublicSubnet1",
    VpcId=Ref(vpc),
    AvailabilityZone=Select("0", GetAZs()),
    CidrBlock="10.0.1.0/24",
    MapPublicIpOnLaunch=True,
    Tags=[
        {"Key": "Name", "Value": Sub("${EnvironmentName}-public-1")},
        {"Key": "Type", "Value": "Public"},
    ]
))

public_subnet_2 = t.add_resource(Subnet(
    "PublicSubnet2", 
    VpcId=Ref(vpc),
    AvailabilityZone=Select("1", GetAZs()),
    CidrBlock="10.0.2.0/24",
    MapPublicIpOnLaunch=True,
    Tags=[
        {"Key": "Name", "Value": Sub("${EnvironmentName}-public-2")},
        {"Key": "Type", "Value": "Public"},
    ]
))

# Private Subnets  
private_subnet_1 = t.add_resource(Subnet(
    "PrivateSubnet1",
    VpcId=Ref(vpc),
    AvailabilityZone=Select("0", GetAZs()),
    CidrBlock="10.0.10.0/24",
    MapPublicIpOnLaunch=False,
    Tags=[
        {"Key": "Name", "Value": Sub("${EnvironmentName}-private-1")},
        {"Key": "Type", "Value": "Private"},
    ]
))

private_subnet_2 = t.add_resource(Subnet(
    "PrivateSubnet2",
    VpcId=Ref(vpc),
    AvailabilityZone=Select("1", GetAZs()),
    CidrBlock="10.0.11.0/24",
    MapPublicIpOnLaunch=False,
    Tags=[
        {"Key": "Name", "Value": Sub("${EnvironmentName}-private-2")},
        {"Key": "Type", "Value": "Private"},
    ]
))

# Public Route Table
public_route_table = t.add_resource(RouteTable(
    "PublicRouteTable",
    VpcId=Ref(vpc),
    Tags=[
        {"Key": "Name", "Value": Sub("${EnvironmentName}-public-rt")},
    ]
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
    Tags=[
        {"Key": "Name", "Value": Sub("${EnvironmentName}-nat-gw")},
    ]
))

# Private Route Table
private_route_table = t.add_resource(RouteTable(
    "PrivateRouteTable",
    VpcId=Ref(vpc),
    Tags=[
        {"Key": "Name", "Value": Sub("${EnvironmentName}-private-rt")},
    ]
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
    Tags=[
        {"Key": "Name", "Value": Sub("${EnvironmentName}-vpce-sg")},
    ]
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
    "InternetGatewayId",
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
    print(t.to_json())