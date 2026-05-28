"""lrdev-baseinfra: standalone base-infrastructure template for our internal test environment.

Stands in for the IT-owned prerequisites a customer would normally bring
to a Cardinal install. Currently provisions:

- An ECS (Fargate) cluster with Container Insights enabled

Customers do not deploy this stack -- it is scaffolding for our test
account, parallel to ``lrdev-vpc``.
"""

from troposphere import (
    GetAtt,
    Output,
    Parameter,
    Ref,
    Sub,
    Tags,
    Template,
)
from troposphere.ecs import (
    Cluster,
    ClusterConfiguration,
    ClusterSetting,
    ExecuteCommandConfiguration,
)


def _baseinfra_tags(*, role: str) -> Tags:
    return Tags(
        Name=Sub(f"${{EnvironmentName}}-{role}"),
        Project="lrdev",
        Component="compute",
        Role=role,
        ManagedBy="lrdev-cfn",
    )


def build() -> Template:
    t = Template()
    t.set_description(
        "lrdev base infra: ECS Fargate cluster for our internal lakerunner "
        "test environment. Stands in for a customer-supplied cluster. "
        "Not a customer-facing stack."
    )

    t.add_parameter(
        Parameter(
            "EnvironmentName",
            Type="String",
            Default="lrdev",
            Description="Environment name used in resource Name tags.",
            AllowedPattern=r"^[a-zA-Z][a-zA-Z0-9-]*$",
        )
    )

    cluster = t.add_resource(
        Cluster(
            "EcsCluster",
            ClusterSettings=[
                ClusterSetting(Name="containerInsights", Value="enabled"),
            ],
            Configuration=ClusterConfiguration(
                ExecuteCommandConfiguration=ExecuteCommandConfiguration(
                    Logging="DEFAULT",
                ),
            ),
            Tags=_baseinfra_tags(role="cluster"),
        )
    )

    t.add_output(
        Output(
            "ClusterName",
            Description="ECS cluster name (feed to cardinal-lakerunner ClusterName parameter).",
            Value=Ref(cluster),
        )
    )
    t.add_output(
        Output(
            "ClusterArn",
            Description="ECS cluster ARN (feed to cardinal-lakerunner ClusterArn parameter).",
            Value=GetAtt(cluster, "Arn"),
        )
    )

    return t


if __name__ == "__main__":
    print(build().to_yaml(), end="")
