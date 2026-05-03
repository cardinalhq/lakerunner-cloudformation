"""cardinal-deployer-role: standalone IAM role for driving lakerunner stack updates.

A failed update on `cardinal-lakerunner` can wedge the stack in
UPDATE_ROLLBACK_FAILED if the calling identity lacks the perms CloudFormation
needs to roll an IAM-touching change back. The fix is to give CloudFormation
its own service role, scoped to exactly the actions the templates use, and
pass it via `aws cloudformation update-stack --role-arn`.

This template creates that role. Operators run it once per account, copy the
output ARN, and pass it to every subsequent update of the lakerunner stack.

Customers may equivalently create the role out-of-band (Terraform, ClickOps,
existing IAM tooling). In that case the template is just a guide to which
actions the role needs; the policy document below is the source of truth.
"""

from troposphere import GetAtt, Output, Parameter, Ref, Sub, Tags, Template
from troposphere.iam import Policy, Role


# Tightly scoped to what the lakerunner templates actually create. Grouped so a
# reader can map each statement back to the resource type it covers; trimming
# any group breaks the corresponding child stack.
_POLICY_STATEMENTS = [
    {
        "Sid": "CloudFormationSelf",
        "Effect": "Allow",
        "Action": [
            "cloudformation:CreateStack",
            "cloudformation:UpdateStack",
            "cloudformation:DeleteStack",
            "cloudformation:DescribeStacks",
            "cloudformation:DescribeStackEvents",
            "cloudformation:DescribeStackResource",
            "cloudformation:DescribeStackResources",
            "cloudformation:GetTemplate",
            "cloudformation:GetTemplateSummary",
            "cloudformation:ListStackResources",
            "cloudformation:CreateChangeSet",
            "cloudformation:DescribeChangeSet",
            "cloudformation:ExecuteChangeSet",
            "cloudformation:DeleteChangeSet",
            "cloudformation:ListChangeSets",
            "cloudformation:ContinueUpdateRollback",
            "cloudformation:SignalResource",
            "cloudformation:ValidateTemplate",
        ],
        "Resource": "*",
    },
    {
        "Sid": "Iam",
        "Effect": "Allow",
        "Action": [
            "iam:CreateRole",
            "iam:DeleteRole",
            "iam:GetRole",
            "iam:UpdateRole",
            "iam:UpdateAssumeRolePolicy",
            "iam:PutRolePolicy",
            "iam:DeleteRolePolicy",
            "iam:GetRolePolicy",
            "iam:ListRolePolicies",
            "iam:AttachRolePolicy",
            "iam:DetachRolePolicy",
            "iam:ListAttachedRolePolicies",
            "iam:PassRole",
            "iam:TagRole",
            "iam:UntagRole",
            "iam:ListRoleTags",
        ],
        "Resource": "*",
    },
    {
        "Sid": "Ecs",
        "Effect": "Allow",
        "Action": [
            "ecs:CreateCluster",
            "ecs:DeleteCluster",
            "ecs:DescribeClusters",
            "ecs:UpdateCluster",
            "ecs:TagResource",
            "ecs:UntagResource",
            "ecs:CreateService",
            "ecs:DeleteService",
            "ecs:DescribeServices",
            "ecs:UpdateService",
            "ecs:RegisterTaskDefinition",
            "ecs:DeregisterTaskDefinition",
            "ecs:DescribeTaskDefinition",
            "ecs:RunTask",
            "ecs:StopTask",
            "ecs:DescribeTasks",
            "ecs:ListTasks",
            "ecs:PutAccountSetting",
        ],
        "Resource": "*",
    },
    {
        "Sid": "Ec2NetworkingForVpcAndSgs",
        "Effect": "Allow",
        "Action": [
            "ec2:CreateSecurityGroup",
            "ec2:DeleteSecurityGroup",
            "ec2:DescribeSecurityGroups",
            "ec2:DescribeSecurityGroupRules",
            "ec2:AuthorizeSecurityGroupIngress",
            "ec2:AuthorizeSecurityGroupEgress",
            "ec2:RevokeSecurityGroupIngress",
            "ec2:RevokeSecurityGroupEgress",
            "ec2:UpdateSecurityGroupRuleDescriptionsIngress",
            "ec2:UpdateSecurityGroupRuleDescriptionsEgress",
            "ec2:DescribeVpcs",
            "ec2:DescribeSubnets",
            "ec2:DescribeAvailabilityZones",
            "ec2:DescribeAccountAttributes",
            "ec2:CreateTags",
            "ec2:DeleteTags",
            "ec2:DescribeTags",
        ],
        "Resource": "*",
    },
    {
        "Sid": "Elbv2",
        "Effect": "Allow",
        "Action": [
            "elasticloadbalancing:CreateLoadBalancer",
            "elasticloadbalancing:DeleteLoadBalancer",
            "elasticloadbalancing:DescribeLoadBalancers",
            "elasticloadbalancing:DescribeLoadBalancerAttributes",
            "elasticloadbalancing:ModifyLoadBalancerAttributes",
            "elasticloadbalancing:CreateListener",
            "elasticloadbalancing:DeleteListener",
            "elasticloadbalancing:DescribeListeners",
            "elasticloadbalancing:ModifyListener",
            "elasticloadbalancing:CreateRule",
            "elasticloadbalancing:DeleteRule",
            "elasticloadbalancing:DescribeRules",
            "elasticloadbalancing:ModifyRule",
            "elasticloadbalancing:CreateTargetGroup",
            "elasticloadbalancing:DeleteTargetGroup",
            "elasticloadbalancing:DescribeTargetGroups",
            "elasticloadbalancing:DescribeTargetGroupAttributes",
            "elasticloadbalancing:ModifyTargetGroup",
            "elasticloadbalancing:ModifyTargetGroupAttributes",
            "elasticloadbalancing:AddTags",
            "elasticloadbalancing:RemoveTags",
            "elasticloadbalancing:DescribeTags",
        ],
        "Resource": "*",
    },
    {
        "Sid": "Rds",
        "Effect": "Allow",
        "Action": [
            "rds:CreateDBInstance",
            "rds:DeleteDBInstance",
            "rds:DescribeDBInstances",
            "rds:ModifyDBInstance",
            "rds:RebootDBInstance",
            "rds:CreateDBSubnetGroup",
            "rds:DeleteDBSubnetGroup",
            "rds:DescribeDBSubnetGroups",
            "rds:ModifyDBSubnetGroup",
            # Snapshot verbs are required for stack delete: the database has
            # DeletionPolicy: Snapshot, so CFN takes a final snapshot during
            # delete-stack. Without these, delete-stack fails with AccessDenied.
            "rds:CreateDBSnapshot",
            "rds:DescribeDBSnapshots",
            "rds:DeleteDBSnapshot",
            "rds:AddTagsToResource",
            "rds:RemoveTagsFromResource",
            "rds:ListTagsForResource",
        ],
        "Resource": "*",
    },
    {
        "Sid": "S3IngestBucket",
        "Effect": "Allow",
        "Action": [
            "s3:CreateBucket",
            "s3:DeleteBucket",
            "s3:GetBucketLocation",
            "s3:GetBucketTagging",
            "s3:PutBucketTagging",
            "s3:GetBucketVersioning",
            "s3:GetBucketPolicy",
            "s3:PutBucketPolicy",
            "s3:DeleteBucketPolicy",
            "s3:GetBucketNotification",
            "s3:PutBucketNotification",
            "s3:GetBucketNotificationConfiguration",
            "s3:PutBucketNotificationConfiguration",
            "s3:GetEncryptionConfiguration",
            "s3:PutEncryptionConfiguration",
            "s3:GetLifecycleConfiguration",
            "s3:PutLifecycleConfiguration",
            "s3:GetBucketPublicAccessBlock",
            "s3:PutBucketPublicAccessBlock",
            # Object-level verbs are required so an operator using this role
            # can drain the retained ingest bucket post-stack-delete.  Without
            # them DeleteBucket fails because S3 will not delete a non-empty
            # bucket and the role can't list/remove objects to empty it.
            "s3:ListBucket",
            "s3:ListBucketVersions",
            "s3:GetObject",
            "s3:GetObjectVersion",
            "s3:DeleteObject",
            "s3:DeleteObjectVersion",
            "s3:AbortMultipartUpload",
            "s3:ListMultipartUploadParts",
        ],
        "Resource": "*",
    },
    {
        "Sid": "Sqs",
        "Effect": "Allow",
        "Action": [
            "sqs:CreateQueue",
            "sqs:DeleteQueue",
            "sqs:GetQueueAttributes",
            "sqs:SetQueueAttributes",
            "sqs:GetQueueUrl",
            "sqs:ListQueues",
            "sqs:TagQueue",
            "sqs:UntagQueue",
            "sqs:ListQueueTags",
            "sqs:AddPermission",
            "sqs:RemovePermission",
        ],
        "Resource": "*",
    },
    {
        "Sid": "Secrets",
        "Effect": "Allow",
        "Action": [
            "secretsmanager:CreateSecret",
            "secretsmanager:DeleteSecret",
            "secretsmanager:DescribeSecret",
            "secretsmanager:UpdateSecret",
            "secretsmanager:GetSecretValue",
            "secretsmanager:PutSecretValue",
            "secretsmanager:GetRandomPassword",
            "secretsmanager:TagResource",
            "secretsmanager:UntagResource",
        ],
        "Resource": "*",
    },
    {
        "Sid": "SsmParameters",
        "Effect": "Allow",
        "Action": [
            "ssm:PutParameter",
            "ssm:GetParameter",
            "ssm:GetParameters",
            "ssm:DeleteParameter",
            "ssm:DescribeParameters",
            "ssm:ListTagsForResource",
            "ssm:AddTagsToResource",
            "ssm:RemoveTagsFromResource",
        ],
        "Resource": "*",
    },
    {
        "Sid": "Logs",
        "Effect": "Allow",
        "Action": [
            "logs:CreateLogGroup",
            "logs:DeleteLogGroup",
            "logs:DescribeLogGroups",
            "logs:PutRetentionPolicy",
            "logs:DeleteRetentionPolicy",
            "logs:TagLogGroup",
            "logs:UntagLogGroup",
            "logs:TagResource",
            "logs:UntagResource",
            "logs:ListTagsLogGroup",
            "logs:ListTagsForResource",
        ],
        "Resource": "*",
    },
    {
        "Sid": "LambdaForCustomResources",
        "Effect": "Allow",
        "Action": [
            "lambda:CreateFunction",
            "lambda:DeleteFunction",
            "lambda:GetFunction",
            "lambda:GetFunctionConfiguration",
            "lambda:UpdateFunctionCode",
            "lambda:UpdateFunctionConfiguration",
            "lambda:InvokeFunction",
            "lambda:ListVersionsByFunction",
            "lambda:TagResource",
            "lambda:UntagResource",
            "lambda:ListTags",
        ],
        "Resource": "*",
    },
    {
        "Sid": "CloudMap",
        "Effect": "Allow",
        "Action": [
            "servicediscovery:CreatePrivateDnsNamespace",
            "servicediscovery:DeleteNamespace",
            "servicediscovery:GetNamespace",
            "servicediscovery:ListNamespaces",
            "servicediscovery:CreateService",
            "servicediscovery:DeleteService",
            "servicediscovery:GetService",
            "servicediscovery:UpdateService",
            "servicediscovery:ListServices",
            "servicediscovery:TagResource",
            "servicediscovery:UntagResource",
            "servicediscovery:ListTagsForResource",
            "servicediscovery:GetOperation",
        ],
        "Resource": "*",
    },
    {
        # AWS::ServiceDiscovery::PrivateDnsNamespace creates a Route 53 private
        # hosted zone under the hood and associates it with the VPC. The
        # deployer principal needs the matching route53 perms or namespace
        # creation fails with AccessDenied on route53:CreateHostedZone.
        "Sid": "Route53ForPrivateDns",
        "Effect": "Allow",
        "Action": [
            "route53:CreateHostedZone",
            "route53:DeleteHostedZone",
            "route53:GetHostedZone",
            "route53:GetChange",
            "route53:ChangeResourceRecordSets",
            "route53:AssociateVPCWithHostedZone",
            "route53:DisassociateVPCFromHostedZone",
            "route53:ListHostedZonesByName",
            "route53:ListHostedZonesByVPC",
            "route53:CreateVPCAssociationAuthorization",
            "route53:DeleteVPCAssociationAuthorization",
            "route53:ChangeTagsForResource",
            "route53:ListTagsForResource",
        ],
        "Resource": "*",
    },
    {
        "Sid": "AcmForImportedCerts",
        "Effect": "Allow",
        "Action": [
            "acm:ImportCertificate",
            "acm:DeleteCertificate",
            "acm:DescribeCertificate",
            "acm:ListCertificates",
            "acm:AddTagsToCertificate",
            "acm:RemoveTagsFromCertificate",
            "acm:ListTagsForCertificate",
        ],
        "Resource": "*",
    },
]


def build() -> Template:
    t = Template()
    t.set_description(
        "Cardinal deployer role: an IAM role assumed by CloudFormation to "
        "create and update the cardinal-lakerunner stack. Pass the role ARN "
        "via `aws cloudformation update-stack --role-arn` so a failed update "
        "can roll back without re-requiring the operator's IAM perms."
    )

    t.add_parameter(
        Parameter(
            "RoleName",
            Type="String",
            Default="cardinal-cfn-deployer",
            Description="Name for the deployer IAM role.",
            AllowedPattern=r"^[\w+=,.@-]{1,64}$",
        )
    )

    role = t.add_resource(
        Role(
            "DeployerRole",
            RoleName=Ref("RoleName"),
            Description=(
                "Service role assumed by CloudFormation when creating or "
                "updating the cardinal-lakerunner stack."
            ),
            AssumeRolePolicyDocument={
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Principal": {"Service": "cloudformation.amazonaws.com"},
                        "Action": "sts:AssumeRole",
                    }
                ],
            },
            Policies=[
                Policy(
                    PolicyName="cardinal-cfn-deployer-policy",
                    PolicyDocument={
                        "Version": "2012-10-17",
                        "Statement": _POLICY_STATEMENTS,
                    },
                )
            ],
            Tags=Tags(
                Project="cardinal",
                Component="iam",
                Role="cfn-deployer",
                ManagedBy="cardinal-cfn",
            ),
        )
    )

    t.add_output(
        Output(
            "DeployerRoleArn",
            Description=(
                "ARN of the deployer role. Pass to `aws cloudformation "
                "update-stack --role-arn <this>` for the cardinal-lakerunner stack."
            ),
            Value=GetAtt(role, "Arn"),
        )
    )
    t.add_output(
        Output(
            "DeployerRoleName",
            Description="Name of the deployer role.",
            Value=Ref(role),
        )
    )
    t.add_output(
        Output(
            "ExampleUpdateCommand",
            Description="Example CLI invocation for updating the lakerunner stack via this role.",
            Value=Sub(
                "aws cloudformation update-stack "
                "--stack-name cardinal-lakerunner "
                "--role-arn ${DeployerRole.Arn} "
                "--use-previous-template "
                "--capabilities CAPABILITY_NAMED_IAM CAPABILITY_AUTO_EXPAND"
            ),
        )
    )

    return t


if __name__ == "__main__":
    print(build().to_yaml(), end="")
