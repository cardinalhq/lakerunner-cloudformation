"""Standalone application-tier root generator: cardinal-lakerunner-services.yaml.

This is a transform of ``root.py``. It stands up the application-tier nested
children (cert, alb, migration, services-query/process/control, maestro)
UNCHANGED, but is fully **parameter-driven**: it creates NO IAM roles,
NO security groups, NO RDS, NO buckets, NO secrets, NO SSM parameters. Every
such value arrives as a parameter (driver-wired from ``lakerunner-infra-base``
and ``lakerunner-infra-rds`` outputs).

The only structural change vs ``root.py`` is that the ``Security`` child is
removed; the SG ids and role ARNs it used to emit now arrive as root
parameters (``AlbSecurityGroupId`` ... ``MaestroRoleArn``, ``ExecutionRoleArn``
...). The cooked bucket from base replaces the ingest bucket, and the SQS queue
is optional.

## Deferred (interim limitations)

- ALB minimization is deferred: query-api and admin-api still attach to the
  ALB (minimization would require child edits in services-query/control/alb).
- The process-tier SQS primary queue (group 0) is driver-supplied via the
  ``QueueUrl`` / ``QueueRoleArn`` parameters, set on the pubsub-sqs container as
  plain ``SQS_QUEUE_URL`` / ``SQS_ROLE_ARN`` env vars. Empty idles the service.
- The cooked bucket backs both otel-raw writes and cooked data in the
  single-account interim.
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
    Not,
)
from troposphere.cloudformation import Stack
from troposphere.servicediscovery import PrivateDnsNamespace

from cardinal_cfn.children.services_process import MAX_ADDITIONAL_QUEUES
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
        {"name": "PubsubAutoRegister", "type": "String", "default": "true",
         "allowed_values": ["true", "false"],
         "description": "Enable pubsub-sqs auto-registration of unseen satellite raw buckets."},
        {"name": "PubsubAutoRegisterWritesToInstance", "type": "String", "default": "1",
         "description": (
             "Central cooked-bucket instance_num pubsub-sqs auto-registered orgs write to. "
             "Required when PubsubAutoRegister is true."
         )},
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
# lakerunner-infra-base / lakerunner-infra-rds outputs (driver-wired).
# Sensitive values (license/admin) arrive as Secret ARNs -- the underlying
# secret values stay in Secrets Manager and are never seen by CloudFormation.
# ---------------------------------------------------------------------------
_INFRA_SETUP_PARAMS = [
    ("DbEndpoint", "String", None, "RDS endpoint hostname (infra output)."),
    ("DbPort", "String", "5432", "RDS port."),
    ("DbName", "String", "lakerunner", "Lakerunner database name."),
    ("DbMasterSecretArn", "String", None,
     "ARN of the master DB credentials secret (infra output)."),
    ("CookedBucketName", "String", None,
     "Name of the S3 cooked bucket (base infra output). Backs both otel-raw "
     "writes and cooked data in the single-account interim."),
    ("QueueUrl", "String", "",
     "SQS queue URL for the pubsub-sqs primary queue (group 0). Set as the "
     "SQS_QUEUE_URL env var on the pubsub-sqs container. Empty idles the "
     "service."),
    ("QueueRoleArn", "String", "",
     "IAM role ARN the pubsub-sqs service STS-assumes for the primary queue "
     "and its bucket (group 0). Set as the SQS_ROLE_ARN env var."),
    ("LicenseSecretArn", "String", None,
     "ARN of the cardinal-license secret (infra output)."),
    ("AdminKeySecretArn", "String", None,
     "ARN of the cardinal-admin-key secret (infra output)."),
    ("ClusterName", "String", None,
     "Name of the ECS cluster (customer-supplied)."),
    ("ClusterArn", "String", None,
     "ARN of the ECS cluster (customer-supplied)."),
]


# ---------------------------------------------------------------------------
# Security-tier inputs: SG ids and role ARNs the (removed) Security child used
# to output. Driver-wired from lakerunner-infra-base outputs.
# ---------------------------------------------------------------------------
_SECURITY_GROUP_PARAMS = [
    ("AlbSecurityGroupId", "Security group ID for the ALB."),
    ("MigrationSecurityGroupId", "Security group ID for the migration task."),
    ("QuerySecurityGroupId", "Security group ID for the query tier."),
    ("ProcessSecurityGroupId", "Security group ID for the process tier."),
    ("ControlSecurityGroupId", "Security group ID for the control tier."),
    ("MaestroSecurityGroupId", "Security group ID for maestro."),
]

_ROLE_PARAMS = [
    ("ExecutionRoleArn", "ARN of the shared ECS task execution role."),
    ("MigrationRoleArn", "ARN of the migration task role."),
    ("QueryRoleArn", "ARN of the query-tier task role."),
    ("ProcessRoleArn", "ARN of the process-tier task role."),
    ("ControlRoleArn", "ARN of the control-tier task role."),
    ("MaestroRoleArn", "ARN of the maestro task role."),
]


def _add_string_params(t: Template, specs: list[tuple]) -> None:
    for name, type_, default, description in specs:
        kwargs = {"Type": type_, "Description": description}
        if default is not None:
            kwargs["Default"] = default
        t.add_parameter(Parameter(name, **kwargs))


def build() -> Template:
    t = Template()
    t.set_description(f"Cardinal Lakerunner services (application-tier) stack ({VERSION}).")

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
        Description=(
            "Private subnet IDs (>=2 across different AZs). All ECS tasks "
            "run here. When AlbScheme=internal (default), the ALB also "
            "lives in these subnets."
        ),
    ))
    t.add_parameter(Parameter(
        "AlbScheme",
        Type="String",
        Default="internal",
        AllowedValues=["internal", "internet-facing"],
        Description=(
            "ALB scheme. Default 'internal': ALB is private and reachable "
            "only from inside the VPC (production posture). "
            "'internet-facing' exposes the ALB to the public internet -- "
            "useful in test/dev environments where OIDC redirect URLs "
            "need to resolve from a developer's browser. When set to "
            "'internet-facing' you MUST supply PublicSubnets. ALB SG ingress "
            "(including any 0.0.0.0/0 rules) is configured on the supplied "
            "AlbSecurityGroupId by lakerunner-infra-base, not here."
        ),
    ))
    t.add_parameter(Parameter(
        "PublicSubnets",
        Type="CommaDelimitedList",
        Default="",
        Description=(
            "Public subnet IDs (>=2 across different AZs). Required when "
            "AlbScheme=internet-facing; ignored otherwise. The ALB attaches "
            "to these so its DNS resolves to internet-routable IPs."
        ),
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
    # Cloud Map (in-cluster service discovery) namespace name
    # ---------------------------------------------------------------------
    t.add_parameter(Parameter(
        "ServiceNamespaceName",
        Type="String",
        Default="cardinal.local",
        AllowedPattern=r"^[a-z0-9]([a-z0-9.-]{0,61}[a-z0-9])?$",
        Description=(
            "Private DNS namespace name created for in-cluster service "
            "discovery (query-api.<name>:8080, ...)."
        ),
    ))

    # ---------------------------------------------------------------------
    # Infra-stack outputs threaded in as parameters
    # ---------------------------------------------------------------------
    _add_string_params(t, _INFRA_SETUP_PARAMS)

    # Additional satellite queue groups (1..MAX_ADDITIONAL_QUEUES), forwarded to
    # the process child where they become SQS_QUEUE_URL_<n>/_REGION_<n>/_ROLE_ARN_<n>
    # on the pubsub-sqs container.
    for n in range(1, MAX_ADDITIONAL_QUEUES + 1):
        _add_string_params(t, [
            (f"QueueUrl{n}", "String", "",
             f"SQS queue URL for additional satellite queue group {n} "
             "(empty skips it)."),
            (f"QueueRegion{n}", "String", "",
             f"AWS region for additional satellite queue group {n} "
             "(empty uses the stack region)."),
            (f"QueueRoleArn{n}", "String", "",
             f"IAM role ARN pubsub-sqs STS-assumes for additional satellite "
             f"queue group {n}."),
        ])

    # ---------------------------------------------------------------------
    # Security-tier inputs (SG ids + role ARNs); replace the Security child.
    # ---------------------------------------------------------------------
    for name, description in _SECURITY_GROUP_PARAMS:
        t.add_parameter(Parameter(
            name, Type="AWS::EC2::SecurityGroup::Id", Description=description))
    for name, description in _ROLE_PARAMS:
        t.add_parameter(Parameter(name, Type="String", Description=description))

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
        AllowedPattern=(
            r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
            r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
        ),
        Description=(
            "Organization UUID for this install (operator-chosen, no default). "
            "Must match every satellite's OrganizationId. Maestro pre-populates "
            "this org and provisions it into the local lakerunner (org, storage "
            "line, and ingest key) via the /api/v1/provision admin API; nothing "
            "else seeds org content."
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
        "SelfTelemetryEndpoint",
        Type="String",
        Default="",
        Description=(
            "OTLP/HTTP endpoint for lakerunner self-telemetry (e.g. the "
            "satellite collector's CollectorEndpoint, http://<alb>:4318). "
            "Empty disables self-telemetry."
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
    dex_image = add_image_override(
        t, name="DexImage",
        default=defaults["images"]["dex"],
        description="DEX OIDC container image.")
    db_init_image = add_image_override(
        t, name="DbInitImage",
        default=defaults["images"]["db_init"],
        description="psql-capable bootstrapper container image (maestro db-init).")
    image_param_names = [
        "LakerunnerImage",
        "MaestroImage",
        "DexImage",
        "DbInitImage",
    ]

    # ---------------------------------------------------------------------
    # Console grouping
    # ---------------------------------------------------------------------
    add_parameter_group_metadata(
        t,
        groups=[
            {"label": "Networking",
             "parameters": ["VpcId", "PrivateSubnets",
                            "AlbScheme", "PublicSubnets",
                            "ServiceNamespaceName",
                            "CertificateArn",
                            "CertificateBody", "CertificatePrivateKey",
                            "CertificateChain"]},
            {"label": "Infrastructure-stack outputs",
             "parameters": ([name for name, *_ in _INFRA_SETUP_PARAMS]
                            + ["SelfTelemetryEndpoint"])},
            {"label": "Security-tier inputs",
             "parameters": ([name for name, _ in _SECURITY_GROUP_PARAMS]
                            + [name for name, _ in _ROLE_PARAMS])},
            {"label": "Sizing", "parameters": sizing_param_names},
            {"label": "Images", "parameters": image_param_names},
            {"label": "Advanced",
             "parameters": ["DeployMaestro",
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

    # Self-telemetry exports to a driver-supplied OTLP/HTTP endpoint (typically
    # the satellite collector's CollectorEndpoint). An empty endpoint disables
    # it; the tier children receive the endpoint and the derived enabled flag.
    t.add_condition(
        "SelfTelemetryOn", Not(Equals(Ref("SelfTelemetryEndpoint"), "")))
    self_telemetry_endpoint = Ref("SelfTelemetryEndpoint")
    self_telemetry_enabled = If("SelfTelemetryOn", "true", "false")

    # ---------------------------------------------------------------------
    # Install-id derivation (computed inline; not a parameter on root)
    # ---------------------------------------------------------------------
    install_short = install_id_short()
    install_long = install_id_long()
    private_subnets_csv = Join(",", Ref("PrivateSubnets"))

    # ALB subnet selection. When AlbScheme=internet-facing, the ALB attaches
    # to the public subnets so its DNS resolves to internet-routable IPs;
    # otherwise it stays in the private subnets the rest of the tier uses.
    t.add_condition("AlbIsInternetFacing", Equals(Ref("AlbScheme"), "internet-facing"))
    alb_subnets_csv = If(
        "AlbIsInternetFacing",
        Join(",", Ref("PublicSubnets")),
        private_subnets_csv,
    )

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
    # Security-tier inputs bound from parameters (replaces the Security child).
    # Keep the same local names as root.py so the wiring below is untouched.
    # ---------------------------------------------------------------------
    sec_alb_sg = Ref("AlbSecurityGroupId")
    sec_migration_sg = Ref("MigrationSecurityGroupId")
    sec_query_sg = Ref("QuerySecurityGroupId")
    sec_process_sg = Ref("ProcessSecurityGroupId")
    sec_control_sg = Ref("ControlSecurityGroupId")
    sec_maestro_sg = Ref("MaestroSecurityGroupId")

    exec_role = Ref("ExecutionRoleArn")
    migration_role = Ref("MigrationRoleArn")
    query_role = Ref("QueryRoleArn")
    process_role = Ref("ProcessRoleArn")
    control_role = Ref("ControlRoleArn")
    maestro_role = Ref("MaestroRoleArn")

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
        "AlbSubnetsCsv": alb_subnets_csv,
        "Scheme": Ref("AlbScheme"),
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
            "BucketName": Ref("CookedBucketName"),
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
        "QueueUrl": Ref("QueueUrl"),
        "QueueRoleArn": Ref("QueueRoleArn"),
        "ProcessLogsReplicas": Ref("ProcessLogsReplicas"),
        "ProcessLogsMemory": Ref("ProcessLogsMemory"),
        "ProcessMetricsReplicas": Ref("ProcessMetricsReplicas"),
        "ProcessMetricsMemory": Ref("ProcessMetricsMemory"),
        "ProcessTracesReplicas": Ref("ProcessTracesReplicas"),
        "ProcessTracesMemory": Ref("ProcessTracesMemory"),
        "PubsubSqsReplicas": Ref("PubsubSqsReplicas"),
        "PubsubAutoRegister": Ref("PubsubAutoRegister"),
        "PubsubAutoRegisterWritesToInstance": Ref("PubsubAutoRegisterWritesToInstance"),
    })
    for n in range(1, MAX_ADDITIONAL_QUEUES + 1):
        services_process_params[f"QueueUrl{n}"] = Ref(f"QueueUrl{n}")
        services_process_params[f"QueueRegion{n}"] = Ref(f"QueueRegion{n}")
        services_process_params[f"QueueRoleArn{n}"] = Ref(f"QueueRoleArn{n}")
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
        "BucketName": Ref("CookedBucketName"),
        "LicenseSecretArn": Ref("LicenseSecretArn"),
        "MigrationComplete": migration_complete,
        "MaestroImage": maestro_image,
        "DexImage": dex_image,
        "DbInitImage": db_init_image,
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
