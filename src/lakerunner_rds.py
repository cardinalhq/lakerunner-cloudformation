#!/usr/bin/env python3
"""RDS PostgreSQL stack for Lakerunner."""

from troposphere import Template, Parameter, Ref, Sub, GetAtt, Export, Output, Select, Split, If, Equals, Not
from troposphere.secretsmanager import Secret, GenerateSecretString
from troposphere.rds import DBInstance, DBSubnetGroup
from troposphere.iam import PolicyType, Role, Policy
from troposphere.ec2 import SecurityGroup, SecurityGroupRule
from troposphere.awslambda import Function, Code, VPCConfig, LayerVersion, Content
from troposphere.cloudformation import CustomResource


t = Template()
t.set_description("RDS PostgreSQL database stack for Lakerunner.")

# -----------------------
# Parameters
# -----------------------
PrivateSubnets = t.add_parameter(Parameter(
    "PrivateSubnets",
    Type="List<AWS::EC2::Subnet::Id>",
    Description="REQUIRED: Private subnet IDs for the database",
))

VpcId = t.add_parameter(Parameter(
    "VpcId",
    Type="AWS::EC2::VPC::Id",
    Description="REQUIRED: VPC ID where the database will be deployed",
))

# Optional parameter for existing task role (BYO scenario)
ExistingTaskRoleArn = t.add_parameter(Parameter(
    "ExistingTaskRoleArn",
    Type="String",
    Default="",
    Description="OPTIONAL: Existing task role ARN to attach database permissions to. Leave blank to create a new role.",
))

DbInstanceClass = t.add_parameter(Parameter(
    "DbInstanceClass",
    Type="String",
    Default="db.r6g.large",
    AllowedValues=[
        "db.r6g.large", "db.r6g.xlarge", "db.r6g.2xlarge", "db.r6g.4xlarge",
        "db.r6g.8xlarge", "db.r6g.12xlarge", "db.r6g.16xlarge"
    ],
    Description="RDS instance class.",
))

# -----------------------
# Conditions
# -----------------------
t.add_condition("CreateTaskRole", Equals(Ref(ExistingTaskRoleArn), ""))
t.add_condition("UseExistingTaskRole", Not(Equals(Ref(ExistingTaskRoleArn), "")))

# -----------------------
# Security Group for Database
# -----------------------
DbSecurityGroup = t.add_resource(SecurityGroup(
    "DatabaseSecurityGroup",
    GroupDescription="Security group for RDS PostgreSQL database",
    VpcId=Ref(VpcId),
    SecurityGroupIngress=[
        SecurityGroupRule(
            IpProtocol="tcp",
            FromPort=5432,
            ToPort=5432,
            CidrIp="10.0.0.0/8",  # Allow from private networks
            Description="PostgreSQL access from private networks"
        )
    ],
    Tags=[
        {"Key": "Name", "Value": Sub("${AWS::StackName}-db-sg")},
        {"Key": "Component", "Value": "Database"},
        {"Key": "ManagedBy", "Value": "Lakerunner"}
    ]
))

# -----------------------
# Task Role for Database Access (conditional)
# -----------------------
DatabaseTaskRole = t.add_resource(Role(
    "DatabaseTaskRole",
    Condition="CreateTaskRole",
    RoleName=Sub("${AWS::StackName}-database-task-role"),
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
            PolicyName="BaseECSTaskPolicy",
            PolicyDocument={
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Action": [
                            "ssm:GetParameter",
                            "ssm:GetParameters"
                        ],
                        "Resource": [
                            Sub("arn:aws:ssm:${AWS::Region}:${AWS::AccountId}:parameter/lakerunner/*"),
                            Sub("arn:aws:ssm:${AWS::Region}:${AWS::AccountId}:parameter/${AWS::StackName}-*")
                        ]
                    }
                ]
            }
        )
    ]
))

# -----------------------
# Secrets for DB
# -----------------------
DbSecret = t.add_resource(Secret(
    "DbSecret",
    GenerateSecretString=GenerateSecretString(
        SecretStringTemplate='{"username":"lakerunner"}',
        GenerateStringKey="password",
        ExcludePunctuation=True,
    ),
))
DbSecretArnValue = Ref(DbSecret)

# -----------------------
# RDS Postgres
# -----------------------
DbSubnets = t.add_resource(DBSubnetGroup(
    "DbSubnetGroup",
    DBSubnetGroupDescription="DB subnets",
    SubnetIds=Ref(PrivateSubnets),
))

DbRes = t.add_resource(DBInstance(
    "LakerunnerDb",
    Engine="postgres",
    EngineVersion="17",
    DBInstanceClass=Ref(DbInstanceClass),
    PubliclyAccessible=False,
    MultiAZ=False,
    CopyTagsToSnapshot=True,
    StorageType="gp3",
    AllocatedStorage="100",
    VPCSecurityGroups=[Ref(DbSecurityGroup)],
    DBSubnetGroupName=Ref(DbSubnets),
    MasterUsername=Sub("{{resolve:secretsmanager:${S}:SecretString:username}}", S=DbSecretArnValue),
    MasterUserPassword=Sub("{{resolve:secretsmanager:${S}:SecretString:password}}", S=DbSecretArnValue),
    DeletionProtection=False,
))

DbEndpoint = GetAtt(DbRes, "Endpoint.Address")
DbPort = GetAtt(DbRes, "Endpoint.Port")

# -----------------------
# Lambda Role for Database Creation
# -----------------------
DatabaseLambdaRole = t.add_resource(Role(
    "DatabaseLambdaRole",
    RoleName=Sub("${AWS::StackName}-database-lambda-role"),
    AssumeRolePolicyDocument={
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Principal": {"Service": "lambda.amazonaws.com"},
            "Action": "sts:AssumeRole"
        }]
    },
    ManagedPolicyArns=[
        "arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"
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
                            DbSecretArnValue
                        ]
                    }
                ]
            }
        )
    ]
))

# -----------------------
# Lambda Function for Database Creation
# -----------------------
Psycopg2Layer = t.add_resource(LayerVersion(
    "Psycopg2Layer",
    LayerName=Sub("${AWS::StackName}-psycopg2"),
    Description="psycopg2-binary for PostgreSQL connectivity",
    Content=Content(
        S3Bucket="aws-data-wrangler-public-artifacts",
        S3Key="releases/3.9.0/awswrangler-layer-3.9.0-py3.11-x86_64.zip"
    ),
    CompatibleRuntimes=["python3.11"],
    CompatibleArchitectures=["x86_64"]
))

DatabaseCreatorFunction = t.add_resource(Function(
    "DatabaseCreatorFunction",
    FunctionName=Sub("${AWS::StackName}-database-creator"),
    Runtime="python3.11",
    Handler="index.lambda_handler",
    Role=GetAtt(DatabaseLambdaRole, "Arn"),
    Timeout=300,
    Layers=[Ref(Psycopg2Layer)],
    Architectures=["x86_64"],
    VpcConfig=VPCConfig(
        SecurityGroupIds=[Ref(DbSecurityGroup)],
        SubnetIds=Ref(PrivateSubnets)
    ),
    Code=Code(
        ZipFile="""
import json
import boto3
import psycopg2
import cfnresponse

def lambda_handler(event, context):
    try:
        request_type = event['RequestType']
        properties = event['ResourceProperties']

        if request_type == 'Delete':
            cfnresponse.send(event, context, cfnresponse.SUCCESS, {})
            return

        # Get database connection info
        secrets_client = boto3.client('secretsmanager')
        secret_response = secrets_client.get_secret_value(SecretId=properties['SecretArn'])
        secret = json.loads(secret_response['SecretString'])

        # Connect to PostgreSQL
        conn = psycopg2.connect(
            host=properties['DbEndpoint'],
            port=properties['DbPort'],
            user=secret['username'],
            password=secret['password'],
            dbname='postgres'
        )
        conn.autocommit = True

        cursor = conn.cursor()

        # Create databases
        databases = properties['Databases'].split(',')
        for db_name in databases:
            db_name = db_name.strip()

            # Check if database exists
            cursor.execute("SELECT 1 FROM pg_database WHERE datname = %s", (db_name,))
            if not cursor.fetchone():
                cursor.execute(f'CREATE DATABASE "{db_name}"')
                print(f"Created database: {db_name}")
            else:
                print(f"Database already exists: {db_name}")

        cursor.close()
        conn.close()

        cfnresponse.send(event, context, cfnresponse.SUCCESS, {})

    except Exception as e:
        print(f"Error: {str(e)}")
        cfnresponse.send(event, context, cfnresponse.FAILED, {}, reason=str(e))
"""
    )
))

# -----------------------
# Custom Resource for Database Creation
# -----------------------
DatabaseCreator = t.add_resource(CustomResource(
    "DatabaseCreator",
    ServiceToken=GetAtt(DatabaseCreatorFunction, "Arn"),
    SecretArn=DbSecretArnValue,
    DbEndpoint=DbEndpoint,
    DbPort=DbPort,
    Databases="lrdb,configdb"
))

# -----------------------
# IAM Policy for Database Access (attach to appropriate role)
# -----------------------
t.add_resource(PolicyType(
    "SecretsManagerTaskPolicy",
    PolicyName="SecretsManagerAccess",
    Roles=[If(
        "UseExistingTaskRole",
        Select(1, Split("/", Ref(ExistingTaskRoleArn))),  # Extract role name from existing ARN
        Ref(DatabaseTaskRole)  # Use created role name directly
    )],
    PolicyDocument={
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": [
                    "secretsmanager:GetSecretValue"
                ],
                "Resource": [
                    DbSecretArnValue,
                    Sub("${DbSecretArn}*", DbSecretArn=DbSecretArnValue)
                ]
            }
        ]
    }
))

# -----------------------
# Outputs
# -----------------------
t.add_output(Output(
    "DbEndpoint",
    Description="Database endpoint",
    Value=DbEndpoint,
    Export=Export(name=Sub("${AWS::StackName}-DbEndpoint"))
))

t.add_output(Output(
    "DbPort",
    Description="Database port",
    Value=DbPort,
    Export=Export(name=Sub("${AWS::StackName}-DbPort"))
))

t.add_output(Output(
    "DbSecretArn",
    Description="Database secret ARN",
    Value=DbSecretArnValue,
    Export=Export(name=Sub("${AWS::StackName}-DbSecretArn"))
))

t.add_output(Output(
    "DatabaseSecurityGroupId",
    Description="Database security group ID",
    Value=Ref(DbSecurityGroup),
    Export=Export(name=Sub("${AWS::StackName}-DatabaseSecurityGroupId"))
))

t.add_output(Output(
    "TaskRoleArn",
    Description="Task role ARN for database access (created or existing)",
    Value=If(
        "UseExistingTaskRole",
        Ref(ExistingTaskRoleArn),
        GetAtt(DatabaseTaskRole, "Arn")
    ),
    Export=Export(name=Sub("${AWS::StackName}-TaskRoleArn"))
))

print(t.to_yaml())
