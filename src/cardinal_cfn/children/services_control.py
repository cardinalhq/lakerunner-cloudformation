"""services-control.yaml nested stack: lakerunner control-plane ECS services.

Owns four ECS Fargate services:

- admin-api (ALB-attached at priority 110, path /admin/*)
- sweeper (internal)
- monitoring (internal; gRPC port not exposed on the ALB)
- alert-evaluator (internal)

These services run at fixed shape; CPU, memory, and replicas all come from
cardinal-defaults.yaml. No per-service tunables are exposed at deployment time.
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
        "Cardinal services-control: lakerunner admin-api (ALB-attached), "
        "sweeper, monitoring, and alert-evaluator ECS services."
    )

    defaults = load_defaults()
    admin_cfg = defaults["services"]["lakerunner-admin-api"]
    sweeper_cfg = defaults["services"]["lakerunner-sweeper"]
    monitoring_cfg = defaults["services"]["lakerunner-monitoring"]
    alert_cfg = defaults["services"]["lakerunner-alert-evaluator"]

    admin_container_port = int(admin_cfg["ingress"]["container_port"])
    admin_health_path = admin_cfg["ingress"].get("health_check_path", "/healthz")

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
    t.add_parameter(
        Parameter("HttpsListenerArn", Type="String", Description="ARN of the ALB HTTPS listener.")
    )
    t.add_parameter(
        Parameter("VpcId", Type="AWS::EC2::VPC::Id", Description="VPC ID (forwarded from root).")
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
                    "HttpsListenerArn",
                    "VpcId",
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
    # admin-api: ALB-attached
    # ---------------------------------------------------------------------
    admin_lg = t.add_resource(services_common.build_log_group(service_key="admin-api"))
    admin_role = t.add_resource(
        services_common.build_task_role(
            service_key="admin-api",
            statements=_task_role_statements(admin_lg),
        )
    )
    admin_tg = t.add_resource(
        services_common.build_target_group(
            service_key="admin-api",
            vpc_id_param="VpcId",
            port=admin_container_port,
            health_check_path=admin_health_path,
        )
    )
    t.add_resource(
        services_common.build_listener_rule(
            service_key="admin-api",
            target_group_ref=admin_tg,
            listener_arn_param="HttpsListenerArn",
            path_patterns=["/admin/*"],
        )
    )
    admin_env = list(base_env) + _service_specific_env(admin_cfg)
    admin_task = t.add_resource(
        services_common.build_task_definition(
            service_key="admin-api",
            image_ref=image_ref,
            cpu=admin_cfg["cpu"],
            memory_mib=admin_cfg["memory_mib"],
            command=admin_cfg.get("command"),
            execution_role_arn_param="ExecutionRoleArn",
            task_role_ref=admin_role,
            environment=admin_env,
            secrets=base_secrets,
            log_group_ref=admin_lg,
        )
    )
    admin_service = t.add_resource(
        services_common.build_ecs_service(
            service_key="admin-api",
            cluster_arn_param="ClusterArn",
            task_definition_ref=admin_task,
            desired_count=int(admin_cfg["replicas"]),
            subnets_csv_param="PrivateSubnetsCsv",
            security_group_id_param="TaskSecurityGroupId",
            target_group_ref=admin_tg,
            container_name="admin-api",
            container_port=admin_container_port,
        )
    )
    t.add_output(Output("AdminApiServiceName", Value=GetAtt(admin_service, "Name")))

    # ---------------------------------------------------------------------
    # Internal services (no ALB attachment)
    # ---------------------------------------------------------------------
    internal_services = [
        {
            "service_key": "sweeper",
            "config": sweeper_cfg,
            "output_name": "SweeperServiceName",
        },
        {
            "service_key": "monitoring",
            "config": monitoring_cfg,
            "output_name": "MonitoringServiceName",
        },
        {
            "service_key": "alert-evaluator",
            "config": alert_cfg,
            "output_name": "AlertEvaluatorServiceName",
        },
    ]

    for spec in internal_services:
        ecs_service = _build_internal_service_block(
            t,
            service_key=spec["service_key"],
            config=spec["config"],
            image_ref=image_ref,
            base_env=base_env,
            base_secrets=base_secrets,
        )
        t.add_output(Output(spec["output_name"], Value=GetAtt(ecs_service, "Name")))

    return t


def _build_internal_service_block(
    t: Template,
    *,
    service_key: str,
    config: dict,
    image_ref,
    base_env: list,
    base_secrets: list,
):
    """Wire up log group, task role, task def, and ECS service for one internal service."""
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
            cpu=config["cpu"],
            memory_mib=config["memory_mib"],
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
            desired_count=int(config["replicas"]),
            subnets_csv_param="PrivateSubnetsCsv",
            security_group_id_param="TaskSecurityGroupId",
            container_name=service_key,
        )
    )


def _service_specific_env(service_cfg: dict) -> list:
    """Convert the YAML environment dict into a list of ECS Environment objects."""
    env = service_cfg.get("environment") or {}
    return [Environment(Name=k, Value=str(v)) for k, v in env.items()]


def _task_role_statements(log_group_ref) -> list:
    """Inline IAM policy for a control-tier task role.

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
