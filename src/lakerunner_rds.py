#!/usr/bin/env python3
"""RDS PostgreSQL stack for Lakerunner."""

from troposphere import Template, Parameter, Ref, Sub, GetAtt, Export, Output, Select, Split, If, Equals, Not
from troposphere.secretsmanager import Secret, GenerateSecretString
from troposphere.rds import DBInstance, DBSubnetGroup
from troposphere.iam import PolicyType, Role, Policy
from troposphere.ec2 import SecurityGroup, SecurityGroupRule


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
    DBName="lakerunner",
    DBInstanceClass="db.t3.medium",
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
