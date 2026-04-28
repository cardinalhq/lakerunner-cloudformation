"""database.yaml nested stack: RDS subnet group, RDS instance, DB master secret."""

from troposphere import (
    Template,
    Parameter,
    Ref,
    GetAtt,
    Output,
    Split,
    Sub,
)
from troposphere.rds import DBInstance, DBSubnetGroup
from troposphere.secretsmanager import GenerateSecretString, Secret

from cardinal_cfn.naming import cardinal_tags
from cardinal_cfn.parameters import add_install_id_parameters
from cardinal_cfn.policies import apply_policy


def build() -> Template:
    t = Template()
    t.set_description("Cardinal database: RDS instance and master secret.")

    add_install_id_parameters(t)
    t.add_parameter(
        Parameter(
            "PrivateSubnetsCsv",
            Type="String",
            Description="Comma-separated list of private subnet IDs.",
        )
    )
    t.add_parameter(
        Parameter(
            "DbInstanceClass",
            Type="String",
            Default="db.t3.medium",
            Description="RDS DB instance class.",
        )
    )
    t.add_parameter(
        Parameter(
            "DbAllocatedStorage",
            Type="Number",
            Default=20,
            MinValue=20,
            Description="RDS allocated storage in GiB.",
        )
    )
    t.add_parameter(
        Parameter(
            "DbEngineVersion",
            Type="String",
            Default="17",
            Description="PostgreSQL engine version.",
        )
    )

    db_secret = t.add_resource(
        Secret(
            "DbMasterSecret",
            GenerateSecretString=GenerateSecretString(
                SecretStringTemplate='{"username":"lakerunner"}',
                GenerateStringKey="password",
                ExcludePunctuation=True,
            ),
            Tags=cardinal_tags(component="database", role="db-master"),
        )
    )
    apply_policy(db_secret, "db-master-secret")

    subnet_group = t.add_resource(
        DBSubnetGroup(
            "DbSubnetGroup",
            DBSubnetGroupDescription="Cardinal DB subnet group",
            SubnetIds=Split(",", Ref("PrivateSubnetsCsv")),
            Tags=cardinal_tags(component="database", role="db-subnet-group"),
        )
    )

    db = t.add_resource(
        DBInstance(
            "Db",
            Engine="postgres",
            EngineVersion=Ref("DbEngineVersion"),
            DBName="lakerunner",
            DBInstanceClass=Ref("DbInstanceClass"),
            AllocatedStorage=Ref("DbAllocatedStorage"),
            PubliclyAccessible=False,
            DBSubnetGroupName=Ref(subnet_group),
            MasterUsername="lakerunner",
            # CFN dynamic reference; ${DbMasterSecret} is substituted by Fn::Sub
            # at deploy time, then CFN resolves the secretsmanager value.
            MasterUserPassword=Sub(
                "{{resolve:secretsmanager:${DbMasterSecret}::password}}"
            ),
            StorageEncrypted=True,
            Tags=cardinal_tags(component="database", role="rds-instance"),
            # NOTE: explicitly do NOT set DBInstanceIdentifier (per spec)
        )
    )
    apply_policy(db, "rds-instance")

    t.add_output(Output("DbEndpoint", Value=GetAtt(db, "Endpoint.Address")))
    t.add_output(Output("DbPort", Value=GetAtt(db, "Endpoint.Port")))
    t.add_output(Output("DbSecretArn", Value=Ref(db_secret)))
    t.add_output(Output("DbName", Value="lakerunner"))

    return t


if __name__ == "__main__":
    print(build().to_yaml())
