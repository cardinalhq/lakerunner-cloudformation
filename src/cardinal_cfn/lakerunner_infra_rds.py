"""cardinal-lakerunner-infra-rds: Aurora PostgreSQL cluster for a Lakerunner install.

Standalone stack that creates the RDS security group (with per-tier 5432
ingress from task SGs supplied as parameters), subnet group, master-credential
secret, an Aurora PostgreSQL cluster with a single writer instance, and the
secret-target attachment.

Deploy order: lakerunner-infra-base (task SGs) → this stack → lakerunner stack.
The task security group IDs are outputs of lakerunner-infra-base and are
threaded in as parameters here.
"""

from troposphere import (
    GetAtt,
    Output,
    Parameter,
    Ref,
    Split,
    Sub,
    Tags,
    Template,
)
from troposphere.ec2 import SecurityGroup, SecurityGroupIngress
from troposphere.rds import DBCluster, DBInstance, DBSubnetGroup
from troposphere.secretsmanager import (
    GenerateSecretString,
    Secret,
    SecretTargetAttachment,
)

from cardinal_cfn.parameters import add_parameter_group_metadata

APPLICATION = "cardinal-lakerunner"
PROJECT = "cardinal"
MANAGED_BY = "cardinal-cfn-rds"


def _tags(*, component: str) -> Tags:
    return Tags(
        Application=APPLICATION,
        Project=PROJECT,
        ManagedBy=MANAGED_BY,
        Component=component,
        Name=f"cardinal-{component}",
    )


def _delete(resource):
    resource.DeletionPolicy = "Delete"
    resource.UpdateReplacePolicy = "Delete"
    return resource


def _retain(resource):
    resource.DeletionPolicy = "Retain"
    resource.UpdateReplacePolicy = "Retain"
    return resource


def _snapshot(resource):
    resource.DeletionPolicy = "Snapshot"
    resource.UpdateReplacePolicy = "Snapshot"
    return resource


def build() -> Template:
    t = Template()
    t.set_description(
        "Cardinal lakerunner infra RDS: Aurora PostgreSQL cluster, security "
        "group, subnet group, and master-credentials secret for a Lakerunner "
        "install. Deploy after lakerunner-infra-base (task SGs are inputs)."
    )

    # -----------------------------------------------------------------------
    # Parameters
    # -----------------------------------------------------------------------

    t.add_parameter(
        Parameter(
            "VpcId",
            Type="AWS::EC2::VPC::Id",
            Description=(
                "VPC the RDS instance and its security group live in. "
                "Same VPC the lakerunner stack is deployed into."
            ),
        )
    )
    t.add_parameter(
        Parameter(
            "PrivateSubnetsCsv",
            Type="String",
            Description=(
                "Comma-separated list of two or more private subnet IDs in "
                "distinct AZs for the RDS subnet group."
            ),
        )
    )
    t.add_parameter(
        Parameter(
            "DBEngineVersion",
            Type="String",
            Default="17.9",
            Description="Aurora PostgreSQL engine version for the cluster.",
        )
    )
    t.add_parameter(
        Parameter(
            "DBInstanceClass",
            Type="String",
            Default="db.r8g.large",
            Description="Aurora instance class for the writer.",
        )
    )

    # DB-client tier security group IDs (outputs of lakerunner-infra-base).
    # otel is intentionally excluded — it has no DB dependency.
    for param_name in (
        "MigrationSecurityGroupId",
        "QuerySecurityGroupId",
        "ProcessSecurityGroupId",
        "ControlSecurityGroupId",
        "MaestroSecurityGroupId",
    ):
        t.add_parameter(
            Parameter(
                param_name,
                Type="AWS::EC2::SecurityGroup::Id",
                Description=(
                    f"Security group ID for the {param_name.replace('SecurityGroupId', '').lower()} "
                    "task tier (output of lakerunner-infra-base)."
                ),
            )
        )

    add_parameter_group_metadata(
        t,
        groups=[
            {
                "label": "Networking",
                "parameters": [
                    "VpcId",
                    "PrivateSubnetsCsv",
                ],
            },
            {
                "label": "DB Sizing",
                "parameters": [
                    "DBEngineVersion",
                    "DBInstanceClass",
                ],
            },
            {
                "label": "DB Clients",
                "parameters": [
                    "MigrationSecurityGroupId",
                    "QuerySecurityGroupId",
                    "ProcessSecurityGroupId",
                    "ControlSecurityGroupId",
                    "MaestroSecurityGroupId",
                ],
            },
        ],
    )

    # -----------------------------------------------------------------------
    # RDS security group
    # -----------------------------------------------------------------------

    rds_sg = t.add_resource(
        _delete(
            SecurityGroup(
                "RdsSecurityGroup",
                GroupDescription=(
                    "Cardinal lakerunner RDS; ingress added per DB-client tier"
                ),
                VpcId=Ref("VpcId"),
                SecurityGroupEgress=[
                    {
                        "IpProtocol": "-1",
                        "CidrIp": "0.0.0.0/0",
                        "Description": (
                            "All egress (RDS does not initiate connections; "
                            "default kept for AWS::EC2::SecurityGroup parity)."
                        ),
                    }
                ],
                Tags=_tags(component="rds-sg"),
            )
        )
    )

    # -----------------------------------------------------------------------
    # Per-tier 5432 ingress rules (otel excluded — no DB dependency)
    # -----------------------------------------------------------------------

    for tier_title, param_name in [
        ("Migration", "MigrationSecurityGroupId"),
        ("Query", "QuerySecurityGroupId"),
        ("Process", "ProcessSecurityGroupId"),
        ("Control", "ControlSecurityGroupId"),
        ("Maestro", "MaestroSecurityGroupId"),
    ]:
        t.add_resource(
            SecurityGroupIngress(
                f"Rds5432From{tier_title}",
                GroupId=Ref(rds_sg),
                SourceSecurityGroupId=Ref(param_name),
                IpProtocol="tcp",
                FromPort=5432,
                ToPort=5432,
                Description=f"{tier_title} to RDS 5432",
            )
        )

    # -----------------------------------------------------------------------
    # Subnet group
    # -----------------------------------------------------------------------

    db_subnet_group = t.add_resource(
        _delete(
            DBSubnetGroup(
                "DBSubnetGroup",
                DBSubnetGroupDescription="Cardinal lakerunner DB subnet group",
                SubnetIds=Split(",", Ref("PrivateSubnetsCsv")),
                Tags=_tags(component="db-subnet-group"),
            )
        )
    )

    # -----------------------------------------------------------------------
    # Master secret
    # -----------------------------------------------------------------------

    db_master_secret = t.add_resource(
        _retain(
            Secret(
                "DBMasterSecret",
                # Explicit name so lakerunner-infra-base's execution/task roles
                # can scope secret access to the cardinal-* name pattern instead
                # of threading this ARN in (base deploys before rds).
                Name="cardinal-db-master",
                Description=(
                    "Cardinal RDS master credentials. Connection JSON "
                    "(host/port/engine/dbname) is filled in by the "
                    "associated SecretTargetAttachment."
                ),
                GenerateSecretString=GenerateSecretString(
                    SecretStringTemplate='{"username":"lakerunner"}',
                    GenerateStringKey="password",
                    PasswordLength=40,
                    ExcludePunctuation=True,
                ),
                Tags=_tags(component="db-master"),
            )
        )
    )

    # -----------------------------------------------------------------------
    # Aurora PostgreSQL cluster + writer instance
    # -----------------------------------------------------------------------

    # The cluster holds the data and every cluster-level setting (engine,
    # storage, credentials, networking).  DeletionProtection=False is
    # intentional: trial teardown relies on being able to delete the stack; the
    # Snapshot DeletionPolicy preserves data.  DeletionProtection=True would
    # block stack deletion entirely.
    db_cluster = t.add_resource(
        _snapshot(
            DBCluster(
                "DBCluster",
                Engine="aurora-postgresql",
                EngineVersion=Ref("DBEngineVersion"),
                DatabaseName="lakerunner",
                Port=5432,
                MasterUsername="lakerunner",
                MasterUserPassword=Sub(
                    "{{resolve:secretsmanager:${SecretArn}::password}}",
                    SecretArn=Ref(db_master_secret),
                ),
                DBSubnetGroupName=Ref(db_subnet_group),
                VpcSecurityGroupIds=[Ref(rds_sg)],
                StorageEncrypted=True,
                BackupRetentionPeriod=7,
                DeletionProtection=False,
                Tags=_tags(component="db"),
            )
        )
    )

    # The writer is stateless compute -- it inherits the subnet group and
    # security groups from the cluster, so they are not repeated here.  Policy
    # Delete: the cluster (Snapshot) owns the data.
    t.add_resource(
        _delete(
            DBInstance(
                "DBInstance",
                Engine="aurora-postgresql",
                DBInstanceClass=Ref("DBInstanceClass"),
                DBClusterIdentifier=Ref(db_cluster),
                PubliclyAccessible=False,
                Tags=_tags(component="db"),
            )
        )
    )

    # SecretTargetAttachment rewrites the secret to embed
    # {engine, host, port, dbname} alongside username/password -- matching
    # the connection JSON the lakerunner task containers consume.  Targets the
    # cluster, so host resolves to the cluster writer endpoint.
    t.add_resource(
        SecretTargetAttachment(
            "DBMasterSecretAttachment",
            SecretId=Ref(db_master_secret),
            TargetId=Ref(db_cluster),
            TargetType="AWS::RDS::DBCluster",
        )
    )

    # -----------------------------------------------------------------------
    # Outputs
    # -----------------------------------------------------------------------

    t.add_output(
        Output(
            "DbEndpoint",
            Description="Aurora cluster writer endpoint address.",
            Value=GetAtt(db_cluster, "Endpoint.Address"),
        )
    )
    t.add_output(
        Output(
            "DbPort",
            Description="Aurora cluster endpoint port.",
            Value=GetAtt(db_cluster, "Endpoint.Port"),
        )
    )
    t.add_output(
        Output(
            "DbName",
            Description="Database name.",
            Value="lakerunner",
        )
    )
    t.add_output(
        Output(
            "DbMasterSecretArn",
            Description="ARN of the RDS master-credentials secret.",
            Value=Ref(db_master_secret),
        )
    )
    t.add_output(
        Output(
            "RdsSecurityGroupId",
            Description="Security group ID of the RDS instance.",
            Value=Ref(rds_sg),
        )
    )

    return t


if __name__ == "__main__":
    print(build().to_yaml(), end="")
