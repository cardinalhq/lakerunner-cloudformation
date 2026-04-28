"""cardinal-vpc: standalone VPC template.

Optional pre-step a customer can deploy if they don't already have a VPC.
The resulting VpcId / subnet CSVs / endpoint SG are then fed as inputs to
the cardinal-lakerunner root template.

Layout (minimum viable):
- 1 VPC, 2 public + 2 private subnets across 2 AZs
- Internet Gateway (always)
- NAT Gateway (optional, single AZ for cost)
- S3 Gateway Endpoint (free)
- Interface endpoints for Secrets Manager / Logs / ECS / ECR (optional)
"""

from troposphere import (
    Cidr,
    Equals,
    GetAtt,
    GetAZs,
    Join,
    Output,
    Parameter,
    Ref,
    Select,
    Sub,
    Tags,
    Template,
)
from troposphere.ec2 import (
    EIP,
    VPC,
    InternetGateway,
    NatGateway,
    Route,
    RouteTable,
    SecurityGroup,
    SecurityGroupRule,
    Subnet,
    SubnetRouteTableAssociation,
    VPCEndpoint,
    VPCGatewayAttachment,
)


def _vpc_tags(*, role: str) -> Tags:
    """Tag set for the standalone VPC.

    Uses EnvironmentName + StackName since this template has no InstallId
    (it is not a child of the lakerunner root).
    """
    return Tags(
        Name=Sub(f"${{EnvironmentName}}-{role}"),
        Project="cardinal",
        Component="networking",
        Role=role,
        ManagedBy="cardinal-cfn",
    )


def build() -> Template:
    t = Template()
    t.set_description(
        "Cardinal VPC: standalone VPC with public/private subnets across 2 AZs, "
        "optional NAT Gateway, S3 gateway endpoint, and optional interface endpoints."
    )

    # Parameters
    t.add_parameter(
        Parameter(
            "EnvironmentName",
            Type="String",
            Default="cardinal",
            Description="Environment name used in resource Name tags.",
            AllowedPattern=r"^[a-zA-Z][a-zA-Z0-9-]*$",
        )
    )
    t.add_parameter(
        Parameter(
            "VpcCidr",
            Type="String",
            Default="10.0.0.0/16",
            Description="CIDR block for the VPC.",
            AllowedPattern=r"^(\d{1,3}\.){3}\d{1,3}/\d{1,2}$",
        )
    )
    t.add_parameter(
        Parameter(
            "CreateNatGateway",
            Type="String",
            Default="Yes",
            AllowedValues=["Yes", "No"],
            Description=(
                "Create a NAT Gateway so private subnets can reach the internet. "
                "No = isolated private subnets (cheaper, but tasks need VPC endpoints)."
            ),
        )
    )
    t.add_parameter(
        Parameter(
            "CreateInterfaceEndpoints",
            Type="String",
            Default="No",
            AllowedValues=["Yes", "No"],
            Description=(
                "Create Interface VPC Endpoints (Secrets Manager, CloudWatch Logs, "
                "ECS, ECR API, ECR DKR). Adds ~$7/endpoint/month per AZ; default No."
            ),
        )
    )

    # Conditions
    t.add_condition("HasNat", Equals(Ref("CreateNatGateway"), "Yes"))
    t.add_condition(
        "HasInterfaceEndpoints", Equals(Ref("CreateInterfaceEndpoints"), "Yes")
    )

    # VPC
    vpc = t.add_resource(
        VPC(
            "VPC",
            CidrBlock=Ref("VpcCidr"),
            EnableDnsHostnames=True,
            EnableDnsSupport=True,
            Tags=_vpc_tags(role="vpc"),
        )
    )

    # Internet Gateway + attachment
    igw = t.add_resource(
        InternetGateway(
            "InternetGateway",
            Tags=_vpc_tags(role="igw"),
        )
    )
    t.add_resource(
        VPCGatewayAttachment(
            "VPCGatewayAttachment",
            VpcId=Ref(vpc),
            InternetGatewayId=Ref(igw),
        )
    )

    # Subnets: split VPC CIDR into 16 /20 subnets, take first 4
    public_subnet_1 = t.add_resource(
        Subnet(
            "PublicSubnet1",
            VpcId=Ref(vpc),
            AvailabilityZone=Select("0", GetAZs()),
            CidrBlock=Select("0", Cidr(Ref("VpcCidr"), 4, 8)),
            MapPublicIpOnLaunch=True,
            Tags=_vpc_tags(role="public-1"),
        )
    )
    public_subnet_2 = t.add_resource(
        Subnet(
            "PublicSubnet2",
            VpcId=Ref(vpc),
            AvailabilityZone=Select("1", GetAZs()),
            CidrBlock=Select("1", Cidr(Ref("VpcCidr"), 4, 8)),
            MapPublicIpOnLaunch=True,
            Tags=_vpc_tags(role="public-2"),
        )
    )
    private_subnet_1 = t.add_resource(
        Subnet(
            "PrivateSubnet1",
            VpcId=Ref(vpc),
            AvailabilityZone=Select("0", GetAZs()),
            CidrBlock=Select("2", Cidr(Ref("VpcCidr"), 4, 8)),
            MapPublicIpOnLaunch=False,
            Tags=_vpc_tags(role="private-1"),
        )
    )
    private_subnet_2 = t.add_resource(
        Subnet(
            "PrivateSubnet2",
            VpcId=Ref(vpc),
            AvailabilityZone=Select("1", GetAZs()),
            CidrBlock=Select("3", Cidr(Ref("VpcCidr"), 4, 8)),
            MapPublicIpOnLaunch=False,
            Tags=_vpc_tags(role="private-2"),
        )
    )

    # Public route table + default route to IGW
    public_rt = t.add_resource(
        RouteTable(
            "PublicRouteTable",
            VpcId=Ref(vpc),
            Tags=_vpc_tags(role="public-rt"),
        )
    )
    t.add_resource(
        Route(
            "PublicDefaultRoute",
            RouteTableId=Ref(public_rt),
            DestinationCidrBlock="0.0.0.0/0",
            GatewayId=Ref(igw),
            DependsOn="VPCGatewayAttachment",
        )
    )
    t.add_resource(
        SubnetRouteTableAssociation(
            "PublicSubnet1RouteAssoc",
            SubnetId=Ref(public_subnet_1),
            RouteTableId=Ref(public_rt),
        )
    )
    t.add_resource(
        SubnetRouteTableAssociation(
            "PublicSubnet2RouteAssoc",
            SubnetId=Ref(public_subnet_2),
            RouteTableId=Ref(public_rt),
        )
    )

    # NAT Gateway (conditional, single AZ)
    nat_eip = t.add_resource(
        EIP(
            "NatEIP",
            Domain="vpc",
            Condition="HasNat",
        )
    )
    nat_gw = t.add_resource(
        NatGateway(
            "NatGateway",
            AllocationId=GetAtt(nat_eip, "AllocationId"),
            SubnetId=Ref(public_subnet_1),
            Condition="HasNat",
            Tags=_vpc_tags(role="nat-gw"),
        )
    )

    # Private route table + conditional default route to NAT
    private_rt = t.add_resource(
        RouteTable(
            "PrivateRouteTable",
            VpcId=Ref(vpc),
            Tags=_vpc_tags(role="private-rt"),
        )
    )
    t.add_resource(
        Route(
            "PrivateDefaultRoute",
            RouteTableId=Ref(private_rt),
            DestinationCidrBlock="0.0.0.0/0",
            NatGatewayId=Ref(nat_gw),
            Condition="HasNat",
        )
    )
    t.add_resource(
        SubnetRouteTableAssociation(
            "PrivateSubnet1RouteAssoc",
            SubnetId=Ref(private_subnet_1),
            RouteTableId=Ref(private_rt),
        )
    )
    t.add_resource(
        SubnetRouteTableAssociation(
            "PrivateSubnet2RouteAssoc",
            SubnetId=Ref(private_subnet_2),
            RouteTableId=Ref(private_rt),
        )
    )

    # Security group for VPC endpoints
    vpce_sg = t.add_resource(
        SecurityGroup(
            "VpcEndpointSecurityGroup",
            GroupDescription="Cardinal VPC endpoints security group (HTTPS from VPC)",
            VpcId=Ref(vpc),
            SecurityGroupIngress=[
                SecurityGroupRule(
                    IpProtocol="tcp",
                    FromPort=443,
                    ToPort=443,
                    CidrIp=Ref("VpcCidr"),
                    Description="HTTPS from VPC",
                )
            ],
            SecurityGroupEgress=[
                SecurityGroupRule(
                    IpProtocol="-1",
                    CidrIp="0.0.0.0/0",
                    Description="All outbound traffic",
                )
            ],
            Tags=_vpc_tags(role="vpce-sg"),
        )
    )

    # S3 Gateway Endpoint (free, attached to private route table)
    t.add_resource(
        VPCEndpoint(
            "S3GatewayEndpoint",
            VpcId=Ref(vpc),
            ServiceName=Sub("com.amazonaws.${AWS::Region}.s3"),
            VpcEndpointType="Gateway",
            RouteTableIds=[Ref(private_rt)],
        )
    )

    # Optional Interface VPC Endpoints
    interface_services = [
        ("SecretsManagerEndpoint", "secretsmanager"),
        ("LogsEndpoint", "logs"),
        ("EcsEndpoint", "ecs"),
        ("EcrApiEndpoint", "ecr.api"),
        ("EcrDkrEndpoint", "ecr.dkr"),
    ]
    for logical_id, service in interface_services:
        t.add_resource(
            VPCEndpoint(
                logical_id,
                VpcId=Ref(vpc),
                ServiceName=Sub(f"com.amazonaws.${{AWS::Region}}.{service}"),
                VpcEndpointType="Interface",
                SubnetIds=[Ref(private_subnet_1), Ref(private_subnet_2)],
                SecurityGroupIds=[Ref(vpce_sg)],
                PrivateDnsEnabled=True,
                Condition="HasInterfaceEndpoints",
            )
        )

    # Outputs (no Export — customer copies values into the lakerunner root inputs)
    t.add_output(Output("VpcId", Description="VPC ID", Value=Ref(vpc)))
    t.add_output(
        Output(
            "PublicSubnetsCsv",
            Description="Comma-separated public subnet IDs.",
            Value=Join(",", [Ref(public_subnet_1), Ref(public_subnet_2)]),
        )
    )
    t.add_output(
        Output(
            "PrivateSubnetsCsv",
            Description="Comma-separated private subnet IDs.",
            Value=Join(",", [Ref(private_subnet_1), Ref(private_subnet_2)]),
        )
    )
    t.add_output(
        Output(
            "VpcEndpointSecurityGroupId",
            Description="Security group ID for VPC interface endpoints.",
            Value=Ref(vpce_sg),
        )
    )

    return t


if __name__ == "__main__":
    print(build().to_yaml(), end="")
