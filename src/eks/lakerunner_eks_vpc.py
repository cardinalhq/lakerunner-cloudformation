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
    Template, Parameter, Ref, Sub, GetAtt, Output, Select, Tags
)
from troposphere.ec2 import (
    VPC, Subnet, RouteTable, Route, SubnetRouteTableAssociation,
    InternetGateway, VPCGatewayAttachment, NatGateway, EIP,
    SecurityGroup, SecurityGroupIngress, VPCEndpoint
)

def create_vpc_template():
    """Create CloudFormation template for VPC and networking infrastructure"""

    t = Template()
    t.set_description("EKS VPC Infrastructure: VPC, private subnets, NAT gateways, and VPC endpoints")

    # -----------------------
    # Parameters
    # -----------------------
    VpcCidr = t.add_parameter(Parameter(
        "VpcCidr",
        Type="String",
        Default="10.0.0.0/16",
        Description="CIDR block for the VPC"
    ))

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

    # Parameter groups for console
    t.set_metadata({
        "AWS::CloudFormation::Interface": {
            "ParameterGroups": [
                {
                    "Label": {"default": "Network Configuration"},
                    "Parameters": ["VpcCidr", "AvailabilityZone1", "AvailabilityZone2"]
                }
            ],
            "ParameterLabels": {
                "VpcCidr": {"default": "VPC CIDR Block"},
                "AvailabilityZone1": {"default": "Availability Zone 1"},
                "AvailabilityZone2": {"default": "Availability Zone 2"}
            }
        }
    })

    # -----------------------
    # VPC
    # -----------------------
    vpc = t.add_resource(VPC(
        "VPC",
        CidrBlock=Ref(VpcCidr),
        EnableDnsHostnames=True,
        EnableDnsSupport=True,
        Tags=Tags(
            Name=Sub("${AWS::StackName}-vpc")
        )
    ))

    # -----------------------
    # Internet Gateway (for NAT gateways)
    # -----------------------
    igw = t.add_resource(InternetGateway(
        "InternetGateway",
        Tags=Tags(
            Name=Sub("${AWS::StackName}-igw")
        )
    ))

    t.add_resource(VPCGatewayAttachment(
        "VPCGatewayAttachment",
        VpcId=Ref(vpc),
        InternetGatewayId=Ref(igw)
    ))

    # -----------------------
    # Public Subnets (for NAT gateways only)
    # -----------------------
    public_subnet_1 = t.add_resource(Subnet(
        "PublicSubnet1",
        VpcId=Ref(vpc),
        CidrBlock="10.0.0.0/24",
        AvailabilityZone=Ref(AvailabilityZone1),
        MapPublicIpOnLaunch=True,
        Tags=Tags(
            Name=Sub("${AWS::StackName}-public-subnet-1"),
            SubnetType="Public"
        )
    ))

    public_subnet_2 = t.add_resource(Subnet(
        "PublicSubnet2",
        VpcId=Ref(vpc),
        CidrBlock="10.0.1.0/24",
        AvailabilityZone=Ref(AvailabilityZone2),
        MapPublicIpOnLaunch=True,
        Tags=Tags(
            Name=Sub("${AWS::StackName}-public-subnet-2"),
            SubnetType="Public"
        )
    ))

    # -----------------------
    # Private Subnets (for EKS nodes and pods)
    # -----------------------
    private_subnet_1 = t.add_resource(Subnet(
        "PrivateSubnet1",
        VpcId=Ref(vpc),
        CidrBlock="10.0.10.0/24",
        AvailabilityZone=Ref(AvailabilityZone1),
        Tags=Tags(
            Name=Sub("${AWS::StackName}-private-subnet-1"),
            SubnetType="Private",
            # EKS-specific tags for subnet discovery
            **{"kubernetes.io/role/internal-elb": "1"}
        )
    ))

    private_subnet_2 = t.add_resource(Subnet(
        "PrivateSubnet2",
        VpcId=Ref(vpc),
        CidrBlock="10.0.11.0/24",
        AvailabilityZone=Ref(AvailabilityZone2),
        Tags=Tags(
            Name=Sub("${AWS::StackName}-private-subnet-2"),
            SubnetType="Private",
            # EKS-specific tags for subnet discovery
            **{"kubernetes.io/role/internal-elb": "1"}
        )
    ))

    # -----------------------
    # NAT Gateways
    # -----------------------
    nat_eip_1 = t.add_resource(EIP(
        "NatEIP1",
        Domain="vpc",
        Tags=Tags(
            Name=Sub("${AWS::StackName}-nat-eip-1")
        )
    ))

    nat_eip_2 = t.add_resource(EIP(
        "NatEIP2",
        Domain="vpc",
        Tags=Tags(
            Name=Sub("${AWS::StackName}-nat-eip-2")
        )
    ))

    nat_gateway_1 = t.add_resource(NatGateway(
        "NatGateway1",
        AllocationId=GetAtt(nat_eip_1, "AllocationId"),
        SubnetId=Ref(public_subnet_1),
        Tags=Tags(
            Name=Sub("${AWS::StackName}-nat-gateway-1")
        )
    ))

    nat_gateway_2 = t.add_resource(NatGateway(
        "NatGateway2",
        AllocationId=GetAtt(nat_eip_2, "AllocationId"),
        SubnetId=Ref(public_subnet_2),
        Tags=Tags(
            Name=Sub("${AWS::StackName}-nat-gateway-2")
        )
    ))

    # -----------------------
    # Route Tables
    # -----------------------
    # Public route table
    public_route_table = t.add_resource(RouteTable(
        "PublicRouteTable",
        VpcId=Ref(vpc),
        Tags=Tags(
            Name=Sub("${AWS::StackName}-public-rt")
        )
    ))

    t.add_resource(Route(
        "PublicRoute",
        RouteTableId=Ref(public_route_table),
        DestinationCidrBlock="0.0.0.0/0",
        GatewayId=Ref(igw)
    ))

    t.add_resource(SubnetRouteTableAssociation(
        "PublicSubnet1RouteTableAssociation",
        SubnetId=Ref(public_subnet_1),
        RouteTableId=Ref(public_route_table)
    ))

    t.add_resource(SubnetRouteTableAssociation(
        "PublicSubnet2RouteTableAssociation",
        SubnetId=Ref(public_subnet_2),
        RouteTableId=Ref(public_route_table)
    ))

    # Private route tables (one per AZ for high availability)
    private_route_table_1 = t.add_resource(RouteTable(
        "PrivateRouteTable1",
        VpcId=Ref(vpc),
        Tags=Tags(
            Name=Sub("${AWS::StackName}-private-rt-1")
        )
    ))

    private_route_table_2 = t.add_resource(RouteTable(
        "PrivateRouteTable2",
        VpcId=Ref(vpc),
        Tags=Tags(
            Name=Sub("${AWS::StackName}-private-rt-2")
        )
    ))

    t.add_resource(Route(
        "PrivateRoute1",
        RouteTableId=Ref(private_route_table_1),
        DestinationCidrBlock="0.0.0.0/0",
        NatGatewayId=Ref(nat_gateway_1)
    ))

    t.add_resource(Route(
        "PrivateRoute2",
        RouteTableId=Ref(private_route_table_2),
        DestinationCidrBlock="0.0.0.0/0",
        NatGatewayId=Ref(nat_gateway_2)
    ))

    t.add_resource(SubnetRouteTableAssociation(
        "PrivateSubnet1RouteTableAssociation",
        SubnetId=Ref(private_subnet_1),
        RouteTableId=Ref(private_route_table_1)
    ))

    t.add_resource(SubnetRouteTableAssociation(
        "PrivateSubnet2RouteTableAssociation",
        SubnetId=Ref(private_subnet_2),
        RouteTableId=Ref(private_route_table_2)
    ))

    # -----------------------
    # Security Groups
    # -----------------------
    # EKS control plane security group
    control_plane_sg = t.add_resource(SecurityGroup(
        "ControlPlaneSecurityGroup",
        GroupDescription="Security group for EKS control plane",
        VpcId=Ref(vpc),
        Tags=Tags(
            Name=Sub("${AWS::StackName}-control-plane-sg")
        )
    ))

    # EKS node group security group
    node_group_sg = t.add_resource(SecurityGroup(
        "NodeGroupSecurityGroup",
        GroupDescription="Security group for EKS worker nodes",
        VpcId=Ref(vpc),
        SecurityGroupEgress=[{
            "IpProtocol": "-1",
            "CidrIp": "0.0.0.0/0",
            "Description": "Allow all outbound"
        }],
        Tags=Tags(
            Name=Sub("${AWS::StackName}-node-group-sg")
        )
    ))

    # Allow node-to-node communication
    t.add_resource(SecurityGroupIngress(
        "NodeToNodeSelf",
        GroupId=Ref(node_group_sg),
        IpProtocol="-1",
        SourceSecurityGroupId=Ref(node_group_sg),
        Description="Allow node-to-node communication"
    ))

    # Allow control plane to node communication
    t.add_resource(SecurityGroupIngress(
        "ControlPlaneToNode",
        GroupId=Ref(node_group_sg),
        IpProtocol="tcp",
        FromPort=443,
        ToPort=443,
        SourceSecurityGroupId=Ref(control_plane_sg),
        Description="Allow HTTPS from control plane to nodes"
    ))

    t.add_resource(SecurityGroupIngress(
        "ControlPlaneToNodeKubelet",
        GroupId=Ref(node_group_sg),
        IpProtocol="tcp",
        FromPort=10250,
        ToPort=10250,
        SourceSecurityGroupId=Ref(control_plane_sg),
        Description="Allow kubelet from control plane to nodes"
    ))

    # -----------------------
    # VPC Endpoints
    # -----------------------
    # S3 endpoint (gateway type)
    s3_endpoint = t.add_resource(VPCEndpoint(
        "S3Endpoint",
        VpcId=Ref(vpc),
        ServiceName=Sub("com.amazonaws.${AWS::Region}.s3"),
        VpcEndpointType="Gateway",
        RouteTableIds=[
            Ref(private_route_table_1),
            Ref(private_route_table_2)
        ]
    ))

    # ECR endpoints (interface type)
    ecr_dkr_endpoint = t.add_resource(VPCEndpoint(
        "ECRDkrEndpoint",
        VpcId=Ref(vpc),
        ServiceName=Sub("com.amazonaws.${AWS::Region}.ecr.dkr"),
        VpcEndpointType="Interface",
        SubnetIds=[Ref(private_subnet_1), Ref(private_subnet_2)],
        SecurityGroupIds=[Ref(node_group_sg)],
        PrivateDnsEnabled=True
    ))

    ecr_api_endpoint = t.add_resource(VPCEndpoint(
        "ECRApiEndpoint",
        VpcId=Ref(vpc),
        ServiceName=Sub("com.amazonaws.${AWS::Region}.ecr.api"),
        VpcEndpointType="Interface",
        SubnetIds=[Ref(private_subnet_1), Ref(private_subnet_2)],
        SecurityGroupIds=[Ref(node_group_sg)],
        PrivateDnsEnabled=True
    ))

    # CloudWatch Logs endpoint
    logs_endpoint = t.add_resource(VPCEndpoint(
        "LogsEndpoint",
        VpcId=Ref(vpc),
        ServiceName=Sub("com.amazonaws.${AWS::Region}.logs"),
        VpcEndpointType="Interface",
        SubnetIds=[Ref(private_subnet_1), Ref(private_subnet_2)],
        SecurityGroupIds=[Ref(node_group_sg)],
        PrivateDnsEnabled=True
    ))

    # STS endpoint (for IRSA)
    sts_endpoint = t.add_resource(VPCEndpoint(
        "STSEndpoint",
        VpcId=Ref(vpc),
        ServiceName=Sub("com.amazonaws.${AWS::Region}.sts"),
        VpcEndpointType="Interface",
        SubnetIds=[Ref(private_subnet_1), Ref(private_subnet_2)],
        SecurityGroupIds=[Ref(node_group_sg)],
        PrivateDnsEnabled=True
    ))

    # -----------------------
    # Outputs
    # -----------------------
    t.add_output(Output(
        "VpcId",
        Value=Ref(vpc),
        Description="VPC ID"
    ))

    t.add_output(Output(
        "PrivateSubnet1Id",
        Value=Ref(private_subnet_1),
        Description="Private Subnet 1 ID"
    ))

    t.add_output(Output(
        "PrivateSubnet2Id",
        Value=Ref(private_subnet_2),
        Description="Private Subnet 2 ID"
    ))

    t.add_output(Output(
        "PrivateSubnetIds",
        Value=Sub("${Subnet1},${Subnet2}",
                  Subnet1=Ref(private_subnet_1),
                  Subnet2=Ref(private_subnet_2)),
        Description="Private Subnet IDs (comma-separated)"
    ))

    t.add_output(Output(
        "ControlPlaneSecurityGroupId",
        Value=Ref(control_plane_sg),
        Description="EKS Control Plane Security Group ID"
    ))

    t.add_output(Output(
        "NodeGroupSecurityGroupId",
        Value=Ref(node_group_sg),
        Description="EKS Node Group Security Group ID"
    ))

    return t

# Generate template
if __name__ == "__main__":
    template = create_vpc_template()
    print(template.to_yaml())