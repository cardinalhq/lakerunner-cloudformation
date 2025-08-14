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

from troposphere import (
  And,
  AWSObject,
  Condition,
  Equals,
  Export,
  GetAtt,
  If,
  ImportValue,
  Not,
  Output,
  Parameter,
  Ref,
  Sub,
  Template,
)
from troposphere.logs import LogGroup
from troposphere.iam import Role, Policy
from troposphere.ecs import (
  ContainerDefinition,
  Environment,
  LogConfiguration,
  Secret as EcsSecret,
  TaskDefinition,
)
from troposphere.awslambda import Function, Code

class RunEcsTask(AWSObject):
    resource_type = "Custom::RunEcsTask"
    props = {
        "ServiceToken": (str, True),
        "ClusterArn": (str, True),
        "TaskDefinitionArn": (str, True),
        "Subnets": ([str], True),
        "SecurityGroups": ([str], False),
        "AssignPublicIp": (str, False),
    }

t = Template()
t.set_description("Lakerunner DB migration (single template): defines TaskDefinition and runs it via Custom Resource.")

# -----------------------
# Parameters (with console hints)
# -----------------------
CommonInfraStackName = t.add_parameter(Parameter(
    "CommonInfraStackName", Type="String", Default="",
    Description="OPTIONAL: Name of the CommonInfra stack to import values from. If set, blank fields below will be auto-filled."
))

ClusterArn = t.add_parameter(Parameter(
    "ClusterArn", Type="String", Default="",
    Description="ECS Cluster ARN. Leave blank to import from CommonInfra if CommonInfraStackName is set."
))

Subnets = t.add_parameter(Parameter(
    "Subnets", Type="List<AWS::EC2::Subnet::Id>",
    Description="REQUIRED: Private subnet IDs (two or more AZs recommended)."
))
SecurityGroups = t.add_parameter(Parameter(
    "SecurityGroups", Type="List<AWS::EC2::SecurityGroup::Id>",
    Description="REQUIRED: Security groups for the task (must reach RDS 5432)."
))
AssignPublicIp = t.add_parameter(Parameter(
    "AssignPublicIp", Type="String", Default="DISABLED",
    AllowedValues=["ENABLED", "DISABLED"],
    Description="Whether to assign a public IP to the task ENI. Use DISABLED for private subnets."
))

TaskRoleArn = t.add_parameter(Parameter(
    "TaskRoleArn", Type="String",
    Description="REQUIRED: IAM Task Role ARN (should allow secretsmanager:GetSecretValue on the DB secret)."
))

DbSecretArn = t.add_parameter(Parameter(
    "DbSecretArn", Type="String", Default="",
    Description="Secrets Manager ARN with {username,password}. Leave blank to import from CommonInfra if CommonInfraStackName is set."
))
ContainerImage = t.add_parameter(Parameter(
    "ContainerImage", Type="String",
    Default="public.ecr.aws/cardinalhq.io/lakerunner:latest",
    Description="Migration container image."
))
DbHost = t.add_parameter(Parameter(
    "DbHost", Type="String", Default="",
    Description="Postgres endpoint hostname. Leave blank to import from CommonInfra if CommonInfraStackName is set."
))
DbName = t.add_parameter(Parameter(
    "DbName", Type="String", Default="metadata",
    Description="Postgres database name."
))
DbUser = t.add_parameter(Parameter(
    "DbUser", Type="String", Default="lakerunner",
    Description="Postgres username (should match the secret's 'username')."
))
DbPort = t.add_parameter(Parameter(
    "DbPort", Type="String", Default="5432",
    Description="Postgres port (usually 5432)."
))
DbSSLMode = t.add_parameter(Parameter(
    "DbSSLMode", Type="String", Default="prefer",
    Description="sslmode (disable, allow, prefer, require, verify-ca, verify-full)."
))
Cpu = t.add_parameter(Parameter(
    "Cpu", Type="String", Default="512",
    Description="Fargate CPU units (e.g., 256/512/1024)."
))
MemoryMiB = t.add_parameter(Parameter(
    "MemoryMiB", Type="String", Default="1024",
    Description="Fargate Memory MiB (e.g., 512/1024/2048)."
))

# Optional: nice parameter groups/labels in the Console
t.set_metadata({
    "AWS::CloudFormation::Interface": {
        "ParameterGroups": [
            {"Label": {"default": "Where to run"}, "Parameters": ["ClusterArn", "Subnets", "SecurityGroups", "AssignPublicIp"]},
            {"Label": {"default": "Task Sizing"}, "Parameters": ["Cpu", "MemoryMiB"]},
            {"Label": {"default": "Container Image"}, "Parameters": ["ContainerImage"]},
            {"Label": {"default": "Database Connection"}, "Parameters": ["DbHost", "DbName", "DbUser", "DbPort", "DbSSLMode", "DbSecretArn"]},
            {"Label": {"default": "Permissions"}, "Parameters": ["TaskRoleArn"]},
        ],
        "ParameterLabels": {
            "ClusterArn": {"default": "ECS Cluster ARN"},
            "Subnets": {"default": "Private Subnets"},
            "SecurityGroups": {"default": "Task Security Groups"},
            "AssignPublicIp": {"default": "Assign Public IP"},
            "Cpu": {"default": "Fargate CPU"},
            "MemoryMiB": {"default": "Fargate Memory (MiB)"},
            "ContainerImage": {"default": "Migration Image"},
            "DbHost": {"default": "DB Hostname"},
            "DbName": {"default": "DB Name"},
            "DbUser": {"default": "DB User"},
            "DbPort": {"default": "DB Port"},
            "DbSSLMode": {"default": "DB sslmode"},
            "DbSecretArn": {"default": "DB Secret ARN"},
            "TaskRoleArn": {"default": "Task Role ARN"},
        }
    }
})

t.add_condition("HasCI", Not(Equals(Ref(CommonInfraStackName), "")))
t.add_condition("NoClusterArn", Equals(Ref(ClusterArn), ""))
t.add_condition("NoDbHost", Equals(Ref(DbHost), ""))
t.add_condition("NoDbSecretArn", Equals(Ref(DbSecretArn), ""))

t.add_condition("UseClusterImport", And(Condition("HasCI"), Condition("NoClusterArn")))
t.add_condition("UseDbHostImport", And(Condition("HasCI"), Condition("NoDbHost")))
t.add_condition("UseDbSecretImport", And(Condition("HasCI"), Condition("NoDbSecretArn")))

# Helper: build "${CommonInfraStackName}-Suffix"
def ci_export(suffix):
    # Sub returns a string like "mystack-ClusterArn"
    return Sub("${CommonInfraStackName}-%s" % suffix)

ClusterArnValue = If(
    "UseClusterImport",
    ImportValue(ci_export("ClusterArn")),
    Ref(ClusterArn)
)

DbHostValue = If(
    "UseDbHostImport",
    ImportValue(ci_export("DbEndpoint")),
    Ref(DbHost)
)

DbSecretArnValue = If(
    "UseDbSecretImport",
    ImportValue(ci_export("DbSecretArn")),
    Ref(DbSecretArn)
)

# -----------------------
# CloudWatch Logs
# -----------------------
LogGroupRes = t.add_resource(LogGroup(
    "MigrationLogGroup",
    LogGroupName=Sub("/lakerunner/migration/${AWS::StackName}"),
    RetentionInDays=14
))

# -----------------------
# Task Execution Role (pull image + write logs)
# -----------------------
ExecutionRole = t.add_resource(Role(
    "TaskExecutionRole",
    AssumeRolePolicyDocument={
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Principal": {"Service": "ecs-tasks.amazonaws.com"},
            "Action": "sts:AssumeRole"
        }]
    },
    ManagedPolicyArns=[
        "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
    ]
))

# -----------------------
# Task Definition (Fargate)
# -----------------------
TaskDef = t.add_resource(TaskDefinition(
    "MigrationTaskDef",
    Family="lakerunner-migration",
    Cpu=Ref(Cpu),
    Memory=Ref(MemoryMiB),
    NetworkMode="awsvpc",
    RequiresCompatibilities=["FARGATE"],
    ExecutionRoleArn=GetAtt(ExecutionRole, "Arn"),
    TaskRoleArn=Ref(TaskRoleArn),
    ContainerDefinitions=[
        ContainerDefinition(
            Name="Migrator",
            Image=Ref(ContainerImage),
            Command=["/app/bin/lakerunner", "migrate"],
            LogConfiguration=LogConfiguration(
                LogDriver="awslogs",
                Options={
                    "awslogs-group": Ref(LogGroupRes),
                    "awslogs-region": Sub("${AWS::Region}"),
                    "awslogs-stream-prefix": "migration"
                }
            ),
            Environment=[
                Environment(Name="LRDB_HOST", Value=DbHostValue),
                Environment(Name="LRDB_PORT", Value=Ref(DbPort)),
                Environment(Name="LRDB_NAME", Value=Ref(DbName)),
                Environment(Name="LRDB_USER", Value=Ref(DbUser)),
                Environment(Name="LRDB_SSLMODE", Value=Ref(DbSSLMode)),
            ],
            Secrets=[
                EcsSecret(Name="LRDB_PASSWORD", ValueFrom=Sub("${S}:password::", S=DbSecretArnValue))
            ]
        )
    ]
))

# -----------------------
# Lambda Role (for the custom resource runner)
# -----------------------
LambdaRole = t.add_resource(Role(
    "RunnerLambdaRole",
    AssumeRolePolicyDocument={
        "Version":"2012-10-17",
        "Statement":[{"Effect":"Allow","Principal":{"Service":"lambda.amazonaws.com"},"Action":"sts:AssumeRole"}]
    },
    Policies=[
        Policy(
            "RunnerInlinePolicy",
            PolicyName="RunnerInlinePolicy",
            PolicyDocument={
                "Version":"2012-10-17",
                "Statement":[
                    # Logs
                    {"Effect":"Allow","Action":["logs:CreateLogGroup","logs:CreateLogStream","logs:PutLogEvents"],"Resource":"*"},
                    # ECS
                    {"Effect":"Allow","Action":["ecs:RunTask","ecs:DescribeTasks"],"Resource":"*"},
                    # Pass the roles embedded in the taskdef
                    {"Effect":"Allow","Action":["iam:PassRole"],"Resource":[Ref(TaskRoleArn), GetAtt(ExecutionRole, "Arn")]},
                ]
            }
        )
    ]
))

# -----------------------
# Inline Lambda that runs the task and waits for completion
# -----------------------
lambda_code = r'''
import json, time, urllib.request, boto3

ecs = boto3.client("ecs")

def send(event, context, status, data=None, reason=""):
    resp = {
        "Status": status,
        "Reason": f"{reason} See CloudWatch Logs for details: {context.log_stream_name}",
        "PhysicalResourceId": event.get("PhysicalResourceId") or "RunEcsTask",
        "StackId": event["StackId"],
        "RequestId": event["RequestId"],
        "LogicalResourceId": event["LogicalResourceId"],
        "NoEcho": False,
        "Data": data or {}
    }
    body = json.dumps(resp).encode("utf-8")
    req = urllib.request.Request(event["ResponseURL"], data=body, method="PUT")
    req.add_header("content-type","")
    req.add_header("content-length", str(len(body)))
    try:
        with urllib.request.urlopen(req) as r:
            r.read()
    except Exception as e:
        print("Failed to send response:", e)

def handler(event, context):
    print("Event:", json.dumps(event))
    reqtype = event["RequestType"]
    props = event.get("ResourceProperties", {})

    if reqtype == "Delete":
        # No-op; nothing to tear down
        send(event, context, "SUCCESS", {"Message":"Delete no-op"})
        return

    cluster = props["ClusterArn"]
    taskdef = props["TaskDefinitionArn"]
    subnets = props["Subnets"]
    sgs = props["SecurityGroups"]
    assign = props.get("AssignPublicIp","DISABLED")

    try:
        run = ecs.run_task(
            cluster=cluster,
            taskDefinition=taskdef,
            launchType="FARGATE",
            platformVersion="LATEST",
            networkConfiguration={
                "awsvpcConfiguration": {
                    "subnets": subnets,
                    "securityGroups": sgs,
                    "assignPublicIp": assign
                }
            },
            count=1
        )
        failures = run.get("failures",[])
        if failures:
            reason = "; ".join(f.get("reason","unknown") for f in failures)
            send(event, context, "FAILED", {"RunFailures": failures}, reason=f"RunTask failures: {reason}")
            return

        tasks = run.get("tasks", [])
        if not tasks:
            send(event, context, "FAILED", {"Message":"ecs.run_task returned no tasks"}, reason="No task started")
            return

        task_arn = tasks[0]["taskArn"]
        print("Started task:", task_arn)

        deadline = time.time() + 14*60
        last_status = None
        while time.time() < deadline:
            d = ecs.describe_tasks(cluster=cluster, tasks=[task_arn])
            ts = d.get("tasks", [])
            if not ts:
                send(event, context, "FAILED", {"Message":"Task disappeared"}, reason="DescribeTasks returned no tasks")
                return
            t = ts[0]
            last_status = t.get("lastStatus","UNKNOWN")
            print("Status:", last_status)
            if last_status == "STOPPED":
                bad = []
                for c in t.get("containers",[]):
                    code = c.get("exitCode")
                    name = c.get("name")
                    if code is None or code != 0:
                        bad.append({"name": name, "exitCode": code})
                if bad:
                    send(event, context, "FAILED",
                         {"TaskArn": task_arn, "StoppedReason": t.get("stoppedReason"), "BadContainers": bad},
                         reason=f"Container(s) non-zero exit: {bad}")
                    return
                send(event, context, "SUCCESS",
                     {"TaskArn": task_arn, "StoppedReason": t.get("stoppedReason","")})
                return
            time.sleep(6)

        send(event, context, "FAILED", {"TaskArn": task_arn, "LastStatus": last_status}, reason="Timeout waiting for STOPPED")

    except Exception as e:
        print("Exception:", e)
        send(event, context, "FAILED", {"Error": str(e)}, reason=str(e))
'''

RunnerFn = t.add_resource(Function(
    "RunEcsTaskFunction",
    Runtime="python3.13",
    Handler="index.handler",
    Role=GetAtt(LambdaRole, "Arn"),
    Timeout=900,  # up to 15 minutes
    Code=Code(ZipFile=lambda_code)
))

# -----------------------
# Custom Resource to trigger the run
# -----------------------
RunMigration = t.add_resource(RunEcsTask(
    "RunMigration",
    ServiceToken=GetAtt(RunnerFn, "Arn"),
    ClusterArn=ClusterArnValue,  # use the import/override value
    TaskDefinitionArn=GetAtt(TaskDef, "TaskDefinitionArn"),
    Subnets=Ref(Subnets),
    SecurityGroups=Ref(SecurityGroups),  # or SecurityGroupsValue if you implemented the optional bit
    AssignPublicIp=Ref(AssignPublicIp)
))

# -----------------------
# Outputs
# -----------------------
t.add_output(Output(
    "TaskDefinitionArn",
    Value=GetAtt(TaskDef, "TaskDefinitionArn"),  # ← change here
    Export=Export(name=Sub("${AWS::StackName}-TaskDefinitionArn"))
))
t.add_output(Output(
    "RunnerFunctionArn",
    Value=GetAtt(RunnerFn, "Arn"),
    Export=Export(name=Sub("${AWS::StackName}-RunnerFnArn"))
))
t.add_output(Output(
    "LogGroupName",
    Value=Ref(LogGroupRes),
    Export=Export(name=Sub("${AWS::StackName}-LogGroup"))
))
t.add_output(Output(
    "RunMigrationId",
    Value=Ref(RunMigration),
    Export=Export(name=Sub("${AWS::StackName}-RunMigrationId"))
))

print(t.to_yaml())
