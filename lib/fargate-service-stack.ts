import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as ecs from 'aws-cdk-lib/aws-ecs';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as secretsmanager from 'aws-cdk-lib/aws-secretsmanager';
import * as logs from 'aws-cdk-lib/aws-logs';
import { ServiceConfig } from './configs';
import * as sqs from 'aws-cdk-lib/aws-sqs';
import * as ec2 from 'aws-cdk-lib/aws-ec2';

export interface FargateServiceStackProps extends cdk.StackProps {
  readonly cluster: ecs.Cluster;
  readonly taskRole: iam.IRole;
  readonly dbEnv: Record<string, string>;
  readonly dbSecret: secretsmanager.ISecret;
  readonly service: ServiceConfig;
  readonly storageProfilesParam: cdk.aws_ssm.IStringParameter;
  readonly apiKeysParam: cdk.aws_ssm.IStringParameter;
  readonly queue: sqs.IQueue;
  readonly taskSecurityGroup: ec2.ISecurityGroup;
}

export class FargateServiceStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: FargateServiceStackProps) {
    super(scope, id, props);

    const taskDef = new ecs.FargateTaskDefinition(this, 'TaskDef', {
      cpu: props.service.cpu ?? 512,
      memoryLimitMiB: props.service.memoryMiB ?? 1024,
      taskRole: props.taskRole,
      family: props.service.name + '-task',
    });

    const logGroup = new logs.LogGroup(this, 'LogGroup', {
      logGroupName: `/ecs/${props.service.name}`,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    const container = taskDef.addContainer('AppContainer', {
      image: ecs.ContainerImage.fromRegistry(props.service.image),
      command: props.service.command,
      healthCheck: props.service.healthCheck,
      logging: ecs.LogDrivers.awsLogs({ logGroup, streamPrefix: props.service.name }),
      environment: {
        OTEL_SERVICE_NAME: props.service.name,
        TMPDIR: '/mnt',
        STORAGE_PROFILE_FILE: 'env:STORAGE_PROFILES_ENV',
        API_KEYS_FILE: 'env:API_KEYS_ENV',
        SQS_QUEUE_URL: props.queue.queueUrl,
        SQS_REGION: this.region,
        ...props.dbEnv,
        ...props.service.environment,
      },
      secrets: {
        STORAGE_PROFILES_ENV: ecs.Secret.fromSsmParameter(props.storageProfilesParam),
        API_KEYS_ENV: ecs.Secret.fromSsmParameter(props.apiKeysParam),
        LRDB_PASSWORD: ecs.Secret.fromSecretsManager(props.dbSecret, 'password'),
      },
    });

    new ecs.FargateService(this, 'Service', {
      cluster: props.cluster,
      securityGroups: [props.taskSecurityGroup],
      taskDefinition: taskDef,
      desiredCount: props.service.replicas ?? 1,
      minHealthyPercent: 100,
      maxHealthyPercent: 200,
      serviceName: props.service.name,
    });
  }
}
