"""alb.yaml nested stack: ALB, HTTPS listener (443), ALB SG, TaskSG ingress."""

from troposphere import (
    Template,
    Parameter,
    Ref,
    GetAtt,
    Output,
    Split,
    If,
    Equals,
)
from troposphere.elasticloadbalancingv2 import (
    LoadBalancer,
    Listener,
    Action,
    FixedResponseConfig,
    Certificate,
)
from troposphere.ec2 import SecurityGroup, SecurityGroupIngress

from cardinal_cfn.naming import cardinal_tags
from cardinal_cfn.parameters import add_install_id_parameters
from cardinal_cfn.policies import apply_policy


def build() -> Template:
    t = Template()
    t.set_description(
        "Cardinal ALB: Application Load Balancer, HTTPS listener (443), ALB SG, TaskSG ingress."
    )

    add_install_id_parameters(t)

    t.add_parameter(
        Parameter(
            "VpcId",
            Type="AWS::EC2::VPC::Id",
            Description="VPC ID (forwarded from root).",
        )
    )
    t.add_parameter(
        Parameter(
            "PublicSubnetsCsv",
            Type="String",
            Default="",
            Description="Comma-separated public subnet IDs. Required when AlbScheme is internet-facing.",
        )
    )
    t.add_parameter(
        Parameter(
            "PrivateSubnetsCsv",
            Type="String",
            Description="Comma-separated private subnet IDs.",
        )
    )
    t.add_parameter(
        Parameter(
            "AlbScheme",
            Type="String",
            Default="internal",
            AllowedValues=["internal", "internet-facing"],
            Description="ALB scheme: internal (private subnets) or internet-facing (public subnets).",
        )
    )
    t.add_parameter(
        Parameter(
            "TaskSecurityGroupId",
            Type="AWS::EC2::SecurityGroup::Id",
            Description="ECS task security group ID from the cluster stack.",
        )
    )
    t.add_parameter(
        Parameter(
            "CertificateArn",
            Type="String",
            Description="ACM certificate ARN for the HTTPS listener. Must be a valid certificate ARN.",
        )
    )

    # Conditions
    t.add_condition("IsInternetFacing", Equals(Ref("AlbScheme"), "internet-facing"))

    alb_sg = t.add_resource(
        SecurityGroup(
            "AlbSG",
            GroupDescription="Cardinal ALB security group",
            VpcId=Ref("VpcId"),
            SecurityGroupIngress=[
                {
                    "IpProtocol": "tcp",
                    "FromPort": 443,
                    "ToPort": 443,
                    "CidrIp": "0.0.0.0/0",
                    "Description": "HTTPS from anywhere",
                }
            ],
            Tags=cardinal_tags(component="networking", role="alb-sg"),
        )
    )

    alb = t.add_resource(
        LoadBalancer(
            "Alb",
            Scheme=Ref("AlbScheme"),
            Subnets=If(
                "IsInternetFacing",
                Split(",", Ref("PublicSubnetsCsv")),
                Split(",", Ref("PrivateSubnetsCsv")),
            ),
            SecurityGroups=[Ref(alb_sg)],
            Type="application",
            Tags=cardinal_tags(component="networking", role="alb"),
        )
    )
    apply_policy(alb, "alb")

    listener = t.add_resource(
        Listener(
            "HttpsListener",
            LoadBalancerArn=Ref(alb),
            Port=443,
            Protocol="HTTPS",
            Certificates=[Certificate(CertificateArn=Ref("CertificateArn"))],
            DefaultActions=[
                Action(
                    Type="fixed-response",
                    FixedResponseConfig=FixedResponseConfig(
                        StatusCode="404",
                        ContentType="text/plain",
                        MessageBody="no listener rule matched",
                    ),
                )
            ],
        )
    )

    # Allow ALB to reach ECS tasks on all ports
    t.add_resource(
        SecurityGroupIngress(
            "AlbToTaskIngress",
            GroupId=Ref("TaskSecurityGroupId"),
            IpProtocol="tcp",
            FromPort=0,
            ToPort=65535,
            SourceSecurityGroupId=Ref(alb_sg),
            Description="Allow ALB to reach ECS tasks",
        )
    )

    t.add_output(Output("AlbArn", Value=Ref(alb)))
    t.add_output(Output("AlbDnsName", Value=GetAtt(alb, "DNSName")))
    t.add_output(Output("AlbSecurityGroupId", Value=Ref(alb_sg)))
    t.add_output(Output("HttpsListenerArn", Value=Ref(listener)))

    return t


if __name__ == "__main__":
    print(build().to_yaml())
