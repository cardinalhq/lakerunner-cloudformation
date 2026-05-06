"""services-query.yaml nested stack: lakerunner query-api and query-worker services.

query-api is the public-facing query endpoint and attaches to the shared ALB
via a TargetGroup + ListenerRule. query-worker is internal; query-api reaches
it through task-to-task SG traffic on its task port.
"""

from troposphere import (
    GetAtt,
    Output,
    Parameter,
    Ref,
    Sub,
    Template,
)
from troposphere.ec2 import SecurityGroupIngress
from troposphere.ecs import Environment, Secret
from troposphere.servicediscovery import (
    DnsConfig,
    DnsRecord,
    Service as DiscoveryService,
)

from cardinal_cfn.children import services_common
from cardinal_cfn.defaults import load_defaults
from cardinal_cfn.images import add_image_override
from cardinal_cfn.parameters import (
    add_install_id_parameters,
    add_parameter_group_metadata,
)


# Parameter name for the env var the binary uses to find the API-keys SSM
# parameter and the storage-profiles SSM parameter. The binary resolves the
# parameter values at startup via SSM GetParameter; we only thread the names.
_API_KEYS_ENV = "LRDB_API_KEYS_SSM_PARAM"
_STORAGE_PROFILES_ENV = "LRDB_STORAGE_PROFILES_SSM_PARAM"


def build() -> Template:
    t = Template()
    t.set_description(
        "Cardinal services-query: lakerunner query-api (ALB-attached) and "
        "query-worker (internal) ECS services."
    )

    defaults = load_defaults()
    api_cfg = defaults["services"]["lakerunner-query-api"]
    worker_cfg = defaults["services"]["lakerunner-query-worker"]
    worker_port = int(worker_cfg["ingress"]["port"])
    api_container_port = int(api_cfg["ingress"]["container_port"])
    api_health_path = api_cfg["ingress"].get("health_check_path", "/healthz")

    add_install_id_parameters(t)

    # ---------------------------------------------------------------------
    # Cross-stack inputs (forwarded from root)
    # ---------------------------------------------------------------------
    t.add_parameter(Parameter("ClusterArn", Type="String", Description="ECS cluster ARN."))
    t.add_parameter(Parameter("ClusterName", Type="String", Description="ECS cluster name."))
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
        Parameter("TaskRoleArn", Type="String", Description="ECS task role ARN (shared across all services).")
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
    t.add_parameter(
        Parameter(
            "ServiceNamespaceId",
            Type="String",
            Description="Cloud Map private DNS namespace ID for in-cluster service discovery.",
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
    # that output, so depending on the parameter is enough — no explicit
    # DependsOn is needed inside the stack.
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
    # Per-service tunables (defaults from cardinal-defaults.yaml)
    # ---------------------------------------------------------------------
    t.add_parameter(
        Parameter(
            "QueryApiReplicas",
            Type="Number",
            Default=str(api_cfg["replicas"]),
            Description="Desired replicas for lakerunner-query-api.",
        )
    )
    t.add_parameter(
        Parameter(
            "QueryApiCpu",
            Type="String",
            Default=str(api_cfg["cpu"]),
            Description="Fargate CPU units for lakerunner-query-api.",
        )
    )
    t.add_parameter(
        Parameter(
            "QueryApiMemory",
            Type="String",
            Default=str(api_cfg["memory_mib"]),
            Description="Fargate memory (MiB) for lakerunner-query-api.",
        )
    )
    t.add_parameter(
        Parameter(
            "QueryWorkerReplicas",
            Type="Number",
            Default=str(worker_cfg["replicas"]),
            Description="Desired replicas for lakerunner-query-worker.",
        )
    )
    t.add_parameter(
        Parameter(
            "QueryWorkerCpu",
            Type="String",
            Default=str(worker_cfg["cpu"]),
            Description="Fargate CPU units for lakerunner-query-worker.",
        )
    )
    t.add_parameter(
        Parameter(
            "QueryWorkerMemory",
            Type="String",
            Default=str(worker_cfg["memory_mib"]),
            Description="Fargate memory (MiB) for lakerunner-query-worker.",
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
                    "TaskRoleArn",
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
                "label": "Query API tunables",
                "parameters": ["QueryApiReplicas", "QueryApiCpu", "QueryApiMemory"],
            },
            {
                "label": "Query Worker tunables",
                "parameters": [
                    "QueryWorkerReplicas",
                    "QueryWorkerCpu",
                    "QueryWorkerMemory",
                ],
            },
            {
                "label": "Image overrides",
                "parameters": ["LakerunnerImage"],
            },
        ],
    )

    # ---------------------------------------------------------------------
    # Per-service shared environment / secrets / IAM
    # ---------------------------------------------------------------------
    base_env = [
        Environment(Name="LRDB_HOST", Value=Ref("DbEndpoint")),
        Environment(Name="LRDB_PORT", Value=Ref("DbPort")),
        Environment(Name="LRDB_DBNAME", Value="lakerunner"),
        Environment(Name="LRDB_SSLMODE", Value="require"),
        Environment(Name="LRDB_S3_BUCKET", Value=Ref("BucketName")),
        Environment(Name="LRDB_SQS_QUEUE_URL", Value=Ref("QueueUrl")),
        Environment(Name="CONFIGDB_HOST", Value=Ref("DbEndpoint")),
        Environment(Name="CONFIGDB_PORT", Value=Ref("DbPort")),
        Environment(Name="CONFIGDB_DBNAME", Value="configdb"),
        Environment(Name="CONFIGDB_SSLMODE", Value="require"),
        Environment(Name=_API_KEYS_ENV, Value=Ref("ApiKeysParamName")),
        Environment(Name=_STORAGE_PROFILES_ENV, Value=Ref("StorageProfilesParamName")),
    ]

    base_secrets = [
        Secret(Name="LRDB_USER", ValueFrom=Sub("${DbSecretArn}:username::")),
        Secret(Name="LRDB_PASSWORD", ValueFrom=Sub("${DbSecretArn}:password::")),
        Secret(Name="CONFIGDB_USER", ValueFrom=Sub("${DbSecretArn}:username::")),
        Secret(Name="CONFIGDB_PASSWORD", ValueFrom=Sub("${DbSecretArn}:password::")),
        Secret(Name="LRDB_INTERNAL_KEYS", ValueFrom=Ref("InternalServiceKeysSecretArn")),
        Secret(Name="LICENSE_DATA", ValueFrom=Ref("LicenseSecretArn")),
    ]

    # ---------------------------------------------------------------------
    # query-api
    # ---------------------------------------------------------------------
    # query-worker (built first so query-api can reference its ECS Service
    # name in QUERY_WORKER_SERVICE_NAME for ECS-based worker discovery).
    # ---------------------------------------------------------------------
    worker_lg = t.add_resource(services_common.build_log_group(service_key="query-worker"))

    worker_env = list(base_env) + _service_specific_env(worker_cfg)
    worker_task = t.add_resource(
        services_common.build_task_definition(
            service_key="query-worker",
            image_ref=image_ref,
            cpu=Ref("QueryWorkerCpu"),
            memory_mib=Ref("QueryWorkerMemory"),
            command=worker_cfg.get("command"),
            execution_role_arn_param="ExecutionRoleArn",
            task_role_arn=Ref("TaskRoleArn"),
            environment=worker_env,
            secrets=base_secrets,
            log_group_ref=worker_lg,
        )
    )
    worker_service = t.add_resource(
        services_common.build_ecs_service(
            service_key="query-worker",
            cluster_arn_param="ClusterArn",
            task_definition_ref=worker_task,
            desired_count=Ref("QueryWorkerReplicas"),
            subnets_csv_param="PrivateSubnetsCsv",
            security_group_id_param="TaskSecurityGroupId",
            container_name="query-worker",
        )
    )

    # query-api reaches query-worker on its task port over the shared task SG.
    # The cluster stack already permits all task-to-task traffic via TaskSGAllSelf,
    # but we add a narrow self-referential ingress here so the wiring is explicit
    # and the port is documented at the service-tier level.
    t.add_resource(
        SecurityGroupIngress(
            "QueryWorkerIngress",
            GroupId=Ref("TaskSecurityGroupId"),
            IpProtocol="tcp",
            FromPort=worker_port,
            ToPort=worker_port,
            SourceSecurityGroupId=Ref("TaskSecurityGroupId"),
            Description="query-api to query-worker task-to-task",
        )
    )

    # ---------------------------------------------------------------------
    # query-api
    # ---------------------------------------------------------------------
    api_lg = t.add_resource(services_common.build_log_group(service_key="query-api"))

    # query-api uses ECS API to discover live query-worker tasks.
    api_env = list(base_env) + _service_specific_env(api_cfg) + [
        Environment(Name="EXECUTION_ENVIRONMENT", Value="ecs"),
        Environment(Name="QUERY_WORKER_CLUSTER_NAME", Value=Ref("ClusterName")),
        Environment(Name="QUERY_WORKER_SERVICE_NAME", Value=GetAtt(worker_service, "Name")),
        Environment(Name="QUERY_WORKER_PORT", Value=str(worker_port)),
    ]
    api_tg = t.add_resource(
        services_common.build_target_group(
            service_key="query-api",
            vpc_id_param="VpcId",
            port=api_container_port,
            health_check_path=api_health_path,
        )
    )
    t.add_resource(
        services_common.build_listener_rule(
            service_key="query-api",
            target_group_ref=api_tg,
            listener_arn_param="HttpsListenerArn",
            path_patterns=["/api/v1/query/*"],
        )
    )
    api_task = t.add_resource(
        services_common.build_task_definition(
            service_key="query-api",
            image_ref=image_ref,
            cpu=Ref("QueryApiCpu"),
            memory_mib=Ref("QueryApiMemory"),
            command=api_cfg.get("command"),
            execution_role_arn_param="ExecutionRoleArn",
            task_role_arn=Ref("TaskRoleArn"),
            environment=api_env,
            secrets=base_secrets,
            log_group_ref=api_lg,
            container_port=api_container_port,
        )
    )
    # Register query-api in Cloud Map so other services in the cluster can
    # reach it at http://query-api.<namespace>:<port> without going through
    # the ALB (alert-evaluator does this).
    api_discovery = t.add_resource(
        DiscoveryService(
            "QueryApiDiscoveryService",
            Name="query-api",
            NamespaceId=Ref("ServiceNamespaceId"),
            DnsConfig=DnsConfig(
                DnsRecords=[DnsRecord(Type="A", TTL="10")],
                RoutingPolicy="MULTIVALUE",
            ),
        )
    )
    api_service = t.add_resource(
        services_common.build_ecs_service(
            service_key="query-api",
            cluster_arn_param="ClusterArn",
            task_definition_ref=api_task,
            desired_count=Ref("QueryApiReplicas"),
            subnets_csv_param="PrivateSubnetsCsv",
            security_group_id_param="TaskSecurityGroupId",
            target_group_ref=api_tg,
            container_name="query-api",
            container_port=api_container_port,
            service_registry_ref=api_discovery,
        )
    )

    # ---------------------------------------------------------------------
    # Outputs
    # ---------------------------------------------------------------------
    t.add_output(Output("QueryApiServiceName", Value=GetAtt(api_service, "Name")))
    t.add_output(Output("QueryWorkerServiceName", Value=GetAtt(worker_service, "Name")))

    return t


def _service_specific_env(service_cfg: dict) -> list:
    """Convert the YAML environment dict into a list of ECS Environment objects."""
    env = service_cfg.get("environment") or {}
    return [Environment(Name=k, Value=str(v)) for k, v in env.items()]


if __name__ == "__main__":
    print(build().to_yaml())
