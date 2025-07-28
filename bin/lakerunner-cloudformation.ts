#!/usr/bin/env node
import * as cdk from 'aws-cdk-lib';
import { CommonInfraStack }    from '../lib/common-infra-stack';
import { FargateServiceStack } from '../lib/fargate-service-stack';
import { services }            from '../lib/configs';

interface AppContext {
  readonly vpcId:        string;
  readonly dbSecretName: string;
  readonly dbConfig: {
    username: string;
    name:     string;
    port?:    string;
    sslmode?: string;
  };
  readonly env: {
    account: string;
    region:  string;
  };
}

const app = new cdk.App();
const ctx = app.node.getContext('app') as AppContext;
if (!ctx) throw new Error('Context key "app" missing in cdk.json');

const { vpcId, dbSecretName, dbConfig, env } = ctx;

// 1) Deploy infra: VPC, Cluster, S3, SQS, RDS + Secret, IAM Role
const common = new CommonInfraStack(app, 'CommonInfra', {
  env,
  vpcId,
  dbSecretName,
  dbConfig,
});

// 2) Build a little map of LRDB_* vars
const dbEnv = {
  LRDB_HOST:   common.dbInstance.dbInstanceEndpointAddress,
  LRDB_PORT:   common.dbInstance.dbInstanceEndpointPort,
  LRDB_DBNAME: 'metadata',
  LRDB_USER:   'lakerunner',
};

// 3) Deploy all services
for (const svc of services) {
  new FargateServiceStack(app, svc.name, {
    env,
    cluster:   common.cluster,
    taskSecurityGroup:   common.taskSecurityGroup,
    taskRole:  common.taskRole,
    dbEnv,
    dbSecret:  common.dbSecret,
    service:   svc,
    storageProfilesParam: common.storageProfilesParam,
    queue:   common.queue,
  });
}
