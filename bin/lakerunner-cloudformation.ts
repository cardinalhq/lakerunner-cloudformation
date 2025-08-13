#!/usr/bin/env node
import * as cdk from 'aws-cdk-lib';
import { CommonInfraStack } from '../lib/common-infra-stack';
import { FargateServiceStack } from '../lib/fargate-service-stack';
import { services } from '../lib/configs';
import { MigrationStack } from '../lib/migration-stack';

const app = new cdk.App();

const vpcId = new cdk.CfnParameter(app, 'VpcId', {
  type: 'AWS::EC2::VPC::Id',
  description: 'ID of the VPC to deploy resources into',
});

const privateSubnetIds = new cdk.CfnParameter(app, 'PrivateSubnetIds', {
  type: 'List<AWS::EC2::Subnet::Id>',
  description: 'Private subnet IDs for workloads',
});

const publicSubnetIds = new cdk.CfnParameter(app, 'PublicSubnetIds', {
  type: 'List<AWS::EC2::Subnet::Id>',
  description: 'Public subnet IDs for load balancers',
});

const dbSecretName = new cdk.CfnParameter(app, 'DbSecretName', {
  type: 'String',
  default: 'lakerunner-pg-password',
});

const dbConfig = {
  username: 'lakerunner',
  name: 'metadata',
  port: '5432',
  sslmode: 'require',
};

const common = new CommonInfraStack(app, 'CommonInfra', {
  vpcId: vpcId.valueAsString,
  privateSubnetIds: privateSubnetIds.valueAsList,
  publicSubnetIds: publicSubnetIds.valueAsList,
  dbSecretName: dbSecretName.valueAsString,
  dbConfig,
});

const dbEnv = {
  LRDB_HOST: common.dbInstance.dbInstanceEndpointAddress,
  LRDB_PORT: common.dbInstance.dbInstanceEndpointPort,
  LRDB_DBNAME: dbConfig.name,
  LRDB_USER: dbConfig.username,
};

new MigrationStack(app, 'MigrationStack', {
  cluster: common.cluster,
  taskRole: common.taskRole,
  dbEnv,
  dbSecretArn: common.dbSecret.secretArn,
  vpcSubnets: common.vpc
    .selectSubnets({ subnetType: cdk.aws_ec2.SubnetType.PRIVATE_WITH_EGRESS })
    .subnets.map(s => s.subnetId),
  securityGroups: [common.taskSecurityGroup.securityGroupId],
  dbSecret: common.dbSecret,
});

for (const svc of services) {
  new FargateServiceStack(app, svc.name, {
    cluster: common.cluster,
    taskSecurityGroup: common.taskSecurityGroup,
    dbEnv,
    dbSecret: common.dbSecret,
    service: svc,
    storageProfilesParam: common.storageProfilesParam,
    apiKeysParam: common.apiKeysParam,
    queue: common.queue,
    alb: common.alb,
  });
}
