"""Root template generator: cardinal-lakerunner.yaml.

The root is a thin orchestrator. It declares the customer-facing parameter
surface and stands up the nested children with TemplateURL pointing at the
vendor S3 bucket.

All infra (RDS, S3 ingest, SQS, secrets, SSM parameters, ECS cluster, Cloud
Map namespace) is created out-of-band by ``scripts/data-setup.sh``; its
identifiers arrive as parameters. All IAM roles and security groups are
pre-created by the customer's IT and passed in as ARN/ID parameters. The
lakerunner stack itself only owns ECS task-tier resources, the ALB and its
listener rules, and the migration / cert custom resources -- the application,
nothing else.
"""

import os

from troposphere import (
    Template,
    Parameter,
    Ref,
    GetAtt,
    Join,
    Output,
    Sub,
    Equals,
)
from troposphere.cloudformation import Stack

from cardinal_cfn.install_id import install_id_short, install_id_long
from cardinal_cfn.parameters import add_parameter_group_metadata, add_no_echo_parameter
from cardinal_cfn.images import add_image_override
from cardinal_cfn.defaults import load_defaults


VERSION = os.environ.get("CARDINAL_VERSION", "dev")
# us-east-1 is the publishing source of truth; us-east-2 is populated via
# S3 bucket replication. Override CARDINAL_BUCKET_NAME / CARDINAL_BUCKET_REGION
# for air-gapped builds that target a customer-owned mirror.
DEFAULT_BUCKET_REGION = os.environ.get("CARDINAL_BUCKET_REGION", "us-east-1")
DEFAULT_BUCKET_NAME = os.environ.get("CARDINAL_BUCKET_NAME", "cardinal-cfn-us-east-1")
DEFAULT_TEMPLATE_BASE_URL = (
    f"https://{DEFAULT_BUCKET_NAME}.s3.{DEFAULT_BUCKET_REGION}.amazonaws.com"
    f"/lakerunner/{VERSION}/cardinal-lakerunner/"
)


# ---------------------------------------------------------------------------
# Sizing-parameter table. Each entry maps a root parameter name to the
# cardinal-defaults.yaml lookup that produces its default. The table keeps
# _add_sizing_parameters and the console-grouping list in sync.
# ---------------------------------------------------------------------------
def _sizing_param_specs(defaults: dict) -> list[dict]:
    services = defaults["services"]
    otel_cfg = defaults["otel"]["otel-gateway"]
    maestro_cfg = defaults["maestro"]

    api = services["lakerunner-query-api"]
    worker = services["lakerunner-query-worker"]
    logs = services["lakerunner-process-logs"]
    metrics = services["lakerunner-process-metrics"]
    traces = services["lakerunner-process-traces"]
    pubsub = services["lakerunner-pubsub-sqs"]

    def _max_replicas(cfg: dict) -> int:
        autoscaling = cfg.get("autoscaling")
        if autoscaling and "max_replicas" in autoscaling:
            return int(autoscaling["max_replicas"])
        return int(cfg["replicas"])

    return [
        # Query tier
        {"name": "QueryApiReplicas", "type": "Number", "default": int(api["replicas"]),
         "min": 1, "description": "Desired replicas for lakerunner-query-api."},
        {"name": "QueryApiCpu", "type": "String", "default": str(api["cpu"]),
         "description": "Fargate CPU units for lakerunner-query-api."},
        {"name": "QueryApiMemory", "type": "String", "default": str(api["memory_mib"]),
         "description": "Fargate memory (MiB) for lakerunner-query-api."},
        {"name": "QueryWorkerReplicas", "type": "Number", "default": int(worker["replicas"]),
         "min": 1, "description": "Desired replicas for lakerunner-query-worker."},
        {"name": "QueryWorkerCpu", "type": "String", "default": str(worker["cpu"]),
         "description": "Fargate CPU units for lakerunner-query-worker."},
        {"name": "QueryWorkerMemory", "type": "String", "default": str(worker["memory_mib"]),
         "description": "Fargate memory (MiB) for lakerunner-query-worker."},
        # Process tier
        {"name": "ProcessLogsReplicas", "type": "Number", "default": _max_replicas(logs),
         "min": 1, "description": "Maximum desired replicas for lakerunner-process-logs."},
        {"name": "ProcessLogsMemory", "type": "String", "default": str(logs["memory_mib"]),
         "description": "Fargate memory (MiB) for lakerunner-process-logs."},
        {"name": "ProcessMetricsReplicas", "type": "Number", "default": _max_replicas(metrics),
         "min": 1, "description": "Maximum desired replicas for lakerunner-process-metrics."},
        {"name": "ProcessMetricsMemory", "type": "String", "default": str(metrics["memory_mib"]),
         "description": "Fargate memory (MiB) for lakerunner-process-metrics."},
        {"name": "ProcessTracesReplicas", "type": "Number", "default": _max_replicas(traces),
         "min": 1, "description": "Maximum desired replicas for lakerunner-process-traces."},
        {"name": "ProcessTracesMemory", "type": "String", "default": str(traces["memory_mib"]),
         "description": "Fargate memory (MiB) for lakerunner-process-traces."},
        {"name": "PubsubSqsReplicas", "type": "Number", "default": int(pubsub["replicas"]),
         "min": 1, "description": "Desired replicas for lakerunner-pubsub-sqs."},
        # Maestro
        {"name": "MaestroTaskCpu", "type": "String", "default": str(maestro_cfg["task"]["cpu"]),
         "description": "Fargate CPU units for the maestro task definition."},
        {"name": "MaestroTaskMemory", "type": "String",
         "default": str(maestro_cfg["task"]["memory_mib"]),
         "description": "Fargate memory (MiB) for the maestro task definition."},
        {"name": "DexClientId", "type": "String",
         "default": str(maestro_cfg["dex"]["client_id"]),
         "description": "OIDC client ID the maestro UI uses against DEX."},
        {"name": "McpMigrateRecoverFromDirty", "type": "String",
         "default": "false", "allowed_values": ["true", "false"],
         "description": "When true, mcp-gateway tries to recover from a "
                        "previously failed (dirty) maestro DB migration on startup."},
        # OTEL
        {"name": "OtelReplicas", "type": "Number", "default": int(otel_cfg["replicas"]),
         "min": 1, "description": "Desired replicas for the otel-gateway service."},
        {"name": "OtelCpu", "type": "String", "default": str(otel_cfg["cpu"]),
         "description": "Fargate CPU units for the otel-gateway service."},
        {"name": "OtelMemory", "type": "String", "default": str(otel_cfg["memory_mib"]),
         "description": "Fargate memory (MiB) for the otel-gateway service."},
        {"name": "OtelExposeOnAlb", "type": "String", "default": "No",
         "allowed_values": ["Yes", "No"],
         "description": "When Yes, attach the otel-gateway to the shared ALB."},
        {"name": "OtelConfigYaml", "type": "String", "default": "",
         "description": "Optional inline OTEL collector config YAML override."},
    ]


def _add_sizing_parameters(t: Template, specs: list[dict]) -> None:
    for spec in specs:
        kwargs = {
            "Type": spec["type"],
            "Default": spec["default"],
            "Description": spec["description"],
        }
        if "min" in spec:
            kwargs["MinValue"] = spec["min"]
        if "allowed_values" in spec:
            kwargs["AllowedValues"] = spec["allowed_values"]
        t.add_parameter(Parameter(spec["name"], **kwargs))


# ---------------------------------------------------------------------------
# Customer-supplied IAM role + SG parameter specs.
# ---------------------------------------------------------------------------
_ROLE_SG_PARAMS = [
    ("TaskRoleArn", "String", None,
     "ARN of the ECS task role used by every lakerunner ECS task."),
    ("ExecutionRoleArn", "String", None,
     "ARN of the ECS task execution role (used at task launch to pull "
     "images and resolve secrets)."),
    ("TaskSgId", "AWS::EC2::SecurityGroup::Id", None,
     "Security group ID applied to every ECS task in the install."),
    ("AlbSgId", "AWS::EC2::SecurityGroup::Id", None,
     "Security group ID applied to the shared ALB."),
]


# ---------------------------------------------------------------------------
# Infra-setup output parameters: identifiers for the resources the
# scripts/data-setup.sh driver already created. Names match the script's
# JSON output keys 1:1. Sensitive values (license/admin) come in as Secret
# ARNs -- the underlying secret values stay in Secrets Manager and are
# never seen by CloudFormation.
# ---------------------------------------------------------------------------
_INFRA_SETUP_PARAMS = [
    ("DbEndpoint", "String", None, "RDS endpoint hostname (infra-setup output)."),
    ("DbPort", "String", "5432", "RDS port."),
    ("DbName", "String", "lakerunner", "Lakerunner database name."),
    ("DbMasterSecretArn", "String", None,
     "ARN of the master DB credentials secret (infra-setup output)."),
    ("MaestroDbSecretArn", "String", None,
     "ARN of the maestro application DB password secret (infra-setup output)."),
    ("IngestBucketName", "String", None,
     "Name of the S3 ingest bucket (infra-setup output)."),
    ("IngestQueueUrl", "String", None,
     "URL of the SQS ingest queue (infra-setup output)."),
    ("IngestQueueArn", "String", None,
     "ARN of the SQS ingest queue (infra-setup output)."),
    ("LicenseSecretArn", "String", None,
     "ARN of the cardinal-license secret (infra-setup output)."),
    ("AdminKeySecretArn", "String", None,
     "ARN of the cardinal-admin-key secret (infra-setup output)."),
    ("StorageProfilesParamName", "String", None,
     "Name of the SSM parameter holding storage_profiles YAML (infra-setup output)."),
    ("ApiKeysParamName", "String", None,
     "Name of the SSM parameter holding api_keys YAML (infra-setup output)."),
    ("ClusterName", "String", None,
     "Name of the ECS cluster (infra-setup output)."),
    ("ClusterArn", "String", None,
     "ARN of the ECS cluster (infra-setup output)."),
    ("ServiceNamespaceId", "String", None,
     "Cloud Map private DNS namespace ID for in-cluster service discovery "
     "(infra-setup output)."),
    ("ServiceNamespaceName", "String", None,
     "Cloud Map private DNS namespace name (infra-setup output)."),
]


def _add_string_params(t: Template, specs: list[tuple]) -> None:
    for name, type_, default, description in specs:
        kwargs = {"Type": type_, "Description": description}
        if default is not None:
            kwargs["Default"] = default
        t.add_parameter(Parameter(name, **kwargs))


def build() -> Template:
    t = Template()
    t.set_description(f"Cardinal Lakerunner root stack ({VERSION}).")

    defaults = load_defaults()

    # ---------------------------------------------------------------------
    # Networking parameters
    # ---------------------------------------------------------------------
    t.add_parameter(Parameter(
        "VpcId",
        Type="AWS::EC2::VPC::Id",
        Description="VPC where Cardinal resources will be deployed.",
    ))
    t.add_parameter(Parameter(
        "PrivateSubnets",
        Type="List<AWS::EC2::Subnet::Id>",
        Description="Private subnet IDs (>=2 across different AZs).",
    ))
    t.add_parameter(Parameter(
        "CertificateArn",
        Type="String",
        Default="",
        Description=(
            "ACM certificate ARN for the ALB HTTPS listener. Leave empty to "
            "import a cert from the CertificateBody / CertificatePrivateKey "
            "parameters instead."
        ),
    ))

    # ---------------------------------------------------------------------
    # Customer-supplied IAM roles / security groups
    # ---------------------------------------------------------------------
    _add_string_params(t, _ROLE_SG_PARAMS)

    # ---------------------------------------------------------------------
    # Infra-setup outputs threaded in as parameters
    # ---------------------------------------------------------------------
    _add_string_params(t, _INFRA_SETUP_PARAMS)

    # ---------------------------------------------------------------------
    # Sensitive / advanced parameters
    # ---------------------------------------------------------------------
    add_no_echo_parameter(
        t, "CertificateBody",
        description=(
            "PEM-encoded certificate. Required when CertificateArn is empty; "
            "ignored otherwise."
        ),
    )
    add_no_echo_parameter(
        t, "CertificatePrivateKey",
        description=(
            "PEM-encoded private key. Required when CertificateArn is empty; "
            "ignored otherwise."
        ),
    )
    add_no_echo_parameter(
        t, "CertificateChain",
        description=(
            "Optional PEM-encoded chain of intermediate certificates. Used "
            "when CertificateArn is empty."
        ),
    )
    t.add_parameter(Parameter(
        "DexAdminEmail",
        Type="String",
        Default="admin@cardinal.local",
        Description="Email address for the DEX local-DB admin login.",
    ))
    add_no_echo_parameter(
        t, "DexAdminPasswordHash",
        description=(
            "Bcrypt hash ($2a$/$2b$/$2y$) of the DEX admin password. Required."
        ),
    )
    t.add_parameter(Parameter(
        "OidcSuperadminEmails",
        Type="String",
        Default="admin@cardinal.local",
        Description=(
            "Comma-separated email allowlist that grants maestro superadmin. "
            "Default matches DexAdminEmail."
        ),
    ))
    t.add_parameter(Parameter(
        "TemplateBaseUrl",
        Type="String",
        Default=DEFAULT_TEMPLATE_BASE_URL,
        AllowedPattern=r"^https://.+/$",
        ConstraintDescription="TemplateBaseUrl must be an https:// URL ending with '/'.",
        Description=(
            "Base URL (must end with '/') for nested-stack templates. "
            "Example: https://<bucket>.s3.<region>.amazonaws.com/lakerunner/<version>/cardinal-lakerunner/. "
            "Override for air-gapped installs."
        ),
    ))

    # ---------------------------------------------------------------------
    # Sizing parameters
    # ---------------------------------------------------------------------
    sizing_specs = _sizing_param_specs(defaults)
    _add_sizing_parameters(t, sizing_specs)
    sizing_param_names = [s["name"] for s in sizing_specs]

    # ---------------------------------------------------------------------
    # Feature toggles
    # ---------------------------------------------------------------------
    t.add_parameter(Parameter(
        "DeployMaestro",
        Type="String",
        Default="Yes",
        AllowedValues=["Yes", "No"],
        Description=(
            "When No, the MaestroStack nested stack is skipped entirely. "
            "Flip to No to recover the overall stack if maestro fails to "
            "create or update."
        ),
    ))

    # ---------------------------------------------------------------------
    # Image overrides
    # ---------------------------------------------------------------------
    lakerunner_image = add_image_override(
        t, name="LakerunnerImage",
        default=defaults["images"]["lakerunner"],
        description="Lakerunner container image.")
    maestro_image = add_image_override(
        t, name="MaestroImage",
        default=defaults["images"]["maestro"],
        description="Maestro container image.")
    otel_image = add_image_override(
        t, name="OtelImage",
        default=defaults["images"]["otel"],
        description="OTEL collector container image.")
    dex_image = add_image_override(
        t, name="DexImage",
        default=defaults["images"]["dex"],
        description="DEX OIDC container image.")
    db_init_image = add_image_override(
        t, name="DbInitImage",
        default=defaults["images"]["db_init"],
        description="psql-capable bootstrapper container image (maestro db-init).")
    dex_init_image = add_image_override(
        t, name="DexInitImage",
        default=defaults["images"]["dex_init"],
        description="BusyBox-style image used to render the dex config.yaml.")
    image_param_names = [
        "LakerunnerImage",
        "MaestroImage",
        "OtelImage",
        "DexImage",
        "DbInitImage",
        "DexInitImage",
    ]

    # ---------------------------------------------------------------------
    # Console grouping
    # ---------------------------------------------------------------------
    add_parameter_group_metadata(
        t,
        groups=[
            {"label": "Networking",
             "parameters": ["VpcId", "PrivateSubnets", "CertificateArn",
                            "CertificateBody", "CertificatePrivateKey",
                            "CertificateChain"]},
            {"label": "IAM roles + security groups",
             "parameters": [name for name, *_ in _ROLE_SG_PARAMS]},
            {"label": "Infra-setup outputs",
             "parameters": [name for name, *_ in _INFRA_SETUP_PARAMS]},
            {"label": "Sizing", "parameters": sizing_param_names},
            {"label": "Images", "parameters": image_param_names},
            {"label": "Advanced",
             "parameters": ["DeployMaestro",
                            "DexAdminEmail", "DexAdminPasswordHash",
                            "OidcSuperadminEmails",
                            "TemplateBaseUrl"]},
        ],
    )

    # ---------------------------------------------------------------------
    # Conditions
    # ---------------------------------------------------------------------
    t.add_condition("DeployMaestroEnabled", Equals(Ref("DeployMaestro"), "Yes"))

    # ---------------------------------------------------------------------
    # Install-id derivation (computed inline; not a parameter on root)
    # ---------------------------------------------------------------------
    install_short = install_id_short()
    install_long = install_id_long()
    private_subnets_csv = Join(",", Ref("PrivateSubnets"))

    # ---------------------------------------------------------------------
    # Nested children
    # ---------------------------------------------------------------------
    cert_stack = _add_child(t, "CertStack", "cert.yaml", {
        "InstallIdShort": install_short,
        "InstallIdLong": install_long,
        "CertificateArn": Ref("CertificateArn"),
        "CertificateBody": Ref("CertificateBody"),
        "CertificatePrivateKey": Ref("CertificatePrivateKey"),
        "CertificateChain": Ref("CertificateChain"),
    })

    alb_stack = _add_child(t, "AlbStack", "alb.yaml", {
        "InstallIdShort": install_short,
        "InstallIdLong": install_long,
        "VpcId": Ref("VpcId"),
        "PrivateSubnetsCsv": private_subnets_csv,
        "AlbSgId": Ref("AlbSgId"),
        "CertificateArn": GetAtt(cert_stack, "Outputs.EffectiveCertificateArn"),
    }, depends_on=["CertStack"])

    migration_stack = _add_child(t, "MigrationStack", "migration.yaml", {
        "InstallIdShort": install_short,
        "InstallIdLong": install_long,
        "ClusterArn": Ref("ClusterArn"),
        "ClusterName": Ref("ClusterName"),
        "TaskSecurityGroupId": Ref("TaskSgId"),
        "ExecutionRoleArn": Ref("ExecutionRoleArn"),
        "TaskRoleArn": Ref("TaskRoleArn"),
        "PrivateSubnetsCsv": private_subnets_csv,
        "DbEndpoint": Ref("DbEndpoint"),
        "DbPort": Ref("DbPort"),
        "DbName": Ref("DbName"),
        "DbSecretArn": Ref("DbMasterSecretArn"),
        "LakerunnerImage": lakerunner_image,
        "DbInitImage": Ref("DbInitImage"),
    })

    migration_complete = GetAtt(migration_stack, "Outputs.MigrationServiceArn")

    # Common cross-stack parameter dict shared by query / process / control.
    # Each tier extends with its own sizing parameters as needed.
    def _service_tier_common() -> dict:
        return {
            "InstallIdShort": install_short,
            "InstallIdLong": install_long,
            "ClusterArn": Ref("ClusterArn"),
            "TaskSecurityGroupId": Ref("TaskSgId"),
            "ExecutionRoleArn": Ref("ExecutionRoleArn"),
            "TaskRoleArn": Ref("TaskRoleArn"),
            "PrivateSubnetsCsv": private_subnets_csv,
            "DbEndpoint": Ref("DbEndpoint"),
            "DbPort": Ref("DbPort"),
            "DbSecretArn": Ref("DbMasterSecretArn"),
            "BucketName": Ref("IngestBucketName"),
            "QueueUrl": Ref("IngestQueueUrl"),
            "QueueArn": Ref("IngestQueueArn"),
            "LicenseSecretArn": Ref("LicenseSecretArn"),
            "ApiKeysParamName": Ref("ApiKeysParamName"),
            "StorageProfilesParamName": Ref("StorageProfilesParamName"),
            "MigrationComplete": migration_complete,
            "LakerunnerImage": lakerunner_image,
        }

    services_query_params = _service_tier_common()
    services_query_params.update({
        "HttpsListenerArn": GetAtt(alb_stack, "Outputs.HttpsListenerArn"),
        "VpcId": Ref("VpcId"),
        "ClusterName": Ref("ClusterName"),
        "ServiceNamespaceId": Ref("ServiceNamespaceId"),
        "QueryApiReplicas": Ref("QueryApiReplicas"),
        "QueryApiCpu": Ref("QueryApiCpu"),
        "QueryApiMemory": Ref("QueryApiMemory"),
        "QueryWorkerReplicas": Ref("QueryWorkerReplicas"),
        "QueryWorkerCpu": Ref("QueryWorkerCpu"),
        "QueryWorkerMemory": Ref("QueryWorkerMemory"),
    })
    _add_child(t, "ServicesQueryStack", "services-query.yaml",
               services_query_params, depends_on=["MigrationStack"])

    services_process_params = _service_tier_common()
    services_process_params.update({
        "ProcessLogsReplicas": Ref("ProcessLogsReplicas"),
        "ProcessLogsMemory": Ref("ProcessLogsMemory"),
        "ProcessMetricsReplicas": Ref("ProcessMetricsReplicas"),
        "ProcessMetricsMemory": Ref("ProcessMetricsMemory"),
        "ProcessTracesReplicas": Ref("ProcessTracesReplicas"),
        "ProcessTracesMemory": Ref("ProcessTracesMemory"),
        "PubsubSqsReplicas": Ref("PubsubSqsReplicas"),
    })
    services_process_stack = _add_child(
        t, "ServicesProcessStack", "services-process.yaml",
        services_process_params, depends_on=["MigrationStack"])

    services_control_params = _service_tier_common()
    services_control_params.update({
        "HttpsListenerArn": GetAtt(alb_stack, "Outputs.HttpsListenerArn"),
        "AdminHttpsListenerArn": GetAtt(alb_stack, "Outputs.AdminHttpsListenerArn"),
        "AdminApiKeySecretArn": Ref("AdminKeySecretArn"),
        "VpcId": Ref("VpcId"),
        "ServiceNamespaceName": Ref("ServiceNamespaceName"),
        "ClusterName": Ref("ClusterName"),
        "ProcessLogsServiceName":
            GetAtt(services_process_stack, "Outputs.ProcessLogsServiceName"),
        "ProcessMetricsServiceName":
            GetAtt(services_process_stack, "Outputs.ProcessMetricsServiceName"),
        "ProcessTracesServiceName":
            GetAtt(services_process_stack, "Outputs.ProcessTracesServiceName"),
        "ProcessLogsReplicas": Ref("ProcessLogsReplicas"),
        "ProcessMetricsReplicas": Ref("ProcessMetricsReplicas"),
        "ProcessTracesReplicas": Ref("ProcessTracesReplicas"),
    })
    _add_child(t, "ServicesControlStack", "services-control.yaml",
               services_control_params,
               depends_on=["MigrationStack", "ServicesProcessStack"])

    _add_child(t, "OtelStack", "otel.yaml", {
        "InstallIdShort": install_short,
        "InstallIdLong": install_long,
        "ClusterArn": Ref("ClusterArn"),
        "TaskSecurityGroupId": Ref("TaskSgId"),
        "ExecutionRoleArn": Ref("ExecutionRoleArn"),
        "TaskRoleArn": Ref("TaskRoleArn"),
        "PrivateSubnetsCsv": private_subnets_csv,
        "BucketName": Ref("IngestBucketName"),
        "QueueArn": Ref("IngestQueueArn"),
        "LicenseSecretArn": Ref("LicenseSecretArn"),
        "ApiKeysParamName": Ref("ApiKeysParamName"),
        "StorageProfilesParamName": Ref("StorageProfilesParamName"),
        "HttpsListenerArn": GetAtt(alb_stack, "Outputs.HttpsListenerArn"),
        "VpcId": Ref("VpcId"),
        "OtelImage": otel_image,
        "OtelReplicas": Ref("OtelReplicas"),
        "OtelCpu": Ref("OtelCpu"),
        "OtelMemory": Ref("OtelMemory"),
        "OtelExposeOnAlb": Ref("OtelExposeOnAlb"),
        "OtelConfigYaml": Ref("OtelConfigYaml"),
    })

    maestro_stack = _add_child(t, "MaestroStack", "maestro.yaml", {
        "InstallIdShort": install_short,
        "InstallIdLong": install_long,
        "ClusterArn": Ref("ClusterArn"),
        "TaskSecurityGroupId": Ref("TaskSgId"),
        "ExecutionRoleArn": Ref("ExecutionRoleArn"),
        "TaskRoleArn": Ref("TaskRoleArn"),
        "PrivateSubnetsCsv": private_subnets_csv,
        "VpcId": Ref("VpcId"),
        "HttpsListenerArn": GetAtt(alb_stack, "Outputs.HttpsListenerArn"),
        "AlbDnsName": GetAtt(alb_stack, "Outputs.AlbDnsName"),
        "DbEndpoint": Ref("DbEndpoint"),
        "DbPort": Ref("DbPort"),
        "DbSecretArn": Ref("DbMasterSecretArn"),
        "MaestroDbSecretArn": Ref("MaestroDbSecretArn"),
        "LicenseSecretArn": Ref("LicenseSecretArn"),
        "ApiKeysParamName": Ref("ApiKeysParamName"),
        "StorageProfilesParamName": Ref("StorageProfilesParamName"),
        "MigrationComplete": migration_complete,
        "MaestroImage": maestro_image,
        "DexImage": dex_image,
        "DbInitImage": db_init_image,
        "DexInitImage": dex_init_image,
        "MaestroTaskCpu": Ref("MaestroTaskCpu"),
        "MaestroTaskMemory": Ref("MaestroTaskMemory"),
        "McpMigrateRecoverFromDirty": Ref("McpMigrateRecoverFromDirty"),
        "DexClientId": Ref("DexClientId"),
        "DexAdminEmail": Ref("DexAdminEmail"),
        "DexAdminPasswordHash": Ref("DexAdminPasswordHash"),
        "OidcSuperadminEmails": Ref("OidcSuperadminEmails"),
    }, depends_on=["MigrationStack"], condition="DeployMaestroEnabled")

    # ---------------------------------------------------------------------
    # Top-level outputs
    # ---------------------------------------------------------------------
    t.add_output(Output("InstallIdShort", Value=install_short,
                        Description="Short per-install identifier."))
    t.add_output(Output("InstallIdLong", Value=install_long,
                        Description="Long per-install identifier."))
    t.add_output(Output("AlbDnsName",
                        Value=GetAtt(alb_stack, "Outputs.AlbDnsName"),
                        Description="DNS name of the shared Cardinal ALB."))
    t.add_output(Output(
        "QueryApiUrl",
        Value=Sub(
            "https://${AlbDns}/api/v1/query/",
            AlbDns=GetAtt(alb_stack, "Outputs.AlbDnsName"),
        ),
        Description="Base URL for the lakerunner query API.",
    ))
    t.add_output(Output("MaestroUrl",
                        Value=GetAtt(maestro_stack, "Outputs.MaestroUrl"),
                        Description="Base URL for the maestro UI.",
                        Condition="DeployMaestroEnabled"))

    return t


def _add_child(t: Template, logical_id: str, child_filename: str,
               parameters: dict, depends_on: list[str] | None = None,
               condition: str | None = None):
    """Add an AWS::CloudFormation::Stack resource with a Sub-rendered TemplateURL."""
    kwargs: dict = dict(
        TemplateURL=Sub("${TemplateBaseUrl}" + child_filename),
        Parameters=parameters,
    )
    if depends_on:
        kwargs["DependsOn"] = depends_on
    if condition:
        kwargs["Condition"] = condition
    return t.add_resource(Stack(logical_id, **kwargs))


if __name__ == "__main__":
    print(build().to_yaml(), end="")
