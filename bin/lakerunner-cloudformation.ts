#!/usr/bin/env node
import * as cdk from 'aws-cdk-lib';
import { CommonInfraStack } from '../lib/common-infra-stack';
import { FargateServiceStack } from '../lib/fargate-service-stack';
import { services } from '../lib/configs';
import { MigrationStack } from '../lib/migration-stack';

interface AppContext {
  readonly vpcId: string;
  readonly dbSecretName: string;
  readonly dbConfig: {
    username: string;
    name: string;
    port?: string;
    sslmode?: string;
  };
  readonly env: {
    account: string;
    region: string;
  };
}

const app = new cdk.App();
const ctx = app.node.getContext('app') as AppContext;
if (!ctx) throw new Error('Context key "app" missing in cdk.json');

const { vpcId, dbSecretName, dbConfig, env } = ctx;

const common = new CommonInfraStack(app, 'CommonInfra', {
  env,
  vpcId,
  dbSecretName,
  dbConfig,
});

const dbEnv = {
  LRDB_HOST: common.dbInstance.dbInstanceEndpointAddress,
  LRDB_PORT: common.dbInstance.dbInstanceEndpointPort,
  LRDB_DBNAME: ctx.dbConfig.name,
  LRDB_USER: ctx.dbConfig.username,
};

new MigrationStack(app, 'MigrationStack', {
  env: ctx.env,
  cluster: common.cluster,
  taskRole: common.taskRole,
  dbEnv: {
    LRDB_HOST: common.dbInstance.dbInstanceEndpointAddress,
    LRDB_PORT: common.dbInstance.dbInstanceEndpointPort,
    LRDB_DBNAME: ctx.dbConfig.name,
    LRDB_USER: ctx.dbConfig.username,
  },
  dbSecretArn: common.dbSecret.secretArn,
  vpcSubnets: common.vpc.selectSubnets({ subnetType: cdk.aws_ec2.SubnetType.PRIVATE_WITH_EGRESS }).subnets.map(s => s.subnetId),
  securityGroups: [common.taskSecurityGroup.securityGroupId],
  dbSecret: common.dbSecret,
});

for (const svc of services) {
  new FargateServiceStack(app, svc.name, {
    env,
    cluster: common.cluster,
    taskSecurityGroup: common.taskSecurityGroup,
    dbEnv,
    dbSecret: common.dbSecret,
    service: svc,
    storageProfilesParam: common.storageProfilesParam,
    apiKeysParam: common.apiKeysParam,
    queue: common.queue,
    vpcId: common.vpc.vpcId,
  });
}
