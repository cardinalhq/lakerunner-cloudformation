"""maestro.yaml nested stack: maestro + DEX ECS Fargate service.

Owns a SINGLE ECS Fargate service running a multi-container task definition:

  1. db-init  (Essential=False, runs psql to bootstrap DB+user, then exits)
  2. maestro  (Essential=True, listens on the maestro and MCP gateway ports)
  3. dex      (Essential=True, listens on the DEX OIDC port)

The maestro container DependsOn db-init=SUCCESS, so the service won't come up
until the database is provisioned. Both maestro and dex are attached to the
shared cardinal HTTPS listener via two ListenerRules:

  /*          -> maestro container, priority 49999 (catch-all default app)
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
    MountPoint,
    NetworkConfiguration,
    PortMapping,
    Secret,
    Service,
    TaskDefinition,
    Volume,
)
from troposphere.secretsmanager import GenerateSecretString
from troposphere.secretsmanager import Secret as SmSecret

from cardinal_cfn.children import services_common
from cardinal_cfn.defaults import load_defaults
from cardinal_cfn.images import add_image_override
from cardinal_cfn.naming import cardinal_tags
from cardinal_cfn.parameters import (
    add_install_id_parameters,
    add_no_echo_parameter,
    add_parameter_group_metadata,
)


_SERVICE_KEY = "maestro"

# Service keys for the per-container log groups; these also show up in the
# log group names and the awslogs-stream-prefix.
_DB_INIT_KEY = "maestro-db-init"
_MAESTRO_KEY = "maestro"
_DEX_KEY = "maestro-dex"
_DEX_INIT_KEY = "maestro-dex-init"

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
    dex_init_image_ref = add_image_override(
        t,
        name="DexInitImage",
        default=defaults["images"]["dex_init"],
        description="BusyBox-style image used by the dex-init container to render config.yaml.",
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
    t.add_parameter(
        Parameter(
            "DexAdminEmail",
            Type="String",
            Default="admin@cardinal.local",
            Description="Email address for the DEX local-DB admin login.",
        )
    )
    add_no_echo_parameter(
        t,
        "DexAdminPasswordHash",
        description=(
            "Bcrypt hash of the DEX admin password. Generate with "
            "`htpasswd -bnBC 10 \"\" 'your-password' | tr -d ':\\n' | sed 's/^/$/' | sed 's/2y/2a/'` "
            "(or any bcrypt $2a$/$2b$/$2y$ hash). Required."
        ),
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
                "parameters": ["MaestroImage", "DexImage", "DbInitImage", "DexInitImage"],
            },
            {
                "label": "DEX configuration",
                "parameters": ["DexClientId", "DexAdminEmail", "DexAdminPasswordHash"],
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
    dex_init_lg = t.add_resource(services_common.build_log_group(service_key=_DEX_INIT_KEY))

    # ---------------------------------------------------------------------
    # IAM task role (single role used by all containers in the task)
    # ---------------------------------------------------------------------
    task_role = t.add_resource(
        services_common.build_task_role(
            service_key=_SERVICE_KEY,
            statements=_task_role_statements(
                log_group_refs=[db_init_lg, maestro_lg, dex_lg, dex_init_lg],
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
        # Idempotent provisioning of the maestro DB / role / extensions.
        # The "|| true" fallbacks tolerate "already exists" on re-runs.
        # PG 15+ revokes CREATE on the public schema from PUBLIC, so we
        # transfer ownership of the database and schema to the maestro
        # role. The pgvector / pgcrypto / citext extensions must be created
        # by an rds_superuser (the lakerunner master) — mcp-gateway's
        # migrations run as the maestro role and use IF NOT EXISTS so
        # they no-op once these are in place.
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
                "-c \"GRANT ALL ON DATABASE maestro TO maestro\"; "
                "PGPASSWORD=$LRDB_PASSWORD psql -h $LRDB_HOST -p $LRDB_PORT "
                "-U $LRDB_USER -d postgres -v ON_ERROR_STOP=1 "
                "-c \"ALTER DATABASE maestro OWNER TO maestro\"; "
                "PGPASSWORD=$LRDB_PASSWORD psql -h $LRDB_HOST -p $LRDB_PORT "
                "-U $LRDB_USER -d maestro -v ON_ERROR_STOP=1 "
                "-c \"ALTER SCHEMA public OWNER TO maestro\"; "
                "for ext in vector pgcrypto citext; do "
                "PGPASSWORD=$LRDB_PASSWORD psql -h $LRDB_HOST -p $LRDB_PORT "
                "-U $LRDB_USER -d maestro -v ON_ERROR_STOP=1 "
                "-c \"CREATE EXTENSION IF NOT EXISTS $ext\"; "
                "done"
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

    # mcp-gateway runs the maestro DB schema migrations on startup, then
    # serves MCP. Maestro shares this DB and breaks if migrations haven't
    # run yet (relation "maestro_*" does not exist).
    db_env = [
        Environment(Name="MAESTRO_DB_HOST", Value=Ref("DbEndpoint")),
        Environment(Name="MAESTRO_DB_PORT", Value=Ref("DbPort")),
        Environment(Name="MAESTRO_DB_NAME", Value="maestro"),
        Environment(Name="MAESTRO_DB_USER", Value="maestro"),
        Environment(Name="MAESTRO_DB_SSLMODE", Value="require"),
    ]
    db_secrets = [
        Secret(
            Name="MAESTRO_DB_PASSWORD",
            ValueFrom=Sub("${MaestroDbSecret}:password::"),
        ),
    ]

    mcp_gateway_container = ContainerDefinition(
        Name="mcp-gateway",
        Image=maestro_image_ref,
        Essential=True,
        EntryPoint=["/app/entrypoint.sh"],
        Command=["mcp-gateway"],
        PortMappings=[PortMapping(ContainerPort=mcp_gateway_port, Protocol="tcp")],
        Environment=db_env + [
            Environment(Name="MCP_PORT", Value=str(mcp_gateway_port)),
        ],
        Secrets=list(db_secrets),
        DependsOn=[{"ContainerName": "db-init", "Condition": "SUCCESS"}],
        LogConfiguration=LogConfiguration(
            LogDriver="awslogs",
            Options={
                "awslogs-group": Ref(maestro_lg),
                "awslogs-region": Ref("AWS::Region"),
                "awslogs-stream-prefix": "mcp-gateway",
            },
        ),
    )

    # Sidecar that polls localhost:<mcp_port> until it accepts a connection,
    # then exits. Maestro depends on this completing so it doesn't start
    # before mcp-gateway has finished migrating.
    wait_for_mcp_container = ContainerDefinition(
        Name="wait-for-mcp",
        Image=maestro_image_ref,
        Essential=False,
        EntryPoint=["/app/entrypoint.sh"],
        Command=["wait-for-tcp", "localhost", str(mcp_gateway_port)],
        DependsOn=[{"ContainerName": "mcp-gateway", "Condition": "START"}],
        LogConfiguration=LogConfiguration(
            LogDriver="awslogs",
            Options={
                "awslogs-group": Ref(maestro_lg),
                "awslogs-region": Ref("AWS::Region"),
                "awslogs-stream-prefix": "wait-for-mcp",
            },
        ),
    )

    maestro_container = ContainerDefinition(
        Name="maestro",
        Image=maestro_image_ref,
        Essential=True,
        PortMappings=[PortMapping(ContainerPort=maestro_port, Protocol="tcp")],
        Environment=db_env + [
            # Maestro reads OIDC_* (see packages/maestro/src/index.ts).
            # Without OIDC_ISSUER_URL the UI renders an "Authentication
            # not configured" placeholder instead of mounting routes.
            Environment(
                Name="OIDC_ISSUER_URL",
                Value=Sub("https://${AlbDnsName}/dex"),
            ),
            Environment(Name="OIDC_CLIENT_ID", Value=Ref("DexClientId")),
            Environment(Name="OIDC_AUDIENCE", Value=Ref("DexClientId")),
            # DEX_* kept for any tooling that still reads the legacy names.
            Environment(
                Name="DEX_ISSUER_URL",
                Value=Sub("https://${AlbDnsName}/dex"),
            ),
            Environment(Name="DEX_CLIENT_ID", Value=Ref("DexClientId")),
        ],
        Secrets=list(db_secrets) + [
            Secret(Name="LICENSE_DATA", ValueFrom=Ref("LicenseSecretArn")),
            Secret(Name="LRDB_INTERNAL_KEYS", ValueFrom=Ref("InternalServiceKeysSecretArn")),
        ],
        DependsOn=[
            {"ContainerName": "db-init", "Condition": "SUCCESS"},
            {"ContainerName": "wait-for-mcp", "Condition": "SUCCESS"},
        ],
        LogConfiguration=LogConfiguration(
            LogDriver="awslogs",
            Options={
                "awslogs-group": Ref(maestro_lg),
                "awslogs-region": Ref("AWS::Region"),
                "awslogs-stream-prefix": "maestro",
            },
        ),
    )

    # dex-init renders /etc/dex/config.yaml from env vars (BusyBox sh + heredoc).
    # The unquoted heredoc expands ${DEX_*} once (no re-scan), so a bcrypt hash
    # containing '$' survives intact. ALB DNS comes back mixed-case but
    # browsers lower-case Host before sending; Dex's redirect_uri match is
    # exact-string, so we register both the original and lowercased URI.
    # 1777 on /dex-tmp because Fargate mounts empty volumes 0755 root:root and
    # the nonroot dex container can't otherwise write its config-expansion
    # tempfile on startup.
    dex_config_render_script = (
        "set -eu; "
        "DEX_REDIRECT_URI_LC=$(echo \"$DEX_REDIRECT_URI\" | tr 'A-Z' 'a-z'); "
        "cat > /etc/dex/config.yaml <<EOF\n"
        "issuer: ${DEX_ISSUER_URL}\n"
        "storage:\n"
        "  type: memory\n"
        "web:\n"
        "  http: 0.0.0.0:${DEX_PORT}\n"
        "oauth2:\n"
        "  skipApprovalScreen: true\n"
        "enablePasswordDB: true\n"
        "staticClients:\n"
        "  - id: \"${DEX_CLIENT_ID}\"\n"
        "    name: \"Maestro UI\"\n"
        "    public: true\n"
        "    redirectURIs:\n"
        "      - \"${DEX_REDIRECT_URI}\"\n"
        "      - \"${DEX_REDIRECT_URI_LC}\"\n"
        "staticPasswords:\n"
        "  - email: \"${DEX_ADMIN_EMAIL}\"\n"
        "    hash: \"${DEX_ADMIN_HASH}\"\n"
        "    username: \"admin\"\n"
        "    userID: \"00000000-0000-0000-0000-000000000001\"\n"
        "EOF\n"
        "chmod 1777 /dex-tmp\n"
    )

    dex_init_container = ContainerDefinition(
        Name="dex-init",
        Image=dex_init_image_ref,
        Essential=False,
        EntryPoint=["/bin/sh", "-c"],
        Command=[dex_config_render_script],
        Environment=[
            Environment(Name="DEX_ISSUER_URL", Value=Sub("https://${AlbDnsName}/dex")),
            Environment(Name="DEX_REDIRECT_URI", Value=Sub("https://${AlbDnsName}/")),
            Environment(Name="DEX_CLIENT_ID", Value=Ref("DexClientId")),
            Environment(Name="DEX_PORT", Value=str(dex_port)),
            Environment(Name="DEX_ADMIN_EMAIL", Value=Ref("DexAdminEmail")),
            Environment(Name="DEX_ADMIN_HASH", Value=Ref("DexAdminPasswordHash")),
        ],
        MountPoints=[
            MountPoint(ContainerPath="/etc/dex", SourceVolume="dex-config", ReadOnly=False),
            MountPoint(ContainerPath="/dex-tmp", SourceVolume="dex-tmp", ReadOnly=False),
        ],
        LogConfiguration=LogConfiguration(
            LogDriver="awslogs",
            Options={
                "awslogs-group": Ref(dex_init_lg),
                "awslogs-region": Ref("AWS::Region"),
                "awslogs-stream-prefix": "dex-init",
            },
        ),
    )

    dex_container = ContainerDefinition(
        Name="dex",
        Image=dex_image_ref,
        Essential=True,
        User="65532",
        ReadonlyRootFilesystem=True,
        Command=["dex", "serve", "/etc/dex/config.yaml"],
        PortMappings=[PortMapping(ContainerPort=dex_port, Protocol="tcp")],
        DependsOn=[{"ContainerName": "dex-init", "Condition": "SUCCESS"}],
        MountPoints=[
            MountPoint(ContainerPath="/etc/dex", SourceVolume="dex-config", ReadOnly=True),
            MountPoint(ContainerPath="/tmp", SourceVolume="dex-tmp", ReadOnly=False),
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
            ContainerDefinitions=[
                db_init_container,
                mcp_gateway_container,
                wait_for_mcp_container,
                maestro_container,
                dex_init_container,
                dex_container,
            ],
            Volumes=[
                Volume(Name="dex-config"),
                Volume(Name="dex-tmp"),
            ],
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
            path_patterns=["/*"],
        )
    )

    dex_tg = t.add_resource(
        services_common.build_target_group(
            service_key=_DEX_LISTENER_KEY,
            vpc_id_param="VpcId",
            port=dex_port,
            # Dex serves all routes under its configured path_prefix; the
            # default "/healthz" returns 404 once the prefix is set.
            health_check_path="/dex/healthz",
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
    t.add_output(Output("MaestroUrl", Value=Sub("https://${AlbDnsName}/")))
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
                Sub("arn:aws:ssm:${AWS::Region}:${AWS::AccountId}:parameter${ApiKeysParamName}"),
                Sub("arn:aws:ssm:${AWS::Region}:${AWS::AccountId}:parameter${StorageProfilesParamName}"),
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
