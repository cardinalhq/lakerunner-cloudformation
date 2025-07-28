import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as ecs from 'aws-cdk-lib/aws-ecs';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as secretsmanager from 'aws-cdk-lib/aws-secretsmanager';
import * as logs from 'aws-cdk-lib/aws-logs';
import { ServiceConfig } from './configs';

export interface FargateServiceStackProps extends cdk.StackProps {
  readonly cluster: ecs.Cluster;
  readonly taskRole: iam.IRole;
  readonly dbEnv: Record<string, string>;
  readonly dbSecret: secretsmanager.ISecret;
  readonly service: ServiceConfig;
  readonly storageProfilesParam: cdk.aws_ssm.IStringParameter;
}

export class FargateServiceStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: FargateServiceStackProps) {
    super(scope, id, props);

    const taskDef = new ecs.FargateTaskDefinition(this, 'TaskDef', {
      cpu: props.service.cpu ?? 512,
      memoryLimitMiB: props.service.memoryMiB ?? 1024,
      taskRole: props.taskRole,
    });

    const logGroup = new logs.LogGroup(this, 'LogGroup', {
      logGroupName: `/ecs/${props.service.name}`,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    const container = taskDef.addContainer('AppContainer', {
      image: ecs.ContainerImage.fromRegistry(props.service.image),
      command: [
        '/bin/sh', '-c',
        [
          // echo the YAML into the file
          'echo "$STORAGE_PROFILES" > /app/config/storage_profiles.yaml',
          // then launch your app
          'exec ' + props.service.command.join(' '),
        ].join(' && ')
      ],
      logging: ecs.LogDrivers.awsLogs({ logGroup, streamPrefix: props.service.name }),
      environment: {
        OTEL_SERVICE_NAME: props.service.name,
        TMPDIR: '/mnt',
        STORAGE_PROFILE_FILE: '/app/config/storage_profiles.yaml',
        ...props.dbEnv,        // LRDB_HOST, LRDB_PORT, LRDB_DBNAME, LRDB_USER
      },
      secrets: {
        STORAGE_PROFILES: ecs.Secret.fromSsmParameter(props.storageProfilesParam),
        LRDB_PASSWORD: ecs.Secret.fromSecretsManager(props.dbSecret),
      },
    });

    new ecs.FargateService(this, 'Service', {
      cluster: props.cluster,
      taskDefinition: taskDef,
      desiredCount: props.service.replicas ?? 1,
      minHealthyPercent: 100,
      maxHealthyPercent: 200,
    });
  }
}
