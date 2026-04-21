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
from troposphere.elasticloadbalancingv2 import (
    Action as AlbAction,
    Listener,
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
                    "MAESTRO_BASE_URL). Leave blank to let the UI infer.",
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
                {"Label": {"default": "OIDC (optional)"},
                 "Parameters": [
                     "OidcIssuerUrl", "OidcAudience", "OidcSuperadminGroup",
                     "OidcJwksUrl", "OidcSuperadminEmails",
                     "OidcTrustUnverifiedEmails",
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
        Description=f"Maestro ALB -> task port {maestro_port}",
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

    return t


if __name__ == "__main__":
    template = create_maestro_template()
    print(template.to_yaml())
