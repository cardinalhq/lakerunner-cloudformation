"""migration.yaml nested stack: DB migration ECS task + custom-resource Lambda."""

from troposphere import (
    Template,
    Parameter,
    Ref,
    GetAtt,
    Output,
    Split,
    Sub,
)
from troposphere.awslambda import Code, Function
from troposphere.cloudformation import CustomResource
from troposphere.ecs import (
    ContainerDefinition,
    ContainerDependency,
    Environment,
    LogConfiguration,
    Secret,
    TaskDefinition,
)
from troposphere.iam import Policy, Role
from troposphere.logs import LogGroup

from cardinal_cfn.children import migration_lambda
from cardinal_cfn.defaults import load_defaults
from cardinal_cfn.naming import cardinal_tags
from cardinal_cfn.parameters import add_install_id_parameters
from cardinal_cfn.policies import apply_policy


def build() -> Template:
    t = Template()
    t.set_description("Cardinal migration: DB migrator ECS task and custom-resource Lambda.")

    defaults = load_defaults()

    add_install_id_parameters(t)

    # Cluster / networking parameters (passed from parent)
    t.add_parameter(Parameter("ClusterArn", Type="String", Description="ECS cluster ARN."))
    t.add_parameter(Parameter("ClusterName", Type="String", Description="ECS cluster name."))
    t.add_parameter(Parameter("TaskSecurityGroupId", Type="String", Description="ECS task security group ID."))
    t.add_parameter(Parameter("ExecutionRoleArn", Type="String", Description="ECS task execution role ARN."))
    t.add_parameter(Parameter("PrivateSubnetsCsv", Type="String", Description="Comma-separated private subnet IDs."))

    # Database parameters
    t.add_parameter(Parameter("DbEndpoint", Type="String", Description="RDS endpoint hostname."))
    t.add_parameter(Parameter("DbPort", Type="String", Default="5432", Description="RDS port."))
    t.add_parameter(Parameter("DbName", Type="String", Default="lakerunner", Description="Database name."))
    t.add_parameter(Parameter("DbSecretArn", Type="String", Description="ARN of the DB master secret."))

    # Image parameters.  The migrator uses the same image as the lakerunner
    # service tasks (LakerunnerImage) so the two cannot drift.  The custom
    # resource keys its trigger off this same value, so any change to
    # LakerunnerImage reruns migrations.
    t.add_parameter(
        Parameter(
            "LakerunnerImage",
            Type="String",
            Default=defaults["images"]["lakerunner"],
            Description="Container image used for both lakerunner tasks and the DB migrator.",
        )
    )
    t.add_parameter(
        Parameter(
            "DbInitImage",
            Type="String",
            Default=defaults["images"]["db_init"],
            Description="Image used by the configdb-init container (must include psql).",
        )
    )

    # ---------------------------------------------------------------------------
    # Log groups
    # ---------------------------------------------------------------------------
    migrator_lg = t.add_resource(
        LogGroup(
            "MigratorLogGroup",
            RetentionInDays=14,
        )
    )
    apply_policy(migrator_lg, "log-group")

    lambda_lg = t.add_resource(
        LogGroup(
            "LambdaLogGroup",
            LogGroupName=Sub("/aws/lambda/cardinal-migration-${InstallIdLong}"),
            RetentionInDays=14,
        )
    )
    apply_policy(lambda_lg, "log-group")

    # ---------------------------------------------------------------------------
    # Migrator ECS task role
    # ---------------------------------------------------------------------------
    task_role = t.add_resource(
        Role(
            "MigratorTaskRole",
            AssumeRolePolicyDocument={
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Principal": {"Service": "ecs-tasks.amazonaws.com"},
                        "Action": "sts:AssumeRole",
                    }
                ],
            },
            Policies=[
                Policy(
                    PolicyName="migrator-secrets",
                    PolicyDocument={
                        "Version": "2012-10-17",
                        "Statement": [
                            {
                                "Effect": "Allow",
                                "Action": ["secretsmanager:GetSecretValue"],
                                "Resource": Ref("DbSecretArn"),
                            },
                            {
                                "Effect": "Allow",
                                "Action": [
                                    "logs:CreateLogGroup",
                                    "logs:CreateLogStream",
                                    "logs:PutLogEvents",
                                ],
                                "Resource": "*",
                            },
                        ],
                    },
                )
            ],
            Tags=cardinal_tags(component="migration", role="migrator-task-role"),
        )
    )

    # ---------------------------------------------------------------------------
    # Migrator ECS task definition.
    #
    # Two containers:
    #   1. configdb-init (non-essential): runs psql to CREATE DATABASE configdb
    #      if it doesn't already exist. lakerunner migrate connects to existing
    #      DBs only and never issues CREATE DATABASE itself.
    #   2. migrator (essential): runs `lakerunner migrate --databases=lrdb,configdb`
    #      against both DBs. Uses dependsOn=COMPLETE so it waits for the init
    #      container to finish first.
    # ---------------------------------------------------------------------------
    db_init_secrets = [
        Secret(Name="LRDB_USER", ValueFrom=Sub("${DbSecretArn}:username::")),
        Secret(Name="LRDB_PASSWORD", ValueFrom=Sub("${DbSecretArn}:password::")),
    ]

    configdb_init_container = ContainerDefinition(
        Name="configdb-init",
        Image=Ref("DbInitImage"),
        Essential=False,
        EntryPoint=["sh", "-c"],
        Command=[
            (
                "PGPASSWORD=$LRDB_PASSWORD psql -h $LRDB_HOST -p $LRDB_PORT "
                "-U $LRDB_USER -d postgres -v ON_ERROR_STOP=1 "
                "-tAc \"SELECT 1 FROM pg_database WHERE datname='configdb'\" "
                "| grep -q 1 || "
                "PGPASSWORD=$LRDB_PASSWORD psql -h $LRDB_HOST -p $LRDB_PORT "
                "-U $LRDB_USER -d postgres -v ON_ERROR_STOP=1 "
                "-c \"CREATE DATABASE configdb\""
            )
        ],
        Environment=[
            Environment(Name="LRDB_HOST", Value=Ref("DbEndpoint")),
            Environment(Name="LRDB_PORT", Value=Ref("DbPort")),
        ],
        Secrets=db_init_secrets,
        LogConfiguration=LogConfiguration(
            LogDriver="awslogs",
            Options={
                "awslogs-group": Ref(migrator_lg),
                "awslogs-region": Ref("AWS::Region"),
                "awslogs-stream-prefix": "configdb-init",
            },
        ),
    )

    migrator_container = ContainerDefinition(
        Name="migrator",
        Image=Ref("LakerunnerImage"),
        Command=["/app/bin/lakerunner", "migrate", "--databases=lrdb,configdb"],
        Essential=True,
        DependsOn=[ContainerDependency(ContainerName="configdb-init", Condition="COMPLETE")],
        Environment=[
            Environment(Name="LRDB_HOST", Value=Ref("DbEndpoint")),
            Environment(Name="LRDB_PORT", Value=Ref("DbPort")),
            Environment(Name="LRDB_DBNAME", Value=Ref("DbName")),
            Environment(Name="LRDB_SSLMODE", Value="require"),
            Environment(Name="CONFIGDB_HOST", Value=Ref("DbEndpoint")),
            Environment(Name="CONFIGDB_PORT", Value=Ref("DbPort")),
            Environment(Name="CONFIGDB_DBNAME", Value="configdb"),
            Environment(Name="CONFIGDB_SSLMODE", Value="require"),
        ],
        Secrets=[
            Secret(Name="LRDB_USER", ValueFrom=Sub("${DbSecretArn}:username::")),
            Secret(Name="LRDB_PASSWORD", ValueFrom=Sub("${DbSecretArn}:password::")),
            Secret(Name="CONFIGDB_USER", ValueFrom=Sub("${DbSecretArn}:username::")),
            Secret(Name="CONFIGDB_PASSWORD", ValueFrom=Sub("${DbSecretArn}:password::")),
        ],
        LogConfiguration=LogConfiguration(
            LogDriver="awslogs",
            Options={
                "awslogs-group": Ref(migrator_lg),
                "awslogs-region": Ref("AWS::Region"),
                "awslogs-stream-prefix": "migrator",
            },
        ),
    )

    task_def = t.add_resource(
        TaskDefinition(
            "MigratorTaskDef",
            Family=Sub("cardinal-migration-${InstallIdShort}"),
            NetworkMode="awsvpc",
            RequiresCompatibilities=["FARGATE"],
            Cpu="256",
            Memory="512",
            ExecutionRoleArn=Ref("ExecutionRoleArn"),
            TaskRoleArn=GetAtt(task_role, "Arn"),
            ContainerDefinitions=[configdb_init_container, migrator_container],
            Tags=cardinal_tags(component="migration", role="migrator-task"),
        )
    )

    # ---------------------------------------------------------------------------
    # Lambda role
    # ---------------------------------------------------------------------------
    lambda_role = t.add_resource(
        Role(
            "MigrationLambdaRole",
            AssumeRolePolicyDocument={
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Principal": {"Service": "lambda.amazonaws.com"},
                        "Action": "sts:AssumeRole",
                    }
                ],
            },
            Policies=[
                Policy(
                    PolicyName="migration-lambda-policy",
                    PolicyDocument={
                        "Version": "2012-10-17",
                        "Statement": [
                            {
                                "Effect": "Allow",
                                "Action": [
                                    "logs:CreateLogGroup",
                                    "logs:CreateLogStream",
                                    "logs:PutLogEvents",
                                ],
                                "Resource": "*",
                            },
                            {
                                "Effect": "Allow",
                                "Action": ["ecs:RunTask"],
                                "Resource": Ref(task_def),
                                "Condition": {
                                    "ArnLike": {"ecs:cluster": Ref("ClusterArn")}
                                },
                            },
                            {
                                "Effect": "Allow",
                                "Action": ["ecs:DescribeTasks"],
                                "Resource": "*",
                                "Condition": {
                                    "ArnLike": {"ecs:cluster": Ref("ClusterArn")}
                                },
                            },
                            {
                                "Effect": "Allow",
                                "Action": ["iam:PassRole"],
                                "Resource": [
                                    Ref("ExecutionRoleArn"),
                                    GetAtt(task_role, "Arn"),
                                ],
                            },
                        ],
                    },
                )
            ],
            Tags=cardinal_tags(component="migration", role="lambda-role"),
        )
    )

    # ---------------------------------------------------------------------------
    # Migration Lambda
    # ---------------------------------------------------------------------------
    migration_fn = t.add_resource(
        Function(
            "MigrationLambda",
            FunctionName=Sub("cardinal-migration-${InstallIdLong}"),
            Code=Code(ZipFile=migration_lambda.SOURCE),
            Runtime="python3.11",
            Handler="index.lambda_handler",
            Role=GetAtt(lambda_role, "Arn"),
            Timeout=900,
            Tags=cardinal_tags(component="migration", role="lambda"),
        )
    )

    # ---------------------------------------------------------------------------
    # Custom resource — triggers the migrator on create/update
    # ---------------------------------------------------------------------------
    custom_resource = t.add_resource(
        CustomResource(
            "MigrationRunner",
            ServiceToken=GetAtt(migration_fn, "Arn"),
            MigrationVersion=Ref("LakerunnerImage"),
            InstallIdLong=Ref("InstallIdLong"),
            ClusterArn=Ref("ClusterArn"),
            TaskDefinitionArn=Ref(task_def),
            PrivateSubnetIds=Split(",", Ref("PrivateSubnetsCsv")),
            TaskSecurityGroupId=Ref("TaskSecurityGroupId"),
        )
    )

    # ---------------------------------------------------------------------------
    # Outputs
    # ---------------------------------------------------------------------------
    t.add_output(
        Output(
            "MigrationCustomResourceRef",
            Value=Ref(custom_resource),
            Description="Custom resource ref — downstream stacks depend on this.",
        )
    )

    return t


if __name__ == "__main__":
    import sys
    print(build().to_yaml(), end="")
