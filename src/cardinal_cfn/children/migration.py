"""migration.yaml nested stack: DB migrator task definition + a long-running
ECS service that runs the migrator once and then sleeps.

No Lambda. The migrator task definition has four containers:

  1. configdb-init (non-essential): psql CREATE DATABASE configdb if absent.
  2. migrator (non-essential): `lakerunner migrate --databases=lrdb,configdb`,
     dependsOn configdb-init=COMPLETE.
  3. ensure-storage-profile (non-essential): idempotent psql upsert that
     guarantees the canonical single-install storage_profile row exists in
     configdb.bucket_configurations + configdb.organization_buckets. The
     lakerunner image's initializeIfNeededFunc only seeds configdb when those
     tables are empty, so installs whose first migration landed before
     #117 (the storage-profiles {} bug) or which got partially seeded under
     a different org/collector never get the canonical row -- ingest still
     works via the otel-collector path, but maestro UI queries (post-#121)
     run as the canonical 12340000 org and fail with "storage profile not
     found." This sidecar self-heals that on every image bump.
  4. keepalive (essential): sleeps forever, dependsOn ensure-storage-profile=
     SUCCESS. A failed upsert blocks keepalive -> the ECS deployment circuit
     breaker fails the service -> the root stack rolls back. Loud, not silent.

Because keepalive is the only essential container and ECS will not start it
until its dependencies exit 0, the task is not RUNNING -- and therefore the
ECS service is not at steady state, and therefore the MigrationStack nested
stack is not CREATE_COMPLETE -- until migrations and the canonical-profile
upsert both succeed. The service-tier stacks already DependsOn MigrationStack,
so they only deploy after that gate clears. An image change redeploys the
service (new migrator run + new upsert) before those stacks update, exactly
as the old custom-resource trigger did.
"""

from troposphere import (
    Template,
    Parameter,
    Ref,
    Output,
    Sub,
)
from troposphere.ecs import (
    ContainerDefinition,
    ContainerDependency,
    Environment,
    LogConfiguration,
    RuntimePlatform,
    Secret,
    TaskDefinition,
)
from troposphere.logs import LogGroup

from cardinal_cfn.children.services_common import build_ecs_service
from cardinal_cfn.defaults import load_defaults
from cardinal_cfn.naming import cardinal_tags
from cardinal_cfn.parameters import add_install_id_parameters
from cardinal_cfn.policies import apply_policy


def build() -> Template:
    t = Template()
    t.set_description(
        "Cardinal migration: DB migrator task definition and the ECS service "
        "that runs it once and then idles."
    )

    defaults = load_defaults()

    add_install_id_parameters(t)

    # Cluster / networking parameters (passed from parent)
    t.add_parameter(Parameter("ClusterArn", Type="String", Description="ECS cluster ARN."))
    t.add_parameter(Parameter("ClusterName", Type="String", Description="ECS cluster name."))
    t.add_parameter(Parameter("TaskSecurityGroupId", Type="String", Description="ECS task security group ID."))
    t.add_parameter(Parameter("ExecutionRoleArn", Type="String", Description="ECS task execution role ARN."))
    t.add_parameter(Parameter("TaskRoleArn", Type="String", Description="ECS task role ARN (used by the migrator container)."))
    t.add_parameter(Parameter("PrivateSubnetsCsv", Type="String", Description="Comma-separated private subnet IDs."))

    # Database parameters
    t.add_parameter(Parameter("DbEndpoint", Type="String", Description="RDS endpoint hostname."))
    t.add_parameter(Parameter("DbPort", Type="String", Default="5432", Description="RDS port."))
    t.add_parameter(Parameter("DbName", Type="String", Default="lakerunner", Description="Database name."))
    t.add_parameter(Parameter("DbSecretArn", Type="String", Description="ARN of the DB master secret."))

    # SSM-backed seeds for configdb (storage profiles, API keys). The migrator
    # reads these via STORAGE_PROFILE_FILE/API_KEYS_FILE = env:VAR indirection
    # (see lakerunner cmd/initialize/loader.go). They are injected as env vars
    # by ECS Secrets resolution against the SSM parameter ARN -- which is why
    # the execution role needs ssm:GetParameter* on /cardinal/* (already
    # documented in docs/operations/permissions-lakerunner.md). The migrator's
    # initializeIfNeededFunc seeds configdb only when the tables are empty, so
    # operator edits via the maestro UI are not clobbered on image bumps.
    t.add_parameter(
        Parameter(
            "StorageProfilesParamName",
            Type="String",
            Description="Name of the SSM parameter holding the storage_profiles YAML.",
        )
    )
    t.add_parameter(
        Parameter(
            "ApiKeysParamName",
            Type="String",
            Description="Name of the SSM parameter holding the api_keys YAML.",
        )
    )

    # Inputs for the ensure-storage-profile sidecar. The canonical
    # single-install org id and the ingest bucket name are exactly the values
    # the SSM-driven seed would have written, so the sidecar's row is
    # indistinguishable from a clean first-install seed.
    t.add_parameter(
        Parameter(
            "OrgId",
            Type="String",
            Default="12340000-0000-4000-8000-000000000000",
            Description=(
                "Canonical single-install organization UUID. Must match the "
                "infrastructure stack's storage-profiles/api-keys seed."
            ),
        )
    )
    t.add_parameter(
        Parameter(
            "IngestBucketName",
            Type="String",
            Description="Name of the S3 ingest bucket (infra-setup output).",
        )
    )

    # Image parameters. The migrator runs from the same image as the lakerunner
    # service tasks (LakerunnerImage), so the two cannot drift; an image change
    # redeploys the migration service (rerunning the migrator) before the
    # service-tier stacks update.
    t.add_parameter(
        Parameter(
            "LakerunnerImage",
            Type="String",
            Default=defaults["images"]["lakerunner"],
            Description="Container image used for both lakerunner tasks and the DB migrator.",
        )
    )
    t.add_parameter(
        Parameter(
            "DbInitImage",
            Type="String",
            Default=defaults["images"]["db_init"],
            Description="Image for the configdb-init and keepalive containers (must include psql and a shell).",
        )
    )

    # ---------------------------------------------------------------------------
    # Log group (shared by all three containers)
    # ---------------------------------------------------------------------------
    migrator_lg = t.add_resource(LogGroup("MigratorLogGroup", RetentionInDays=14))
    apply_policy(migrator_lg, "log-group")

    def _logs(stream_prefix: str) -> LogConfiguration:
        return LogConfiguration(
            LogDriver="awslogs",
            Options={
                "awslogs-group": Ref(migrator_lg),
                "awslogs-region": Ref("AWS::Region"),
                "awslogs-stream-prefix": stream_prefix,
            },
        )

    # ---------------------------------------------------------------------------
    # Migrator ECS task definition (configdb-init -> migrator -> keepalive)
    # ---------------------------------------------------------------------------
    configdb_init_container = ContainerDefinition(
        Name="configdb-init",
        Image=Ref("DbInitImage"),
        Essential=False,
        EntryPoint=["sh", "-c"],
        Command=[
            (
                "PGPASSWORD=$LRDB_PASSWORD psql -h $LRDB_HOST -p $LRDB_PORT "
                "-U $LRDB_USER -d postgres -v ON_ERROR_STOP=1 "
                "-tAc \"SELECT 1 FROM pg_database WHERE datname='configdb'\" "
                "| grep -q 1 || "
                "PGPASSWORD=$LRDB_PASSWORD psql -h $LRDB_HOST -p $LRDB_PORT "
                "-U $LRDB_USER -d postgres -v ON_ERROR_STOP=1 "
                "-c \"CREATE DATABASE configdb\""
            )
        ],
        Environment=[
            Environment(Name="LRDB_HOST", Value=Ref("DbEndpoint")),
            Environment(Name="LRDB_PORT", Value=Ref("DbPort")),
        ],
        Secrets=[
            Secret(Name="LRDB_USER", ValueFrom=Sub("${DbSecretArn}:username::")),
            Secret(Name="LRDB_PASSWORD", ValueFrom=Sub("${DbSecretArn}:password::")),
        ],
        LogConfiguration=_logs("configdb-init"),
    )

    migrator_container = ContainerDefinition(
        Name="migrator",
        Image=Ref("LakerunnerImage"),
        Command=["/app/bin/lakerunner", "migrate", "--databases=lrdb,configdb"],
        # Non-essential: it runs to completion and exits. The task keeps running
        # via the keepalive container, which only starts once migrator exits 0.
        Essential=False,
        DependsOn=[ContainerDependency(ContainerName="configdb-init", Condition="COMPLETE")],
        Environment=[
            Environment(Name="LRDB_HOST", Value=Ref("DbEndpoint")),
            Environment(Name="LRDB_PORT", Value=Ref("DbPort")),
            Environment(Name="LRDB_DBNAME", Value=Ref("DbName")),
            Environment(Name="LRDB_SSLMODE", Value="require"),
            Environment(Name="CONFIGDB_HOST", Value=Ref("DbEndpoint")),
            Environment(Name="CONFIGDB_PORT", Value=Ref("DbPort")),
            Environment(Name="CONFIGDB_DBNAME", Value="configdb"),
            Environment(Name="CONFIGDB_SSLMODE", Value="require"),
            # The env: prefix tells the binary's initializeIfNeededFunc to
            # read the YAML body from the named env var, which ECS Secrets
            # populates from SSM below.
            Environment(Name="STORAGE_PROFILE_FILE", Value="env:STORAGE_PROFILES_YAML"),
            Environment(Name="API_KEYS_FILE", Value="env:API_KEYS_YAML"),
        ],
        Secrets=[
            Secret(Name="LRDB_USER", ValueFrom=Sub("${DbSecretArn}:username::")),
            Secret(Name="LRDB_PASSWORD", ValueFrom=Sub("${DbSecretArn}:password::")),
            Secret(Name="CONFIGDB_USER", ValueFrom=Sub("${DbSecretArn}:username::")),
            Secret(Name="CONFIGDB_PASSWORD", ValueFrom=Sub("${DbSecretArn}:password::")),
            # ECS resolves these at task launch by calling SSM with the
            # execution role and injects the parameter value as the env var.
            Secret(
                Name="STORAGE_PROFILES_YAML",
                ValueFrom=Sub(
                    "arn:${AWS::Partition}:ssm:${AWS::Region}:${AWS::AccountId}:parameter${StorageProfilesParamName}"
                ),
            ),
            Secret(
                Name="API_KEYS_YAML",
                ValueFrom=Sub(
                    "arn:${AWS::Partition}:ssm:${AWS::Region}:${AWS::AccountId}:parameter${ApiKeysParamName}"
                ),
            ),
        ],
        LogConfiguration=_logs("migrator"),
    )

    # ensure-storage-profile: idempotent upsert that guarantees the canonical
    # org's storage_profile row exists in configdb. Schema verified against
    # the live lakerunner repo migrations (1755582182, 1755706737, 1755713265,
    # 1755727712, 1755747368, 1755747422, 1755753938, 1779112554). ON CONFLICT
    # uses bucket_configurations.bucket_name UNIQUE and the
    # organization_buckets (organization_id, bucket_id, instance_num,
    # collector_name) UNIQUE constraint -- both present on every live install.
    # The legacy organization_id UNIQUE on organization_buckets was dropped in
    # 1755727712 (allow_multiple_buckets_per_org), so this never collides with
    # operator-added second buckets for the same org. DO NOTHING preserves
    # operator edits made via the maestro UI.
    #
    # SQL goes in via stdin (not -c) so psql performs \set + :'var' client-side
    # quoting -- psql's -c mode does not interpret :'var', and shell-substituting
    # values into the SQL string would lean on parameter pattern hygiene the
    # migration child does not own. The heredoc is UNQUOTED so $VARS expand via
    # the shell before psql sees them; \set then re-quotes them properly for
    # SQL via :'name' substitution inside the INSERT statements.
    ensure_sp_container = ContainerDefinition(
        Name="ensure-storage-profile",
        Image=Ref("DbInitImage"),
        Essential=False,
        DependsOn=[ContainerDependency(ContainerName="migrator", Condition="SUCCESS")],
        EntryPoint=["sh", "-c"],
        Command=[
            "set -e\n"
            "export PGSSLMODE=require PGPASSWORD=\"$CONFIGDB_PASSWORD\"\n"
            "psql -v ON_ERROR_STOP=1 -e \\\n"
            "  -h \"$CONFIGDB_HOST\" -p \"$CONFIGDB_PORT\" \\\n"
            "  -U \"$CONFIGDB_USER\" -d \"$CONFIGDB_DBNAME\" <<SQL\n"
            "\\set bucket '$BUCKET_NAME'\n"
            "\\set region '$AWS_REGION_NAME'\n"
            "\\set org    '$ORG_ID'\n"
            "\\echo === ensure-storage-profile inputs ===\n"
            "\\echo  bucket = :'bucket'\n"
            "\\echo  region = :'region'\n"
            "\\echo  org    = :'org'\n"
            "\\echo === state BEFORE upsert ===\n"
            "SELECT id, bucket_name, cloud_provider, region FROM bucket_configurations\n"
            "  WHERE bucket_name = :'bucket';\n"
            "SELECT id, organization_id, bucket_id, instance_num, collector_name\n"
            "  FROM organization_buckets;\n"
            "\\echo === upsert bucket_configurations ===\n"
            "INSERT INTO bucket_configurations\n"
            "  (bucket_name, cloud_provider, region, use_path_style)\n"
            "VALUES (:'bucket', 'aws', :'region', TRUE)\n"
            "ON CONFLICT (bucket_name) DO NOTHING\n"
            "RETURNING id, bucket_name;\n"
            "\\echo === upsert organization_buckets ===\n"
            "INSERT INTO organization_buckets\n"
            "  (organization_id, bucket_id, instance_num, collector_name)\n"
            "SELECT (:'org')::uuid, id, 1, 'lakerunner'\n"
            "FROM bucket_configurations WHERE bucket_name = :'bucket'\n"
            "ON CONFLICT (organization_id, bucket_id, instance_num, collector_name)\n"
            "DO NOTHING\n"
            "RETURNING id, organization_id, bucket_id, instance_num, collector_name;\n"
            "\\echo === state AFTER upsert ===\n"
            "SELECT id, bucket_name, cloud_provider, region FROM bucket_configurations\n"
            "  WHERE bucket_name = :'bucket';\n"
            "SELECT id, organization_id, bucket_id, instance_num, collector_name\n"
            "  FROM organization_buckets;\n"
            "\\echo === done ===\n"
            "SQL\n"
        ],
        Environment=[
            Environment(Name="CONFIGDB_HOST", Value=Ref("DbEndpoint")),
            Environment(Name="CONFIGDB_PORT", Value=Ref("DbPort")),
            Environment(Name="CONFIGDB_DBNAME", Value="configdb"),
            Environment(Name="BUCKET_NAME", Value=Ref("IngestBucketName")),
            Environment(Name="AWS_REGION_NAME", Value=Ref("AWS::Region")),
            Environment(Name="ORG_ID", Value=Ref("OrgId")),
        ],
        Secrets=[
            Secret(Name="CONFIGDB_USER", ValueFrom=Sub("${DbSecretArn}:username::")),
            Secret(Name="CONFIGDB_PASSWORD", ValueFrom=Sub("${DbSecretArn}:password::")),
        ],
        LogConfiguration=_logs("ensure-storage-profile"),
    )

    keepalive_container = ContainerDefinition(
        Name="keepalive",
        Image=Ref("DbInitImage"),
        # The single essential container. ECS will not start it until the
        # canonical-profile upsert has exited 0, so the task -- and thus the
        # service, and thus this nested stack -- only reaches a stable RUNNING
        # state after both the migrator and the upsert succeed.
        Essential=True,
        DependsOn=[ContainerDependency(ContainerName="ensure-storage-profile", Condition="SUCCESS")],
        EntryPoint=["sh", "-c"],
        Command=["echo 'migrations complete; idling'; exec sleep 2147483647"],
        LogConfiguration=_logs("keepalive"),
    )

    task_def = t.add_resource(
        TaskDefinition(
            "MigratorTaskDef",
            Family="cardinal-migrator",
            NetworkMode="awsvpc",
            RequiresCompatibilities=["FARGATE"],
            RuntimePlatform=RuntimePlatform(
                CpuArchitecture="ARM64",
                OperatingSystemFamily="LINUX",
            ),
            Cpu="256",
            Memory="512",
            ExecutionRoleArn=Ref("ExecutionRoleArn"),
            TaskRoleArn=Ref("TaskRoleArn"),
            ContainerDefinitions=[
                configdb_init_container,
                migrator_container,
                ensure_sp_container,
                keepalive_container,
            ],
            Tags=cardinal_tags(component="migration", role="migrator-task"),
        )
    )

    # ---------------------------------------------------------------------------
    # Migration service: desired count 1, runs the migrator task and idles.
    # DesiredCount is intentionally hardcoded (not a parameter): an operator may
    # `aws ecs update-service --desired-count 0` to reclaim the slot -- that is
    # harmless CFN drift, and the next LakerunnerImage bump re-applies 1 and
    # reruns migrations. A parameter at 0 would instead suppress migrations on
    # the next image bump.
    # ---------------------------------------------------------------------------
    migrator_service = build_ecs_service(
        service_key="migrator",
        cluster_arn_param="ClusterArn",
        task_definition_ref=task_def,
        desired_count=1,
        subnets_csv_param="PrivateSubnetsCsv",
        security_group_id_param="TaskSecurityGroupId",
        container_name="keepalive",
        capacity="fallback",
    )
    t.add_resource(migrator_service)

    # ---------------------------------------------------------------------------
    # Outputs
    # ---------------------------------------------------------------------------
    t.add_output(
        Output(
            "MigrationServiceArn",
            Value=Ref(migrator_service),
            Description=(
                "ARN of the migration ECS service. Downstream stacks depend on "
                "MigrationStack, which is only complete once this service is "
                "stable -- i.e. once migrations have run."
            ),
        )
    )

    return t


if __name__ == "__main__":
    print(build().to_yaml(), end="")
