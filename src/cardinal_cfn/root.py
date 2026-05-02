"""Root template generator: cardinal-lakerunner.yaml.

The root is a thin orchestrator. It declares the customer-facing parameter
surface, derives the InstallId, and stands up eleven nested children with
TemplateURL pointing at the vendor S3 bucket.
"""

import os

from troposphere import (
    Template,
    Parameter,
    Ref,
    GetAtt,
    Equals,
    Join,
    Not,
    Output,
    Sub,
)
from troposphere.cloudformation import Stack

from cardinal_cfn.install_id import install_id_short, install_id_long
from cardinal_cfn.parameters import add_parameter_group_metadata, add_no_echo_parameter
from cardinal_cfn.images import add_image_override
from cardinal_cfn.defaults import load_defaults


VERSION = os.environ.get("CARDINAL_VERSION", "dev")
DEFAULT_BUCKET_REGION = os.environ.get("CARDINAL_BUCKET_REGION", "us-east-2")
DEFAULT_BUCKET_NAME = os.environ.get("CARDINAL_BUCKET_NAME", "cardinal-cfn")
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
        # Database
        {"name": "DbInstanceClass", "type": "String", "default": "db.t4g.xlarge",
         "description": (
             "RDS DB instance class. Default db.t4g.xlarge (Graviton burstable, "
             "16 GiB RAM, ~1700 max_connections) — enough headroom for "
             "lakerunner's many concurrent service replicas. Bump higher "
             "(e.g. db.r7g.xlarge) for sustained heavy workloads."
         )},
        {"name": "DbAllocatedStorage", "type": "Number", "default": 100, "min": 20,
         "description": "RDS allocated storage in GiB."},
        {"name": "DbEngineVersion", "type": "String", "default": "18.3",
         "description": "PostgreSQL engine version."},
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
        "PublicSubnets",
        Type="List<AWS::EC2::Subnet::Id>",
        Default="",
        Description="Public subnet IDs. Required only when AlbScheme=internet-facing.",
    ))
    t.add_parameter(Parameter(
        "AlbScheme",
        Type="String",
        Default="internal",
        AllowedValues=["internal", "internet-facing"],
        Description="ALB scheme.",
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
    # Sensitive / advanced parameters
    # ---------------------------------------------------------------------
    add_no_echo_parameter(t, "LicenseData", description="License JSON content (required).")
    add_no_echo_parameter(t, "ApiKeysOverride",
                          description="Optional YAML override for API keys.")
    add_no_echo_parameter(t, "StorageProfilesOverride",
                          description="Optional YAML override for storage profiles.")
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
        Description="Base URL for nested-stack templates (override for air-gapped).",
    ))

    # ---------------------------------------------------------------------
    # Sizing parameters
    # ---------------------------------------------------------------------
    sizing_specs = _sizing_param_specs(defaults)
    _add_sizing_parameters(t, sizing_specs)
    sizing_param_names = [s["name"] for s in sizing_specs]

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
    # Conditions
    # ---------------------------------------------------------------------
    t.add_condition("HasPublicSubnets",
                    Not(Equals(Join(",", Ref("PublicSubnets")), "")))

    # ---------------------------------------------------------------------
    # Console grouping
    # ---------------------------------------------------------------------
    add_parameter_group_metadata(
        t,
        groups=[
            {"label": "Networking",
             "parameters": ["VpcId", "PrivateSubnets", "PublicSubnets",
                            "AlbScheme", "CertificateArn",
                            "CertificateBody", "CertificatePrivateKey",
                            "CertificateChain"]},
            {"label": "Sizing", "parameters": sizing_param_names},
            {"label": "Images", "parameters": image_param_names},
            {"label": "Advanced",
             "parameters": ["LicenseData", "ApiKeysOverride",
                            "StorageProfilesOverride",
                            "DexAdminEmail", "DexAdminPasswordHash",
                            "OidcSuperadminEmails",
                            "TemplateBaseUrl"]},
        ],
    )

    # ---------------------------------------------------------------------
    # Install-id derivation (computed inline; not a parameter on root)
    # ---------------------------------------------------------------------
    install_short = install_id_short()
    install_long = install_id_long()
    private_subnets_csv = Join(",", Ref("PrivateSubnets"))
    public_subnets_csv = Join(",", Ref("PublicSubnets"))

    # ---------------------------------------------------------------------
    # Nested children
    # ---------------------------------------------------------------------
    cluster_stack = _add_child(t, "ClusterStack", "cluster.yaml", {
        "InstallIdShort": install_short,
        "InstallIdLong": install_long,
        "VpcId": Ref("VpcId"),
    })

    database_stack = _add_child(t, "DatabaseStack", "database.yaml", {
        "InstallIdShort": install_short,
        "InstallIdLong": install_long,
        "VpcId": Ref("VpcId"),
        "TaskSecurityGroupId": GetAtt(cluster_stack, "Outputs.TaskSecurityGroupId"),
        "PrivateSubnetsCsv": private_subnets_csv,
        "DbInstanceClass": Ref("DbInstanceClass"),
        "DbAllocatedStorage": Ref("DbAllocatedStorage"),
        "DbEngineVersion": Ref("DbEngineVersion"),
    }, depends_on=["ClusterStack"])

    storage_stack = _add_child(t, "StorageStack", "storage.yaml", {
        "InstallIdShort": install_short,
        "InstallIdLong": install_long,
    })

    config_stack = _add_child(t, "ConfigStack", "config.yaml", {
        "InstallIdShort": install_short,
        "InstallIdLong": install_long,
        "LicenseData": Ref("LicenseData"),
        "ApiKeysOverride": Ref("ApiKeysOverride"),
        "StorageProfilesOverride": Ref("StorageProfilesOverride"),
    })

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
        "PublicSubnetsCsv": public_subnets_csv,
        "PrivateSubnetsCsv": private_subnets_csv,
        "AlbScheme": Ref("AlbScheme"),
        "TaskSecurityGroupId": GetAtt(cluster_stack, "Outputs.TaskSecurityGroupId"),
        "CertificateArn": GetAtt(cert_stack, "Outputs.EffectiveCertificateArn"),
    }, depends_on=["ClusterStack", "CertStack"])

    migration_stack = _add_child(t, "MigrationStack", "migration.yaml", {
        "InstallIdShort": install_short,
        "InstallIdLong": install_long,
        "ClusterArn": GetAtt(cluster_stack, "Outputs.ClusterArn"),
        "ClusterName": GetAtt(cluster_stack, "Outputs.ClusterName"),
        "TaskSecurityGroupId": GetAtt(cluster_stack, "Outputs.TaskSecurityGroupId"),
        "ExecutionRoleArn": GetAtt(cluster_stack, "Outputs.ExecutionRoleArn"),
        "PrivateSubnetsCsv": private_subnets_csv,
        "DbEndpoint": GetAtt(database_stack, "Outputs.DbEndpoint"),
        "DbPort": GetAtt(database_stack, "Outputs.DbPort"),
        "DbName": GetAtt(database_stack, "Outputs.DbName"),
        "DbSecretArn": GetAtt(database_stack, "Outputs.DbSecretArn"),
        "LakerunnerImage": lakerunner_image,
        "DbInitImage": Ref("DbInitImage"),
    })

    migration_complete = GetAtt(migration_stack, "Outputs.MigrationCustomResourceRef")

    # Common cross-stack parameter dict shared by query / process / control.
    # Each tier extends with its own sizing parameters as needed.
    def _service_tier_common() -> dict:
        return {
            "InstallIdShort": install_short,
            "InstallIdLong": install_long,
            "ClusterArn": GetAtt(cluster_stack, "Outputs.ClusterArn"),
            "TaskSecurityGroupId": GetAtt(cluster_stack, "Outputs.TaskSecurityGroupId"),
            "ExecutionRoleArn": GetAtt(cluster_stack, "Outputs.ExecutionRoleArn"),
            "PrivateSubnetsCsv": private_subnets_csv,
            "DbEndpoint": GetAtt(database_stack, "Outputs.DbEndpoint"),
            "DbPort": GetAtt(database_stack, "Outputs.DbPort"),
            "DbSecretArn": GetAtt(database_stack, "Outputs.DbSecretArn"),
            "BucketName": GetAtt(storage_stack, "Outputs.BucketName"),
            "QueueUrl": GetAtt(storage_stack, "Outputs.QueueUrl"),
            "QueueArn": GetAtt(storage_stack, "Outputs.QueueArn"),
            "LicenseSecretArn": GetAtt(config_stack, "Outputs.LicenseSecretArn"),
            "InternalServiceKeysSecretArn":
                GetAtt(config_stack, "Outputs.InternalServiceKeysSecretArn"),
            "ApiKeysParamName": GetAtt(config_stack, "Outputs.ApiKeysParamName"),
            "StorageProfilesParamName":
                GetAtt(config_stack, "Outputs.StorageProfilesParamName"),
            "MigrationComplete": migration_complete,
            "LakerunnerImage": lakerunner_image,
        }

    services_query_params = _service_tier_common()
    services_query_params.update({
        "HttpsListenerArn": GetAtt(alb_stack, "Outputs.HttpsListenerArn"),
        "VpcId": Ref("VpcId"),
        "ClusterName": GetAtt(cluster_stack, "Outputs.ClusterName"),
        "ServiceNamespaceId": GetAtt(cluster_stack, "Outputs.ServiceNamespaceId"),
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
        "AdminApiKeySecretArn": GetAtt(config_stack, "Outputs.AdminApiKeySecretArn"),
        "VpcId": Ref("VpcId"),
        "ServiceNamespaceName": GetAtt(cluster_stack, "Outputs.ServiceNamespaceName"),
        # Inputs for the monitoring service's ECS autoscaler. The service-name
        # outputs come from services-process; the replica Refs are the same
        # values that gate process-* DesiredCount in services-process so the
        # autoscaler max tracks the customer's deploy-time setting.
        "ClusterName": GetAtt(cluster_stack, "Outputs.ClusterName"),
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
        "ClusterArn": GetAtt(cluster_stack, "Outputs.ClusterArn"),
        "TaskSecurityGroupId": GetAtt(cluster_stack, "Outputs.TaskSecurityGroupId"),
        "ExecutionRoleArn": GetAtt(cluster_stack, "Outputs.ExecutionRoleArn"),
        "PrivateSubnetsCsv": private_subnets_csv,
        "BucketName": GetAtt(storage_stack, "Outputs.BucketName"),
        "QueueArn": GetAtt(storage_stack, "Outputs.QueueArn"),
        "LicenseSecretArn": GetAtt(config_stack, "Outputs.LicenseSecretArn"),
        "InternalServiceKeysSecretArn":
            GetAtt(config_stack, "Outputs.InternalServiceKeysSecretArn"),
        "ApiKeysParamName": GetAtt(config_stack, "Outputs.ApiKeysParamName"),
        "StorageProfilesParamName":
            GetAtt(config_stack, "Outputs.StorageProfilesParamName"),
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
        "ClusterArn": GetAtt(cluster_stack, "Outputs.ClusterArn"),
        "TaskSecurityGroupId": GetAtt(cluster_stack, "Outputs.TaskSecurityGroupId"),
        "ExecutionRoleArn": GetAtt(cluster_stack, "Outputs.ExecutionRoleArn"),
        "PrivateSubnetsCsv": private_subnets_csv,
        "VpcId": Ref("VpcId"),
        "HttpsListenerArn": GetAtt(alb_stack, "Outputs.HttpsListenerArn"),
        "AlbDnsName": GetAtt(alb_stack, "Outputs.AlbDnsName"),
        "DbEndpoint": GetAtt(database_stack, "Outputs.DbEndpoint"),
        "DbPort": GetAtt(database_stack, "Outputs.DbPort"),
        "DbSecretArn": GetAtt(database_stack, "Outputs.DbSecretArn"),
        "LicenseSecretArn": GetAtt(config_stack, "Outputs.LicenseSecretArn"),
        "InternalServiceKeysSecretArn":
            GetAtt(config_stack, "Outputs.InternalServiceKeysSecretArn"),
        "ApiKeysParamName": GetAtt(config_stack, "Outputs.ApiKeysParamName"),
        "StorageProfilesParamName":
            GetAtt(config_stack, "Outputs.StorageProfilesParamName"),
        "MigrationComplete": migration_complete,
        "MaestroImage": maestro_image,
        "DexImage": dex_image,
        "DbInitImage": db_init_image,
        "DexInitImage": dex_init_image,
        "MaestroTaskCpu": Ref("MaestroTaskCpu"),
        "MaestroTaskMemory": Ref("MaestroTaskMemory"),
        "DexClientId": Ref("DexClientId"),
        "DexAdminEmail": Ref("DexAdminEmail"),
        "DexAdminPasswordHash": Ref("DexAdminPasswordHash"),
        "OidcSuperadminEmails": Ref("OidcSuperadminEmails"),
    }, depends_on=["MigrationStack"])

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
                        Description="Base URL for the maestro UI."))

    return t


def _add_child(t: Template, logical_id: str, child_filename: str,
               parameters: dict, depends_on: list[str] | None = None):
    """Add an AWS::CloudFormation::Stack resource with a Sub-rendered TemplateURL."""
    kwargs: dict = dict(
        TemplateURL=Sub("${TemplateBaseUrl}" + child_filename),
        Parameters=parameters,
    )
    if depends_on:
        kwargs["DependsOn"] = depends_on
    return t.add_resource(Stack(logical_id, **kwargs))


if __name__ == "__main__":
    print(build().to_yaml(), end="")
