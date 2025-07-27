#!/usr/bin/env node
import * as cdk from 'aws-cdk-lib';
import { CommonInfraStack }    from '../lib/common-infra-stack';
import { FargateServiceStack } from '../lib/fargate-service-stack';
import { services }            from '../lib/configs';

interface AppContext {
  readonly vpcId:       string;
  readonly dbSecretName:string;
  readonly dbConfig: {
    host: string; port: string; name: string; user: string; sslmode: string;
  };
  readonly env: {
    account: string;
    region:  string;
  };
}

const app = new cdk.App();
const ctx = app.node.getContext('app') as AppContext;
if (!ctx) throw new Error('Context key "app" is required in cdk.json');

const { vpcId, dbSecretName, dbConfig, env } = ctx;

// 1) Instantiate your CommonInfraStack with the vpcId prop
const common = new CommonInfraStack(app, 'CommonInfra', {
  env,
  vpcId,      // ← now legal
});

// 2) Prepare your shared DB env block
const dbEnv = {
  LRDB_HOST:    dbConfig.host,
  LRDB_PORT:    dbConfig.port,
  LRDB_DBNAME:  dbConfig.name,
  LRDB_USER:    dbConfig.user,
  LRDB_SSLMODE: dbConfig.sslmode,
};

// 3) Spin up one stack per service
for (const svc of services) {
  new FargateServiceStack(app, svc.name, {
    env,
    cluster:      common.cluster,
    dbSecretName,
    dbEnv,
    service:      svc,
  });
}
