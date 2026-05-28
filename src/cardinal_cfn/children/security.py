"""security.yaml nested stack: SGs and IAM roles for the lakerunner tier.

Owns every security group and IAM role the lakerunner application needs,
so the customer no longer supplies any of them. The infra stack already
owns its RDS security group; this child adds ingress rules to it,
referencing the per-tier task SGs created here.

Resources created:

- 1 ALB SG (cardinal-alb-sg). Inbound 443 / 9443 / 4318 from
  ``AlbAllowedCidrs``; all egress.
- 6 task SGs, one per child tier:
    cardinal-svc-migration-sg, cardinal-svc-query-sg,
    cardinal-svc-process-sg,  cardinal-svc-control-sg,
    cardinal-svc-otel-sg,     cardinal-svc-maestro-sg
  Tier-specific ingress (from ALB SG, from sibling SGs, or self);
  all egress.
- 6 ``AWS::EC2::SecurityGroupIngress`` adds to the infra-supplied RDS
  SG (one per tier that needs DB access; otel does not).
- 1 shared ECS task execution role (cardinal-task-exec-role).
- 6 task roles, one per tier, each scoped to the exact AWS APIs the
  tier's services call.

Outputs all SG IDs and role ARNs so the other nested children can
consume them.
"""

from __future__ import annotations

from troposphere import (
    Equals,
    GetAtt,
    Not,
    Output,
    Parameter,
    Ref,
    Sub,
    Tags,
    Template,
)
from troposphere.ec2 import SecurityGroup, SecurityGroupIngress, SecurityGroupRule
from troposphere.iam import Policy, Role


PROJECT = "cardinal"
APPLICATION = "cardinal-lakerunner"
MANAGED_BY = "cardinal-cfn-security"


def _tags(*, component: str) -> Tags:
    return Tags(
        Application=APPLICATION,
        Project=PROJECT,
        ManagedBy=MANAGED_BY,
        Component=component,
        Name=f"cardinal-{component}",
    )


# --------------------------------------------------------------------------
# Service port table. Each entry lists the (container-port, description)
# pairs that the ALB connects to on a given tier. Health-check probes use
# the target-group port (i.e. the container port) unless a child template
# overrides HealthCheckPort, so a single rule per tier covers both.
# --------------------------------------------------------------------------
_ALB_INGRESS = [443, 9443, 4318]
_QUERY_API_PORT = 8080
_QUERY_WORKER_PORT = 8081
_ADMIN_API_PORT = 9091
_OTLP_HTTP_PORT = 4318
_OTEL_HEALTH_PORT = 13133  # otel.py's target group sets HealthCheckPort=13133
_MAESTRO_PORT = 4200
_MAESTRO_DEX_PORT = 5556


def build() -> Template:
    t = Template()
    t.set_description(
        "Cardinal lakerunner security: ALB SG, six per-tier task SGs, six "
        "per-tier task roles, one shared execution role, and ingress rules "
        "into the infra-owned RDS SG."
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
    # only need one or two can leave the rest blank without breaking
    # Fn::Select. Empty -> rule skipped via Condition.
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
            "ALB scheme. When 'internet-facing', the Security child "
            "automatically adds a 0.0.0.0/0 ingress rule on each ALB port "
            "in addition to the AlbAllowedCidr1/2/3 rules. Pure convenience "
            "so the operator doesn't have to remember to flip the CIDRs."
        ),
    ))
    t.add_parameter(Parameter(
        "RdsSecurityGroupId",
        Type="AWS::EC2::SecurityGroup::Id",
        Description=(
            "Security group ID attached to the RDS instance (output "
            "RdsSecurityGroupId from cardinal-infrastructure). This stack "
            "adds tier-specific 5432 ingress rules to it."
        ),
    ))
    t.add_parameter(Parameter(
        "ClusterArn",
        Type="String",
        Description="ECS cluster ARN. Used to scope IAM ecs:* actions.",
    ))
    t.add_parameter(Parameter(
        "BucketName",
        Type="String",
        Description="Name of the cardinal-infrastructure ingest bucket.",
    ))
    t.add_parameter(Parameter(
        "QueueArn",
        Type="String",
        Description="ARN of the cardinal-infrastructure ingest SQS queue.",
    ))
    t.add_parameter(Parameter(
        "DbMasterSecretArn",
        Type="String",
        Description="ARN of the RDS master credentials secret.",
    ))
    t.add_parameter(Parameter(
        "LicenseSecretArn",
        Type="String",
        Description="ARN of the cardinal-license secret.",
    ))
    t.add_parameter(Parameter(
        "AdminKeySecretArn",
        Type="String",
        Description="ARN of the cardinal-admin-key secret.",
    ))
    t.add_parameter(Parameter(
        "StorageProfilesParamName",
        Type="String",
        Description="SSM parameter name holding storage-profiles YAML.",
    ))
    t.add_parameter(Parameter(
        "ApiKeysParamName",
        Type="String",
        Description="SSM parameter name holding api-keys YAML.",
    ))

    # ----------------------------------------------------------------------
    # ALB SG
    # ----------------------------------------------------------------------
    alb_sg = t.add_resource(SecurityGroup(
        "AlbSecurityGroup",
        GroupDescription=(
            "Cardinal ALB (internal). Inbound 443 / 9443 / 4318 from "
            "AlbAllowedCidrs."
        ),
        VpcId=Ref(vpc_id),
        # Ingress is added via separate SecurityGroupIngress resources so
        # we can fan out one rule per (port, cidr) pair sourced from the
        # CommaDelimitedList parameter.
        SecurityGroupEgress=[SecurityGroupRule(
            IpProtocol="-1",
            CidrIp="0.0.0.0/0",
            Description="All egress",
        )],
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
    # on top of the AlbAllowedCidr1/2/3 rules above. Gated by an explicit
    # condition so deploying with the default (internal) doesn't surprise
    # the operator by exposing the ALB.
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
            SecurityGroupEgress=[SecurityGroupRule(
                IpProtocol="-1",
                CidrIp="0.0.0.0/0",
                Description="All egress",
            )],
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
    otel_sg = _task_sg(
        "OtelSecurityGroup",
        component="svc-otel-sg",
        description="Cardinal otel collector tier.",
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
    # query-api -> query-worker on 8081 (self-referential within the tier SG)
    t.add_resource(SecurityGroupIngress(
        "QueryWorkerFromQuery",
        GroupId=Ref(query_sg),
        SourceSecurityGroupId=Ref(query_sg),
        IpProtocol="tcp",
        FromPort=_QUERY_WORKER_PORT,
        ToPort=_QUERY_WORKER_PORT,
        Description="query-api to query-worker (same tier SG)",
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

    # ALB -> otel on 4318 (data plane) and 13133 (health check). The OTel
    # target group routes traffic to 4318 but health-checks 13133, so both
    # ports must be reachable from the ALB SG or the ECS task is marked
    # unhealthy and the deployment circuit breaker rolls the stack back.
    t.add_resource(SecurityGroupIngress(
        "OtelFromAlb",
        GroupId=Ref(otel_sg),
        SourceSecurityGroupId=Ref(alb_sg),
        IpProtocol="tcp",
        FromPort=_OTLP_HTTP_PORT,
        ToPort=_OTLP_HTTP_PORT,
        Description="ALB to otel-collector data plane",
    ))
    t.add_resource(SecurityGroupIngress(
        "OtelHealthFromAlb",
        GroupId=Ref(otel_sg),
        SourceSecurityGroupId=Ref(alb_sg),
        IpProtocol="tcp",
        FromPort=_OTEL_HEALTH_PORT,
        ToPort=_OTEL_HEALTH_PORT,
        Description="ALB health probe to otel-collector",
    ))
    # Lakerunner tasks -> otel on 4318 (self-telemetry from each tier)
    for tier_title, tier_ref in [
        ("Query", query_sg),
        ("Process", process_sg),
        ("Control", control_sg),
        ("Maestro", maestro_sg),
    ]:
        t.add_resource(SecurityGroupIngress(
            f"OtelFrom{tier_title}",
            GroupId=Ref(otel_sg),
            SourceSecurityGroupId=Ref(tier_ref),
            IpProtocol="tcp",
            FromPort=_OTLP_HTTP_PORT,
            ToPort=_OTLP_HTTP_PORT,
            Description=f"{tier_title} to otel-collector self-telemetry",
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

    # ----------------------------------------------------------------------
    # RDS ingress: each tier that talks to RDS gets 5432 into the
    # infra-supplied RDS SG.
    # ----------------------------------------------------------------------
    for tier_title, tier_ref in [
        ("Migration", migration_sg),
        ("Query", query_sg),
        ("Process", process_sg),
        ("Control", control_sg),
        ("Maestro", maestro_sg),
    ]:
        t.add_resource(SecurityGroupIngress(
            f"Rds5432From{tier_title}",
            GroupId=Ref("RdsSecurityGroupId"),
            SourceSecurityGroupId=Ref(tier_ref),
            IpProtocol="tcp",
            FromPort=5432,
            ToPort=5432,
            Description=f"{tier_title} to RDS 5432",
        ))

    # ----------------------------------------------------------------------
    # Shared ECS execution role
    # ----------------------------------------------------------------------
    exec_role = t.add_resource(Role(
        "ExecutionRole",
        AssumeRolePolicyDocument=_ecs_tasks_trust(),
        ManagedPolicyArns=[
            "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy",
        ],
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
                            # The DBMaster secret is CFN-generated and does NOT match
                            # the cardinal-* prefix, so list the actual ARNs the
                            # infrastructure stack hands us instead of a wildcard.
                            "Resource": [
                                Ref("DbMasterSecretArn"),
                                Ref("LicenseSecretArn"),
                                Ref("AdminKeySecretArn"),
                            ],
                        },
                        {
                            "Sid": "ResolveCardinalSsm",
                            "Effect": "Allow",
                            "Action": [
                                "ssm:GetParameter",
                                "ssm:GetParameters",
                            ],
                            "Resource": Sub(
                                "arn:${AWS::Partition}:ssm:${AWS::Region}:"
                                "${AWS::AccountId}:parameter/cardinal/*"
                            ),
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
                        _stmt_secrets_read(["DbMasterSecretArn"]),
                        _stmt_ssm_read([
                            "StorageProfilesParamName",
                            "ApiKeysParamName",
                        ]),
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
                        _stmt_secrets_read([
                            "DbMasterSecretArn",
                            "LicenseSecretArn",
                        ]),
                        _stmt_ssm_read([
                            "StorageProfilesParamName",
                            "ApiKeysParamName",
                        ]),
                        _stmt_s3_read(),
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
                        _stmt_secrets_read([
                            "DbMasterSecretArn",
                            "LicenseSecretArn",
                        ]),
                        _stmt_ssm_read([
                            "StorageProfilesParamName",
                            "ApiKeysParamName",
                        ]),
                        _stmt_s3_readwrite(),
                        _stmt_sqs_consume(),
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
                        _stmt_secrets_read([
                            "DbMasterSecretArn",
                            "LicenseSecretArn",
                            "AdminKeySecretArn",
                        ]),
                        _stmt_ssm_read([
                            "StorageProfilesParamName",
                            "ApiKeysParamName",
                        ]),
                        {
                            "Sid": "SweeperS3Cleanup",
                            "Effect": "Allow",
                            "Action": [
                                "s3:DeleteObject",
                                "s3:GetObject",
                                "s3:ListBucket",
                            ],
                            "Resource": [
                                Sub("arn:${AWS::Partition}:s3:::${BucketName}"),
                                Sub("arn:${AWS::Partition}:s3:::${BucketName}/*"),
                            ],
                        },
                        {
                            "Sid": "MonitoringEcsScale",
                            "Effect": "Allow",
                            "Action": [
                                "ecs:UpdateService",
                                "ecs:DescribeServices",
                            ],
                            "Resource": "*",
                            "Condition": {
                                "ArnEquals": {"ecs:cluster": Ref("ClusterArn")},
                            },
                        },
                        _stmt_cw_logs(),
                    ],
                },
            ),
        ],
        Tags=_tags(component="svc-control-role"),
    ))

    otel_role = t.add_resource(Role(
        "OtelRole",
        AssumeRolePolicyDocument=_ecs_tasks_trust(),
        Policies=[
            Policy(
                PolicyName="cardinal-svc-otel",
                PolicyDocument={
                    "Version": "2012-10-17",
                    "Statement": [
                        _stmt_secrets_read(["LicenseSecretArn"]),
                        # OTel writes raw OTLP signals under otel-raw/ in the
                        # ingest bucket via the awss3 exporter; the
                        # process-{logs,metrics,traces} tier reads them and
                        # writes cooked output under db/. Restrict to the
                        # write-side prefix only.
                        {
                            "Sid": "OtelRawWrite",
                            "Effect": "Allow",
                            "Action": ["s3:PutObject"],
                            "Resource": Sub(
                                "arn:${AWS::Partition}:s3:::${BucketName}/otel-raw/*"
                            ),
                        },
                        _stmt_cw_logs(),
                    ],
                },
            ),
        ],
        Tags=_tags(component="svc-otel-role"),
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
                        _stmt_secrets_read([
                            "DbMasterSecretArn",
                            "LicenseSecretArn",
                            "AdminKeySecretArn",
                        ]),
                        _stmt_ssm_read([
                            "StorageProfilesParamName",
                            "ApiKeysParamName",
                        ]),
                        _stmt_cw_logs(),
                    ],
                },
            ),
        ],
        Tags=_tags(component="svc-maestro-role"),
    ))

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
    _emit("OtelSecurityGroupId", "OTel tier SG id.", Ref(otel_sg))
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
    _emit("OtelRoleArn", "OTel tier task role ARN.",
          GetAtt(otel_role, "Arn"))
    _emit("MaestroRoleArn", "Maestro tier task role ARN.",
          GetAtt(maestro_role, "Arn"))

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


def _stmt_secrets_read(param_names: list[str]) -> dict:
    return {
        "Sid": "ReadSecrets",
        "Effect": "Allow",
        "Action": [
            "secretsmanager:GetSecretValue",
            "secretsmanager:DescribeSecret",
        ],
        "Resource": [Ref(n) for n in param_names],
    }


def _stmt_ssm_read(param_names: list[str]) -> dict:
    return {
        "Sid": "ReadSsmParams",
        "Effect": "Allow",
        "Action": [
            "ssm:GetParameter",
            "ssm:GetParameters",
        ],
        "Resource": [
            Sub(
                "arn:${AWS::Partition}:ssm:${AWS::Region}:"
                "${AWS::AccountId}:parameter${ParamName}",
                ParamName=Ref(n),
            )
            for n in param_names
        ],
    }


def _stmt_s3_read() -> dict:
    return {
        "Sid": "IngestBucketRead",
        "Effect": "Allow",
        "Action": [
            "s3:GetObject",
            "s3:ListBucket",
            "s3:GetBucketLocation",
        ],
        "Resource": [
            Sub("arn:${AWS::Partition}:s3:::${BucketName}"),
            Sub("arn:${AWS::Partition}:s3:::${BucketName}/*"),
        ],
    }


def _stmt_s3_readwrite() -> dict:
    return {
        "Sid": "IngestBucketReadWrite",
        "Effect": "Allow",
        "Action": [
            "s3:GetObject",
            "s3:PutObject",
            "s3:DeleteObject",
            "s3:ListBucket",
            "s3:GetBucketLocation",
        ],
        "Resource": [
            Sub("arn:${AWS::Partition}:s3:::${BucketName}"),
            Sub("arn:${AWS::Partition}:s3:::${BucketName}/*"),
        ],
    }


def _stmt_sqs_consume() -> dict:
    return {
        "Sid": "IngestQueueConsume",
        "Effect": "Allow",
        "Action": [
            "sqs:ReceiveMessage",
            "sqs:DeleteMessage",
            "sqs:GetQueueAttributes",
            "sqs:GetQueueUrl",
            "sqs:ChangeMessageVisibility",
        ],
        "Resource": Ref("QueueArn"),
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
    print(build().to_yaml())
