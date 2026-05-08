"""alb.yaml nested stack: ALB and HTTPS listeners (443 / 9443).

The ALB security group and the ALB-to-task ingress rules are customer-owned
(see ``docs/operations/required-roles.md``); this stack consumes the SG ID
and never creates or mutates security groups.
"""

from troposphere import (
    Template,
    Parameter,
    Ref,
    GetAtt,
    Output,
    Split,
)
from troposphere.elasticloadbalancingv2 import (
    LoadBalancer,
    Listener,
    Action,
    FixedResponseConfig,
    Certificate,
)

from cardinal_cfn.naming import cardinal_tags
from cardinal_cfn.parameters import add_install_id_parameters
from cardinal_cfn.policies import apply_policy


def build() -> Template:
    t = Template()
    t.set_description(
        "Cardinal ALB: Application Load Balancer and HTTPS listeners (443 / 9443)."
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
            "PrivateSubnetsCsv",
            Type="String",
            Description="Comma-separated private subnet IDs.",
        )
    )
    t.add_parameter(
        Parameter(
            "AlbSgId",
            Type="AWS::EC2::SecurityGroup::Id",
            Description="ALB security group ID (customer-supplied).",
        )
    )
    t.add_parameter(
        Parameter(
            "CertificateArn",
            Type="String",
            Description="ACM certificate ARN for the HTTPS listener. Must be a valid certificate ARN.",
        )
    )

    alb = t.add_resource(
        LoadBalancer(
            "Alb",
            Scheme="internal",
            Subnets=Split(",", Ref("PrivateSubnetsCsv")),
            SecurityGroups=[Ref("AlbSgId")],
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

    # Dedicated listener for the lakerunner admin-api. The admin-api binary
    # serves its embedded UI at "/" so we can't share a path-prefixed rule
    # on 443 without either breaking the API (no path stripping in ALB) or
    # breaking the UI's react-router. Giving admin-api its own listener at
    # 9443 lets the container see request paths verbatim. The default
    # action is a 503; admin-api owns a single catch-all rule on this
    # listener (registered from services_control.py).
    admin_listener = t.add_resource(
        Listener(
            "AdminHttpsListener",
            LoadBalancerArn=Ref(alb),
            Port=9443,
            Protocol="HTTPS",
            Certificates=[Certificate(CertificateArn=Ref("CertificateArn"))],
            DefaultActions=[
                Action(
                    Type="fixed-response",
                    FixedResponseConfig=FixedResponseConfig(
                        StatusCode="503",
                        ContentType="text/plain",
                        MessageBody="admin-api listener rule not registered",
                    ),
                )
            ],
        )
    )

    t.add_output(Output("AlbArn", Value=Ref(alb)))
    t.add_output(Output("AlbDnsName", Value=GetAtt(alb, "DNSName")))
    t.add_output(Output("HttpsListenerArn", Value=Ref(listener)))
    t.add_output(Output("AdminHttpsListenerArn", Value=Ref(admin_listener)))

    return t


if __name__ == "__main__":
    print(build().to_yaml())
