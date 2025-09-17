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
  AWSObject,
  Export,
  GetAtt,
  ImportValue,
  Output,
  Parameter,
  Ref,
  Split,
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
import yaml
import os

def load_defaults(config_file="lakerunner-stack-defaults.yaml"):
    """Load default configuration from YAML file"""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    config_path = os.path.join(script_dir, "..", config_file)

    with open(config_path, 'r') as f:
        return yaml.safe_load(f)

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

# Load defaults for image configuration
defaults = load_defaults()
images = defaults.get('images', {})

# -----------------------
# Parameters (with console hints)
# -----------------------
CommonInfraStackName = t.add_parameter(Parameter(
    "CommonInfraStackName", Type="String", Default="",
    Description="REQUIRED: Name of the CommonInfra stack to import values from."
))

ContainerImage = t.add_parameter(Parameter(
    "ContainerImage", Type="String",
    Default=images.get('migration', 'public.ecr.aws/cardinalhq.io/lakerunner:latest'),
    Description="Migration container image."
))
Cpu = t.add_parameter(Parameter(
    "Cpu", Type="String", Default="512",
    Description="Fargate CPU units (e.g., 256/512/1024)."
))
MemoryMiB = t.add_parameter(Parameter(
    "MemoryMiB", Type="String", Default="1024",
    Description="Fargate Memory MiB (e.g., 512/1024/2048)."
))

MSKBrokers = t.add_parameter(Parameter(
    "MSKBrokers", Type="String", Default="",
    Description="REQUIRED: Comma-separated list of MSK broker endpoints (hostname:port)"
))

t.set_metadata({
    "AWS::CloudFormation::Interface": {
        "ParameterGroups": [
            {"Label": {"default": "CommonInfra Stack"}, "Parameters": ["CommonInfraStackName"]},
            {"Label": {"default": "MSK Configuration"}, "Parameters": ["MSKBrokers"]},
            {"Label": {"default": "Task Sizing"}, "Parameters": ["Cpu", "MemoryMiB"]},
            {"Label": {"default": "Container Image"}, "Parameters": ["ContainerImage"]},
        ],
        "ParameterLabels": {
            "CommonInfraStackName": {"default": "CommonInfra Stack Name"},
            "MSKBrokers": {"default": "MSK Broker Endpoints"},
            "Cpu": {"default": "Fargate CPU"},
            "MemoryMiB": {"default": "Fargate Memory (MiB)"},
            "ContainerImage": {"default": "Migration Image"},
        }
    }
})


# Helper: build "${CommonInfraStackName}-Suffix"
def ci_export(suffix):
    # Sub returns a string like "mystack-ClusterArn"
    return Sub("${CommonInfraStackName}-%s" % suffix)

ClusterArnValue = ImportValue(ci_export("ClusterArn"))
DbHostValue = ImportValue(ci_export("DbEndpoint"))
DbSecretArnValue = ImportValue(ci_export("DbSecretArn"))
MSKCredentialsArnValue = ImportValue(ci_export("MSKCredentialsArn"))
SecurityGroupsValue = ImportValue(ci_export("TaskSGId"))
PrivateSubnetsValue = Split(",", ImportValue(ci_export("PrivateSubnets")))

# -----------------------
# CloudWatch Logs
# -----------------------
LogGroupRes = t.add_resource(LogGroup(
    "MigrationLogGroup",
    LogGroupName=Sub("/lakerunner/migration/${AWS::StackName}"),
    RetentionInDays=14
))

# -----------------------
# Task Role (runtime permissions for the migration task)
# -----------------------
TaskRole = t.add_resource(Role(
    "MigrationTaskRole",
    AssumeRolePolicyDocument={
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Principal": {"Service": "ecs-tasks.amazonaws.com"},
            "Action": "sts:AssumeRole"
        }]
    },
    Policies=[
        Policy(
            "MigrationTaskPolicy",
            PolicyName="MigrationTaskPolicy",
            PolicyDocument={
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Action": ["secretsmanager:GetSecretValue"],
                        "Resource": [DbSecretArnValue, MSKCredentialsArnValue]
                    }
                ]
            }
        )
    ]
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
    ],
    Policies=[
        Policy(
            "TaskExecutionSecretsPolicy",
            PolicyName="TaskExecutionSecretsPolicy",
            PolicyDocument={
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Action": ["secretsmanager:GetSecretValue"],
                        "Resource": [
                            Sub("${SecretArn}*", SecretArn=DbSecretArnValue),
                            Sub("${SecretArn}*", SecretArn=MSKCredentialsArnValue)
                        ]
                    },
                    {
                        "Effect": "Allow",
                        "Action": ["ssm:GetParameters", "ssm:GetParameter"],
                        "Resource": [
                            Sub("arn:aws:ssm:${AWS::Region}:${AWS::AccountId}:parameter/lakerunner/api_keys"),
                            Sub("arn:aws:ssm:${AWS::Region}:${AWS::AccountId}:parameter/lakerunner/storage_profiles")
                        ]
                    }
                ]
            }
        )
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
    TaskRoleArn=GetAtt(TaskRole, "Arn"),
    ContainerDefinitions=[
        ContainerDefinition(
            Name="Migrator",
            Image=Ref(ContainerImage),
            Command=["/app/bin/lakerunner", "setup"],
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
                Environment(Name="LRDB_PORT", Value="5432"),
                Environment(Name="LRDB_DBNAME", Value="lakerunner"),
                Environment(Name="LRDB_USER", Value="lakerunner"),
                Environment(Name="LRDB_SSLMODE", Value="require"),
                Environment(Name="CONFIGDB_HOST", Value=DbHostValue),
                Environment(Name="CONFIGDB_PORT", Value="5432"),
                Environment(Name="CONFIGDB_DBNAME", Value="lakerunner"),
                Environment(Name="CONFIGDB_USER", Value="lakerunner"),
                Environment(Name="CONFIGDB_SSLMODE", Value="require"),
                Environment(Name="API_KEYS_FILE", Value="env:API_KEYS_ENV"),
                Environment(Name="STORAGE_PROFILE_FILE", Value="env:STORAGE_PROFILES_ENV"),
                # MSK Kafka Configuration
                Environment(Name="LAKERUNNER_KAFKA_BROKERS", Value=Ref(MSKBrokers)),
                Environment(Name="LAKERUNNER_KAFKA_TLS_ENABLED", Value="true"),
                Environment(Name="LAKERUNNER_KAFKA_SASL_ENABLED", Value="true"),
                Environment(Name="LAKERUNNER_KAFKA_SASL_MECHANISM", Value="SCRAM-SHA-512"),
                Environment(Name="LAKERUNNER_KAFKA_TOPICS_DEFAULTS_REPLICATIONFACTOR", Value="2"),
            ],
            Secrets=[
                EcsSecret(Name="LRDB_PASSWORD", ValueFrom=Sub("${S}:password::", S=DbSecretArnValue)),
                EcsSecret(Name="CONFIGDB_PASSWORD", ValueFrom=Sub("${S}:password::", S=DbSecretArnValue)),
                EcsSecret(Name="API_KEYS_ENV", ValueFrom=Sub("arn:aws:ssm:${AWS::Region}:${AWS::AccountId}:parameter/lakerunner/api_keys")),
                EcsSecret(Name="STORAGE_PROFILES_ENV", ValueFrom=Sub("arn:aws:ssm:${AWS::Region}:${AWS::AccountId}:parameter/lakerunner/storage_profiles")),
                # MSK SASL/SCRAM Credentials
                EcsSecret(Name="LAKERUNNER_KAFKA_SASL_USERNAME", ValueFrom=Sub("${S}:username::", S=MSKCredentialsArnValue)),
                EcsSecret(Name="LAKERUNNER_KAFKA_SASL_PASSWORD", ValueFrom=Sub("${S}:password::", S=MSKCredentialsArnValue))
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
                    {"Effect":"Allow","Action":["iam:PassRole"],"Resource":[GetAtt(TaskRole, "Arn"), GetAtt(ExecutionRole, "Arn")]},
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
    ClusterArn=ClusterArnValue,
    TaskDefinitionArn=GetAtt(TaskDef, "TaskDefinitionArn"),
    Subnets=PrivateSubnetsValue,
    SecurityGroups=[SecurityGroupsValue],
    AssignPublicIp="DISABLED"
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
