"""cert.yaml nested stack: optional ACM cert importer custom resource.

Lets the customer pass cert/private-key/chain PEMs directly into the stack
parameters. A Lambda-backed custom resource imports them into ACM and the
output ``EffectiveCertificateArn`` is wired into the ALB child. If the
customer instead supplies an existing ``CertificateArn`` the Lambda is not
deployed and the existing ARN is forwarded as-is.
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
    Sub,
    Template,
)
from troposphere.awslambda import Code, Function
from troposphere.cloudformation import CustomResource
from troposphere.iam import Policy, Role
from troposphere.logs import LogGroup

from cardinal_cfn.children import cert_lambda
from cardinal_cfn.naming import cardinal_tags
from cardinal_cfn.parameters import add_install_id_parameters
from cardinal_cfn.policies import apply_policy


def build() -> Template:
    t = Template()
    t.set_description(
        "Cardinal cert: optional ACM certificate import (custom-resource Lambda)."
    )

    add_install_id_parameters(t)

    t.add_parameter(Parameter(
        "CertificateArn",
        Type="String",
        Default="",
        Description=(
            "Existing ACM certificate ARN. If empty, the cert is imported "
            "from CertificateBody + CertificatePrivateKey."
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

    t.add_condition(
        "ImportCert",
        And(Equals(Ref("CertificateArn"), ""),
            Not(Equals(Ref("CertificateBody"), ""))),
    )

    log_group = t.add_resource(LogGroup(
        "CertLambdaLogGroup",
        Condition="ImportCert",
        LogGroupName=Sub("/aws/lambda/cardinal-cert-${InstallIdLong}"),
        RetentionInDays=14,
    ))
    apply_policy(log_group, "log-group")

    lambda_role = t.add_resource(Role(
        "CertLambdaRole",
        Condition="ImportCert",
        AssumeRolePolicyDocument={
            "Version": "2012-10-17",
            "Statement": [{
                "Effect": "Allow",
                "Principal": {"Service": "lambda.amazonaws.com"},
                "Action": "sts:AssumeRole",
            }],
        },
        Policies=[Policy(
            PolicyName="cert-lambda-policy",
            PolicyDocument={
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Action": [
                            "logs:CreateLogGroup",
                            "logs:CreateLogStream",
                            "logs:PutLogEvents",
                        ],
                        "Resource": "*",
                    },
                    {
                        # Create has no ARN to scope to; ACM requires ImportCertificate
                        # on "*" for the initial import call.
                        "Effect": "Allow",
                        "Action": ["acm:ImportCertificate"],
                        "Resource": "*",
                    },
                    {
                        "Effect": "Allow",
                        "Action": [
                            "acm:DeleteCertificate",
                            "acm:AddTagsToCertificate",
                            "acm:RemoveTagsFromCertificate",
                        ],
                        "Resource": Sub(
                            "arn:${AWS::Partition}:acm:${AWS::Region}:"
                            "${AWS::AccountId}:certificate/*"
                        ),
                    },
                ],
            },
        )],
        Tags=cardinal_tags(component="cert", role="lambda-role"),
    ))

    cert_fn = t.add_resource(Function(
        "CertLambda",
        Condition="ImportCert",
        FunctionName=Sub("cardinal-cert-${InstallIdLong}"),
        Code=Code(ZipFile=cert_lambda.SOURCE),
        Runtime="python3.11",
        Handler="index.lambda_handler",
        Role=GetAtt(lambda_role, "Arn"),
        Timeout=900,
        Tags=cardinal_tags(component="cert", role="lambda"),
    ))

    custom = t.add_resource(CustomResource(
        "ImportedCertificate",
        Condition="ImportCert",
        ServiceToken=GetAtt(cert_fn, "Arn"),
        InstallIdLong=Ref("InstallIdLong"),
        CertificateBody=Ref("CertificateBody"),
        CertificatePrivateKey=Ref("CertificatePrivateKey"),
        CertificateChain=Ref("CertificateChain"),
    ))

    t.add_output(Output(
        "EffectiveCertificateArn",
        Description="ACM certificate ARN to use on the ALB HTTPS listener.",
        Value=If(
            "ImportCert",
            GetAtt(custom, "CertificateArn"),
            Ref("CertificateArn"),
        ),
    ))

    return t


if __name__ == "__main__":
    print(build().to_yaml())
