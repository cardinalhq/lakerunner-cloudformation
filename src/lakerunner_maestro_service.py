#!/usr/bin/env python3
# Copyright (C) 2026 CardinalHQ, Inc
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, version 3.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>.

import os
import yaml

from troposphere import (
    Equals, Export, GetAtt, If, ImportValue, Output, Parameter, Ref, Split,
    Sub, Tags, Template,
)
from troposphere.ec2 import SecurityGroup, SecurityGroupIngress
from troposphere.ecs import (
    AwsvpcConfiguration,
    ContainerDefinition,
    Environment,
    HealthCheck,
    LoadBalancer as EcsLoadBalancer,
    LogConfiguration,
    MountPoint,
    NetworkConfiguration,
    PortMapping,
    RuntimePlatform,
    Secret as EcsSecret,
    Service,
    TaskDefinition,
    Volume,
)
from troposphere.elasticloadbalancingv2 import (
    Action as AlbAction,
    Condition as AlbCondition,
    Listener,
    ListenerRule,
    ListenerRuleAction,
    LoadBalancer,
    Matcher,
    TargetGroup,
    TargetGroupAttribute,
)
from troposphere.iam import Policy, Role
from troposphere.logs import LogGroup
from troposphere.secretsmanager import GenerateSecretString, Secret


def load_maestro_config(config_file="lakerunner-maestro-defaults.yaml"):
    """Load default configuration for the Maestro stack from YAML."""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    config_path = os.path.join(script_dir, "..", config_file)
    with open(config_path, 'r') as f:
        return yaml.safe_load(f)


def create_maestro_template():
    """Create the CloudFormation template for the Maestro + MCP Gateway stack."""
    config = load_maestro_config()
    images = config.get("images", {})
    task_cfg = config.get("task", {})
    ports = config.get("ports", {})

    maestro_image_default = images.get(
        "maestro", "public.ecr.aws/cardinalhq.io/maestro:v0.23.0"
    )

    t = Template()
    t.set_description(
        "Lakerunner Maestro + MCP Gateway: single ECS Fargate service with a"
        " stack-local ALB. Reuses CommonInfra RDS and runs a psql init"
        " container that creates the maestro DB and user."
    )

    # -----------------------
    # Parameters
    # -----------------------
    CommonInfraStackName = t.add_parameter(Parameter(
        "CommonInfraStackName", Type="String",
        Description="REQUIRED: Name of the CommonInfra stack to import values from."
    ))
    AlbScheme = t.add_parameter(Parameter(
        "AlbScheme", Type="String",
        AllowedValues=["internet-facing", "internal"],
        Default="internal",
        Description="Load balancer scheme: 'internet-facing' for external access "
                    "or 'internal' for internal access only.",
    ))
    TaskCpu = t.add_parameter(Parameter(
        "TaskCpu", Type="String",
        Default=str(task_cfg.get("cpu", 1024)),
        Description="Fargate CPU units for the Maestro task (e.g., 512/1024/2048).",
    ))
    TaskMemoryMiB = t.add_parameter(Parameter(
        "TaskMemoryMiB", Type="String",
        Default=str(task_cfg.get("memory_mib", 2048)),
        Description="Fargate memory (MiB) for the Maestro task.",
    ))
    MaestroImage = t.add_parameter(Parameter(
        "MaestroImage", Type="String",
        Default=maestro_image_default,
        Description="Container image for both Maestro and the MCP Gateway "
                    "(same image, different entrypoints).",
    ))
    OidcIssuerUrl = t.add_parameter(Parameter(
        "OidcIssuerUrl", Type="String", Default="",
        Description="OIDC issuer URL. Leave blank to disable OIDC (Maestro "
                    "treats an empty value as 'OIDC disabled').",
    ))
    OidcAudience = t.add_parameter(Parameter(
        "OidcAudience", Type="String", Default="maestro-ui",
        Description="OIDC audience. Also used as the web UI OAuth client_id.",
    ))
    OidcSuperadminGroup = t.add_parameter(Parameter(
        "OidcSuperadminGroup", Type="String", Default="maestro-superadmin",
        Description="OIDC group name that grants Maestro superadmin access.",
    ))
    OidcJwksUrl = t.add_parameter(Parameter(
        "OidcJwksUrl", Type="String", Default="",
        Description="Optional OIDC JWKS URL override. Leave blank to use the "
                    "issuer's well-known JWKS endpoint.",
    ))
    OidcSuperadminEmails = t.add_parameter(Parameter(
        "OidcSuperadminEmails", Type="String", Default="",
        Description="Optional comma-separated email allowlist granted "
                    "superadmin access via OIDC.",
    ))
    OidcTrustUnverifiedEmails = t.add_parameter(Parameter(
        "OidcTrustUnverifiedEmails", Type="String",
        AllowedValues=["true", "false"], Default="false",
        Description="When 'true', treat all OIDC emails as verified. Leave "
                    "'false' unless you understand the security implications.",
    ))
    MaestroBaseUrl = t.add_parameter(Parameter(
        "MaestroBaseUrl", Type="String", Default="",
        Description="Optional public base URL for Maestro (forwarded as "
                    "MAESTRO_BASE_URL). Leave blank to auto-derive from the "
                    "stack's ALB DNS when DEX is enabled.",
    ))

    # -----------------------
    # DEX parameters (optional bundled OIDC provider)
    # -----------------------
    dex_cfg = config.get("dex", {}) or {}
    dex_image_default = images.get("dex", "ghcr.io/dexidp/dex:v2.41.1")
    dex_init_image_default = images.get(
        "dex_init", "public.ecr.aws/docker/library/busybox:1.37"
    )
    dex_path_prefix_default = dex_cfg.get("path_prefix", "/dex")
    dex_client_id_default = dex_cfg.get("client_id", "maestro-ui")

    DexEnabled = t.add_parameter(Parameter(
        "DexEnabled", Type="String",
        AllowedValues=["Yes", "No"], Default="No",
        Description="When 'Yes', run a bundled DEX OIDC provider as a sidecar "
                    "in the Maestro task and point Maestro's OIDC settings at "
                    "it. Single-replica POC-grade (in-memory storage).",
    ))
    DexAdminEmail = t.add_parameter(Parameter(
        "DexAdminEmail", Type="String", Default="",
        Description="Email of the static DEX admin user. Required when "
                    "DexEnabled=Yes.",
    ))
    DexAdminPasswordHash = t.add_parameter(Parameter(
        "DexAdminPasswordHash", Type="String", Default="", NoEcho=True,
        Description="Bcrypt hash of the DEX admin password (generate "
                    "out-of-band, e.g. `htpasswd -bnBC 10 \"\" 'secret' | "
                    "tr -d ':\\n'`). Required when DexEnabled=Yes.",
    ))
    DexClientId = t.add_parameter(Parameter(
        "DexClientId", Type="String", Default=dex_client_id_default,
        Description="OAuth client_id DEX registers for the Maestro UI. Must "
                    "match OIDC_AUDIENCE on the Maestro container (set "
                    "automatically when DEX is enabled).",
    ))
    DexPathPrefix = t.add_parameter(Parameter(
        "DexPathPrefix", Type="String", Default=dex_path_prefix_default,
        AllowedPattern=r"^/[A-Za-z0-9._~%!$&'()*+,;=:@/-]*$",
        Description="Path prefix the DEX service is served under (must start "
                    "with '/'). The ALB forwards '<prefix>*' to DEX.",
    ))
    DexImage = t.add_parameter(Parameter(
        "DexImage", Type="String", Default=dex_image_default,
        Description="Container image for the bundled DEX server.",
    ))
    DexInitImage = t.add_parameter(Parameter(
        "DexInitImage", Type="String", Default=dex_init_image_default,
        Description="Container image for the DEX init container (any image "
                    "with POSIX 'sh' suffices; busybox by default).",
    ))

    t.set_metadata({
        "AWS::CloudFormation::Interface": {
            "ParameterGroups": [
                {"Label": {"default": "Infrastructure"},
                 "Parameters": ["CommonInfraStackName", "AlbScheme"]},
                {"Label": {"default": "Task Sizing"},
                 "Parameters": ["TaskCpu", "TaskMemoryMiB"]},
                {"Label": {"default": "Image"},
                 "Parameters": ["MaestroImage"]},
                {"Label": {"default": "OIDC (optional, external provider)"},
                 "Parameters": [
                     "OidcIssuerUrl", "OidcAudience", "OidcSuperadminGroup",
                     "OidcJwksUrl", "OidcSuperadminEmails",
                     "OidcTrustUnverifiedEmails",
                 ]},
                {"Label": {"default": "DEX (optional bundled OIDC provider)"},
                 "Parameters": [
                     "DexEnabled", "DexAdminEmail", "DexAdminPasswordHash",
                     "DexClientId", "DexPathPrefix",
                     "DexImage", "DexInitImage",
                 ]},
                {"Label": {"default": "Misc"},
                 "Parameters": ["MaestroBaseUrl"]},
            ],
            "ParameterLabels": {
                "CommonInfraStackName": {"default": "Common Infra Stack Name"},
                "AlbScheme": {"default": "ALB Scheme"},
                "TaskCpu": {"default": "Fargate CPU"},
                "TaskMemoryMiB": {"default": "Fargate Memory (MiB)"},
                "MaestroImage": {"default": "Maestro Image"},
                "OidcIssuerUrl": {"default": "OIDC Issuer URL"},
                "OidcAudience": {"default": "OIDC Audience / UI client_id"},
                "OidcSuperadminGroup": {"default": "OIDC Superadmin Group"},
                "OidcJwksUrl": {"default": "OIDC JWKS URL"},
                "OidcSuperadminEmails": {"default": "OIDC Superadmin Emails"},
                "OidcTrustUnverifiedEmails": {"default": "OIDC Trust Unverified Emails"},
                "DexEnabled": {"default": "Enable Bundled DEX"},
                "DexAdminEmail": {"default": "DEX Admin Email"},
                "DexAdminPasswordHash": {"default": "DEX Admin Password (bcrypt)"},
                "DexClientId": {"default": "DEX Client ID"},
                "DexPathPrefix": {"default": "DEX Path Prefix"},
                "DexImage": {"default": "DEX Image"},
                "DexInitImage": {"default": "DEX Init Image"},
                "MaestroBaseUrl": {"default": "Maestro Base URL"},
            },
        }
    })

    # -----------------------
    # Cross-stack imports
    # -----------------------
    def ci_export(suffix):
        return Sub("${CommonInfraStackName}-%s" % suffix,
                   CommonInfraStackName=Ref(CommonInfraStackName))

    ClusterArnValue = ImportValue(ci_export("ClusterArn"))
    VpcIdValue = ImportValue(ci_export("VpcId"))
    TaskSecurityGroupIdValue = ImportValue(ci_export("TaskSGId"))
    PrivateSubnetsValue = Split(",", ImportValue(ci_export("PrivateSubnets")))
    PublicSubnetsValue = Split(",", ImportValue(ci_export("PublicSubnets")))
    DbEndpointValue = ImportValue(ci_export("DbEndpoint"))
    DbPortValue = ImportValue(ci_export("DbPort"))
    DbSecretArnValue = ImportValue(ci_export("DbSecretArn"))

    # -----------------------
    # Conditions
    # -----------------------
    t.add_condition("IsInternetFacing", Equals(Ref(AlbScheme), "internet-facing"))
    t.add_condition("HasDex", Equals(Ref(DexEnabled), "Yes"))
    t.add_condition("MaestroBaseUrlBlank", Equals(Ref(MaestroBaseUrl), ""))

    # -----------------------
    # Database password secret
    # -----------------------
    maestro_db_secret = t.add_resource(Secret(
        "MaestroDbSecret",
        Name=Sub("${AWS::StackName}-maestro-db"),
        Description="Maestro PostgreSQL user password",
        GenerateSecretString=GenerateSecretString(
            SecretStringTemplate='{"username":"maestro"}',
            GenerateStringKey="password",
            ExcludeCharacters=' !"#$%&\'()*+,./:;<=>?@[\\]^`{|}~',
            PasswordLength=32,
        ),
    ))

    # -----------------------
    # Log groups
    # -----------------------
    db_init_lg = t.add_resource(LogGroup(
        "MaestroDbInitLogGroup",
        LogGroupName=Sub("/ecs/${AWS::StackName}/db-init"),
        RetentionInDays=14,
    ))
    mcp_gw_lg = t.add_resource(LogGroup(
        "MaestroMcpGatewayLogGroup",
        LogGroupName=Sub("/ecs/${AWS::StackName}/mcp-gateway"),
        RetentionInDays=14,
    ))
    maestro_lg = t.add_resource(LogGroup(
        "MaestroServerLogGroup",
        LogGroupName=Sub("/ecs/${AWS::StackName}/maestro"),
        RetentionInDays=14,
    ))
    dex_init_lg = t.add_resource(LogGroup(
        "MaestroDexInitLogGroup",
        Condition="HasDex",
        LogGroupName=Sub("/ecs/${AWS::StackName}/dex-init"),
        RetentionInDays=14,
    ))
    dex_lg = t.add_resource(LogGroup(
        "MaestroDexLogGroup",
        Condition="HasDex",
        LogGroupName=Sub("/ecs/${AWS::StackName}/dex"),
        RetentionInDays=14,
    ))

    # -----------------------
    # IAM: execution and task roles
    # -----------------------
    exec_role = t.add_resource(Role(
        "MaestroExecRole",
        RoleName=Sub("${AWS::StackName}-exec-role"),
        AssumeRolePolicyDocument={
            "Version": "2012-10-17",
            "Statement": [{
                "Effect": "Allow",
                "Principal": {"Service": "ecs-tasks.amazonaws.com"},
                "Action": "sts:AssumeRole",
            }],
        },
        ManagedPolicyArns=[
            "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy",
        ],
        Policies=[Policy(
            PolicyName="SecretsManagerAccess",
            PolicyDocument={
                "Version": "2012-10-17",
                "Statement": [{
                    "Effect": "Allow",
                    "Action": ["secretsmanager:GetSecretValue"],
                    "Resource": [
                        Sub("arn:aws:secretsmanager:${AWS::Region}:"
                            "${AWS::AccountId}:secret:${AWS::StackName}-*"),
                        Sub("${S}*", S=DbSecretArnValue),
                    ],
                }],
            },
        )],
    ))

    task_role = t.add_resource(Role(
        "MaestroTaskRole",
        RoleName=Sub("${AWS::StackName}-task-role"),
        AssumeRolePolicyDocument={
            "Version": "2012-10-17",
            "Statement": [{
                "Effect": "Allow",
                "Principal": {"Service": "ecs-tasks.amazonaws.com"},
                "Action": "sts:AssumeRole",
            }],
        },
        Policies=[Policy(
            PolicyName="LogAccess",
            PolicyDocument={
                "Version": "2012-10-17",
                "Statement": [{
                    "Effect": "Allow",
                    "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
                    "Resource": "*",
                }],
            },
        )],
    ))

    # Stash for subsequent sections in this same function as it grows.
    t._maestro = {
        "ports": ports,
        "task_cfg": task_cfg,
        "images": images,
        "params": {
            "CommonInfraStackName": CommonInfraStackName,
            "AlbScheme": AlbScheme,
            "TaskCpu": TaskCpu,
            "TaskMemoryMiB": TaskMemoryMiB,
            "MaestroImage": MaestroImage,
            "OidcIssuerUrl": OidcIssuerUrl,
            "OidcAudience": OidcAudience,
            "OidcSuperadminGroup": OidcSuperadminGroup,
            "OidcJwksUrl": OidcJwksUrl,
            "OidcSuperadminEmails": OidcSuperadminEmails,
            "OidcTrustUnverifiedEmails": OidcTrustUnverifiedEmails,
            "MaestroBaseUrl": MaestroBaseUrl,
            "DexEnabled": DexEnabled,
            "DexAdminEmail": DexAdminEmail,
            "DexAdminPasswordHash": DexAdminPasswordHash,
            "DexClientId": DexClientId,
            "DexPathPrefix": DexPathPrefix,
            "DexImage": DexImage,
            "DexInitImage": DexInitImage,
        },
        "imports": {
            "ClusterArn": ClusterArnValue,
            "VpcId": VpcIdValue,
            "TaskSGId": TaskSecurityGroupIdValue,
            "PrivateSubnets": PrivateSubnetsValue,
            "PublicSubnets": PublicSubnetsValue,
            "DbEndpoint": DbEndpointValue,
            "DbPort": DbPortValue,
            "DbSecretArn": DbSecretArnValue,
        },
        "resources": {
            "MaestroDbSecret": maestro_db_secret,
            "DbInitLogGroup": db_init_lg,
            "McpGatewayLogGroup": mcp_gw_lg,
            "MaestroServerLogGroup": maestro_lg,
            "ExecRole": exec_role,
            "TaskRole": task_role,
        },
    }

    # -----------------------
    # ALB security group + ingress rules
    # -----------------------
    maestro_port = ports.get("maestro", 4200)
    listener_port = ports.get("alb_listener", 80)

    alb_sg = t.add_resource(SecurityGroup(
        "MaestroAlbSecurityGroup",
        GroupDescription="Security group for Maestro ALB",
        VpcId=VpcIdValue,
        SecurityGroupEgress=[{
            "IpProtocol": "-1",
            "CidrIp": "0.0.0.0/0",
            "Description": "Allow all outbound",
        }],
    ))

    t.add_resource(SecurityGroupIngress(
        "MaestroAlbListenerIngress",
        GroupId=Ref(alb_sg),
        IpProtocol="tcp",
        FromPort=listener_port, ToPort=listener_port,
        CidrIp="0.0.0.0/0",
        Description=f"HTTP {listener_port} for Maestro ALB",
    ))

    t.add_resource(SecurityGroupIngress(
        "MaestroTaskFromAlbIngress",
        GroupId=TaskSecurityGroupIdValue,
        IpProtocol="tcp",
        FromPort=maestro_port, ToPort=maestro_port,
        SourceSecurityGroupId=Ref(alb_sg),
        Description=f"Maestro ALB to task port {maestro_port}",
    ))

    # -----------------------
    # ALB, target group, listener
    # -----------------------
    alb = t.add_resource(LoadBalancer(
        "MaestroAlb",
        Scheme=Ref(AlbScheme),
        SecurityGroups=[Ref(alb_sg)],
        Subnets=If("IsInternetFacing", PublicSubnetsValue, PrivateSubnetsValue),
        Type="application",
    ))

    tg = t.add_resource(TargetGroup(
        "MaestroTg",
        Name=If("IsInternetFacing",
                Sub("${AWS::StackName}-ext"),
                Sub("${AWS::StackName}-int")),
        Port=maestro_port, Protocol="HTTP",
        VpcId=VpcIdValue,
        TargetType="ip",
        HealthCheckPath="/api/health",
        HealthCheckProtocol="HTTP",
        HealthCheckIntervalSeconds=30,
        HealthCheckTimeoutSeconds=5,
        HealthyThresholdCount=2,
        UnhealthyThresholdCount=3,
        Matcher=Matcher(HttpCode="200"),
        TargetGroupAttributes=[
            TargetGroupAttribute(Key="stickiness.enabled", Value="false"),
            TargetGroupAttribute(Key="deregistration_delay.timeout_seconds",
                                 Value="30"),
        ],
    ))

    listener = t.add_resource(Listener(
        "MaestroListener",
        LoadBalancerArn=Ref(alb),
        Port=str(listener_port),
        Protocol="HTTP",
        DefaultActions=[AlbAction(Type="forward", TargetGroupArn=Ref(tg))],
    ))

    t._maestro["resources"].update({
        "AlbSg": alb_sg,
        "Alb": alb,
        "Tg": tg,
        "Listener": listener,
    })

    # -----------------------
    # DEX ALB target group, listener rule, and task SG ingress
    # -----------------------
    dex_port = ports.get("dex", 5556)

    dex_tg = t.add_resource(TargetGroup(
        "MaestroDexTg",
        Condition="HasDex",
        Name=Sub("${AWS::StackName}-dex"),
        Port=dex_port, Protocol="HTTP",
        VpcId=VpcIdValue,
        TargetType="ip",
        HealthCheckPath=Sub("${P}/healthz", P=Ref(DexPathPrefix)),
        HealthCheckProtocol="HTTP",
        HealthCheckIntervalSeconds=30,
        HealthCheckTimeoutSeconds=5,
        HealthyThresholdCount=2,
        UnhealthyThresholdCount=3,
        Matcher=Matcher(HttpCode="200"),
        TargetGroupAttributes=[
            TargetGroupAttribute(Key="stickiness.enabled", Value="false"),
            TargetGroupAttribute(Key="deregistration_delay.timeout_seconds",
                                 Value="30"),
        ],
    ))

    dex_listener_rule = t.add_resource(ListenerRule(
        "MaestroDexListenerRule",
        Condition="HasDex",
        ListenerArn=Ref(listener),
        Priority=10,
        Conditions=[
            AlbCondition(
                Field="path-pattern",
                # Match both the prefix itself and everything below it.
                Values=[
                    Sub("${P}", P=Ref(DexPathPrefix)),
                    Sub("${P}/*", P=Ref(DexPathPrefix)),
                ],
            ),
        ],
        Actions=[ListenerRuleAction(Type="forward", TargetGroupArn=Ref(dex_tg))],
    ))

    t.add_resource(SecurityGroupIngress(
        "MaestroDexTaskFromAlbIngress",
        Condition="HasDex",
        GroupId=TaskSecurityGroupIdValue,
        IpProtocol="tcp",
        FromPort=dex_port, ToPort=dex_port,
        SourceSecurityGroupId=Ref(alb_sg),
        Description=f"Maestro ALB to DEX task port {dex_port}",
    ))

    t._maestro["resources"].update({
        "DexTg": dex_tg,
        "DexListenerRule": dex_listener_rule,
    })

    # -----------------------
    # Shared env / secret helpers
    # -----------------------
    def _db_env():
        # Maestro and mcp-gateway assemble the DSN from these parts when
        # MAESTRO_DATABASE_URL is unset (conductor #379). Unlike Kubernetes,
        # ECS does not interpolate $(VAR) across env entries, so we can't
        # ship a pre-built URL from here.
        return [
            Environment(Name="MAESTRO_DB_HOST", Value=DbEndpointValue),
            Environment(Name="MAESTRO_DB_PORT", Value=DbPortValue),
            Environment(Name="MAESTRO_DB_NAME", Value="maestro"),
            Environment(Name="MAESTRO_DB_USER", Value="maestro"),
            Environment(Name="MAESTRO_DB_SSLMODE", Value="require"),
        ]

    def _db_password_secret():
        return EcsSecret(
            Name="MAESTRO_DB_PASSWORD",
            ValueFrom=Sub("${S}:password::", S=Ref(maestro_db_secret)),
        )

    # -----------------------
    # DbInit container (generic psql bootstrapper)
    # -----------------------
    db_init_image = images.get(
        "db_init", "ghcr.io/cardinalhq/initcontainer-grafana:latest"
    )

    # After setup-grafana-db.sh creates the database + app user, install the
    # extensions McpGateway migrations need (pgvector, pgcrypto, citext).
    # CREATE EXTENSION requires rds_superuser on RDS, so it must happen here
    # under the master user, not in the app-level migrations.
    #
    # Also clear any stale dirty-flag rows in gomigrate_maestro: if a prior
    # deploy's app-level migrations failed mid-flight (e.g. pgvector not yet
    # installed), golang-migrate leaves dirty=true and refuses to retry. The
    # DO block is guarded on pg_tables so a fresh DB is a no-op.
    db_init_script = (
        "set -e; "
        "/app/scripts/init-grafana.sh; "
        "PGDATABASE=$GRAFANA_DB_NAME psql -v ON_ERROR_STOP=1 "
        "-c 'CREATE EXTENSION IF NOT EXISTS vector;' "
        "-c 'CREATE EXTENSION IF NOT EXISTS pgcrypto;' "
        "-c 'CREATE EXTENSION IF NOT EXISTS citext;' "
        "-c \"DO \\$\\$ BEGIN "
        "IF EXISTS (SELECT 1 FROM pg_tables WHERE tablename='gomigrate_maestro' AND schemaname='public') THEN "
        "DELETE FROM gomigrate_maestro WHERE dirty = true; "
        "END IF; END \\$\\$;\""
    )

    db_init_container = ContainerDefinition(
        Name="DbInit",
        Image=db_init_image,
        Essential=False,
        EntryPoint=["/bin/sh", "-c"],
        Command=[db_init_script],
        Environment=[
            Environment(Name="PGHOST", Value=DbEndpointValue),
            Environment(Name="PGPORT", Value=DbPortValue),
            Environment(Name="PGDATABASE", Value="postgres"),
            Environment(Name="PGSSLMODE", Value="require"),
            Environment(Name="GRAFANA_DB_NAME", Value="maestro"),
            Environment(Name="GRAFANA_DB_USER", Value="maestro"),
        ],
        Secrets=[
            EcsSecret(
                Name="PGUSER",
                ValueFrom=Sub("${S}:username::", S=DbSecretArnValue),
            ),
            EcsSecret(
                Name="PGPASSWORD",
                ValueFrom=Sub("${S}:password::", S=DbSecretArnValue),
            ),
            EcsSecret(
                Name="GRAFANA_DB_PASSWORD",
                ValueFrom=Sub("${S}:password::", S=Ref(maestro_db_secret)),
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

    # -----------------------
    # McpGateway container
    # -----------------------
    mcp_port = ports.get("mcp_gateway", 8080)
    mcp_debug_port = ports.get("mcp_gateway_debug", 9090)

    mcp_container = ContainerDefinition(
        Name="McpGateway",
        Image=Ref(MaestroImage),
        Essential=True,
        User="65532",
        ReadonlyRootFilesystem=True,
        Command=["/app/entrypoint.sh", "mcp-gateway"],
        PortMappings=[
            PortMapping(ContainerPort=mcp_port, Protocol="tcp"),
            PortMapping(ContainerPort=mcp_debug_port, Protocol="tcp"),
        ],
        Environment=_db_env() + [
            Environment(Name="MCP_PORT", Value=str(mcp_port)),
            Environment(Name="MCP_DEBUG_PORT", Value=str(mcp_debug_port)),
        ],
        Secrets=[_db_password_secret()],
        HealthCheck=HealthCheck(
            Command=["CMD-SHELL",
                     f"wget --no-verbose --tries=1 --spider "
                     f"http://localhost:{mcp_port}/healthz || exit 1"],
            Interval=30, Timeout=5, Retries=3, StartPeriod=30,
        ),
        DependsOn=[{"ContainerName": "DbInit", "Condition": "SUCCESS"}],
        LogConfiguration=LogConfiguration(
            LogDriver="awslogs",
            Options={
                "awslogs-group": Ref(mcp_gw_lg),
                "awslogs-region": Ref("AWS::Region"),
                "awslogs-stream-prefix": "mcp-gateway",
            },
        ),
    )

    # -----------------------
    # Base URL + DEX-derived URLs
    # -----------------------
    # When DEX is enabled and the operator left MaestroBaseUrl blank, fall
    # back to the stack's own ALB DNS so the OIDC issuer URL and SPA
    # redirect URI resolve to a reachable host without extra DNS setup.
    # When DEX is disabled, honor the parameter as-is (blank meant "let
    # Maestro infer" in the old behavior; preserved here).
    base_url_when_dex = If(
        "MaestroBaseUrlBlank",
        Sub("http://${Dns}", Dns=GetAtt(alb, "DNSName")),
        Ref(MaestroBaseUrl),
    )
    maestro_base_url_value = If("HasDex", base_url_when_dex, Ref(MaestroBaseUrl))
    dex_issuer_url_value = Sub("${B}${P}", B=base_url_when_dex, P=Ref(DexPathPrefix))
    dex_redirect_uri_value = Sub("${B}/", B=base_url_when_dex)
    # Internal JWKS URL bypasses the ALB — DEX is a sidecar in the same
    # task, so loopback works and avoids an ALB roundtrip + any future TLS
    # termination concerns.
    dex_internal_jwks_value = Sub(
        "http://localhost:${Port}${P}/keys",
        Port=str(dex_port),
        P=Ref(DexPathPrefix),
    )

    # -----------------------
    # Maestro container
    # -----------------------
    maestro_env = _db_env() + [
        Environment(Name="MCP_GATEWAY_URL",
                    Value=f"http://localhost:{mcp_port}"),
        Environment(Name="PORT", Value=str(maestro_port)),
        Environment(Name="MAESTRO_BASE_URL", Value=maestro_base_url_value),
        Environment(Name="OIDC_ISSUER_URL",
                    Value=If("HasDex", dex_issuer_url_value, Ref(OidcIssuerUrl))),
        Environment(Name="OIDC_AUDIENCE",
                    Value=If("HasDex", Ref(DexClientId), Ref(OidcAudience))),
        Environment(Name="OIDC_SUPERADMIN_GROUP", Value=Ref(OidcSuperadminGroup)),
        Environment(Name="OIDC_JWKS_URL",
                    Value=If("HasDex", dex_internal_jwks_value, Ref(OidcJwksUrl))),
        Environment(Name="OIDC_SUPERADMIN_EMAILS", Value=Ref(OidcSuperadminEmails)),
        Environment(Name="OIDC_TRUST_UNVERIFIED_EMAILS",
                    Value=Ref(OidcTrustUnverifiedEmails)),
    ]

    maestro_container = ContainerDefinition(
        Name="Maestro",
        Image=Ref(MaestroImage),
        Essential=True,
        User="65532",
        ReadonlyRootFilesystem=True,
        PortMappings=[PortMapping(ContainerPort=maestro_port, Protocol="tcp")],
        Environment=maestro_env,
        Secrets=[_db_password_secret()],
        MountPoints=[MountPoint(ContainerPath="/tmp", SourceVolume="tmp",
                                ReadOnly=False)],
        HealthCheck=HealthCheck(
            Command=["CMD-SHELL",
                     f"wget --no-verbose --tries=1 --spider "
                     f"http://localhost:{maestro_port}/api/health || exit 1"],
            Interval=30, Timeout=5, Retries=3, StartPeriod=60,
        ),
        DependsOn=[
            {"ContainerName": "DbInit", "Condition": "SUCCESS"},
            {"ContainerName": "McpGateway", "Condition": "HEALTHY"},
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

    # -----------------------
    # DEX init + DEX containers (rendered only when HasDex is true; task def
    # wraps them in Fn::If so they disappear cleanly when DEX is disabled).
    # -----------------------
    # BusyBox sh renders /etc/dex/config.yaml from env vars. The unquoted
    # heredoc expands ${DEX_*} once (no re-scan), so a bcrypt hash
    # containing '$' survives intact in the rendered YAML.
    # Also chmod 1777 the dex-tmp volume: Fargate mounts empty volumes as
    # root:root 0755, so the nonroot DEX container otherwise can't write
    # the config-expansion tempfile it creates on startup.
    dex_config_render_script = (
        "set -eu; "
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
        "staticPasswords:\n"
        "  - email: \"${DEX_ADMIN_EMAIL}\"\n"
        "    hash: \"${DEX_ADMIN_HASH}\"\n"
        "    username: \"admin\"\n"
        "    userID: \"00000000-0000-0000-0000-000000000001\"\n"
        "EOF\n"
        "chmod 1777 /dex-tmp\n"
    )

    dex_init_container = ContainerDefinition(
        Name="DexInit",
        Image=Ref(DexInitImage),
        Essential=False,
        EntryPoint=["/bin/sh", "-c"],
        Command=[dex_config_render_script],
        Environment=[
            Environment(Name="DEX_ISSUER_URL", Value=dex_issuer_url_value),
            Environment(Name="DEX_REDIRECT_URI", Value=dex_redirect_uri_value),
            Environment(Name="DEX_CLIENT_ID", Value=Ref(DexClientId)),
            Environment(Name="DEX_PORT", Value=str(dex_port)),
            Environment(Name="DEX_ADMIN_EMAIL", Value=Ref(DexAdminEmail)),
            Environment(Name="DEX_ADMIN_HASH", Value=Ref(DexAdminPasswordHash)),
        ],
        MountPoints=[
            MountPoint(ContainerPath="/etc/dex",
                       SourceVolume="dex-config", ReadOnly=False),
            MountPoint(ContainerPath="/dex-tmp",
                       SourceVolume="dex-tmp", ReadOnly=False),
        ],
        LogConfiguration=LogConfiguration(
            LogDriver="awslogs",
            Options={
                "awslogs-group": Sub("/ecs/${AWS::StackName}/dex-init"),
                "awslogs-region": Ref("AWS::Region"),
                "awslogs-stream-prefix": "dex-init",
            },
        ),
    )

    dex_container = ContainerDefinition(
        Name="Dex",
        Image=Ref(DexImage),
        Essential=True,
        User="65532",
        ReadonlyRootFilesystem=True,
        Command=["dex", "serve", "/etc/dex/config.yaml"],
        PortMappings=[PortMapping(ContainerPort=dex_port, Protocol="tcp")],
        MountPoints=[
            MountPoint(ContainerPath="/etc/dex", SourceVolume="dex-config",
                       ReadOnly=True),
            # DEX writes a config-expansion tempfile into /tmp under its
            # readOnlyRootFilesystem guard. Use a dedicated volume (not
            # the Maestro-shared 'tmp') that DexInit chmods 1777, since
            # Fargate creates empty volumes as root:root 0755.
            MountPoint(ContainerPath="/tmp", SourceVolume="dex-tmp",
                       ReadOnly=False),
        ],
        HealthCheck=HealthCheck(
            Command=["CMD-SHELL",
                     f"wget --no-verbose --tries=1 --spider "
                     f"http://localhost:{dex_port}$DEX_PATH_PREFIX/healthz "
                     f"|| exit 1"],
            Interval=30, Timeout=5, Retries=3, StartPeriod=30,
        ),
        Environment=[
            # Referenced by the HealthCheck command above.
            Environment(Name="DEX_PATH_PREFIX", Value=Ref(DexPathPrefix)),
        ],
        DependsOn=[{"ContainerName": "DexInit", "Condition": "SUCCESS"}],
        LogConfiguration=LogConfiguration(
            LogDriver="awslogs",
            Options={
                "awslogs-group": Sub("/ecs/${AWS::StackName}/dex"),
                "awslogs-region": Ref("AWS::Region"),
                "awslogs-stream-prefix": "dex",
            },
        ),
    )

    # Task definition container + volume lists, conditional on HasDex.
    # Use Fn::If + AWS::NoValue to drop DEX-only entries when disabled.
    container_defs = [
        db_init_container,
        mcp_container,
        maestro_container,
        If("HasDex", dex_init_container, Ref("AWS::NoValue")),
        If("HasDex", dex_container, Ref("AWS::NoValue")),
    ]
    task_volumes = [
        Volume(Name="tmp"),
        If("HasDex", Volume(Name="dex-config"), Ref("AWS::NoValue")),
        If("HasDex", Volume(Name="dex-tmp"), Ref("AWS::NoValue")),
    ]

    # -----------------------
    # Task Definition
    # -----------------------
    task_def = t.add_resource(TaskDefinition(
        "MaestroTaskDef",
        Family=Sub("${AWS::StackName}-maestro"),
        Cpu=Ref(TaskCpu),
        Memory=Ref(TaskMemoryMiB),
        NetworkMode="awsvpc",
        RequiresCompatibilities=["FARGATE"],
        ExecutionRoleArn=GetAtt(exec_role, "Arn"),
        TaskRoleArn=GetAtt(task_role, "Arn"),
        ContainerDefinitions=container_defs,
        Volumes=task_volumes,
        RuntimePlatform=RuntimePlatform(
            CpuArchitecture="ARM64",
            OperatingSystemFamily="LINUX",
        ),
    ))

    t._maestro["resources"]["TaskDef"] = task_def
    t._maestro["resources"]["DexInitLogGroup"] = dex_init_lg
    t._maestro["resources"]["DexLogGroup"] = dex_lg

    # -----------------------
    # ECS Service
    # -----------------------
    service = t.add_resource(Service(
        "MaestroService",
        ServiceName=Sub("${AWS::StackName}-maestro"),
        Cluster=ClusterArnValue,
        TaskDefinition=Ref(task_def),
        LaunchType="FARGATE",
        DesiredCount=1,
        NetworkConfiguration=NetworkConfiguration(
            AwsvpcConfiguration=AwsvpcConfiguration(
                Subnets=PrivateSubnetsValue,
                SecurityGroups=[TaskSecurityGroupIdValue],
                AssignPublicIp="DISABLED",
            ),
        ),
        LoadBalancers=[
            EcsLoadBalancer(
                ContainerName="Maestro",
                ContainerPort=maestro_port,
                TargetGroupArn=Ref(tg),
            ),
            If(
                "HasDex",
                EcsLoadBalancer(
                    ContainerName="Dex",
                    ContainerPort=dex_port,
                    TargetGroupArn=Ref(dex_tg),
                ),
                Ref("AWS::NoValue"),
            ),
        ],
        DependsOn=["MaestroListener"],
        EnableExecuteCommand=True,
        EnableECSManagedTags=True,
        PropagateTags="SERVICE",
        Tags=Tags(
            Name=Sub("${AWS::StackName}-maestro"),
            ManagedBy="Lakerunner",
            Environment=Ref("AWS::StackName"),
            Component="Service",
        ),
    ))

    # -----------------------
    # Outputs
    # -----------------------
    t.add_output(Output(
        "MaestroAlbDNS",
        Value=GetAtt(alb, "DNSName"),
        Export=Export(name=Sub("${AWS::StackName}-MaestroAlbDNS")),
    ))
    t.add_output(Output(
        "MaestroAlbArn",
        Value=Ref(alb),
        Export=Export(name=Sub("${AWS::StackName}-MaestroAlbArn")),
    ))
    t.add_output(Output(
        "MaestroServiceArn",
        Value=Ref(service),
        Export=Export(name=Sub("${AWS::StackName}-MaestroServiceArn")),
    ))
    t.add_output(Output(
        "MaestroDbSecretArn",
        Value=Ref(maestro_db_secret),
        Export=Export(name=Sub("${AWS::StackName}-MaestroDbSecretArn")),
    ))
    t.add_output(Output(
        "MaestroUrl",
        Description="URL to access the Maestro UI/API",
        Value=Sub("http://${Dns}", Dns=GetAtt(alb, "DNSName")),
    ))

    return t


if __name__ == "__main__":
    template = create_maestro_template()
    print(template.to_yaml())
