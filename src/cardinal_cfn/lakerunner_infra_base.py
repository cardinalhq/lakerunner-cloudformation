"""cardinal-lakerunner-infra-base: the IT/security-owned base stack.

Standalone stack deployed FIRST in the lakerunner install order
(base -> rds -> services). It owns the security and IAM surface plus the
durable cooked bucket and operator-managed config, but no RDS and no
ingest queue.

Resources created:

- 1 ALB SG (cardinal-alb-sg). Inbound 443 / 9443 / 4318 from the
  AlbAllowedCidr1/2/3 params; all egress.
- 5 task SGs, one per child tier (migration/query/process/control/
  maestro) with tier-specific inter-tier ingress; all egress.
- 1 shared ECS task execution role + 5 per-tier task roles.
- 1 cooked bucket (durable; cooked-only output; Retain).
- license + admin-key secrets (Retain, named cardinal-*).

No org-content SSM params: Lakerunner installs admin-key-only (the admin-api
binary seeds its first key from the cardinal-admin-key secret via
ADMIN_INITIAL_API_KEY), and Maestro is the sole owner of the org, its storage
line, and its ingest key -- provisioned at runtime through Lakerunner's
/api/v1/provision admin API.

Because base deploys before rds/services, its roles CANNOT reference
RDS/service ARNs. Instead they scope by NAME PATTERN:

- secrets -> arn:...:secret:cardinal-* (requires the rds master secret to
  be named cardinal-db-master and base's secrets cardinal-license /
  cardinal-admin-key).
- S3 -> the cooked bucket this stack creates (by name).
- process tier -> sts:AssumeRole on cardinal-satellite-access* (the poller
  assumes each satellite's access role, which carries the real S3/SQS perms;
  there is no local ingest queue here).

Outputs all SG IDs, role ARNs, the cooked bucket name, and the license/admin
secret ARNs so rds + services (wired by the deploy driver) can consume them.
"""

from __future__ import annotations

from troposphere import (
    Equals,
    GetAtt,
    If,
    Not,
    Output,
    Parameter,
    Ref,
    Split,
    Sub,
    Tags,
    Template,
)
from troposphere.ec2 import SecurityGroup, SecurityGroupIngress
from troposphere.iam import Policy, Role
from troposphere.s3 import (
    Bucket,
    BucketEncryption,
    PublicAccessBlockConfiguration,
    ServerSideEncryptionByDefault,
    ServerSideEncryptionRule,
)
from troposphere.secretsmanager import GenerateSecretString, Secret

from cardinal_cfn.parameters import add_no_echo_parameter, add_parameter_group_metadata


PROJECT = "cardinal"
APPLICATION = "cardinal-lakerunner"
MANAGED_BY = "cardinal-cfn-infra-base"


def _tags(*, component: str) -> Tags:
    return Tags(
        Application=APPLICATION,
        Project=PROJECT,
        ManagedBy=MANAGED_BY,
        Component=component,
        Name=f"cardinal-{component}",
    )


def _retain(resource):
    resource.DeletionPolicy = "Retain"
    resource.UpdateReplacePolicy = "Retain"
    return resource


# --------------------------------------------------------------------------
# Service port table (lifted from security.py). Each tier connects to the
# ALB (or sibling tiers) on these container ports.
# --------------------------------------------------------------------------
_ALB_INGRESS = [443, 9443, 4318]
_QUERY_API_PORT = 8080
# query-worker exposes 8081 (HTTP REST artifact fetch) and 8082 (gRPC
# control stream / Discovery bridge); both must be open in the QuerySG
# self-referential ingress.
_QUERY_WORKER_ARTIFACT_PORT = 8081
_QUERY_WORKER_PORT = 8082
_ADMIN_API_PORT = 9091
# lakerunner v1.39+ serves /healthz on a dedicated health-check port (env
# HEALTH_CHECK_PORT, default 8090) separate from the traffic port, so the ALB
# health probe for the LB-fronted services (admin-api, query-api) targets it.
_HEALTH_CHECK_PORT = 8090
_MAESTRO_PORT = 4200
_MAESTRO_DEX_PORT = 5556


def build() -> Template:
    t = Template()
    t.set_description(
        "Cardinal lakerunner infra base: ALB SG, six per-tier task SGs with "
        "inter-tier ingress, one shared ECS execution role, six per-tier task "
        "roles (name-pattern scoped), the durable cooked bucket, and the "
        "license/admin-key secrets. Lakerunner installs admin-key-only; Maestro "
        "owns org content via /api/v1/provision. "
        "Deploy first (base -> rds -> services); owns no RDS, no ingest queue."
    )

    # ----------------------------------------------------------------------
    # Parameters
    # ----------------------------------------------------------------------
    vpc_id = t.add_parameter(Parameter(
        "VpcId",
        Type="AWS::EC2::VPC::Id",
        Description="VPC ID the security groups are created in.",
    ))
    # ALB inbound CIDRs as three independent parameters so customers who
    # only need one or two can leave the rest blank; empty -> rule skipped
    # via Condition.
    t.add_parameter(Parameter(
        "AlbAllowedCidr1",
        Type="String",
        Default="10.0.0.0/8",
        AllowedPattern=r"^$|^\d{1,3}(\.\d{1,3}){3}/\d{1,2}$",
        Description="First CIDR block allowed inbound to the ALB.",
    ))
    t.add_parameter(Parameter(
        "AlbAllowedCidr2",
        Type="String",
        Default="172.16.0.0/12",
        AllowedPattern=r"^$|^\d{1,3}(\.\d{1,3}){3}/\d{1,2}$",
        Description="Second CIDR block allowed inbound to the ALB. Blank to skip.",
    ))
    t.add_parameter(Parameter(
        "AlbAllowedCidr3",
        Type="String",
        Default="192.168.0.0/16",
        AllowedPattern=r"^$|^\d{1,3}(\.\d{1,3}){3}/\d{1,2}$",
        Description="Third CIDR block allowed inbound to the ALB. Blank to skip.",
    ))
    t.add_parameter(Parameter(
        "AlbScheme",
        Type="String",
        Default="internal",
        AllowedValues=["internal", "internet-facing"],
        Description=(
            "ALB scheme. When 'internet-facing', this stack adds a 0.0.0.0/0 "
            "ingress rule on each ALB port in addition to the "
            "AlbAllowedCidr1/2/3 rules."
        ),
    ))
    t.add_parameter(Parameter(
        "ClusterArn",
        Type="String",
        Description="ECS cluster ARN. Used to scope IAM ecs:* actions.",
    ))
    cooked_bucket_name = t.add_parameter(Parameter(
        "CookedBucketName",
        Type="String",
        Default="",
        Description=(
            "Name for the durable cooked-output bucket. Leave blank to use "
            "the default cardinal-cooked-<account>-<region>."
        ),
        AllowedPattern=r"^$|^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$",
    ))
    t.add_parameter(Parameter(
        "ConfigureBucketPublicAccessBlock",
        Type="String",
        Default="false",
        AllowedValues=["false", "true"],
        Description=(
            "When 'true', set the cooked bucket's PublicAccessBlockConfiguration "
            "(all four flags). Default 'false' leaves it unset: new buckets are "
            "already covered by AWS account/bucket default Block Public Access, "
            "and setting it explicitly requires s3:PutBucketPublicAccessBlock."
        ),
    ))
    license_secret_name = t.add_parameter(Parameter(
        "LicenseSecretName",
        Type="String",
        Default="cardinal-license",
        Description=(
            "Secrets Manager name for the license secret. Must match the "
            "cardinal-* pattern the task roles grant."
        ),
        MinLength=1,
    ))
    admin_key_secret_name = t.add_parameter(Parameter(
        "AdminKeySecretName",
        Type="String",
        Default="cardinal-admin-key",
        Description=(
            "Secrets Manager name for the first-boot admin key secret. Must "
            "match the cardinal-* pattern the task roles grant."
        ),
        MinLength=1,
    ))
    license_data = add_no_echo_parameter(
        t,
        "LicenseData",
        description=(
            "Cardinal license token (z64:...). Stored verbatim as the string "
            "body of the license secret."
        ),
    )

    add_parameter_group_metadata(
        t,
        groups=[
            {
                "label": "Networking",
                "parameters": ["VpcId"],
            },
            {
                "label": "ALB ingress",
                "parameters": [
                    "AlbAllowedCidr1",
                    "AlbAllowedCidr2",
                    "AlbAllowedCidr3",
                    "AlbScheme",
                ],
            },
            {
                "label": "ECS",
                "parameters": ["ClusterArn"],
            },
            {
                "label": "Cooked storage",
                "parameters": ["CookedBucketName"],
            },
            {
                "label": "License",
                "parameters": ["LicenseData"],
            },
            {
                "label": "Names (advanced)",
                "parameters": [
                    "LicenseSecretName",
                    "AdminKeySecretName",
                ],
            },
        ],
        labels={
            "VpcId": "VPC for the security groups",
            "CookedBucketName": "Cooked bucket name (blank = default)",
            "LicenseData": "License token (z64:...)",
        },
    )

    # ----------------------------------------------------------------------
    # Conditions
    # ----------------------------------------------------------------------
    t.add_condition(
        "UseDefaultCookedBucketName",
        Equals(Ref(cooked_bucket_name), ""),
    )
    t.add_condition(
        "AddCookedBucketPublicAccessBlock",
        Equals(Ref("ConfigureBucketPublicAccessBlock"), "true"),
    )

    cooked_bucket_name_value = If(
        "UseDefaultCookedBucketName",
        Sub("cardinal-cooked-${AWS::AccountId}-${AWS::Region}"),
        Ref(cooked_bucket_name),
    )

    # ----------------------------------------------------------------------
    # ALB SG
    # ----------------------------------------------------------------------
    alb_sg = t.add_resource(SecurityGroup(
        "AlbSecurityGroup",
        GroupDescription=(
            "Cardinal ALB. Inbound 443 / 9443 / 4318 from AlbAllowedCidrs."
        ),
        VpcId=Ref(vpc_id),
        # No inline SecurityGroupEgress: AWS auto-creates an all-allow egress
        # rule on the SG, which is exactly what we want. Specifying it would
        # force CloudFormation to RevokeSecurityGroupEgress the default first,
        # an action some customer SCPs explicitly deny (VPC-destructive guard).
        Tags=_tags(component="alb-sg"),
    ))

    # Cross-product: ports x non-empty CIDRs. Each CIDR slot has its own
    # Condition; a blank slot skips its rules entirely.
    for cidr_idx in (1, 2, 3):
        condition_name = f"HasAlbCidr{cidr_idx}"
        t.add_condition(
            condition_name,
            Not(Equals(Ref(f"AlbAllowedCidr{cidr_idx}"), "")),
        )
        for port in _ALB_INGRESS:
            t.add_resource(SecurityGroupIngress(
                f"AlbIngress{port}From{cidr_idx}",
                Condition=condition_name,
                GroupId=Ref(alb_sg),
                IpProtocol="tcp",
                FromPort=port,
                ToPort=port,
                CidrIp=Ref(f"AlbAllowedCidr{cidr_idx}"),
                Description=f"ALB inbound on {port} from AlbAllowedCidr{cidr_idx}",
            ))

    # When Scheme=internet-facing, layer a 0.0.0.0/0 rule on every ALB port
    # on top of the AlbAllowedCidr rules. Gated by a condition so the default
    # (internal) doesn't surprise the operator by exposing the ALB.
    t.add_condition(
        "AlbIsInternetFacing",
        Equals(Ref("AlbScheme"), "internet-facing"),
    )
    for port in _ALB_INGRESS:
        t.add_resource(SecurityGroupIngress(
            f"AlbIngress{port}FromInternet",
            Condition="AlbIsInternetFacing",
            GroupId=Ref(alb_sg),
            IpProtocol="tcp",
            FromPort=port,
            ToPort=port,
            CidrIp="0.0.0.0/0",
            Description=f"ALB inbound on {port} from 0.0.0.0/0 (internet-facing)",
        ))

    # ----------------------------------------------------------------------
    # Task SGs (per tier)
    # ----------------------------------------------------------------------
    def _task_sg(title: str, *, component: str, description: str) -> SecurityGroup:
        return t.add_resource(SecurityGroup(
            title,
            GroupDescription=description,
            VpcId=Ref(vpc_id),
            # No inline SecurityGroupEgress: see AlbSecurityGroup above.
            Tags=_tags(component=component),
        ))

    migration_sg = _task_sg(
        "MigrationSecurityGroup",
        component="svc-migration-sg",
        description="Cardinal migration task. No inbound; one-shot DB migrator.",
    )
    query_sg = _task_sg(
        "QuerySecurityGroup",
        component="svc-query-sg",
        description="Cardinal query tier (query-api, query-worker).",
    )
    process_sg = _task_sg(
        "ProcessSecurityGroup",
        component="svc-process-sg",
        description="Cardinal process tier (process-{logs,metrics,traces}, pubsub-sqs).",
    )
    control_sg = _task_sg(
        "ControlSecurityGroup",
        component="svc-control-sg",
        description="Cardinal control tier (sweeper, monitoring, admin-api, alert-evaluator).",
    )
    maestro_sg = _task_sg(
        "MaestroSecurityGroup",
        component="svc-maestro-sg",
        description="Cardinal maestro tier (maestro UI, MCP gateway, DEX OIDC).",
    )

    # ----------------------------------------------------------------------
    # Per-tier inbound rules
    # ----------------------------------------------------------------------
    # ALB -> query-api on 8080
    t.add_resource(SecurityGroupIngress(
        "QueryFromAlb",
        GroupId=Ref(query_sg),
        SourceSecurityGroupId=Ref(alb_sg),
        IpProtocol="tcp",
        FromPort=_QUERY_API_PORT,
        ToPort=_QUERY_API_PORT,
        Description="ALB to query-api",
    ))
    # ALB -> query-api health server on 8090 (/healthz)
    t.add_resource(SecurityGroupIngress(
        "QueryHealthFromAlb",
        GroupId=Ref(query_sg),
        SourceSecurityGroupId=Ref(alb_sg),
        IpProtocol="tcp",
        FromPort=_HEALTH_CHECK_PORT,
        ToPort=_HEALTH_CHECK_PORT,
        Description="ALB to query-api health check",
    ))
    # query-api -> query-worker (self-referential). 8082 gRPC control stream,
    # 8081 HTTP REST artifact fetch.
    t.add_resource(SecurityGroupIngress(
        "QueryWorkerFromQuery",
        GroupId=Ref(query_sg),
        SourceSecurityGroupId=Ref(query_sg),
        IpProtocol="tcp",
        FromPort=_QUERY_WORKER_PORT,
        ToPort=_QUERY_WORKER_PORT,
        Description="query-api to query-worker gRPC control stream (same tier SG)",
    ))
    t.add_resource(SecurityGroupIngress(
        "QueryWorkerArtifactFromQuery",
        GroupId=Ref(query_sg),
        SourceSecurityGroupId=Ref(query_sg),
        IpProtocol="tcp",
        FromPort=_QUERY_WORKER_ARTIFACT_PORT,
        ToPort=_QUERY_WORKER_ARTIFACT_PORT,
        Description="query-api to query-worker HTTP artifact fetch (same tier SG)",
    ))

    # ALB -> admin-api on 9091
    t.add_resource(SecurityGroupIngress(
        "ControlAdminApiFromAlb",
        GroupId=Ref(control_sg),
        SourceSecurityGroupId=Ref(alb_sg),
        IpProtocol="tcp",
        FromPort=_ADMIN_API_PORT,
        ToPort=_ADMIN_API_PORT,
        Description="ALB to admin-api",
    ))
    # ALB -> admin-api health server on 8090 (/healthz)
    t.add_resource(SecurityGroupIngress(
        "ControlHealthFromAlb",
        GroupId=Ref(control_sg),
        SourceSecurityGroupId=Ref(alb_sg),
        IpProtocol="tcp",
        FromPort=_HEALTH_CHECK_PORT,
        ToPort=_HEALTH_CHECK_PORT,
        Description="ALB to admin-api health check",
    ))

    # Maestro -> lakerunner cross-tier calls via Cloud Map service discovery
    # (query-api 8080, admin-api 9091), bypassing the ALB.
    t.add_resource(SecurityGroupIngress(
        "QueryFromMaestro",
        GroupId=Ref(query_sg),
        SourceSecurityGroupId=Ref(maestro_sg),
        IpProtocol="tcp",
        FromPort=_QUERY_API_PORT,
        ToPort=_QUERY_API_PORT,
        Description="Maestro to query-api Cloud Map",
    ))
    t.add_resource(SecurityGroupIngress(
        "ControlAdminApiFromMaestro",
        GroupId=Ref(control_sg),
        SourceSecurityGroupId=Ref(maestro_sg),
        IpProtocol="tcp",
        FromPort=_ADMIN_API_PORT,
        ToPort=_ADMIN_API_PORT,
        Description="Maestro to admin-api Cloud Map",
    ))

    # ALB -> maestro UI on 4200 and dex on 5556
    t.add_resource(SecurityGroupIngress(
        "MaestroUiFromAlb",
        GroupId=Ref(maestro_sg),
        SourceSecurityGroupId=Ref(alb_sg),
        IpProtocol="tcp",
        FromPort=_MAESTRO_PORT,
        ToPort=_MAESTRO_PORT,
        Description="ALB to maestro UI",
    ))
    t.add_resource(SecurityGroupIngress(
        "MaestroDexFromAlb",
        GroupId=Ref(maestro_sg),
        SourceSecurityGroupId=Ref(alb_sg),
        IpProtocol="tcp",
        FromPort=_MAESTRO_DEX_PORT,
        ToPort=_MAESTRO_DEX_PORT,
        Description="ALB to maestro dex",
    ))

    # NOTE: security.py's Rds5432From* ingress rules and its RdsSecurityGroupId
    # param are intentionally DROPPED here. RDS ingress now lives in
    # lakerunner-infra-rds (which deploys after base and threads these task SG
    # IDs in as parameters).

    # ----------------------------------------------------------------------
    # Shared ECS execution role
    # ----------------------------------------------------------------------
    # Optional customer-supplied managed policies appended to the execution role
    # (e.g. ECR pull-through-cache import, cross-account ECR, KMS decrypt). The
    # deploy driver can build one from a pasted JSON policy, or the operator can
    # pass ready-made managed-policy ARNs directly (CSV) as they grow into ops.
    t.add_parameter(Parameter(
        "ExecutionRoleExtraPolicyArns",
        Type="String",
        Default="",
        Description=(
            "Optional comma-separated managed-policy ARNs to attach to the ECS "
            "task execution role, in addition to AmazonECSTaskExecutionRolePolicy."
        ),
    ))
    t.add_condition(
        "HasExecutionRoleExtraPolicies",
        Not(Equals(Ref("ExecutionRoleExtraPolicyArns"), "")),
    )
    exec_role = t.add_resource(Role(
        "ExecutionRole",
        AssumeRolePolicyDocument=_ecs_tasks_trust(),
        ManagedPolicyArns=If(
            "HasExecutionRoleExtraPolicies",
            Split(",", Sub(
                "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy,"
                "${ExecutionRoleExtraPolicyArns}"
            )),
            ["arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"],
        ),
        Policies=[
            Policy(
                PolicyName="cardinal-task-exec-extras",
                PolicyDocument={
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Sid": "ResolveCardinalSecrets",
                            "Effect": "Allow",
                            "Action": [
                                "secretsmanager:GetSecretValue",
                                "secretsmanager:DescribeSecret",
                            ],
                            # NAME-PATTERN DECOUPLING (diverges from security.py,
                            # which threaded Db/License/AdminKey secret ARN Refs):
                            # base deploys before rds, so it cannot reference the
                            # rds master secret ARN. Scope to the cardinal-*
                            # secret name pattern instead. Requires the rds master
                            # secret to be named cardinal-db-master (Amendment A)
                            # and base's secrets cardinal-license/cardinal-admin-key.
                            "Resource": _cardinal_secret_arn_pattern(),
                        },
                    ],
                },
            ),
        ],
        Tags=_tags(component="task-exec-role"),
    ))

    # ----------------------------------------------------------------------
    # Tier task roles
    # ----------------------------------------------------------------------
    migration_role = t.add_resource(Role(
        "MigrationRole",
        AssumeRolePolicyDocument=_ecs_tasks_trust(),
        Policies=[
            Policy(
                PolicyName="cardinal-svc-migration",
                PolicyDocument={
                    "Version": "2012-10-17",
                    "Statement": [
                        _stmt_secrets_read(),
                        _stmt_cw_logs(),
                    ],
                },
            ),
        ],
        Tags=_tags(component="svc-migration-role"),
    ))

    query_role = t.add_resource(Role(
        "QueryRole",
        AssumeRolePolicyDocument=_ecs_tasks_trust(),
        Policies=[
            Policy(
                PolicyName="cardinal-svc-query",
                PolicyDocument={
                    "Version": "2012-10-17",
                    "Statement": [
                        _stmt_secrets_read(),
                        _stmt_s3_read(cooked_bucket_name_value),
                        _stmt_cw_logs(),
                        {
                            "Sid": "DescribeWorkerTasks",
                            "Effect": "Allow",
                            "Action": [
                                "ecs:DescribeServices",
                                "ecs:DescribeTasks",
                                "ecs:ListTasks",
                            ],
                            "Resource": "*",
                            "Condition": {
                                "ArnEquals": {"ecs:cluster": Ref("ClusterArn")},
                            },
                        },
                    ],
                },
            ),
        ],
        Tags=_tags(component="svc-query-role"),
    ))

    process_role = t.add_resource(Role(
        "ProcessRole",
        AssumeRolePolicyDocument=_ecs_tasks_trust(),
        Policies=[
            Policy(
                PolicyName="cardinal-svc-process",
                PolicyDocument={
                    "Version": "2012-10-17",
                    "Statement": [
                        _stmt_secrets_read(),
                        _stmt_s3_readwrite(cooked_bucket_name_value),
                        # NAME-PATTERN DECOUPLING (diverges from security.py's
                        # local _stmt_sqs_consume on a threaded QueueArn): this
                        # stack owns no ingest queue. The lakerunner poller
                        # instead assumes each satellite's cross-account access
                        # role (named cardinal-satellite-access, Amendment B);
                        # that role carries the real S3/SQS perms.
                        {
                            "Sid": "AssumeSatelliteAccess",
                            "Effect": "Allow",
                            "Action": "sts:AssumeRole",
                            "Resource": Sub(
                                "arn:${AWS::Partition}:iam::*:role/"
                                "cardinal-satellite-access*"
                            ),
                        },
                        {
                            "Sid": "InvokeBedrockFoundationModels",
                            "Effect": "Allow",
                            "Action": [
                                "bedrock:InvokeModel",
                                "bedrock:InvokeModelWithResponseStream",
                            ],
                            "Resource": Sub(
                                "arn:${AWS::Partition}:bedrock:*::foundation-model/*"
                            ),
                        },
                        _stmt_cw_logs(),
                    ],
                },
            ),
        ],
        Tags=_tags(component="svc-process-role"),
    ))

    control_role = t.add_resource(Role(
        "ControlRole",
        AssumeRolePolicyDocument=_ecs_tasks_trust(),
        Policies=[
            Policy(
                PolicyName="cardinal-svc-control",
                PolicyDocument={
                    "Version": "2012-10-17",
                    "Statement": [
                        _stmt_secrets_read(),
                        {
                            "Sid": "SweeperS3Cleanup",
                            "Effect": "Allow",
                            "Action": [
                                "s3:DeleteObject",
                                "s3:GetObject",
                                "s3:ListBucket",
                            ],
                            # S3 targets the cooked bucket base creates (was the
                            # threaded BucketName param in security.py).
                            "Resource": [
                                Sub(
                                    "arn:${AWS::Partition}:s3:::${BucketName}",
                                    BucketName=cooked_bucket_name_value,
                                ),
                                Sub(
                                    "arn:${AWS::Partition}:s3:::${BucketName}/*",
                                    BucketName=cooked_bucket_name_value,
                                ),
                            ],
                        },
                        _stmt_cw_logs(),
                    ],
                },
            ),
        ],
        Tags=_tags(component="svc-control-role"),
    ))

    maestro_role = t.add_resource(Role(
        "MaestroRole",
        AssumeRolePolicyDocument=_ecs_tasks_trust(),
        Policies=[
            Policy(
                PolicyName="cardinal-svc-maestro",
                PolicyDocument={
                    "Version": "2012-10-17",
                    "Statement": [
                        _stmt_secrets_read(),
                        _stmt_cw_logs(),
                    ],
                },
            ),
        ],
        Tags=_tags(component="svc-maestro-role"),
    ))

    # ----------------------------------------------------------------------
    # Cooked bucket (durable; cooked-only output). Unlike the ingest bucket
    # in cardinal_infrastructure.py this has NO SQS, NO notification, NO
    # lifecycle expiry, and Retain — cooked output is the system of record.
    # ----------------------------------------------------------------------
    cooked_bucket = t.add_resource(
        _retain(
            Bucket(
                "CookedBucket",
                BucketName=cooked_bucket_name_value,
                PublicAccessBlockConfiguration=If(
                    "AddCookedBucketPublicAccessBlock",
                    PublicAccessBlockConfiguration(
                        BlockPublicAcls=True,
                        BlockPublicPolicy=True,
                        IgnorePublicAcls=True,
                        RestrictPublicBuckets=True,
                    ),
                    Ref("AWS::NoValue"),
                ),
                BucketEncryption=BucketEncryption(
                    ServerSideEncryptionConfiguration=[
                        ServerSideEncryptionRule(
                            ServerSideEncryptionByDefault=(
                                ServerSideEncryptionByDefault(
                                    SSEAlgorithm="AES256"
                                )
                            )
                        )
                    ]
                ),
                Tags=_tags(component="cooked-bucket"),
            )
        )
    )

    # ----------------------------------------------------------------------
    # Application secrets (license, admin-key). Named cardinal-* so the task
    # roles' name-pattern secret access resolves them.
    # ----------------------------------------------------------------------
    license_secret = t.add_resource(
        _retain(
            Secret(
                "LicenseSecret",
                Name=Ref(license_secret_name),
                Description="Cardinal lakerunner license token (z64:...).",
                SecretString=Ref(license_data),
                Tags=_tags(component="license"),
            )
        )
    )

    admin_key_secret = t.add_resource(
        _retain(
            Secret(
                "AdminKeySecret",
                Name=Ref(admin_key_secret_name),
                Description=(
                    "First-boot admin API key. JSON shape "
                    '{"key": "<random>"} so the ECS secret pointer '
                    '":key::" resolves at task launch.'
                ),
                GenerateSecretString=GenerateSecretString(
                    SecretStringTemplate="{}",
                    GenerateStringKey="key",
                    PasswordLength=64,
                    ExcludePunctuation=True,
                ),
                Tags=_tags(component="admin-key"),
            )
        )
    )

    # ----------------------------------------------------------------------
    # Outputs
    # ----------------------------------------------------------------------
    def _emit(name: str, description: str, value):
        t.add_output(Output(name, Description=description, Value=value))

    _emit("AlbSecurityGroupId", "ALB SG id.", Ref(alb_sg))
    _emit("MigrationSecurityGroupId", "Migration tier SG id.", Ref(migration_sg))
    _emit("QuerySecurityGroupId", "Query tier SG id.", Ref(query_sg))
    _emit("ProcessSecurityGroupId", "Process tier SG id.", Ref(process_sg))
    _emit("ControlSecurityGroupId", "Control tier SG id.", Ref(control_sg))
    _emit("MaestroSecurityGroupId", "Maestro tier SG id.", Ref(maestro_sg))

    _emit("ExecutionRoleArn", "Shared ECS task execution role ARN.",
          GetAtt(exec_role, "Arn"))
    _emit("MigrationRoleArn", "Migration tier task role ARN.",
          GetAtt(migration_role, "Arn"))
    _emit("QueryRoleArn", "Query tier task role ARN.",
          GetAtt(query_role, "Arn"))
    _emit("ProcessRoleArn", "Process tier task role ARN.",
          GetAtt(process_role, "Arn"))
    _emit("ControlRoleArn", "Control tier task role ARN.",
          GetAtt(control_role, "Arn"))
    _emit("MaestroRoleArn", "Maestro tier task role ARN.",
          GetAtt(maestro_role, "Arn"))

    _emit("CookedBucketName", "Name of the durable cooked-output bucket.",
          Ref(cooked_bucket))
    _emit("LicenseSecretArn", "ARN of the license secret.",
          Ref(license_secret))
    _emit("AdminKeySecretArn", "ARN of the first-boot admin key secret.",
          Ref(admin_key_secret))

    return t


# --------------------------------------------------------------------------
# IAM helpers
# --------------------------------------------------------------------------
def _ecs_tasks_trust() -> dict:
    return {
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Principal": {"Service": "ecs-tasks.amazonaws.com"},
            "Action": "sts:AssumeRole",
        }],
    }


def _cardinal_secret_arn_pattern():
    # Secrets Manager appends a random 6-char suffix to physical ARNs, so the
    # trailing wildcard matches cardinal-db-master, cardinal-license, and
    # cardinal-admin-key regardless of suffix.
    return Sub(
        "arn:${AWS::Partition}:secretsmanager:${AWS::Region}:"
        "${AWS::AccountId}:secret:cardinal-*"
    )


def _stmt_secrets_read() -> dict:
    # NAME-PATTERN DECOUPLING: security.py scoped this to threaded secret ARN
    # Refs (Db/License/AdminKey). base deploys before rds and creates only two
    # of the three secrets, so scope to the cardinal-* name pattern instead.
    return {
        "Sid": "ReadSecrets",
        "Effect": "Allow",
        "Action": [
            "secretsmanager:GetSecretValue",
            "secretsmanager:DescribeSecret",
        ],
        "Resource": _cardinal_secret_arn_pattern(),
    }


def _stmt_s3_read(bucket_name_value) -> dict:
    return {
        "Sid": "CookedBucketRead",
        "Effect": "Allow",
        "Action": [
            "s3:GetObject",
            "s3:ListBucket",
            "s3:GetBucketLocation",
        ],
        "Resource": [
            Sub("arn:${AWS::Partition}:s3:::${BucketName}",
                BucketName=bucket_name_value),
            Sub("arn:${AWS::Partition}:s3:::${BucketName}/*",
                BucketName=bucket_name_value),
        ],
    }


def _stmt_s3_readwrite(bucket_name_value) -> dict:
    return {
        "Sid": "CookedBucketReadWrite",
        "Effect": "Allow",
        "Action": [
            "s3:GetObject",
            "s3:PutObject",
            "s3:DeleteObject",
            "s3:ListBucket",
            "s3:GetBucketLocation",
        ],
        "Resource": [
            Sub("arn:${AWS::Partition}:s3:::${BucketName}",
                BucketName=bucket_name_value),
            Sub("arn:${AWS::Partition}:s3:::${BucketName}/*",
                BucketName=bucket_name_value),
        ],
    }


def _stmt_cw_logs() -> dict:
    return {
        "Sid": "CardinalLogStreams",
        "Effect": "Allow",
        "Action": [
            "logs:CreateLogStream",
            "logs:PutLogEvents",
            "logs:DescribeLogStreams",
        ],
        "Resource": Sub(
            "arn:${AWS::Partition}:logs:${AWS::Region}:"
            "${AWS::AccountId}:log-group:/cardinal/*"
        ),
    }


if __name__ == "__main__":
    print(build().to_yaml(), end="")
