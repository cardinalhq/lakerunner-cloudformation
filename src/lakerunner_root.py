#!/usr/bin/env python3
"""Lakerunner root CloudFormation template.

This template orchestrates the deployment of Lakerunner by creating nested
stacks for each major component. Individual stacks are only deployed when the
corresponding Deploy* parameter is set to "Yes".
"""

from troposphere import Template, Parameter, Ref, Equals, Sub
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


# Nested stacks
stack_urls = {
    "VpcStack": (deploy_vpc, "lakerunner-vpc.yaml"),
    "EcsStack": (deploy_ecs, "lakerunner-ecs.yaml"),
    "RdsStack": (deploy_rds, "lakerunner-rds.yaml"),
    "StorageStack": (deploy_storage, "lakerunner-storage.yaml"),
    "MigrationStack": (deploy_migration, "lakerunner-migration.yaml"),
    "ServicesStack": (deploy_services, "lakerunner-services.yaml"),
    "GrafanaServiceStack": (deploy_grafana, "lakerunner-grafana-service.yaml"),
    "OtelCollectorStack": (deploy_otel, "lakerunner-demo-otel-collector.yaml"),
}

for stack_name, (condition, filename) in stack_urls.items():
    t.add_resource(
        Stack(
            stack_name,
            Condition=condition,
            TemplateURL=Sub(f"${{TemplateBaseUrl}}/{filename}"),
        )
    )


if __name__ == "__main__":
    print(t.to_json())

