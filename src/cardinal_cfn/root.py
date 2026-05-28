"""Root template generator: cardinal-lakerunner.yaml.

The root is a thin orchestrator. It declares the customer-facing parameter
surface and stands up the nested children with TemplateURL pointing at the
vendor S3 bucket.

Data resources (RDS, S3 ingest bucket, SQS, secrets, SSM parameters) are
created by ``cardinal-infrastructure.yaml`` and threaded into this stack
as parameters. Every security group and IAM role the lakerunner tier
needs is created by the ``Security`` child stack here -- nothing on the
SG/role surface is customer-supplied any more. The customer's
contributions are: ECS cluster + VPC + private subnets + license token
(plus the small set of feature/sizing knobs).
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
    If,
)
from troposphere.cloudformation import Stack
from troposphere.servicediscovery import PrivateDnsNamespace

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
# Infrastructure-stack outputs threaded in as parameters. These come from
# cardinal-infrastructure.yaml outputs. Sensitive values (license/admin)
# arrive as Secret ARNs -- the underlying secret values stay in Secrets
# Manager and are never seen by CloudFormation.
# ---------------------------------------------------------------------------
_INFRA_SETUP_PARAMS = [
    ("DbEndpoint", "String", None, "RDS endpoint hostname (infra output)."),
    ("DbPort", "String", "5432", "RDS port."),
    ("DbName", "String", "lakerunner", "Lakerunner database name."),
    ("DbMasterSecretArn", "String", None,
     "ARN of the master DB credentials secret (infra output)."),
    ("RdsSecurityGroupId", "AWS::EC2::SecurityGroup::Id", None,
     "Security group ID attached to the RDS instance (infra output). "
     "The Security child adds tier-specific 5432 ingress rules to it."),
    ("IngestBucketName", "String", None,
     "Name of the S3 ingest bucket (infra output)."),
    ("IngestQueueUrl", "String", None,
     "URL of the SQS ingest queue (infra output)."),
    ("IngestQueueArn", "String", None,
     "ARN of the SQS ingest queue (infra output)."),
    ("LicenseSecretArn", "String", None,
     "ARN of the cardinal-license secret (infra output)."),
    ("AdminKeySecretArn", "String", None,
     "ARN of the cardinal-admin-key secret (infra output)."),
    ("StorageProfilesParamName", "String", None,
     "Name of the SSM parameter holding storage_profiles YAML (infra output)."),
    ("ApiKeysParamName", "String", None,
     "Name of the SSM parameter holding api_keys YAML (infra output)."),
    ("ClusterName", "String", None,
     "Name of the ECS cluster (customer-supplied)."),
    ("ClusterArn", "String", None,
     "ARN of the ECS cluster (customer-supplied)."),
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
    # ALB inbound CIDRs. Up to three; blanks are skipped. Default RFC1918.
    # ---------------------------------------------------------------------
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

    # ---------------------------------------------------------------------
    # Cloud Map (in-cluster service discovery) namespace name
    # ---------------------------------------------------------------------
    t.add_parameter(Parameter(
        "ServiceNamespaceName",
        Type="String",
        Default="cardinal.local",
        AllowedPattern=r"^[a-z0-9]([a-z0-9.-]{0,61}[a-z0-9])?$",
        Description=(
            "Private DNS namespace name created for in-cluster service "
            "discovery (cardinal-otel.<name>:4318, query-api.<name>:8080, ...)."
        ),
    ))

    # ---------------------------------------------------------------------
    # Infra-stack outputs threaded in as parameters
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
        "OrganizationId",
        Type="String",
        Default="12340000-0000-4000-8000-000000000000",
        Description=(
            "Canonical single-install organization UUID. Must match the value "
            "the infrastructure stack seeded into storage-profiles / api-keys; "
            "Maestro pre-populates this org and wires it to the local lakerunner."
        ),
    ))
    t.add_parameter(Parameter(
        "OrgName",
        Type="String",
        Default="My Organization",
        Description="Display name for the org Maestro pre-populates.",
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
            "When No, the Maestro nested stack is skipped entirely. "
            "Flip to No to recover the overall stack if maestro fails to "
            "create or update."
        ),
    ))
    t.add_parameter(Parameter(
        "SelfTelemetry",
        Type="String",
        Default="Yes",
        AllowedValues=["Yes", "No"],
        Description=(
            "When Yes, lakerunner tasks emit OTLP telemetry to the in-cluster "
            "otel-collector at cardinal-otel.<namespace>:4318 (OTLP/HTTP)."
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
             "parameters": ["VpcId", "PrivateSubnets",
                            "AlbAllowedCidr1", "AlbAllowedCidr2", "AlbAllowedCidr3",
                            "ServiceNamespaceName",
                            "CertificateArn",
                            "CertificateBody", "CertificatePrivateKey",
                            "CertificateChain"]},
            {"label": "Infrastructure-stack outputs",
             "parameters": [name for name, *_ in _INFRA_SETUP_PARAMS]},
            {"label": "Sizing", "parameters": sizing_param_names},
            {"label": "Images", "parameters": image_param_names},
            {"label": "Advanced",
             "parameters": ["DeployMaestro", "SelfTelemetry",
                            "DexAdminEmail", "DexAdminPasswordHash",
                            "OidcSuperadminEmails",
                            "OrganizationId", "OrgName",
                            "TemplateBaseUrl"]},
        ],
    )

    # ---------------------------------------------------------------------
    # Conditions
    # ---------------------------------------------------------------------
    t.add_condition("DeployMaestroEnabled", Equals(Ref("DeployMaestro"), "Yes"))
    t.add_condition("SelfTelemetryEnabled", Equals(Ref("SelfTelemetry"), "Yes"))

    # Endpoint resolves at deploy time. When disabled the URL is blanked so
    # the OTel SDK has nothing to dial -- ENABLE_OTLP_TELEMETRY=false is the
    # actual gate, the empty URL is belt + suspenders. Port 4318 is OTLP/HTTP;
    # lakerunner's OTel SDK uses the HTTP exporter, so 4317 (gRPC) returns
    # HTTP/2 SETTINGS frames the SDK can't parse.
    self_telemetry_endpoint = If(
        "SelfTelemetryEnabled",
        Sub("http://cardinal-otel.${ServiceNamespaceName}:4318"),
        "",
    )
    self_telemetry_enabled = If("SelfTelemetryEnabled", "true", "false")

    # ---------------------------------------------------------------------
    # Install-id derivation (computed inline; not a parameter on root)
    # ---------------------------------------------------------------------
    install_short = install_id_short()
    install_long = install_id_long()
    private_subnets_csv = Join(",", Ref("PrivateSubnets"))

    # ---------------------------------------------------------------------
    # Cloud Map private DNS namespace (created by the root, not a child)
    # ---------------------------------------------------------------------
    namespace = t.add_resource(PrivateDnsNamespace(
        "ServiceNamespace",
        Name=Ref("ServiceNamespaceName"),
        Vpc=Ref("VpcId"),
        Description="Cardinal in-cluster service discovery namespace.",
    ))
    namespace_id = GetAtt(namespace, "Id")
    namespace_name = Ref("ServiceNamespaceName")

    # ---------------------------------------------------------------------
    # Security child (SGs + IAM). Instantiated first; every other child
    # consumes its outputs.
    # ---------------------------------------------------------------------
    security_stack = _add_child(t, "Security", "security.yaml", {
        "VpcId": Ref("VpcId"),
        "AlbAllowedCidr1": Ref("AlbAllowedCidr1"),
        "AlbAllowedCidr2": Ref("AlbAllowedCidr2"),
        "AlbAllowedCidr3": Ref("AlbAllowedCidr3"),
        "RdsSecurityGroupId": Ref("RdsSecurityGroupId"),
        "ClusterArn": Ref("ClusterArn"),
        "BucketName": Ref("IngestBucketName"),
        "QueueArn": Ref("IngestQueueArn"),
        "DbMasterSecretArn": Ref("DbMasterSecretArn"),
        "LicenseSecretArn": Ref("LicenseSecretArn"),
        "AdminKeySecretArn": Ref("AdminKeySecretArn"),
        "StorageProfilesParamName": Ref("StorageProfilesParamName"),
        "ApiKeysParamName": Ref("ApiKeysParamName"),
    })

    sec_alb_sg = GetAtt(security_stack, "Outputs.AlbSecurityGroupId")
    sec_migration_sg = GetAtt(security_stack, "Outputs.MigrationSecurityGroupId")
    sec_query_sg = GetAtt(security_stack, "Outputs.QuerySecurityGroupId")
    sec_process_sg = GetAtt(security_stack, "Outputs.ProcessSecurityGroupId")
    sec_control_sg = GetAtt(security_stack, "Outputs.ControlSecurityGroupId")
    sec_otel_sg = GetAtt(security_stack, "Outputs.OtelSecurityGroupId")
    sec_maestro_sg = GetAtt(security_stack, "Outputs.MaestroSecurityGroupId")

    exec_role = GetAtt(security_stack, "Outputs.ExecutionRoleArn")
    migration_role = GetAtt(security_stack, "Outputs.MigrationRoleArn")
    query_role = GetAtt(security_stack, "Outputs.QueryRoleArn")
    process_role = GetAtt(security_stack, "Outputs.ProcessRoleArn")
    control_role = GetAtt(security_stack, "Outputs.ControlRoleArn")
    otel_role = GetAtt(security_stack, "Outputs.OtelRoleArn")
    maestro_role = GetAtt(security_stack, "Outputs.MaestroRoleArn")

    # ---------------------------------------------------------------------
    # Nested children
    # ---------------------------------------------------------------------
    cert_stack = _add_child(t, "Cert", "cert.yaml", {
        "InstallIdShort": install_short,
        "InstallIdLong": install_long,
        "CertificateArn": Ref("CertificateArn"),
        "CertificateBody": Ref("CertificateBody"),
        "CertificatePrivateKey": Ref("CertificatePrivateKey"),
        "CertificateChain": Ref("CertificateChain"),
    })

    alb_stack = _add_child(t, "Alb", "alb.yaml", {
        "InstallIdShort": install_short,
        "InstallIdLong": install_long,
        "VpcId": Ref("VpcId"),
        "PrivateSubnetsCsv": private_subnets_csv,
        "AlbSgId": sec_alb_sg,
        "CertificateArn": GetAtt(cert_stack, "Outputs.EffectiveCertificateArn"),
    }, depends_on=["Cert"])

    migration_stack = _add_child(t, "Migration", "migration.yaml", {
        "InstallIdShort": install_short,
        "InstallIdLong": install_long,
        "ClusterArn": Ref("ClusterArn"),
        "ClusterName": Ref("ClusterName"),
        "TaskSecurityGroupId": sec_migration_sg,
        "ExecutionRoleArn": exec_role,
        "TaskRoleArn": migration_role,
        "PrivateSubnetsCsv": private_subnets_csv,
        "DbEndpoint": Ref("DbEndpoint"),
        "DbPort": Ref("DbPort"),
        "DbName": Ref("DbName"),
        "DbSecretArn": Ref("DbMasterSecretArn"),
        "StorageProfilesParamName": Ref("StorageProfilesParamName"),
        "ApiKeysParamName": Ref("ApiKeysParamName"),
        "OrgId": Ref("OrganizationId"),
        "IngestBucketName": Ref("IngestBucketName"),
        "LakerunnerImage": lakerunner_image,
        "DbInitImage": db_init_image,
    })

    migration_complete = GetAtt(migration_stack, "Outputs.MigrationServiceArn")

    # Common cross-stack parameter dict shared by query / process / control.
    # Each tier extends with its own sizing parameters and tier-specific SG +
    # task role.
    def _service_tier_common(*, task_sg, task_role) -> dict:
        return {
            "InstallIdShort": install_short,
            "InstallIdLong": install_long,
            "ClusterArn": Ref("ClusterArn"),
            "ClusterName": Ref("ClusterName"),
            "TaskSecurityGroupId": task_sg,
            "ExecutionRoleArn": exec_role,
            "TaskRoleArn": task_role,
            "PrivateSubnetsCsv": private_subnets_csv,
            "DbEndpoint": Ref("DbEndpoint"),
            "DbPort": Ref("DbPort"),
            "DbSecretArn": Ref("DbMasterSecretArn"),
            "BucketName": Ref("IngestBucketName"),
            "QueueUrl": Ref("IngestQueueUrl"),
            "QueueArn": Ref("IngestQueueArn"),
            "LicenseSecretArn": Ref("LicenseSecretArn"),
            "MigrationComplete": migration_complete,
            "LakerunnerImage": lakerunner_image,
            "SelfTelemetryEndpoint": self_telemetry_endpoint,
            "SelfTelemetryEnabled": self_telemetry_enabled,
        }

    services_query_params = _service_tier_common(
        task_sg=sec_query_sg, task_role=query_role,
    )
    services_query_params.update({
        "HttpsListenerArn": GetAtt(alb_stack, "Outputs.HttpsListenerArn"),
        "VpcId": Ref("VpcId"),
        "ServiceNamespaceId": namespace_id,
        "QueryApiReplicas": Ref("QueryApiReplicas"),
        "QueryApiCpu": Ref("QueryApiCpu"),
        "QueryApiMemory": Ref("QueryApiMemory"),
        "QueryWorkerReplicas": Ref("QueryWorkerReplicas"),
        "QueryWorkerCpu": Ref("QueryWorkerCpu"),
        "QueryWorkerMemory": Ref("QueryWorkerMemory"),
    })
    _add_child(t, "Query", "services-query.yaml",
               services_query_params, depends_on=["Migration"])

    services_process_params = _service_tier_common(
        task_sg=sec_process_sg, task_role=process_role,
    )
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
        t, "Process", "services-process.yaml",
        services_process_params, depends_on=["Migration"])

    services_control_params = _service_tier_common(
        task_sg=sec_control_sg, task_role=control_role,
    )
    services_control_params.update({
        "HttpsListenerArn": GetAtt(alb_stack, "Outputs.HttpsListenerArn"),
        "AdminHttpsListenerArn": GetAtt(alb_stack, "Outputs.AdminHttpsListenerArn"),
        "AdminApiKeySecretArn": Ref("AdminKeySecretArn"),
        "VpcId": Ref("VpcId"),
        "ServiceNamespaceName": namespace_name,
        "ServiceNamespaceId": namespace_id,
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
    _add_child(t, "Control", "services-control.yaml",
               services_control_params,
               depends_on=["Migration", "Process"])

    _add_child(t, "Otel", "otel.yaml", {
        "InstallIdShort": install_short,
        "InstallIdLong": install_long,
        "ClusterArn": Ref("ClusterArn"),
        "TaskSecurityGroupId": sec_otel_sg,
        "ExecutionRoleArn": exec_role,
        "TaskRoleArn": otel_role,
        "PrivateSubnetsCsv": private_subnets_csv,
        "BucketName": Ref("IngestBucketName"),
        "QueueArn": Ref("IngestQueueArn"),
        "LicenseSecretArn": Ref("LicenseSecretArn"),
        "OtelHttpListenerArn": GetAtt(alb_stack, "Outputs.OtelHttpListenerArn"),
        "AlbDnsName": GetAtt(alb_stack, "Outputs.AlbDnsName"),
        "VpcId": Ref("VpcId"),
        "ServiceNamespaceId": namespace_id,
        "ServiceNamespaceName": namespace_name,
        "OtelImage": otel_image,
        "OtelReplicas": Ref("OtelReplicas"),
        "OtelCpu": Ref("OtelCpu"),
        "OtelMemory": Ref("OtelMemory"),
        "OtelConfigYaml": Ref("OtelConfigYaml"),
    })

    maestro_stack = _add_child(t, "Maestro", "maestro.yaml", {
        "InstallIdShort": install_short,
        "InstallIdLong": install_long,
        "ClusterArn": Ref("ClusterArn"),
        "TaskSecurityGroupId": sec_maestro_sg,
        "ExecutionRoleArn": exec_role,
        "TaskRoleArn": maestro_role,
        "PrivateSubnetsCsv": private_subnets_csv,
        "VpcId": Ref("VpcId"),
        "HttpsListenerArn": GetAtt(alb_stack, "Outputs.HttpsListenerArn"),
        "AlbDnsName": GetAtt(alb_stack, "Outputs.AlbDnsName"),
        "ServiceNamespaceName": namespace_name,
        "DbEndpoint": Ref("DbEndpoint"),
        "DbPort": Ref("DbPort"),
        "DbSecretArn": Ref("DbMasterSecretArn"),
        "LicenseSecretArn": Ref("LicenseSecretArn"),
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
        "AdminApiKeySecretArn": Ref("AdminKeySecretArn"),
        "OrganizationId": Ref("OrganizationId"),
        "OrgName": Ref("OrgName"),
    }, depends_on=["Migration"], condition="DeployMaestroEnabled")

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
