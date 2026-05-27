"""cardinal-cleanup: stand-alone CFN root template for end-to-end teardown.

Companion to cardinal-lakerunner.yaml. Holds a single Fargate task
definition whose container runs the inline POSIX-sh teardown body from
``cardinal_cfn.cleanup_script.SCRIPT``. The script lives in EntryPoint
(not Command) so an operator with ecs:RunTask cannot substitute their
own command via containerOverrides.

See ``docs/superpowers/specs/2026-05-27-cleanup-stack-design.md``.
"""

import sys

from troposphere import (
    Output,
    Parameter,
    Ref,
    Sub,
    Template,
)
from troposphere.ecs import (
    ContainerDefinition,
    Environment,
    LogConfiguration,
    TaskDefinition,
)
from troposphere.logs import LogGroup

from cardinal_cfn.cleanup_script import SCRIPT


def build() -> Template:
    t = Template(
        Description=(
            "Cardinal cleanup task. Wipes a cardinal-lakerunner install and "
            "self-deletes. Operator-launched via ecs:RunTask after the "
            "stack reaches CREATE_COMPLETE."
        ),
    )

    p_lakerunner = t.add_parameter(Parameter(
        "LakerunnerStackName",
        Type="String",
        Default="cardinal-lakerunner",
        Description="The cardinal-lakerunner CFN stack name to tear down.",
    ))
    p_task_role = t.add_parameter(Parameter(
        "CleanupTaskRoleArn",
        Type="String",
        Description=(
            "Privileged IAM role the cleanup task assumes. See operator runbook "
            "for the required policy."
        ),
    ))
    p_exec_role = t.add_parameter(Parameter(
        "CleanupExecutionRoleArn",
        Type="String",
        Description=(
            "Standard Fargate execution role (ECR pull + log writes). "
            "May be the same role as CleanupTaskRoleArn."
        ),
    ))
    p_cluster = t.add_parameter(Parameter(
        "ClusterName",
        Type="String",
        Description="ECS cluster the cleanup task is launched into.",
    ))
    p_deployer = t.add_parameter(Parameter(
        "DeployerRoleArn",
        Type="String",
        Description=(
            "CFN service role (cardinal-cfn-deployer). The in-task delete-stack "
            "calls pass this as --role-arn so the task role itself does not "
            "need stack-mechanics verbs."
        ),
    ))

    log_group = t.add_resource(LogGroup(
        "CleanupLogGroup",
        LogGroupName=Sub("/aws/ecs/cardinal-cleanup/${AWS::StackName}"),
        RetentionInDays=7,
        DeletionPolicy="Delete",
        UpdateReplacePolicy="Delete",
    ))

    task_def = t.add_resource(TaskDefinition(
        "CleanupTaskDefinition",
        Family="cardinal-cleanup",
        RequiresCompatibilities=["FARGATE"],
        NetworkMode="awsvpc",
        Cpu="512",
        Memory="1024",
        TaskRoleArn=Ref(p_task_role),
        ExecutionRoleArn=Ref(p_exec_role),
        ContainerDefinitions=[ContainerDefinition(
            Name="cleanup",
            Image="public.ecr.aws/aws-cli/aws-cli:latest",
            Essential=True,
            # Script is in EntryPoint so ecs:RunTask containerOverrides.command
            # cannot bypass it (containerOverrides has no entryPoint override).
            EntryPoint=["/bin/sh", "-c", SCRIPT],
            Command=[],
            Environment=[
                Environment(Name="AWS_REGION",            Value=Ref("AWS::Region")),
                Environment(Name="AWS_ACCOUNT_ID",        Value=Ref("AWS::AccountId")),
                Environment(Name="CLUSTER_NAME",          Value=Ref(p_cluster)),
                Environment(Name="LAKERUNNER_STACK_NAME", Value=Ref(p_lakerunner)),
                Environment(Name="CLEANUP_STACK_NAME",    Value=Ref("AWS::StackName")),
                Environment(Name="DEPLOYER_ROLE_ARN",     Value=Ref(p_deployer)),
            ],
            LogConfiguration=LogConfiguration(
                LogDriver="awslogs",
                Options={
                    "awslogs-group":         Ref(log_group),
                    "awslogs-region":        Ref("AWS::Region"),
                    "awslogs-stream-prefix": "cleanup",
                },
            ),
        )],
    ))

    t.add_output(Output(
        "TaskDefinitionArn",
        Value=Ref(task_def),
        Description="ARN the driver passes to ecs:RunTask.",
    ))
    t.add_output(Output(
        "LogGroupName",
        Value=Ref(log_group),
        Description="Log group the driver tails for cleanup-task output.",
    ))

    return t


def main() -> None:
    sys.stdout.write(build().to_yaml())


if __name__ == "__main__":
    main()
