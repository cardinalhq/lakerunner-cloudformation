"""services-process.yaml nested stack: lakerunner process-* and pubsub-sqs services.

Owns four ECS Fargate services that ingest from SQS and write to S3:

- pubsub-sqs (signal_type: common, fixed shape, replicas-only tunable)
- process-logs (signal_type: logs, replicas + memory tunable)
- process-metrics (signal_type: metrics, replicas + memory tunable)
- process-traces (signal_type: traces, replicas + memory tunable)

None of these services attach to the ALB. Replicas parameters set the maximum
desired count; the external monitoring service manages actual scaling. CPU
values come from cardinal-defaults.yaml directly.
"""

from troposphere import (
    GetAtt,
    Output,
    Parameter,
    Ref,
    Sub,
    Template,
)
from troposphere.ecs import Environment, Secret

from cardinal_cfn.children import services_common
from cardinal_cfn.defaults import load_defaults
from cardinal_cfn.images import add_image_override
from cardinal_cfn.parameters import (
    add_install_id_parameters,
    add_parameter_group_metadata,
)


# Env-var names the binary uses to find the SSM-parameter names for api_keys
# and storage_profiles. The binary resolves the values at startup; we only
# thread the parameter names.
_API_KEYS_ENV = "LRDB_API_KEYS_SSM_PARAM"
_STORAGE_PROFILES_ENV = "LRDB_STORAGE_PROFILES_SSM_PARAM"


def build() -> Template:
    t = Template()
    t.set_description(
        "Cardinal services-process: lakerunner pubsub-sqs and process-logs/"
        "metrics/traces ECS services. None attach to the ALB."
    )

    defaults = load_defaults()
    pubsub_cfg = defaults["services"]["lakerunner-pubsub-sqs"]
    logs_cfg = defaults["services"]["lakerunner-process-logs"]
    metrics_cfg = defaults["services"]["lakerunner-process-metrics"]
    traces_cfg = defaults["services"]["lakerunner-process-traces"]

    add_install_id_parameters(t)

    # ---------------------------------------------------------------------
    # Cross-stack inputs (forwarded from root)
    # ---------------------------------------------------------------------
    t.add_parameter(Parameter("ClusterArn", Type="String", Description="ECS cluster ARN."))
    t.add_parameter(
        Parameter(
            "TaskSecurityGroupId",
            Type="AWS::EC2::SecurityGroup::Id",
            Description="ECS task security group ID from the cluster stack.",
        )
    )
    t.add_parameter(
        Parameter("ExecutionRoleArn", Type="String", Description="ECS task execution role ARN.")
    )
    t.add_parameter(
        Parameter(
            "PrivateSubnetsCsv",
            Type="String",
            Description="Comma-separated private subnet IDs.",
        )
    )
    t.add_parameter(Parameter("DbEndpoint", Type="String", Description="RDS endpoint hostname."))
    t.add_parameter(Parameter("DbPort", Type="String", Default="5432", Description="RDS port."))
    t.add_parameter(
        Parameter("DbSecretArn", Type="String", Description="ARN of the DB master secret.")
    )
    t.add_parameter(
        Parameter("BucketName", Type="String", Description="Name of the ingest S3 bucket.")
    )
    t.add_parameter(Parameter("QueueUrl", Type="String", Description="URL of the ingest SQS queue."))
    t.add_parameter(Parameter("QueueArn", Type="String", Description="ARN of the ingest SQS queue."))
    t.add_parameter(
        Parameter(
            "LicenseSecretArn",
            Type="String",
            Description="ARN of the license Secrets Manager secret.",
        )
    )
    t.add_parameter(
        Parameter(
            "InternalServiceKeysSecretArn",
            Type="String",
            Description="ARN of the internal service keys (HMAC) Secrets Manager secret.",
        )
    )
    t.add_parameter(
        Parameter(
            "ApiKeysParamName",
            Type="String",
            Description="Name of the SSM parameter holding the api_keys YAML.",
        )
    )
    t.add_parameter(
        Parameter(
            "StorageProfilesParamName",
            Type="String",
            Description="Name of the SSM parameter holding the storage_profiles YAML.",
        )
    )

    # MigrationComplete is unused inside this stack on purpose. The root passes
    # the migration-stack output through this parameter; CloudFormation cannot
    # render this nested stack until the migration stack finishes producing
    # that output, so depending on the parameter is enough.
    t.add_parameter(
        Parameter(
            "MigrationComplete",
            Type="String",
            Description=(
                "Sentinel forwarded from the migration stack output. Forces this "
                "stack to wait for migration to finish; not used inside the stack."
            ),
        )
    )

    # ---------------------------------------------------------------------
    # Image override
    # ---------------------------------------------------------------------
    image_ref = add_image_override(
        t,
        name="LakerunnerImage",
        default=defaults["images"]["lakerunner"],
        description="Container image for all lakerunner services in this tier.",
    )

    # ---------------------------------------------------------------------
    # Per-service tunables (defaults from cardinal-defaults.yaml).
    #
    # Per the project CLAUDE.md table: process-* services expose Replicas and
    # Memory as parameters; CPU stays in YAML. pubsub-sqs exposes Replicas
    # only; CPU and Memory both stay in YAML.
    # ---------------------------------------------------------------------
    t.add_parameter(
        Parameter(
            "ProcessLogsReplicas",
            Type="Number",
            Default=str(_max_replicas(logs_cfg)),
            Description=(
                "Maximum desired replicas for lakerunner-process-logs. The "
                "monitoring service manages actual replica count up to this max."
            ),
        )
    )
    t.add_parameter(
        Parameter(
            "ProcessLogsMemory",
            Type="String",
            Default=str(logs_cfg["memory_mib"]),
            Description="Fargate memory (MiB) for lakerunner-process-logs.",
        )
    )
    t.add_parameter(
        Parameter(
            "ProcessMetricsReplicas",
            Type="Number",
            Default=str(_max_replicas(metrics_cfg)),
            Description=(
                "Maximum desired replicas for lakerunner-process-metrics. The "
                "monitoring service manages actual replica count up to this max."
            ),
        )
    )
    t.add_parameter(
        Parameter(
            "ProcessMetricsMemory",
            Type="String",
            Default=str(metrics_cfg["memory_mib"]),
            Description="Fargate memory (MiB) for lakerunner-process-metrics.",
        )
    )
    t.add_parameter(
        Parameter(
            "ProcessTracesReplicas",
            Type="Number",
            Default=str(_max_replicas(traces_cfg)),
            Description=(
                "Maximum desired replicas for lakerunner-process-traces. The "
                "monitoring service manages actual replica count up to this max."
            ),
        )
    )
    t.add_parameter(
        Parameter(
            "ProcessTracesMemory",
            Type="String",
            Default=str(traces_cfg["memory_mib"]),
            Description="Fargate memory (MiB) for lakerunner-process-traces.",
        )
    )
    t.add_parameter(
        Parameter(
            "PubsubSqsReplicas",
            Type="Number",
            Default=str(pubsub_cfg["replicas"]),
            Description="Desired replicas for lakerunner-pubsub-sqs.",
        )
    )

    # ---------------------------------------------------------------------
    # Console parameter grouping
    # ---------------------------------------------------------------------
    add_parameter_group_metadata(
        t,
        groups=[
            {
                "label": "Cross-stack inputs",
                "parameters": [
                    "InstallIdShort",
                    "InstallIdLong",
                    "ClusterArn",
                    "TaskSecurityGroupId",
                    "ExecutionRoleArn",
                    "PrivateSubnetsCsv",
                    "DbEndpoint",
                    "DbPort",
                    "DbSecretArn",
                    "BucketName",
                    "QueueUrl",
                    "QueueArn",
                    "LicenseSecretArn",
                    "InternalServiceKeysSecretArn",
                    "ApiKeysParamName",
                    "StorageProfilesParamName",
                    "MigrationComplete",
                ],
            },
            {
                "label": "Process Logs tunables",
                "parameters": ["ProcessLogsReplicas", "ProcessLogsMemory"],
            },
            {
                "label": "Process Metrics tunables",
                "parameters": ["ProcessMetricsReplicas", "ProcessMetricsMemory"],
            },
            {
                "label": "Process Traces tunables",
                "parameters": ["ProcessTracesReplicas", "ProcessTracesMemory"],
            },
            {
                "label": "Pubsub-SQS tunables",
                "parameters": ["PubsubSqsReplicas"],
            },
            {
                "label": "Image overrides",
                "parameters": ["LakerunnerImage"],
            },
        ],
    )

    # ---------------------------------------------------------------------
    # Per-service shared environment / secrets
    # ---------------------------------------------------------------------
    base_env = [
        Environment(Name="LRDB_HOST", Value=Ref("DbEndpoint")),
        Environment(Name="LRDB_PORT", Value=Ref("DbPort")),
        Environment(Name="LRDB_DBNAME", Value="lakerunner"),
        Environment(Name="LRDB_SSLMODE", Value="require"),
        Environment(Name="LRDB_S3_BUCKET", Value=Ref("BucketName")),
        Environment(Name="LRDB_SQS_QUEUE_URL", Value=Ref("QueueUrl")),
        Environment(Name=_API_KEYS_ENV, Value=Ref("ApiKeysParamName")),
        Environment(Name=_STORAGE_PROFILES_ENV, Value=Ref("StorageProfilesParamName")),
    ]

    base_secrets = [
        Secret(Name="LRDB_USER", ValueFrom=Sub("${DbSecretArn}:username::")),
        Secret(Name="LRDB_PASSWORD", ValueFrom=Sub("${DbSecretArn}:password::")),
        Secret(Name="LRDB_INTERNAL_KEYS", ValueFrom=Ref("InternalServiceKeysSecretArn")),
        Secret(Name="LRDB_LICENSE", ValueFrom=Ref("LicenseSecretArn")),
    ]

    # ---------------------------------------------------------------------
    # Per-service blocks (log group, task role, task def, ECS service)
    # ---------------------------------------------------------------------
    services = [
        {
            "service_key": "process-logs",
            "config": logs_cfg,
            "cpu": logs_cfg["cpu"],
            "memory_mib": Ref("ProcessLogsMemory"),
            "desired_count": Ref("ProcessLogsReplicas"),
            "output_name": "ProcessLogsServiceName",
        },
        {
            "service_key": "process-metrics",
            "config": metrics_cfg,
            "cpu": metrics_cfg["cpu"],
            "memory_mib": Ref("ProcessMetricsMemory"),
            "desired_count": Ref("ProcessMetricsReplicas"),
            "output_name": "ProcessMetricsServiceName",
        },
        {
            "service_key": "process-traces",
            "config": traces_cfg,
            "cpu": traces_cfg["cpu"],
            "memory_mib": Ref("ProcessTracesMemory"),
            "desired_count": Ref("ProcessTracesReplicas"),
            "output_name": "ProcessTracesServiceName",
        },
        {
            "service_key": "pubsub-sqs",
            "config": pubsub_cfg,
            "cpu": pubsub_cfg["cpu"],
            "memory_mib": pubsub_cfg["memory_mib"],
            "desired_count": Ref("PubsubSqsReplicas"),
            "output_name": "PubsubSqsServiceName",
        },
    ]

    for spec in services:
        ecs_service = _build_service_block(
            t,
            service_key=spec["service_key"],
            config=spec["config"],
            image_ref=image_ref,
            cpu=spec["cpu"],
            memory_mib=spec["memory_mib"],
            desired_count=spec["desired_count"],
            base_env=base_env,
            base_secrets=base_secrets,
        )
        t.add_output(Output(spec["output_name"], Value=GetAtt(ecs_service, "Name")))

    return t


def _build_service_block(
    t: Template,
    *,
    service_key: str,
    config: dict,
    image_ref,
    cpu,
    memory_mib,
    desired_count,
    base_env: list,
    base_secrets: list,
):
    """Wire up the four resources (log group, task role, task def, service) for one service."""
    log_group = t.add_resource(services_common.build_log_group(service_key=service_key))
    task_role = t.add_resource(
        services_common.build_task_role(
            service_key=service_key,
            statements=_task_role_statements(log_group),
        )
    )
    env = list(base_env) + _service_specific_env(config)
    task_def = t.add_resource(
        services_common.build_task_definition(
            service_key=service_key,
            image_ref=image_ref,
            cpu=cpu,
            memory_mib=memory_mib,
            command=config.get("command"),
            execution_role_arn_param="ExecutionRoleArn",
            task_role_ref=task_role,
            environment=env,
            secrets=base_secrets,
            log_group_ref=log_group,
        )
    )
    return t.add_resource(
        services_common.build_ecs_service(
            service_key=service_key,
            cluster_arn_param="ClusterArn",
            task_definition_ref=task_def,
            desired_count=desired_count,
            subnets_csv_param="PrivateSubnetsCsv",
            security_group_id_param="TaskSecurityGroupId",
            container_name=service_key,
        )
    )


def _max_replicas(service_cfg: dict) -> int:
    """Default replicas for an autoscaling-eligible service.

    process-* configs encode min/max under autoscaling; the parameter default
    uses max_replicas. Falls back to `replicas` if autoscaling is absent.
    """
    autoscaling = service_cfg.get("autoscaling")
    if autoscaling and "max_replicas" in autoscaling:
        return int(autoscaling["max_replicas"])
    return int(service_cfg["replicas"])


def _service_specific_env(service_cfg: dict) -> list:
    """Convert the YAML environment dict into a list of ECS Environment objects."""
    env = service_cfg.get("environment") or {}
    return [Environment(Name=k, Value=str(v)) for k, v in env.items()]


def _task_role_statements(log_group_ref) -> list:
    """Inline IAM policy for a process-tier task role.

    Grants S3 (bucket+objects), SQS (consume+send), SSM GetParameter on the
    two config params, Secrets Manager GetSecretValue on the three secrets,
    and CloudWatch Logs writes to the per-service log group.
    """
    return [
        {
            "Effect": "Allow",
            "Action": [
                "s3:GetObject",
                "s3:PutObject",
                "s3:DeleteObject",
                "s3:ListBucket",
            ],
            "Resource": [
                Sub("arn:aws:s3:::${BucketName}"),
                Sub("arn:aws:s3:::${BucketName}/*"),
            ],
        },
        {
            "Effect": "Allow",
            "Action": [
                "sqs:ReceiveMessage",
                "sqs:DeleteMessage",
                "sqs:GetQueueAttributes",
                "sqs:GetQueueUrl",
                "sqs:SendMessage",
            ],
            "Resource": Ref("QueueArn"),
        },
        {
            "Effect": "Allow",
            "Action": ["ssm:GetParameter", "ssm:GetParameters"],
            "Resource": [
                Sub("arn:aws:ssm:${AWS::Region}:${AWS::AccountId}:parameter/${ApiKeysParamName}"),
                Sub("arn:aws:ssm:${AWS::Region}:${AWS::AccountId}:parameter/${StorageProfilesParamName}"),
            ],
        },
        {
            "Effect": "Allow",
            "Action": ["secretsmanager:GetSecretValue"],
            "Resource": [
                Ref("DbSecretArn"),
                Ref("LicenseSecretArn"),
                Ref("InternalServiceKeysSecretArn"),
            ],
        },
        {
            "Effect": "Allow",
            "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
            "Resource": GetAtt(log_group_ref, "Arn"),
        },
    ]


if __name__ == "__main__":
    print(build().to_yaml())
