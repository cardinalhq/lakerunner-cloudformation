#!/usr/bin/env python3
"""Lakerunner root CloudFormation template.

This template orchestrates the deployment of Lakerunner by creating nested
stacks for each major component. Individual stacks are only deployed when the
corresponding Deploy* parameter is set to "Yes".
"""

from troposphere import Template, Parameter, Ref, Equals, Sub, GetAtt
from troposphere.cloudformation import Stack


# Initialize template
TEMPLATE_DESCRIPTION = (
    "Root stack that links all Lakerunner nested stacks. Set Deploy* parameters "
    "to control which components are launched."
)

t = Template()
t.set_description(TEMPLATE_DESCRIPTION)


# Base URL for nested templates
base_url = t.add_parameter(
    Parameter(
        "TemplateBaseUrl",
        Type="String",
        Description="Base URL where nested templates are stored",
    )
)


# Deploy parameters and conditions

def deploy_param(name, description, default="Yes"):
    param = t.add_parameter(
        Parameter(
            name,
            Type="String",
            Default=default,
            AllowedValues=["Yes", "No"],
            Description=description,
        )
    )
    t.add_condition(name, Equals(Ref(param), "Yes"))
    return name


deploy_vpc = deploy_param("DeployVpc", "Deploy the VPC stack")
deploy_ecs = deploy_param("DeployEcs", "Deploy the ECS infrastructure stack")
deploy_rds = deploy_param("DeployRds", "Deploy the RDS database stack")
deploy_storage = deploy_param("DeployStorage", "Deploy the Storage (S3/SQS) stack")
deploy_migration = deploy_param("DeployMigration", "Deploy the Migration stack", default="No")
deploy_services = deploy_param("DeployServices", "Deploy the Services stack")
deploy_grafana = deploy_param("DeployGrafanaService", "Deploy the Grafana Service stack", default="No")
deploy_otel = deploy_param("DeployOtelCollector", "Deploy the demo OTEL Collector stack", default="No")


# Nested stacks with parameter wiring
vpc_stack = t.add_resource(
    Stack(
        "VpcStack",
        Condition=deploy_vpc,
        TemplateURL=Sub("${TemplateBaseUrl}/lakerunner-vpc.yaml"),
    )
)

ecs_stack = t.add_resource(
    Stack(
        "EcsStack",
        Condition=deploy_ecs,
        TemplateURL=Sub("${TemplateBaseUrl}/lakerunner-ecs.yaml"),
        Parameters={
            "VpcId": GetAtt(vpc_stack, "Outputs.VpcId"),
        },
    )
)

rds_stack = t.add_resource(
    Stack(
        "RdsStack",
        Condition=deploy_rds,
        TemplateURL=Sub("${TemplateBaseUrl}/lakerunner-rds.yaml"),
        Parameters={
            "PrivateSubnets": GetAtt(vpc_stack, "Outputs.PrivateSubnets"),
            "TaskSecurityGroupId": GetAtt(ecs_stack, "Outputs.TaskSGId"),
        },
    )
)

storage_stack = t.add_resource(
    Stack(
        "StorageStack",
        Condition=deploy_storage,
        TemplateURL=Sub("${TemplateBaseUrl}/lakerunner-storage.yaml"),
    )
)

t.add_resource(
    Stack(
        "MigrationStack",
        Condition=deploy_migration,
        TemplateURL=Sub("${TemplateBaseUrl}/lakerunner-migration.yaml"),
    )
)

t.add_resource(
    Stack(
        "ServicesStack",
        Condition=deploy_services,
        TemplateURL=Sub("${TemplateBaseUrl}/lakerunner-services.yaml"),
        Parameters={
            "ClusterArn": GetAtt(ecs_stack, "Outputs.ClusterArn"),
            "DbSecretArn": GetAtt(rds_stack, "Outputs.DbSecretArn"),
            "DbHost": GetAtt(rds_stack, "Outputs.DbEndpoint"),
            "DbPort": GetAtt(rds_stack, "Outputs.DbPort"),
            "TaskSecurityGroupId": GetAtt(ecs_stack, "Outputs.TaskSGId"),
            "VpcId": GetAtt(vpc_stack, "Outputs.VpcId"),
            "PrivateSubnets": GetAtt(vpc_stack, "Outputs.PrivateSubnets"),
            "PublicSubnets": GetAtt(vpc_stack, "Outputs.PublicSubnets"),
            "BucketArn": GetAtt(storage_stack, "Outputs.BucketArn"),
        },
    )
)

t.add_resource(
    Stack(
        "GrafanaServiceStack",
        Condition=deploy_grafana,
        TemplateURL=Sub("${TemplateBaseUrl}/lakerunner-grafana-service.yaml"),
    )
)

t.add_resource(
    Stack(
        "OtelCollectorStack",
        Condition=deploy_otel,
        TemplateURL=Sub("${TemplateBaseUrl}/lakerunner-demo-otel-collector.yaml"),
    )
)


if __name__ == "__main__":
    print(t.to_json())

