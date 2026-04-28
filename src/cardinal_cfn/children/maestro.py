"""maestro.yaml nested stack: maestro + DEX ECS Fargate service.

Owns a SINGLE ECS Fargate service running a multi-container task definition:

  1. db-init  (Essential=False, runs psql to bootstrap DB+user, then exits)
  2. maestro  (Essential=True, listens on the maestro and MCP gateway ports)
  3. dex      (Essential=True, listens on the DEX OIDC port)

The maestro container DependsOn db-init=SUCCESS, so the service won't come up
until the database is provisioned. Both maestro and dex are attached to the
shared cardinal HTTPS listener via two ListenerRules:

  /maestro/*  -> maestro container, priority 200
  /dex/*      -> dex container,     priority 210

Scope cuts vs. the pre-refactor generator: no self-signed cert Lambda, no
dex-init container, no maestro-local ALB, no HTTP-only fallback. The shared
ALB and its certificate are owned by the alb child stack.
"""

from troposphere import (
    GetAtt,
    Output,
    Parameter,
    Ref,
    Split,
    Sub,
    Template,
)
from troposphere.ecs import (
    AwsvpcConfiguration,
    ContainerDefinition,
    DeploymentCircuitBreaker,
    DeploymentConfiguration,
    Environment,
    LoadBalancer as EcsLoadBalancer,
    LogConfiguration,
    NetworkConfiguration,
    PortMapping,
    Secret,
    Service,
    TaskDefinition,
)
from troposphere.secretsmanager import GenerateSecretString
from troposphere.secretsmanager import Secret as SmSecret

from cardinal_cfn.children import services_common
from cardinal_cfn.defaults import load_defaults
from cardinal_cfn.images import add_image_override
from cardinal_cfn.naming import cardinal_tags
from cardinal_cfn.parameters import (
    add_install_id_parameters,
    add_parameter_group_metadata,
)


_SERVICE_KEY = "maestro"

# Service keys for the per-container log groups; these also show up in the
# log group names and the awslogs-stream-prefix.
_DB_INIT_KEY = "maestro-db-init"
_MAESTRO_KEY = "maestro"
_DEX_KEY = "maestro-dex"

# Listener-rule registration keys (see listener_priorities.py).
_MAESTRO_LISTENER_KEY = "maestro-https"
_DEX_LISTENER_KEY = "maestro-dex"


def build() -> Template:
    t = Template()
    t.set_description(
        "Cardinal maestro: ECS Fargate service running db-init, maestro, and "
        "DEX containers behind the shared ALB HTTPS listener."
    )

    defaults = load_defaults()
    maestro_cfg = defaults["maestro"]
    ports = maestro_cfg["ports"]
    maestro_port = int(ports["maestro"])
    mcp_gateway_port = int(ports["mcp_gateway"])
    dex_port = int(ports["dex"])

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
        Parameter("VpcId", Type="AWS::EC2::VPC::Id", Description="VPC ID (forwarded from root).")
    )
    t.add_parameter(
        Parameter("HttpsListenerArn", Type="String", Description="ARN of the ALB HTTPS listener.")
    )
    t.add_parameter(
        Parameter(
            "AlbDnsName",
            Type="String",
            Description="DNS name of the shared ALB (used to derive issuer URLs).",
        )
    )
    t.add_parameter(Parameter("DbEndpoint", Type="String", Description="RDS endpoint hostname."))
    t.add_parameter(Parameter("DbPort", Type="String", Default="5432", Description="RDS port."))
    t.add_parameter(
        Parameter(
            "DbSecretArn",
            Type="String",
            Description="ARN of the master DB secret (used by db-init to provision maestro DB+user).",
        )
    )
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

    # MigrationComplete is unused on purpose (same convention as services-*).
    # The root passes the migration-stack output through this parameter so
    # CloudFormation defers rendering until migrations finish.
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
    # Image overrides (one parameter per container image)
    # ---------------------------------------------------------------------
    maestro_image_ref = add_image_override(
        t,
        name="MaestroImage",
        default=defaults["images"]["maestro"],
        description="Container image for the maestro service.",
    )
    dex_image_ref = add_image_override(
        t,
        name="DexImage",
        default=defaults["images"]["dex"],
        description="Container image for the bundled DEX OIDC sidecar.",
    )
    db_init_image_ref = add_image_override(
        t,
        name="DbInitImage",
        default=defaults["images"]["db_init"],
        description="Container image for the psql-capable db-init bootstrapper.",
    )

    # ---------------------------------------------------------------------
    # Customer-tunable parameters
    # ---------------------------------------------------------------------
    t.add_parameter(
        Parameter(
            "MaestroTaskCpu",
            Type="String",
            Default=str(maestro_cfg["task"]["cpu"]),
            Description="Fargate CPU units for the maestro task definition.",
        )
    )
    t.add_parameter(
        Parameter(
            "MaestroTaskMemory",
            Type="String",
            Default=str(maestro_cfg["task"]["memory_mib"]),
            Description="Fargate memory (MiB) for the maestro task definition.",
        )
    )
    t.add_parameter(
        Parameter(
            "DexClientId",
            Type="String",
            Default=str(maestro_cfg["dex"]["client_id"]),
            Description="OIDC client ID the maestro UI uses to authenticate against DEX.",
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
                    "VpcId",
                    "HttpsListenerArn",
                    "AlbDnsName",
                    "DbEndpoint",
                    "DbPort",
                    "DbSecretArn",
                    "LicenseSecretArn",
                    "InternalServiceKeysSecretArn",
                    "ApiKeysParamName",
                    "StorageProfilesParamName",
                    "MigrationComplete",
                ],
            },
            {
                "label": "Maestro tunables",
                "parameters": ["MaestroTaskCpu", "MaestroTaskMemory"],
            },
            {
                "label": "Image overrides",
                "parameters": ["MaestroImage", "DexImage", "DbInitImage"],
            },
            {
                "label": "DEX configuration",
                "parameters": ["DexClientId"],
            },
        ],
    )

    # ---------------------------------------------------------------------
    # Maestro DB password (separate secret from the master DB secret).
    # Generated once at stack create; the db-init container injects this
    # password as the maestro role's password during CREATE USER.
    # No apply_policy() — this secret has no entry in policies.py and the
    # default Delete policy is fine (a fresh install regenerates).
    # ---------------------------------------------------------------------
    maestro_db_secret = t.add_resource(
        SmSecret(
            "MaestroDbSecret",
            Description=Sub(
                "Maestro application DB password for install ${InstallIdShort}."
            ),
            GenerateSecretString=GenerateSecretString(
                SecretStringTemplate='{"username":"maestro"}',
                GenerateStringKey="password",
                ExcludePunctuation=True,
            ),
            Tags=cardinal_tags(component="database", role="maestro-db-secret"),
        )
    )

    # ---------------------------------------------------------------------
    # Log groups (one per container so streams stay separable)
    # ---------------------------------------------------------------------
    db_init_lg = t.add_resource(services_common.build_log_group(service_key=_DB_INIT_KEY))
    maestro_lg = t.add_resource(services_common.build_log_group(service_key=_MAESTRO_KEY))
    dex_lg = t.add_resource(services_common.build_log_group(service_key=_DEX_KEY))

    # ---------------------------------------------------------------------
    # IAM task role (single role used by all three containers in the task)
    # ---------------------------------------------------------------------
    task_role = t.add_resource(
        services_common.build_task_role(
            service_key=_SERVICE_KEY,
            statements=_task_role_statements(
                log_group_refs=[db_init_lg, maestro_lg, dex_lg],
                maestro_db_secret_ref=maestro_db_secret,
            ),
        )
    )

    # ---------------------------------------------------------------------
    # Container definitions (inlined: services_common.build_task_definition
    # only supports one container, and maestro intentionally bundles three).
    # ---------------------------------------------------------------------
    db_init_container = ContainerDefinition(
        Name="db-init",
        Image=db_init_image_ref,
        Essential=False,
        EntryPoint=["sh", "-c"],
        # CREATE-IF-NOT-EXISTS is approximated via "|| true". The image must
        # have psql installed; the default db_init image (initcontainer-grafana)
        # is assumed to be psql-capable.
        Command=[
            (
                "PGPASSWORD=$LRDB_PASSWORD psql -h $LRDB_HOST -p $LRDB_PORT "
                "-U $LRDB_USER -d postgres -v ON_ERROR_STOP=1 "
                "-c \"CREATE DATABASE maestro\" || true; "
                "PGPASSWORD=$LRDB_PASSWORD psql -h $LRDB_HOST -p $LRDB_PORT "
                "-U $LRDB_USER -d postgres -v ON_ERROR_STOP=1 "
                "-c \"CREATE USER maestro WITH PASSWORD '$MAESTRO_DB_PASSWORD'\" "
                "|| true; "
                "PGPASSWORD=$LRDB_PASSWORD psql -h $LRDB_HOST -p $LRDB_PORT "
                "-U $LRDB_USER -d postgres -v ON_ERROR_STOP=1 "
                "-c \"GRANT ALL ON DATABASE maestro TO maestro\""
            )
        ],
        Environment=[
            Environment(Name="LRDB_HOST", Value=Ref("DbEndpoint")),
            Environment(Name="LRDB_PORT", Value=Ref("DbPort")),
        ],
        Secrets=[
            Secret(Name="LRDB_USER", ValueFrom=Sub("${DbSecretArn}:username::")),
            Secret(Name="LRDB_PASSWORD", ValueFrom=Sub("${DbSecretArn}:password::")),
            Secret(
                Name="MAESTRO_DB_PASSWORD",
                ValueFrom=Sub("${MaestroDbSecret}:password::"),
            ),
        ],
        LogConfiguration=LogConfiguration(
            LogDriver="awslogs",
            Options={
                "awslogs-group": Ref(db_init_lg),
                "awslogs-region": Ref("AWS::Region"),
                "awslogs-stream-prefix": "db-init",
            },
        ),
    )

    maestro_container = ContainerDefinition(
        Name="maestro",
        Image=maestro_image_ref,
        Essential=True,
        PortMappings=[
            PortMapping(ContainerPort=maestro_port, Protocol="tcp"),
            PortMapping(ContainerPort=mcp_gateway_port, Protocol="tcp"),
        ],
        Environment=[
            Environment(Name="MAESTRO_DB_HOST", Value=Ref("DbEndpoint")),
            Environment(Name="MAESTRO_DB_PORT", Value=Ref("DbPort")),
            Environment(Name="MAESTRO_DB_NAME", Value="maestro"),
            Environment(Name="MAESTRO_DB_USER", Value="maestro"),
            Environment(Name="MAESTRO_DB_SSLMODE", Value="require"),
            Environment(
                Name="DEX_ISSUER_URL",
                Value=Sub("https://${AlbDnsName}/dex"),
            ),
            Environment(Name="DEX_CLIENT_ID", Value=Ref("DexClientId")),
        ],
        Secrets=[
            Secret(
                Name="MAESTRO_DB_PASSWORD",
                ValueFrom=Sub("${MaestroDbSecret}:password::"),
            ),
            Secret(Name="LRDB_LICENSE", ValueFrom=Ref("LicenseSecretArn")),
            Secret(Name="LRDB_INTERNAL_KEYS", ValueFrom=Ref("InternalServiceKeysSecretArn")),
        ],
        DependsOn=[{"ContainerName": "db-init", "Condition": "SUCCESS"}],
        LogConfiguration=LogConfiguration(
            LogDriver="awslogs",
            Options={
                "awslogs-group": Ref(maestro_lg),
                "awslogs-region": Ref("AWS::Region"),
                "awslogs-stream-prefix": "maestro",
            },
        ),
    )

    # TODO(maestro): static DEX config injection (issuer, connectors, clients)
    # is out of scope here. The container ships with default config; richer
    # config injection (SSM-backed YAML, init-rendered file) can be layered
    # on later.
    dex_container = ContainerDefinition(
        Name="dex",
        Image=dex_image_ref,
        Essential=True,
        PortMappings=[PortMapping(ContainerPort=dex_port, Protocol="tcp")],
        Environment=[
            Environment(Name="DEX_ISSUER", Value=Sub("https://${AlbDnsName}/dex")),
        ],
        LogConfiguration=LogConfiguration(
            LogDriver="awslogs",
            Options={
                "awslogs-group": Ref(dex_lg),
                "awslogs-region": Ref("AWS::Region"),
                "awslogs-stream-prefix": "dex",
            },
        ),
    )

    task_def = t.add_resource(
        TaskDefinition(
            "MaestroTaskDef",
            RequiresCompatibilities=["FARGATE"],
            NetworkMode="awsvpc",
            Cpu=Ref("MaestroTaskCpu"),
            Memory=Ref("MaestroTaskMemory"),
            ExecutionRoleArn=Ref("ExecutionRoleArn"),
            TaskRoleArn=GetAtt(task_role, "Arn"),
            ContainerDefinitions=[db_init_container, maestro_container, dex_container],
            Tags=cardinal_tags(component="compute", role=_SERVICE_KEY),
        )
    )

    # ---------------------------------------------------------------------
    # ALB plumbing: two TargetGroups + two ListenerRules on the shared HTTPS
    # listener. priority_for() pulls the registered priority from
    # listener_priorities.py (200 for maestro-https, 210 for maestro-dex).
    # ---------------------------------------------------------------------
    maestro_tg = t.add_resource(
        services_common.build_target_group(
            service_key=_MAESTRO_LISTENER_KEY,
            vpc_id_param="VpcId",
            port=maestro_port,
        )
    )
    t.add_resource(
        services_common.build_listener_rule(
            service_key=_MAESTRO_LISTENER_KEY,
            target_group_ref=maestro_tg,
            listener_arn_param="HttpsListenerArn",
            path_patterns=["/maestro/*"],
        )
    )

    dex_tg = t.add_resource(
        services_common.build_target_group(
            service_key=_DEX_LISTENER_KEY,
            vpc_id_param="VpcId",
            port=dex_port,
        )
    )
    t.add_resource(
        services_common.build_listener_rule(
            service_key=_DEX_LISTENER_KEY,
            target_group_ref=dex_tg,
            listener_arn_param="HttpsListenerArn",
            path_patterns=["/dex/*"],
        )
    )

    # ---------------------------------------------------------------------
    # ECS Service (inlined like otel.py, because the LoadBalancers list has
    # two entries — the shared services_common.build_ecs_service helper only
    # supports one target_group_ref).
    # ---------------------------------------------------------------------
    service = t.add_resource(
        Service(
            "MaestroService",
            Cluster=Ref("ClusterArn"),
            LaunchType="FARGATE",
            DesiredCount=1,
            TaskDefinition=Ref(task_def),
            NetworkConfiguration=NetworkConfiguration(
                AwsvpcConfiguration=AwsvpcConfiguration(
                    Subnets=Split(",", Ref("PrivateSubnetsCsv")),
                    SecurityGroups=[Ref("TaskSecurityGroupId")],
                    AssignPublicIp="DISABLED",
                )
            ),
            DeploymentConfiguration=DeploymentConfiguration(
                MinimumHealthyPercent=50,
                MaximumPercent=200,
                DeploymentCircuitBreaker=DeploymentCircuitBreaker(
                    Enable=True,
                    Rollback=True,
                ),
            ),
            LoadBalancers=[
                EcsLoadBalancer(
                    ContainerName="maestro",
                    ContainerPort=maestro_port,
                    TargetGroupArn=Ref(maestro_tg),
                ),
                EcsLoadBalancer(
                    ContainerName="dex",
                    ContainerPort=dex_port,
                    TargetGroupArn=Ref(dex_tg),
                ),
            ],
            Tags=cardinal_tags(component="compute", role=_SERVICE_KEY),
        )
    )

    # ---------------------------------------------------------------------
    # Outputs
    # ---------------------------------------------------------------------
    t.add_output(Output("MaestroUrl", Value=Sub("https://${AlbDnsName}/maestro/")))
    t.add_output(Output("DexUrl", Value=Sub("https://${AlbDnsName}/dex/")))
    t.add_output(Output("MaestroServiceName", Value=GetAtt(service, "Name")))
    t.add_output(Output("MaestroDbSecretArn", Value=Ref(maestro_db_secret)))

    return t


def _task_role_statements(*, log_group_refs: list, maestro_db_secret_ref) -> list:
    """Inline IAM policy for the maestro task role.

    Grants Secrets Manager read on the master DB secret (db-init), the
    maestro DB secret, the license secret, and the internal-service-keys
    secret; SSM read on the two config params; and CloudWatch Logs writes
    to all three per-container log groups.
    """
    return [
        {
            "Effect": "Allow",
            "Action": ["secretsmanager:GetSecretValue"],
            "Resource": [
                Ref("DbSecretArn"),
                Ref(maestro_db_secret_ref),
                Ref("LicenseSecretArn"),
                Ref("InternalServiceKeysSecretArn"),
            ],
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
            "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
            "Resource": [GetAtt(lg, "Arn") for lg in log_group_refs],
        },
    ]


if __name__ == "__main__":
    print(build().to_yaml())
