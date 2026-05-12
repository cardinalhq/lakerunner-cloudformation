"""cert.yaml nested stack: optional TLS certificate for the ALB HTTPS listener.

No Lambda. If the customer supplies an existing ``CertificateArn`` it is
forwarded as-is. Otherwise the customer passes the certificate / private key
(and optional chain) as PEM strings and the stack creates an
``AWS::IAM::ServerCertificate`` from them -- an ALB HTTPS listener accepts an
IAM server certificate ARN exactly like an ACM one. Either way the output
``EffectiveCertificateArn`` is wired into the ALB child.
"""

from troposphere import (
    And,
    Equals,
    GetAtt,
    If,
    Not,
    Output,
    Parameter,
    Ref,
    Template,
)
from troposphere.iam import ServerCertificate

from cardinal_cfn.naming import cardinal_tags
from cardinal_cfn.parameters import add_install_id_parameters

AWS_NO_VALUE = "AWS::NoValue"


def build() -> Template:
    t = Template()
    t.set_description(
        "Cardinal cert: optional TLS certificate for the ALB HTTPS listener "
        "(an existing ACM/IAM cert ARN, or an IAM server certificate built from PEMs)."
    )

    add_install_id_parameters(t)

    t.add_parameter(Parameter(
        "CertificateArn",
        Type="String",
        Default="",
        Description=(
            "Existing ACM (or IAM server) certificate ARN. If empty, a "
            "certificate is built from CertificateBody + CertificatePrivateKey."
        ),
    ))
    t.add_parameter(Parameter(
        "CertificateBody",
        Type="String",
        Default="",
        NoEcho=True,
        Description="PEM-encoded certificate. Required when CertificateArn is empty.",
    ))
    t.add_parameter(Parameter(
        "CertificatePrivateKey",
        Type="String",
        Default="",
        NoEcho=True,
        Description="PEM-encoded private key. Required when CertificateArn is empty.",
    ))
    t.add_parameter(Parameter(
        "CertificateChain",
        Type="String",
        Default="",
        NoEcho=True,
        Description="Optional PEM-encoded chain of intermediate certificates.",
    ))

    # CertificateArn empty + a PEM body provided -> build an IAM server cert.
    t.add_condition(
        "CreateServerCert",
        And(Equals(Ref("CertificateArn"), ""),
            Not(Equals(Ref("CertificateBody"), ""))),
    )
    t.add_condition("HasCertChain", Not(Equals(Ref("CertificateChain"), "")))

    server_cert = t.add_resource(ServerCertificate(
        "ServerCertificate",
        Condition="CreateServerCert",
        CertificateBody=Ref("CertificateBody"),
        PrivateKey=Ref("CertificatePrivateKey"),
        CertificateChain=If("HasCertChain", Ref("CertificateChain"), Ref(AWS_NO_VALUE)),
        Tags=cardinal_tags(component="cert", role="server-cert"),
    ))

    t.add_output(Output(
        "EffectiveCertificateArn",
        Description="Certificate ARN to use on the ALB HTTPS listener.",
        Value=If(
            "CreateServerCert",
            GetAtt(server_cert, "Arn"),
            Ref("CertificateArn"),
        ),
    ))

    return t


if __name__ == "__main__":
    print(build().to_yaml())
