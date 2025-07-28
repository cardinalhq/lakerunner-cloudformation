// lib/migration-stack.ts
import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as ecs from 'aws-cdk-lib/aws-ecs';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as secretsmanager from 'aws-cdk-lib/aws-secretsmanager';

export class MigrationStack extends cdk.Stack {
  public readonly taskDefinition: ecs.FargateTaskDefinition;
  constructor(scope: Construct, id: string, props: cdk.StackProps & {
    cluster: ecs.Cluster;
    taskRole: iam.IRole;
    dbEnv: Record<string, string>;
    dbSecretArn: string;
    vpcSubnets: string[];
    securityGroups: string[];
    dbSecret: secretsmanager.ISecret;
  }) {
    super(scope, id, props);

    // 1) Migration TaskDef (no Service; we’ll run it on‑demand)
    this.taskDefinition = new ecs.FargateTaskDefinition(this, 'MigrationTaskDef', {
      cpu: 512,
      memoryLimitMiB: 1024,
      taskRole: props.taskRole,
      family: 'lakerunner-migration',
    });

    const migrator = this.taskDefinition.addContainer('Migrator', {
      image: ecs.ContainerImage.fromRegistry('public.ecr.aws/cardinalhq.io/lakerunner:latest'),
      command: ['/app/bin/lakerunner', 'migrate'],
      logging: ecs.LogDrivers.awsLogs({
        logGroup: new logs.LogGroup(this, 'MigrationLogGroup', { removalPolicy: cdk.RemovalPolicy.DESTROY }),
        streamPrefix: 'migration',
      }),
      environment: props.dbEnv,
      secrets: {
        LRDB_PASSWORD: ecs.Secret.fromSecretsManager(props.dbSecret, 'password'),
      },
    });
  }
}
