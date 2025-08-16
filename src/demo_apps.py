#!/usr/bin/env python3
# Copyright (C) 2025 CardinalHQ, Inc
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

import yaml
import os
from troposphere import (
    Template, Parameter, Ref, Sub, GetAtt, If, Equals, Export, Output,
    Select, Not, Tags, ImportValue, Join, And, Split, Condition
)
from troposphere.ecs import (
    Service, TaskDefinition, ContainerDefinition, Environment,
    LogConfiguration, Secret as EcsSecret, Volume, MountPoint,
    HealthCheck, PortMapping, RuntimePlatform, NetworkConfiguration, AwsvpcConfiguration,
    LoadBalancer as EcsLoadBalancer, EFSVolumeConfiguration, AuthorizationConfig
)
from troposphere.iam import Role, Policy
from troposphere.elasticloadbalancingv2 import TargetGroup, TargetGroupAttribute, Listener, Matcher
from troposphere.elasticloadbalancingv2 import Action as AlbAction
from troposphere.efs import AccessPoint, PosixUser, RootDirectory, CreationInfo
from troposphere.logs import LogGroup
from troposphere.secretsmanager import Secret, GenerateSecretString

def load_demo_config(config_file="demo-apps-stack-defaults.yaml"):
    """Load demo app configuration from YAML file"""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    config_path = os.path.join(script_dir, "..", config_file)

    with open(config_path, 'r') as f:
        return yaml.safe_load(f)

def create_demo_apps_template():
    """Create CloudFormation template for demo applications stack"""

    t = Template()
    t.set_description("Lakerunner Demo Apps: OTEL-instrumented applications for testing telemetry collection")

    # Load demo app configurations and image defaults
    config = load_demo_config()
    demo_apps = config.get('demo_apps', {})
    images = config.get('images', {})

    # -----------------------
    # Parameters
    # -----------------------
    CommonInfraStackName = t.add_parameter(Parameter(
        "CommonInfraStackName", Type="String",
        Description="REQUIRED: Name of the CommonInfra stack to import infrastructure values from."
    ))

    ServicesStackName = t.add_parameter(Parameter(
        "ServicesStackName", Type="String", 
        Description="REQUIRED: Name of the Services stack to import ALB target groups from."
    ))

    OtelCollectorStackName = t.add_parameter(Parameter(
        "OtelCollectorStackName", Type="String",
        Description="REQUIRED: Name of the OTEL Collector stack to get collector endpoint from."
    ))

    # Container image overrides for air-gapped deployments
    image_parameters = {}
    for app_name, app_config in demo_apps.items():
        param_name = f"{''.join(word.capitalize() for word in app_name.replace('-', '_').split('_'))}Image"
        default_image = app_config.get('image', images.get(f'demo_{app_name.replace("-", "_")}', 'busybox:latest'))
        
        image_parameters[app_name] = t.add_parameter(Parameter(
            param_name, Type="String",
            Default=default_image,
            Description=f"Container image for {app_name} service"
        ))

    # Parameter groups for console
    t.set_metadata({
        "AWS::CloudFormation::Interface": {
            "ParameterGroups": [
                {
                    "Label": {"default": "Infrastructure"},
                    "Parameters": ["CommonInfraStackName", "ServicesStackName", "OtelCollectorStackName"]
                },
                {
                    "Label": {"default": "Container Images"},
                    "Parameters": list(param.title for param in image_parameters.values())
                }
            ],
            "ParameterLabels": {
                "CommonInfraStackName": {"default": "Common Infra Stack Name"},
                "ServicesStackName": {"default": "Services Stack Name"}, 
                "OtelCollectorStackName": {"default": "OTEL Collector Stack Name"},
                **{param.title: {"default": f"{app_name.replace('-', ' ').title()} Image"} 
                   for app_name, param in image_parameters.items()}
            }
        }
    })

    # Helper function for imports
    def ci_export(suffix):
        return Sub("${CommonInfraStackName}-%s" % suffix, CommonInfraStackName=Ref(CommonInfraStackName))

    def services_export(suffix):
        return Sub("${ServicesStackName}-%s" % suffix, ServicesStackName=Ref(ServicesStackName))

    def otel_export(suffix):
        return Sub("${OtelCollectorStackName}-%s" % suffix, OtelCollectorStackName=Ref(OtelCollectorStackName))

    # Resolved values (import from other stacks)
    ClusterArnValue = ImportValue(ci_export("ClusterArn"))
    PrivateSubnetsValue = Split(",", ImportValue(ci_export("PrivateSubnets")))
    TaskSecurityGroupIdValue = ImportValue(services_export("TaskSecurityGroupId"))
    ExecutionRoleArnValue = ImportValue(services_export("ExecutionRoleArn"))
    TaskRoleArnValue = ImportValue(services_export("TaskRoleArn"))
    
    # OTEL endpoints - get ALB DNS name from OTEL collector stack
    OtelAlbDnsValue = ImportValue(otel_export("AlbDnsName"))

    # -----------------------
    # Demo App Services
    # -----------------------
    for app_name, app_config in demo_apps.items():
        # Create service-specific resources
        title_name = ''.join(word.capitalize() for word in app_name.replace('-', '_').split('_'))

        # Log Group
        log_group = t.add_resource(LogGroup(
            f"LogGroup{title_name}",
            LogGroupName=Sub(f"/ecs/{app_name}"),
            RetentionInDays=14
        ))

        # Environment variables
        environment = [
            Environment(Name="OTEL_SERVICE_NAME", Value=app_name),
            Environment(Name="OTEL_EXPORTER_OTLP_ENDPOINT", Value=Sub("http://${OtelAlbDns}:4317", OtelAlbDns=OtelAlbDnsValue)),
            Environment(Name="OTEL_EXPORTER_OTLP_PROTOCOL", Value="grpc"),
            Environment(Name="OTEL_RESOURCE_ATTRIBUTES", Value=f"service.name={app_name}"),
            Environment(Name="TMPDIR", Value="/scratch"),
            Environment(Name="HOME", Value="/scratch")
        ]

        # Add app-specific environment variables
        app_env = app_config.get('environment', {})
        for key, value in app_env.items():
            environment.append(Environment(Name=key, Value=value))

        # Health check
        health_check_config = app_config.get('health_check', {})
        health_check = None
        if health_check_config:
            hc_type = health_check_config.get('type', 'http')
            if hc_type == 'http':
                port = health_check_config.get('port', 8080)
                path = health_check_config.get('path', '/health')
                health_check = HealthCheck(
                    Command=["CMD-SHELL", f"curl -f http://localhost:{port}{path} || exit 1"],
                    Interval=30,
                    Timeout=5,
                    Retries=3,
                    StartPeriod=60
                )
            elif hc_type == 'command':
                command = health_check_config.get('command', ["echo", "healthy"])
                health_check = HealthCheck(
                    Command=["CMD-SHELL"] + command,
                    Interval=30,
                    Timeout=5,
                    Retries=3,
                    StartPeriod=60
                )

        # Port mappings
        port_mappings = []
        ingress_config = app_config.get('ingress', {})
        if ingress_config:
            port = ingress_config.get('port', 8080)
            port_mappings.append(PortMapping(ContainerPort=port, Protocol="tcp"))

        # Mount points
        mount_points = [
            MountPoint(
                ContainerPath="/scratch",
                SourceVolume="scratch",
                ReadOnly=False
            )
        ]

        # Add service-specific bind mounts
        bind_mounts = app_config.get('bind_mounts', [])
        for mount in bind_mounts:
            mount_points.append(MountPoint(
                ContainerPath=mount['container_path'],
                SourceVolume=mount['source_volume'],
                ReadOnly=mount.get('read_only', False)
            ))

        # Container definition
        container_args = {
            "Name": "AppContainer",
            "Image": Ref(image_parameters[app_name]),
            "Environment": environment,
            "MountPoints": mount_points,
            "PortMappings": port_mappings,
            "User": "0",
            "LogConfiguration": LogConfiguration(
                LogDriver="awslogs",
                Options={
                    "awslogs-group": Ref(log_group),
                    "awslogs-region": Ref("AWS::Region"),
                    "awslogs-stream-prefix": app_name
                }
            )
        }

        # Add command if specified
        command = app_config.get('command', [])
        if command:
            container_args["Command"] = command

        # Only add HealthCheck if it's defined
        if health_check is not None:
            container_args["HealthCheck"] = health_check

        container = ContainerDefinition(**container_args)

        # Volumes (shared scratch space, optional EFS mounts)
        volumes = [Volume(Name="scratch")]

        # EFS volumes for services that need them
        efs_mounts = app_config.get('efs_mounts', [])
        if efs_mounts:
            EfsIdValue = ImportValue(ci_export("EfsId"))
            for efs_mount in efs_mounts:
                access_point_id = efs_mount.get('access_point_id')
                if access_point_id:
                    volumes.append(Volume(
                        Name=efs_mount['volume_name'],
                        EFSVolumeConfiguration=EFSVolumeConfiguration(
                            FilesystemId=EfsIdValue,
                            TransitEncryption="ENABLED",
                            AuthorizationConfig=AuthorizationConfig(
                                AccessPointId=access_point_id,
                                IAM="ENABLED"
                            )
                        )
                    ))

        # Task definition
        task_definition = t.add_resource(TaskDefinition(
            f"TaskDef{title_name}",
            Family=f"{app_name}-task",
            Cpu=str(app_config.get('cpu', 512)),
            Memory=str(app_config.get('memory_mib', 1024)),
            NetworkMode="awsvpc",
            RequiresCompatibilities=["FARGATE"],
            ExecutionRoleArn=ExecutionRoleArnValue,
            TaskRoleArn=TaskRoleArnValue,
            ContainerDefinitions=[container],
            Volumes=volumes,
            RuntimePlatform=RuntimePlatform(
                CpuArchitecture="ARM64",
                OperatingSystemFamily="LINUX"
            )
        ))

        # ECS Service
        load_balancers = []
        if ingress_config and ingress_config.get('attach_alb', False):
            # If service needs ALB attachment, import target group from services stack
            target_group_import = ingress_config.get('target_group_export', f"{app_name}-TargetGroupArn")
            load_balancers.append(EcsLoadBalancer(
                ContainerName="AppContainer",
                ContainerPort=ingress_config.get('port', 8080),
                TargetGroupArn=ImportValue(services_export(target_group_import))
            ))

        service = t.add_resource(Service(
            f"Service{title_name}",
            ServiceName=app_name,
            Cluster=ClusterArnValue,
            TaskDefinition=Ref(task_definition),
            DesiredCount=app_config.get('replicas', 1),
            LaunchType="FARGATE",
            LoadBalancers=load_balancers,
            NetworkConfiguration=NetworkConfiguration(
                AwsvpcConfiguration=AwsvpcConfiguration(
                    Subnets=PrivateSubnetsValue,
                    SecurityGroups=[TaskSecurityGroupIdValue],
                    AssignPublicIp="DISABLED"
                )
            ),
            EnableExecuteCommand=True
        ))

    # -----------------------
    # Outputs
    # -----------------------
    # Output service ARNs for each demo app
    for app_name, _ in demo_apps.items():
        title_name = ''.join(word.capitalize() for word in app_name.replace('-', '_').split('_'))
        t.add_output(Output(
            f"Service{title_name}Arn",
            Value=Ref(f"Service{title_name}"),
            Export=Export(name=Sub(f"${{AWS::StackName}}-{app_name}-ServiceArn"))
        ))

    return t

if __name__ == "__main__":
    template = create_demo_apps_template()
    print(template.to_yaml())