#!/usr/bin/env python3

"""
Lakerunner Grafana Setup Stack

Creates a separate database for Grafana on the existing RDS instance.
This stack creates the "lakerunner_grafana" database and associated user/secret.
"""

from troposphere import (
    Template, Parameter, Output, Ref, Sub, GetAtt, ImportValue,
    Export, Split
)
from troposphere.rds import DBInstance
from troposphere.secretsmanager import Secret, GenerateSecretString
from troposphere.awslambda import Function, Code, VPCConfig
from troposphere.iam import Role, Policy
from troposphere.logs import LogGroup
from troposphere.cloudformation import CustomResource

def load_defaults(config_file="lakerunner-grafana-setup-defaults.yaml"):
    """Load default configuration from YAML file"""
    # For this template, we don't need complex YAML parsing since it's mostly just parameters
    # The configuration is minimal and handled via CloudFormation parameters
    return {}

def create_grafana_setup_template():
    """Create CloudFormation template for Grafana database setup"""
    
    t = Template()
    t.set_description("Lakerunner Grafana Setup: Creates dedicated database for Grafana on existing RDS instance")

    # Load defaults
    defaults = load_defaults()

    # Parameters
    common_infra_stack_name = t.add_parameter(Parameter(
        "CommonInfraStackName",
        Type="String",
        Description="REQUIRED: Name of the CommonInfra stack to import RDS instance from.",
    ))

    grafana_db_name = t.add_parameter(Parameter(
        "GrafanaDbName",
        Type="String",
        Default="lakerunner_grafana",
        Description="Name of the database to create for Grafana",
    ))

    grafana_db_user = t.add_parameter(Parameter(
        "GrafanaDbUser", 
        Type="String",
        Default="grafana",
        Description="Database user for Grafana",
    ))

    # Create secret for Grafana database credentials
    grafana_db_secret = t.add_resource(Secret(
        "GrafanaDbSecret",
        Description="Grafana database credentials",
        GenerateSecretString=GenerateSecretString(
            SecretStringTemplate=Sub('{"username":"${User}"}', User=Ref(grafana_db_user)),
            GenerateStringKey="password",
            ExcludeCharacters=' !"#$%&\'()*+,./:;<=>?@[\\]^`{|}~',
            PasswordLength=32
        )
    ))

    # Lambda function to create database
    create_db_function_role = t.add_resource(Role(
        "CreateDbFunctionRole",
        AssumeRolePolicyDocument={
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {"Service": "lambda.amazonaws.com"},
                    "Action": "sts:AssumeRole"
                }
            ]
        },
        ManagedPolicyArns=[
            "arn:aws:iam::aws:policy/service-role/AWSLambdaVPCExecutionRole"
        ],
        Policies=[
            Policy(
                PolicyName="DatabaseAccess",
                PolicyDocument={
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Action": [
                                "secretsmanager:GetSecretValue"
                            ],
                            "Resource": [
                                ImportValue(Sub("${CommonInfraStackName}-DbSecretArn", 
                                               CommonInfraStackName=Ref(common_infra_stack_name))),
                                Ref(grafana_db_secret)
                            ]
                        }
                    ]
                }
            )
        ]
    ))

    # Lambda function log group
    create_db_log_group = t.add_resource(LogGroup(
        "CreateDbLogGroup",
        LogGroupName="/aws/lambda/grafana-db-setup",
        RetentionInDays=14
    ))

    # Lambda function to create database and user
    create_db_function = t.add_resource(Function(
        "CreateDbFunction",
        FunctionName=Sub("${AWS::StackName}-grafana-db-setup"),
        Runtime="python3.13",
        Handler="index.handler",
        Role=GetAtt(create_db_function_role, "Arn"),
        Timeout=300,
        VpcConfig=VPCConfig(
            SecurityGroupIds=[
                ImportValue(Sub("${CommonInfraStackName}-TaskSGId",
                              CommonInfraStackName=Ref(common_infra_stack_name)))
            ],
            SubnetIds=Split(",", ImportValue(Sub("${CommonInfraStackName}-PrivateSubnets",
                                                CommonInfraStackName=Ref(common_infra_stack_name))))
        ),
        Code=Code(ZipFile="""
import json
import boto3
import psycopg2
import urllib.request

def send_response(event, context, status, data=None, reason=""):
    resp = {
        "Status": status,
        "Reason": f"{reason} See CloudWatch Logs for details: {context.log_stream_name}",
        "PhysicalResourceId": event.get("PhysicalResourceId") or "GrafanaDbSetup",
        "StackId": event["StackId"],
        "RequestId": event["RequestId"],
        "LogicalResourceId": event["LogicalResourceId"],
        "NoEcho": False,
        "Data": data or {}
    }
    body = json.dumps(resp).encode("utf-8")
    req = urllib.request.Request(event["ResponseURL"], data=body, method="PUT")
    req.add_header("content-type", "")
    req.add_header("content-length", str(len(body)))
    try:
        with urllib.request.urlopen(req) as r:
            r.read()
    except Exception as e:
        print("Failed to send response:", e)

def get_secret(secret_arn):
    client = boto3.client('secretsmanager')
    response = client.get_secret_value(SecretId=secret_arn)
    return json.loads(response['SecretString'])

def handler(event, context):
    print("Event:", json.dumps(event))
    
    try:
        request_type = event["RequestType"]
        props = event.get("ResourceProperties", {})
        
        if request_type == "Delete":
            send_response(event, context, "SUCCESS", {"Message": "Delete no-op"})
            return
            
        # Get connection details
        main_secret_arn = props["MainDbSecretArn"]
        grafana_secret_arn = props["GrafanaDbSecretArn"]
        db_host = props["DbHost"]
        db_port = int(props["DbPort"])
        grafana_db_name = props["GrafanaDbName"]
        grafana_db_user = props["GrafanaDbUser"]
        
        # Get credentials
        main_creds = get_secret(main_secret_arn)
        grafana_creds = get_secret(grafana_secret_arn)
        
        # Connect to main database as superuser
        conn = psycopg2.connect(
            host=db_host,
            port=db_port,
            database="lakerunner",
            user=main_creds["username"],
            password=main_creds["password"],
            sslmode="require"
        )
        conn.autocommit = True
        cursor = conn.cursor()
        
        # Create database if it doesn't exist
        cursor.execute(f"SELECT 1 FROM pg_database WHERE datname = %s", (grafana_db_name,))
        if not cursor.fetchone():
            cursor.execute(f'CREATE DATABASE "{grafana_db_name}"')
            print(f"Created database {grafana_db_name}")
        else:
            print(f"Database {grafana_db_name} already exists")
            
        # Create user if it doesn't exist  
        cursor.execute("SELECT 1 FROM pg_user WHERE usename = %s", (grafana_db_user,))
        if not cursor.fetchone():
            cursor.execute(f'CREATE USER "{grafana_db_user}" WITH PASSWORD %s', 
                         (grafana_creds["password"],))
            print(f"Created user {grafana_db_user}")
        else:
            # Update password in case secret was rotated
            cursor.execute(f'ALTER USER "{grafana_db_user}" WITH PASSWORD %s',
                         (grafana_creds["password"],))
            print(f"Updated password for user {grafana_db_user}")
            
        # Grant privileges
        cursor.execute(f'GRANT ALL PRIVILEGES ON DATABASE "{grafana_db_name}" TO "{grafana_db_user}"')
        
        cursor.close()
        conn.close()
        
        # Connect to Grafana database to set up schema permissions
        conn = psycopg2.connect(
            host=db_host,
            port=db_port,
            database=grafana_db_name,
            user=main_creds["username"],
            password=main_creds["password"],
            sslmode="require"
        )
        conn.autocommit = True
        cursor = conn.cursor()
        
        # Grant schema privileges
        cursor.execute(f'GRANT ALL ON SCHEMA public TO "{grafana_db_user}"')
        cursor.execute(f'GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO "{grafana_db_user}"')
        cursor.execute(f'GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO "{grafana_db_user}"')
        cursor.execute(f'ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO "{grafana_db_user}"')
        cursor.execute(f'ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO "{grafana_db_user}"')
        
        cursor.close()
        conn.close()
        
        send_response(event, context, "SUCCESS", {
            "DatabaseName": grafana_db_name,
            "Username": grafana_db_user
        })
        
    except Exception as e:
        print("Exception:", str(e))
        send_response(event, context, "FAILED", {"Error": str(e)}, reason=str(e))
""")
    ))

    # Custom resource to trigger database creation
    grafana_db_setup = t.add_resource(CustomResource(
        "GrafanaDbSetup",
        ServiceToken=GetAtt(create_db_function, "Arn"),
        MainDbSecretArn=ImportValue(Sub("${CommonInfraStackName}-DbSecretArn",
                                       CommonInfraStackName=Ref(common_infra_stack_name))),
        GrafanaDbSecretArn=Ref(grafana_db_secret),
        DbHost=ImportValue(Sub("${CommonInfraStackName}-DbEndpoint", 
                             CommonInfraStackName=Ref(common_infra_stack_name))),
        DbPort=ImportValue(Sub("${CommonInfraStackName}-DbPort",
                             CommonInfraStackName=Ref(common_infra_stack_name))),
        GrafanaDbName=Ref(grafana_db_name),
        GrafanaDbUser=Ref(grafana_db_user)
    ))

    # Outputs
    t.add_output(Output(
        "GrafanaDbSecretArn",
        Description="ARN of Grafana database credentials secret",
        Value=Ref(grafana_db_secret),
        Export=Export(Sub("${AWS::StackName}-DbSecretArn"))
    ))

    t.add_output(Output(
        "GrafanaDbName",
        Description="Name of Grafana database",
        Value=Ref(grafana_db_name),
        Export=Export(Sub("${AWS::StackName}-DbName"))
    ))

    t.add_output(Output(
        "GrafanaDbUser",
        Description="Grafana database username",
        Value=Ref(grafana_db_user),
        Export=Export(Sub("${AWS::StackName}-DbUser"))
    ))

    t.add_output(Output(
        "GrafanaDbHost",
        Description="Grafana database host (imported from CommonInfra)",
        Value=ImportValue(Sub("${CommonInfraStackName}-DbEndpoint",
                            CommonInfraStackName=Ref(common_infra_stack_name))),
        Export=Export(Sub("${AWS::StackName}-DbHost"))
    ))

    t.add_output(Output(
        "GrafanaDbPort", 
        Description="Grafana database port (imported from CommonInfra)",
        Value=ImportValue(Sub("${CommonInfraStackName}-DbPort",
                            CommonInfraStackName=Ref(common_infra_stack_name))),
        Export=Export(Sub("${AWS::StackName}-DbPort"))
    ))

    return t

if __name__ == "__main__":
    template = create_grafana_setup_template()
    print(template.to_yaml())