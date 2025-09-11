#!/usr/bin/env python3
"""RDS PostgreSQL stack for Lakerunner."""

from troposphere import Template, Parameter, Ref, Sub, GetAtt, Export, Output
from troposphere.secretsmanager import Secret, GenerateSecretString
from troposphere.rds import DBInstance, DBSubnetGroup


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

TaskSecurityGroupId = t.add_parameter(Parameter(
    "TaskSecurityGroupId",
    Type="AWS::EC2::SecurityGroup::Id",
    Description="REQUIRED: Security group used by ECS tasks (allows DB access)",
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
    VPCSecurityGroups=[Ref(TaskSecurityGroupId)],
    DBSubnetGroupName=Ref(DbSubnets),
    MasterUsername=Sub("{{resolve:secretsmanager:${S}:SecretString:username}}", S=DbSecretArnValue),
    MasterUserPassword=Sub("{{resolve:secretsmanager:${S}:SecretString:password}}", S=DbSecretArnValue),
    DeletionProtection=False,
))

DbEndpoint = GetAtt(DbRes, "Endpoint.Address")
DbPort = GetAtt(DbRes, "Endpoint.Port")

# -----------------------
# Outputs
# -----------------------
t.add_output(Output("DbEndpoint", Value=DbEndpoint, Export=Export(name=Sub("${AWS::StackName}-DbEndpoint"))))
t.add_output(Output("DbPort", Value=DbPort, Export=Export(name=Sub("${AWS::StackName}-DbPort"))))
t.add_output(Output("DbSecretArn", Value=DbSecretArnValue, Export=Export(name=Sub("${AWS::StackName}-DbSecretArn"))))

print(t.to_yaml())
