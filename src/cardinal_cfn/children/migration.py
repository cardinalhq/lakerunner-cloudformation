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

    # Image parameters
    t.add_parameter(
        Parameter(
            "MigrationImage",
            Type="String",
            Default=defaults["images"]["migration"],
            Description="Container image for the DB migrator.",
        )
    )
    t.add_parameter(
        Parameter(
            "MigrationImageDigest",
            Type="String",
            AllowedPattern=r"^sha256:[0-9a-f]{64}$",
            ConstraintDescription="Must be a sha256 image digest (sha256:<64 hex chars>).",
            Description="Image digest (sha256:...) used to trigger re-runs on upgrade.",
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
    # Migrator ECS task definition
    # ---------------------------------------------------------------------------
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
            ContainerDefinitions=[
                ContainerDefinition(
                    Name="migrator",
                    Image=Ref("MigrationImage"),
                    Command=["/app/bin/lakerunner", "migrate"],
                    Essential=True,
                    Environment=[
                        Environment(Name="LRDB_HOST", Value=Ref("DbEndpoint")),
                        Environment(Name="LRDB_PORT", Value=Ref("DbPort")),
                        Environment(Name="LRDB_DBNAME", Value=Ref("DbName")),
                        Environment(Name="LRDB_SSLMODE", Value="require"),
                    ],
                    Secrets=[
                        Secret(
                            Name="LRDB_PASSWORD",
                            ValueFrom=Sub("${DbSecretArn}:password::"),
                        )
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
            ],
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
            MigrationVersion=Ref("MigrationImageDigest"),
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
