"""alb.yaml nested stack: ALB and listeners (HTTPS 443 / 9443, HTTP 4318).

The ALB security group and the ALB-to-task ingress rules are customer-owned
(see ``docs/operations/required-roles.md``); this stack consumes the SG ID
and never creates or mutates security groups.

The OTel listener is HTTP (not HTTPS): when the ALB is internal-scheme the
deployment model assumes the caller has VPC-layer reachability (peering /
TGW / VPN). Plain OTLP/HTTP on 4318 matches the target group's protocol and
removes the need to install the ALB cert on external senders.

The ``Scheme`` parameter selects between internal (default; production) and
internet-facing (for test/dev environments where browser access from outside
the VPC is required -- e.g. so OIDC redirects to the ALB's DNS resolve from
a developer's laptop). When internet-facing, the operator must supply public
subnets via the root's ``PublicSubnets`` parameter.
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
            "AlbSubnetsCsv",
            Type="String",
            Description=(
                "Comma-separated subnet IDs the ALB is attached to. Must be "
                "private subnets when Scheme=internal; public subnets when "
                "Scheme=internet-facing. Wired by the root template."
            ),
        )
    )
    t.add_parameter(
        Parameter(
            "Scheme",
            Type="String",
            Default="internal",
            AllowedValues=["internal", "internet-facing"],
            Description=(
                "ALB scheme. Default 'internal' keeps the ALB private and "
                "requires VPC-layer reachability (peering / TGW / VPN / SSM "
                "port-forward). Set 'internet-facing' for dev/test "
                "environments where browser access from outside the VPC is "
                "needed -- e.g. OIDC redirect-back-to-ALB flows."
            ),
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
            Scheme=Ref("Scheme"),
            Subnets=Split(",", Ref("AlbSubnetsCsv")),
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

    # Dedicated plain-HTTP listener for the OTel collector on the canonical
    # OTLP/HTTP port (4318). Internal-scheme ALB + caller-owned VPC
    # reachability means TLS termination at the ALB adds no value over the
    # underlying peering encryption; serving plain HTTP keeps SDK clients on
    # http://<alb>:4318 without needing the ALB cert in their trust store.
    # The collector rule (registered from otel.py) owns this listener; the
    # default 404 only fires for non-OTLP paths.
    otel_listener = t.add_resource(
        Listener(
            "OtelHttpListener",
            LoadBalancerArn=Ref(alb),
            Port=4318,
            Protocol="HTTP",
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

    t.add_output(Output("AlbArn", Value=Ref(alb)))
    t.add_output(Output("AlbDnsName", Value=GetAtt(alb, "DNSName")))
    t.add_output(Output("HttpsListenerArn", Value=Ref(listener)))
    t.add_output(Output("AdminHttpsListenerArn", Value=Ref(admin_listener)))
    t.add_output(Output("OtelHttpListenerArn", Value=Ref(otel_listener)))

    return t


if __name__ == "__main__":
    print(build().to_yaml())
