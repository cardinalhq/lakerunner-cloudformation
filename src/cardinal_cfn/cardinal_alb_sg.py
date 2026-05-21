"""cardinal-alb-sg: standalone ALB security group.

A deliberately tiny, optional helper stack. Creates one named security
group meant for the shared ALB: inbound HTTPS (443) from the private
RFC1918 ranges and unrestricted egress. The resulting GroupId is fed to
the cardinal-lakerunner root as ``AlbSgId`` (which is independent of the
task security group, ``TaskSgId``).

The task SG must still allow inbound from this SG on each service port
(e.g. otel gRPC 4317) for ALB->task traffic; that ingress rule is owned by
whoever manages the task SG and is intentionally out of scope here.
"""

from troposphere import (
    GetAtt,
    Output,
    Parameter,
    Ref,
    Tags,
    Template,
)
from troposphere.ec2 import SecurityGroup, SecurityGroupRule


# Private RFC1918 ranges allowed inbound on 443.
_ALLOWED_CIDRS = ["10.0.0.0/8", "172.16.0.0/12"]
_HTTPS_PORT = 443


def build() -> Template:
    t = Template()
    t.set_description(
        "Cardinal ALB security group: HTTPS (443) inbound from the private "
        "RFC1918 ranges, unrestricted egress. Feed GroupId to the "
        "cardinal-lakerunner root as AlbSgId."
    )

    t.add_parameter(
        Parameter(
            "VpcId",
            Type="AWS::EC2::VPC::Id",
            Description="VPC the security group is created in.",
        )
    )
    t.add_parameter(
        Parameter(
            "SecurityGroupName",
            Type="String",
            Default="cardinal-alb-sg",
            Description="Name for the security group (GroupName and Name tag).",
        )
    )

    sg = t.add_resource(
        SecurityGroup(
            "AlbSecurityGroup",
            GroupName=Ref("SecurityGroupName"),
            GroupDescription="Cardinal ALB: HTTPS in from RFC1918, all egress.",
            VpcId=Ref("VpcId"),
            SecurityGroupIngress=[
                SecurityGroupRule(
                    IpProtocol="tcp",
                    FromPort=_HTTPS_PORT,
                    ToPort=_HTTPS_PORT,
                    CidrIp=cidr,
                    Description=f"HTTPS from {cidr}",
                )
                for cidr in _ALLOWED_CIDRS
            ],
            SecurityGroupEgress=[
                SecurityGroupRule(
                    IpProtocol="-1",
                    CidrIp="0.0.0.0/0",
                    Description="All egress",
                )
            ],
            Tags=Tags(Name=Ref("SecurityGroupName")),
        )
    )

    t.add_output(
        Output(
            "AlbSecurityGroupId",
            Description="Security group ID; pass to cardinal-lakerunner as AlbSgId.",
            Value=GetAtt(sg, "GroupId"),
        )
    )

    return t


if __name__ == "__main__":
    print(build().to_yaml())
