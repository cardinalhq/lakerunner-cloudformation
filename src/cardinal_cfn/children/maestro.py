"""maestro.yaml nested stack: maestro + DEX ECS Fargate service.

Owns a SINGLE ECS Fargate service running a five-container task definition:

  1. db-init       (Essential=False, runs psql to create the maestro database)
  2. mcp-gateway   (Essential=True, runs maestro DB schema migrations on
                    startup as the DB superuser -- so CREATE EXTENSION in the
                    migrations succeeds -- then serves the MCP gateway port)
  3. wait-for-mcp  (Essential=False, blocks until mcp-gateway has finished
                    its on-startup migrations so maestro doesn't race them)
  4. maestro      (Essential=True, listens on the maestro HTTPS port)
  5. dex          (Essential=True, listens on the DEX OIDC port; renders its
                    own config from CFN-supplied OIDC params -- DexAdminEmail,
                    DexAdminPasswordHash, OidcSuperadminEmails -- via the dex
                    image's gomplate entrypoint, so no dex-init sidecar).
                    Omitted, with its target group, listener rule and log
                    group, when OidcIssuerUrl names a customer-supplied IdP.

Container dependsOn graph: db-init -> mcp-gateway -> wait-for-mcp -> maestro.
Both maestro and dex attach to the shared cardinal HTTPS listener via two
ListenerRules:

  /*          -> maestro container, priority 49999 (catch-all default app)
  /dex/*      -> dex container,     priority 210

The shared ALB and its certificate are owned by the alb child stack.
"""

from troposphere import (
    Condition,
    Equals,
    GetAtt,
    If,
    Not,
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
    RuntimePlatform,
    Secret,
    Service,
    TaskDefinition,
)
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

# Listener-rule registration keys (see listener_priorities.py).
_MAESTRO_LISTENER_KEY = "maestro-https"
_DEX_LISTENER_KEY = "maestro-dex"

# Customer-supplied OIDC IdP settings, mirroring the maestro helm chart's
# OIDC_* / MCP_OAUTH_* env vars. A non-empty OidcIssuerUrl replaces the bundled
# DEX. The root re-declares these via add_external_oidc_parameters() and the
# services driver sets each from the same-named environment variable.
# (parameter, env var, kind, description); "str" is omitted from the container
# env when empty, "bool" is always passed.
EXTERNAL_OIDC_PARAMS = [
    ("OidcIssuerUrl", "OIDC_ISSUER_URL", "str",
     "Issuer URL of a customer-supplied OIDC IdP (Okta, Entra ID, Keycloak, "
     "...). Set to replace the bundled DEX; empty keeps DEX."),
    ("OidcClientId", "OIDC_CLIENT_ID", "str",
     "OAuth client_id the maestro UI sends to the external IdP (a public "
     "PKCE client). Empty defaults to OidcAudience. Ignored with DEX."),
    ("OidcAudience", "OIDC_AUDIENCE", "str",
     "Expected aud claim on external IdP tokens. Empty means maestro-ui. "
     "Ignored with DEX."),
    ("OidcJwksUrl", "OIDC_JWKS_URL", "str",
     "External IdP JWKS URL. Empty means "
     "<issuer>/protocol/openid-connect/certs (Keycloak); set it for other "
     "IdPs, e.g. <issuer>/v1/keys for Okta. Ignored with DEX."),
    ("OidcSuperadminGroup", "OIDC_SUPERADMIN_GROUP", "str",
     "IdP group whose members get maestro superadmin. Empty leaves "
     "maestro's default (maestro-superadmin) and does not request the "
     "groups scope."),
    ("OidcEmailClaim", "OIDC_EMAIL_CLAIM", "str",
     "Token claim holding the user's email. Empty means email."),
    ("OidcEmailVerifiedClaim", "OIDC_EMAIL_VERIFIED_CLAIM", "str",
     "Token claim holding the email-verified flag. Empty means "
     "email_verified."),
    ("OidcDisplayNameClaims", "OIDC_DISPLAY_NAME_CLAIMS", "str",
     "Comma-separated claims tried for the display name. Empty means "
     "name,preferred_username,email."),
    ("OidcGroupsClaim", "OIDC_GROUPS_CLAIM", "str",
     "Token claim holding group membership. Empty means groups."),
    ("OidcExternalIdClaim", "OIDC_EXTERNAL_ID_CLAIM", "str",
     "Token claim used as the persistent account key. Empty means sub. "
     "Changing it on a live install is a migration event."),
    ("OidcTrustUnverifiedEmails", "OIDC_TRUST_UNVERIFIED_EMAILS", "bool",
     "When true, accept emails the IdP does not mark verified (e.g. Entra "
     "ID, which omits email_verified)."),
    ("McpOauthEnabled", "MCP_OAUTH_ENABLED", "bool",
     "When true, MCP clients sign in to /mcp with OAuth against the external "
     "IdP instead of an API key. Requires OidcIssuerUrl."),
    ("McpOauthIssuer", "MCP_OAUTH_ISSUER", "str",
     "Authorization server advertised to MCP clients. Empty uses "
     "OidcIssuerUrl; its tokens must still carry iss=OidcIssuerUrl."),
    ("McpOauthAudience", "MCP_OAUTH_AUDIENCE", "str",
     "aud accepted on /mcp only. Empty means <base URL>/mcp."),
    ("McpOauthAsMetadataProxy", "MCP_OAUTH_AS_METADATA_PROXY", "bool",
     "When true, maestro serves /.well-known/oauth-authorization-server "
     "mirrored from the issuer (for IdPs such as Keycloak realms that "
     "publish only OIDC discovery)."),
    ("McpOauthSelfSignup", "MCP_OAUTH_SELF_SIGNUP", "bool",
     "When true, a new user signing in through an MCP connector gets a "
     "personal workspace even with self-signup off."),
]


def add_external_oidc_parameters(t: Template) -> None:
    for name, _, kind, description in EXTERNAL_OIDC_PARAMS:
        if kind == "bool":
            t.add_parameter(Parameter(
                name, Type="String", Default="false",
                AllowedValues=["true", "false"], Description=description,
            ))
        else:
            t.add_parameter(Parameter(
                name, Type="String", Default="", Description=description,
            ))


def _env_if(condition: str, name: str, value):
    return If(condition, Environment(Name=name, Value=value), Ref("AWS::NoValue"))


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
        Parameter("VpcId", Type="AWS::EC2::VPC::Id", Description="VPC ID (forwarded from root).")
    )
    t.add_parameter(
        Parameter("HttpsListenerArn", Type="String", Description="ARN of the ALB HTTPS listener.")
    )
    t.add_parameter(
        Parameter(
            "AlbDnsName",
            Type="String",
            Description=(
                "Public DNS name the install is reached at (the shared ALB's "
                "DNS name, or a vanity CNAME pointing at it); used to derive "
                "issuer URLs."
            ),
        )
    )
    t.add_parameter(
        Parameter(
            "ServiceNamespaceName",
            Type="String",
            Description=(
                "Cloud Map private DNS namespace name (e.g. cardinal-<id>.local). "
                "Used to reach lakerunner query-api and admin-api directly from "
                "maestro without hairpinning through the ALB."
            ),
        )
    )
    t.add_parameter(Parameter(
        "BucketName",
        Type="String",
        Description=(
            "Central S3 bucket name (the infra stack's cooked/ingest bucket "
            "output). Maestro consumes this only to seed the "
            "MAESTRO_BOOTSTRAP_BUCKET_* env vars so its in-process Lakerunner "
            "provisioning worker writes the organization_buckets storage line "
            "via /api/v1/provision -- the sole writer of that row, since CFN "
            "seeds no org content. No S3 IAM is granted to the maestro task "
            "role -- the bucket name is metadata only."
        ),
    ))
    t.add_parameter(Parameter("DbEndpoint", Type="String", Description="RDS endpoint hostname."))
    t.add_parameter(Parameter("DbPort", Type="String", Default="5432", Description="RDS port."))
    t.add_parameter(
        Parameter(
            "DbSecretArn",
            Type="String",
            Description=(
                "ARN of the master DB secret. db-init uses it to create the "
                "maestro database, and mcp-gateway + maestro connect with it "
                "(the migrations run CREATE EXTENSION, which needs superuser)."
            ),
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
            "AdminApiKeySecretArn",
            Type="String",
            Description=(
                "ARN of the cardinal-admin-key secret. Mounted into the maestro "
                "container as MAESTRO_BOOTSTRAP_LAKERUNNER_ADMIN_API_KEY so the "
                "seeded lakerunner datasource holds the same key admin-api validates."
            ),
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
    t.add_parameter(
        Parameter(
            "DexAdminEmail",
            Type="String",
            Default="admin@cardinal.local",
            Description="Email address for the DEX local-DB admin login.",
        )
    )
    t.add_parameter(
        Parameter(
            "OidcSuperadminEmails",
            Type="String",
            Default="admin@cardinal.local",
            Description=(
                "Comma-separated email allowlist whose holders get maestro "
                "superadmin. Default matches DexAdminEmail so the bundled "
                "DEX admin can bootstrap orgs."
            ),
        )
    )
    t.add_parameter(
        Parameter(
            "OrganizationId",
            Type="String",
            AllowedPattern=(
                r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
            ),
            Description=(
                "Organization UUID for this install (operator-chosen, no default). "
                "Seeded as the org Maestro pre-populates; matches the lakerunner "
                "storage-profiles / api-keys org so both sides own the same data feed."
            ),
        )
    )
    t.add_parameter(
        Parameter(
            "OrgName",
            Type="String",
            Default="My Organization",
            Description="Display name for the pre-populated organization.",
        )
    )
    t.add_parameter(
        Parameter(
            "McpMigrateRecoverFromDirty",
            Type="String",
            Default="false",
            AllowedValues=["true", "false"],
            Description=(
                "When true, sets MCP_MIGRATE_RECOVER_FROM_DIRTY=true on the "
                "mcp-gateway container, allowing it to recover from a "
                "previously failed (dirty) maestro DB migration on startup."
            ),
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
    add_no_echo_parameter(
        t,
        "DexExtraUsers",
        default="",
        description=(
            "Optional additional DEX login accounts, beyond the admin above. "
            "JSON array of objects, each with a required \"email\" and bcrypt "
            "\"hash\" (same form as DexAdminPasswordHash) plus optional "
            "\"username\"/\"userID\": "
            "[{\"email\":\"a@b.com\",\"hash\":\"$2y$...\"}]. Leave empty for "
            "admin-only. Make any of them a superadmin by also adding their "
            "email to OidcSuperadminEmails; otherwise an admin invites them to "
            "an org in the Maestro UI after their first login."
        ),
    )
    add_external_oidc_parameters(t)

    # ---------------------------------------------------------------------
    # Conditions
    # ---------------------------------------------------------------------
    t.add_condition("UseBundledDex", Equals(Ref("OidcIssuerUrl"), ""))
    t.add_condition("McpOauthOn", Equals(Ref("McpOauthEnabled"), "true"))
    for name, _, kind, _ in EXTERNAL_OIDC_PARAMS:
        if kind == "str" and name != "OidcIssuerUrl":
            t.add_condition(f"Has{name}", Not(Equals(Ref(name), "")))

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
                    "VpcId",
                    "HttpsListenerArn",
                    "AlbDnsName",
                    "DbEndpoint",
                    "DbPort",
                    "DbSecretArn",
                    "LicenseSecretArn",
                    "AdminApiKeySecretArn",
                    "MigrationComplete",
                ],
            },
            {
                "label": "Organization bootstrap",
                "parameters": ["OrganizationId", "OrgName"],
            },
            {
                "label": "Maestro tunables",
                "parameters": [
                    "MaestroTaskCpu",
                    "MaestroTaskMemory",
                    "McpMigrateRecoverFromDirty",
                ],
            },
            {
                "label": "Image overrides",
                "parameters": ["MaestroImage", "DexImage", "DbInitImage"],
            },
            {
                "label": "DEX configuration",
                "parameters": [
                    "DexClientId",
                    "DexAdminEmail",
                    "DexAdminPasswordHash",
                    "DexExtraUsers",
                    "OidcSuperadminEmails",
                ],
            },
            {
                "label": "External OIDC IdP (replaces DEX)",
                "parameters": [name for name, *_ in EXTERNAL_OIDC_PARAMS],
            },
        ],
    )

    # ---------------------------------------------------------------------
    # Log groups (one per container so streams stay separable)
    # ---------------------------------------------------------------------
    db_init_lg = t.add_resource(services_common.build_log_group(service_key=_DB_INIT_KEY))
    maestro_lg = t.add_resource(services_common.build_log_group(service_key=_MAESTRO_KEY))
    dex_lg = services_common.build_log_group(service_key=_DEX_KEY)
    dex_lg.Condition = "UseBundledDex"
    t.add_resource(dex_lg)

    # ---------------------------------------------------------------------
    # Container definitions (inlined: services_common.build_task_definition
    # only supports one container, and maestro intentionally bundles three).
    # ---------------------------------------------------------------------
    db_init_container = ContainerDefinition(
        Name="db-init",
        Image=db_init_image_ref,
        Essential=False,
        EntryPoint=["sh", "-c"],
        # Create the maestro database and its extensions; mcp-gateway can't
        # create the database it connects to, and since maestro v1.87.8 its
        # startup preflight requires vector/pgcrypto/citext to already exist
        # ("provisioned by the operator, not by migrations"). Schema and
        # ownership are still mcp-gateway's migrations. The "|| true" tolerates
        # re-runs of CREATE DATABASE; the extension step must succeed.
        Command=[
            (
                "PGPASSWORD=$LRDB_PASSWORD psql -h $LRDB_HOST -p $LRDB_PORT "
                "-U $LRDB_USER -d postgres -v ON_ERROR_STOP=1 "
                "-c \"CREATE DATABASE maestro\" || true; "
                "PGPASSWORD=$LRDB_PASSWORD psql -h $LRDB_HOST -p $LRDB_PORT "
                "-U $LRDB_USER -d maestro -v ON_ERROR_STOP=1 "
                "-c \"CREATE EXTENSION IF NOT EXISTS vector\" "
                "-c \"CREATE EXTENSION IF NOT EXISTS pgcrypto\" "
                "-c \"CREATE EXTENSION IF NOT EXISTS citext\""
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
    # mcp-gateway (migrations + MCP server) and maestro connect as the DB
    # master/superuser. The migrations issue CREATE EXTENSION (pgvector et al.),
    # which RDS only allows for rds_superuser, so there is no separate, lesser
    # maestro role -- the instance is single-tenant to this install.
    db_env = [
        Environment(Name="MAESTRO_DB_HOST", Value=Ref("DbEndpoint")),
        Environment(Name="MAESTRO_DB_PORT", Value=Ref("DbPort")),
        Environment(Name="MAESTRO_DB_NAME", Value="maestro"),
        Environment(Name="MAESTRO_DB_SSLMODE", Value="require"),
    ]
    db_secrets = [
        Secret(Name="MAESTRO_DB_USER", ValueFrom=Sub("${DbSecretArn}:username::")),
        Secret(
            Name="MAESTRO_DB_PASSWORD",
            ValueFrom=Sub("${DbSecretArn}:password::"),
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
            # The gateway fails closed on a missing API key since maestro
            # v1.87.8. Its port has no target group and no SG ingress, so it
            # is reachable only over loopback inside this task -- the same
            # pod-local sidecar topology conductor's own deployment uses,
            # which sets this flag too.
            Environment(Name="MCP_ALLOW_NO_AUTH", Value="true"),
            Environment(
                Name="MCP_MIGRATE_RECOVER_FROM_DIRTY",
                Value=Ref("McpMigrateRecoverFromDirty"),
            ),
        ],
        # mcp-gateway loads its license via license-go; LICENSE_DATA env var
        # is honored (priority > LICENSE_FILE > /app/license/license.json).
        Secrets=list(db_secrets) + [
            Secret(Name="LICENSE_DATA", ValueFrom=Ref("LicenseSecretArn")),
        ],
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
                Value=If(
                    "UseBundledDex",
                    Sub("https://${AlbDnsName}/dex"),
                    Ref("OidcIssuerUrl"),
                ),
            ),
            # With DEX these are pinned to DexClientId and the DEX JWKS path
            # (maestro defaults to Keycloak's <issuer>/protocol/openid-connect/
            # certs, but Dex serves keys at <issuer>/keys). With an external
            # IdP each is passed only when set, leaving maestro's defaults.
            *[
                If(
                    "UseBundledDex",
                    Environment(Name=env_name, Value=dex_value),
                    _env_if(f"Has{param}", env_name, Ref(param)),
                )
                for env_name, param, dex_value in (
                    ("OIDC_CLIENT_ID", "OidcClientId", Ref("DexClientId")),
                    ("OIDC_AUDIENCE", "OidcAudience", Ref("DexClientId")),
                    ("OIDC_JWKS_URL", "OidcJwksUrl", Sub("https://${AlbDnsName}/dex/keys")),
                )
            ],
            # DEX_* kept for any tooling that still reads the legacy names.
            _env_if("UseBundledDex", "DEX_ISSUER_URL", Sub("https://${AlbDnsName}/dex")),
            _env_if("UseBundledDex", "DEX_CLIENT_ID", Ref("DexClientId")),
            # Maestro fetches the DEX JWKS from the ALB's HTTPS endpoint, but
            # the bundled self-signed cert isn't trusted by Node's TLS stack so
            # undici's fetch fails before it can read the keys, surfacing as
            # "JWT verification failed: fetch failed" and a 401 on /api/me.
            # An external IdP is reached over its own trusted cert, so TLS
            # verification stays on.
            _env_if("UseBundledDex", "NODE_TLS_REJECT_UNAUTHORIZED", "0"),
            # Email allowlist that grants maestro superadmin. The dex login
            # token has groups=[], so without this the DEX admin lands on
            # /onboard with no way to bootstrap an org.
            Environment(Name="OIDC_SUPERADMIN_EMAILS", Value=Ref("OidcSuperadminEmails")),
            *[
                _env_if(f"Has{param}", env_name, Ref(param))
                if kind == "str"
                else Environment(Name=env_name, Value=Ref(param))
                for param, env_name, kind, _ in EXTERNAL_OIDC_PARAMS
                if param not in ("OidcIssuerUrl", "OidcClientId", "OidcAudience", "OidcJwksUrl")
            ],
            # MCP OAuth discovery needs the public origin (the chart derives
            # it from its base URL the same way).
            _env_if("McpOauthOn", "MAESTRO_BASE_URL", Sub("https://${AlbDnsName}")),
            # Idempotent seed-if-missing bootstrap: org + owner + a
            # shared_cardinal lakerunner datasource (auto_add_to_all_orgs).
            # Both URLs point at the in-cluster Cloud Map names (query-api
            # and admin-api both register a ServiceDiscovery::Service in
            # ServicesQueryStack / ServicesControlStack), so maestro reaches
            # lakerunner over plain HTTP on the task port and does NOT
            # depend on the bundled self-signed ALB cert. The ALB
            # attachments stay in place for any external/admin-UI callers.
            # NODE_TLS_REJECT_UNAUTHORIZED=0 above is still needed for the
            # DEX issuer / JWKS fetch, which continues to go via the ALB.
            # The owner email must match the owner's login, so with an
            # external IdP set DexAdminEmail to their IdP email.
            Environment(Name="MAESTRO_BOOTSTRAP_ORG_ID", Value=Ref("OrganizationId")),
            Environment(Name="MAESTRO_BOOTSTRAP_ORG_NAME", Value=Ref("OrgName")),
            Environment(Name="MAESTRO_BOOTSTRAP_OWNER_EMAIL", Value=Ref("DexAdminEmail")),
            # Bucket coordinates so maestro's provision_org writes the
            # organization_buckets join row (the central bucket's storage line).
            # This is now the ONLY writer of that row -- CFN seeds no org
            # content -- so it must always be populated here.
            Environment(Name="MAESTRO_BOOTSTRAP_BUCKET_NAME", Value=Ref("BucketName")),
            Environment(Name="MAESTRO_BOOTSTRAP_BUCKET_REGION", Value=Ref("AWS::Region")),
            Environment(Name="MAESTRO_BOOTSTRAP_BUCKET_CLOUD_PROVIDER", Value="aws"),
            Environment(Name="MAESTRO_BOOTSTRAP_BUCKET_COLLECTOR_NAME", Value="lakerunner"),
            Environment(
                Name="MAESTRO_BOOTSTRAP_LAKERUNNER_QUERY_API_URL",
                Value=Sub("http://query-api.${ServiceNamespaceName}:8080"),
            ),
            Environment(
                Name="MAESTRO_BOOTSTRAP_LAKERUNNER_ADMIN_API_URL",
                Value=Sub("http://admin-api.${ServiceNamespaceName}:9091"),
            ),
        ],
        Secrets=list(db_secrets) + [
            Secret(Name="LICENSE_DATA", ValueFrom=Ref("LicenseSecretArn")),
            Secret(
                Name="MAESTRO_BOOTSTRAP_LAKERUNNER_ADMIN_API_KEY",
                ValueFrom=Sub("${AdminApiKeySecretArn}:key::"),
            ),
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

    # dex-customization v0.3.0 renders its own config at startup: the inherited
    # docker-entrypoint runs gomplate over the baked /etc/dex/config.docker.yaml
    # (the image's default CMD) from the DEX_* env vars below and writes the
    # result under /tmp before exec'ing dex -- so there is no dex-init sidecar.
    # gomplate uses {{ }} delimiters (not '$'), so the bcrypt hash survives, and
    # the template registers a lowercased copy of the redirect URI itself. We do
    # NOT mount /etc/dex (a mount there shadows the baked template) and do NOT
    # set ReadonlyRootFilesystem: the image's /tmp is a 1777 dir on the task's
    # free ephemeral storage, writable by the nonroot dex user (uid 1001), so no
    # writable volume or root chmod is needed.
    dex_container = ContainerDefinition(
        Name="dex",
        Image=dex_image_ref,
        Essential=True,
        User="1001",
        PortMappings=[PortMapping(ContainerPort=dex_port, Protocol="tcp")],
        Environment=[
            Environment(Name="DEX_ISSUER_URL", Value=Sub("https://${AlbDnsName}/dex")),
            Environment(Name="DEX_REDIRECT_URI", Value=Sub("https://${AlbDnsName}/")),
            Environment(Name="DEX_CLIENT_ID", Value=Ref("DexClientId")),
            Environment(Name="DEX_PORT", Value=str(dex_port)),
            Environment(Name="DEX_ADMIN_EMAIL", Value=Ref("DexAdminEmail")),
            Environment(Name="DEX_ADMIN_HASH", Value=Ref("DexAdminPasswordHash")),
            # Additive non-admin accounts; empty -> the image renders admin only.
            Environment(Name="DEX_EXTRA_USERS", Value=Ref("DexExtraUsers")),
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
            RuntimePlatform=RuntimePlatform(
                CpuArchitecture="ARM64",
                OperatingSystemFamily="LINUX",
            ),
            Cpu=Ref("MaestroTaskCpu"),
            Memory=Ref("MaestroTaskMemory"),
            ExecutionRoleArn=Ref("ExecutionRoleArn"),
            TaskRoleArn=Ref("TaskRoleArn"),
            ContainerDefinitions=[
                db_init_container,
                mcp_gateway_container,
                wait_for_mcp_container,
                maestro_container,
                If("UseBundledDex", dex_container, Ref("AWS::NoValue")),
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
    maestro_listener_rule = t.add_resource(
        services_common.build_listener_rule(
            service_key=_MAESTRO_LISTENER_KEY,
            target_group_ref=maestro_tg,
            listener_arn_param="HttpsListenerArn",
            path_patterns=["/*"],
        )
    )

    dex_tg = services_common.build_target_group(
        service_key=_DEX_LISTENER_KEY,
        vpc_id_param="VpcId",
        port=dex_port,
        # Dex serves all routes under its configured path_prefix; the
        # default "/healthz" returns 404 once the prefix is set.
        health_check_path="/dex/healthz",
    )
    dex_tg.Condition = "UseBundledDex"
    t.add_resource(dex_tg)
    dex_listener_rule = services_common.build_listener_rule(
        service_key=_DEX_LISTENER_KEY,
        target_group_ref=dex_tg,
        listener_arn_param="HttpsListenerArn",
        path_patterns=["/dex/*"],
    )
    dex_listener_rule.Condition = "UseBundledDex"
    t.add_resource(dex_listener_rule)

    # ---------------------------------------------------------------------
    # ECS Service (inlined like otel.py, because the LoadBalancers list has
    # two entries — the shared services_common.build_ecs_service helper only
    # supports one target_group_ref).
    # ---------------------------------------------------------------------
    service = t.add_resource(
        Service(
            "MaestroService",
            Cluster=Ref("ClusterArn"),
            # ECS copies these onto every task it launches; Fargate cost
            # allocation bills against task tags, not service tags.
            PropagateTags="SERVICE",
            # Singleton: pure on-demand FARGATE so its one task always places
            # during a rolling deploy; a transient FARGATE_SPOT shortage must
            # never block the task and trip the deploy circuit breaker.
            CapacityProviderStrategy=services_common.capacity_provider_strategy("ondemand"),
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
                If(
                    "UseBundledDex",
                    EcsLoadBalancer(
                        ContainerName="dex",
                        ContainerPort=dex_port,
                        TargetGroupArn=Ref(dex_tg),
                    ),
                    Ref("AWS::NoValue"),
                ),
            ],
            Tags=cardinal_tags(component="compute", role=_SERVICE_KEY),
            # ECS validates that target groups are attached to a listener at
            # service-create time; depend on both ListenerRules to avoid the
            # race. DependsOn cannot name a conditional resource, so the DEX
            # rule's dependency rides a conditional Ref in Metadata.
            DependsOn=[maestro_listener_rule.title],
            Metadata={
                "DexListenerRule": If(
                    "UseBundledDex", Ref(dex_listener_rule), Ref("AWS::NoValue")
                )
            },
        )
    )

    # ---------------------------------------------------------------------
    # Outputs
    # ---------------------------------------------------------------------
    t.add_output(Output("MaestroUrl", Value=Sub("https://${AlbDnsName}/")))
    t.add_output(Output(
        "DexUrl", Value=Sub("https://${AlbDnsName}/dex/"), Condition="UseBundledDex"
    ))
    t.add_output(Output("MaestroServiceName", Value=GetAtt(service, "Name")))

    return t


if __name__ == "__main__":
    print(build().to_yaml())
