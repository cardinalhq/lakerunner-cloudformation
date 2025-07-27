import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as ecs from 'aws-cdk-lib/aws-ecs';
import * as secretsmanager from 'aws-cdk-lib/aws-secretsmanager';
import * as logs from 'aws-cdk-lib/aws-logs';

export interface IngestLogsStackProps extends cdk.StackProps {
  readonly dbConfig: {
    host: string;
    port: string;
    name: string;
    user: string;
    sslmode: string;
  };
}

export class IngestLogsStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: IngestLogsStackProps) {
    super(scope, id, props);

    const vpc = ec2.Vpc.fromLookup(this, 'Vpc', { isDefault: true });
    const cluster = new ecs.Cluster(this, 'Cluster', { vpc });

    const dbPassword = secretsmanager.Secret.fromSecretNameV2(
      this, 'DBPassword', 'lakerunner-pg-password'
    );

    const dbEnv = {
      LRDB_HOST:    props.dbConfig.host,
      LRDB_PORT:    props.dbConfig.port,
      LRDB_DBNAME:  props.dbConfig.name,
      LRDB_USER:    props.dbConfig.user,
      LRDB_SSLMODE: props.dbConfig.sslmode,
    };

    const taskDef = new ecs.FargateTaskDefinition(this, 'TaskDef', {
      cpu:             512,       // ~0.5 vCPU
      memoryLimitMiB:  1024,      // 1 GiB RAM
    });

    const logGroup = new logs.LogGroup(this, 'LogGroup', {
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    const container = taskDef.addContainer('IngestLogsContainer', {
      image: ecs.ContainerImage.fromRegistry(
        'public.ecr.aws/cardinalhq.io/lakerunner:latest'
      ),
      command: ['/app/bin/lakerunner','ingest-logs'],
      logging: ecs.LogDrivers.awsLogs({
        logGroup, streamPrefix: 'ingest-logs'
      }),
      environment: {
        OTEL_SERVICE_NAME:     'lakerunner-ingest-logs',
        TMPDIR:                '/mnt',
        STORAGE_PROFILE_FILE:  '/app/config/storage_profiles.yaml',
        ...dbEnv,
      },
      secrets: {
        LRDB_PASSWORD: ecs.Secret.fromSecretsManager(dbPassword),
      },
    });

    new ecs.FargateService(this, 'Service', {
      cluster,
      taskDefinition: taskDef,
      desiredCount: 1,
      assignPublicIp: false,
      // securityGroups, vpcSubnets, etc. customize as needed
    });
  }
}
