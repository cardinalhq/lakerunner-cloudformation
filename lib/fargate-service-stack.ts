import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as ecs from 'aws-cdk-lib/aws-ecs';
import * as secretsmanager from 'aws-cdk-lib/aws-secretsmanager';
import * as logs from 'aws-cdk-lib/aws-logs';
import { ServiceConfig } from './configs';

export interface FargateServiceStackProps extends cdk.StackProps {
  readonly cluster: ecs.Cluster;
  readonly dbSecretName: string;
  readonly dbEnv: Record<string, string>;
  readonly service: ServiceConfig;
  readonly replicas?: number; // Optional, default is 1
}

export class FargateServiceStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: FargateServiceStackProps) {
    super(scope, id, props);

    const dbPassword = secretsmanager.Secret.fromSecretNameV2(this, 'DbPass', props.dbSecretName);

    const logGroup = new logs.LogGroup(this, 'LogGroup', {
      logGroupName: `/ecs/${props.service.name}`,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    const taskDef = new ecs.FargateTaskDefinition(this, 'TaskDef', {
      cpu: props.service.cpu ?? 512,
      memoryLimitMiB: props.service.memoryMiB ?? 1024,
    });

    const container = taskDef.addContainer('AppContainer', {
      image: ecs.ContainerImage.fromRegistry(props.service.image),
      command: props.service.command,
      logging: ecs.LogDrivers.awsLogs({
        logGroup,
        streamPrefix: props.service.name,
      }),
      environment: {
        OTEL_SERVICE_NAME: props.service.name,
        TMPDIR: '/mnt',
        STORAGE_PROFILE_FILE: '/app/config/storage_profiles.yaml',
        ...props.dbEnv,
      },
      secrets: {
        LRDB_PASSWORD: ecs.Secret.fromSecretsManager(dbPassword),
      },
    });

    // // optional scratch volume
    // taskDef.addVolume({ name: 'scratch', ephemeral: {} });
    // container.addMountPoints({
    //   containerPath: '/scratch',
    //   sourceVolume:  'scratch',
    //   readOnly:      false,
    // });

    // service
    new ecs.FargateService(this, 'Service', {
      cluster: props.cluster,
      taskDefinition: taskDef,
      desiredCount: props.replicas ?? 1,
      minHealthyPercent: 100,
      maxHealthyPercent: 200,
    });
  }
}
