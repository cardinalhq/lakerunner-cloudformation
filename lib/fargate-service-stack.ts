import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as ecs from 'aws-cdk-lib/aws-ecs';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as secretsmanager from 'aws-cdk-lib/aws-secretsmanager';
import * as logs from 'aws-cdk-lib/aws-logs';
import { ServiceConfig } from './configs';
import * as sqs from 'aws-cdk-lib/aws-sqs';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as elbv2 from 'aws-cdk-lib/aws-elasticloadbalancingv2';

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
  readonly vpcId: string;
  readonly alb?: elbv2.ApplicationLoadBalancer;
}

export class FargateServiceStack extends cdk.Stack {
  private readonly vpc: ec2.IVpc;

  constructor(scope: Construct, id: string, props: FargateServiceStackProps) {
    super(scope, id, props);

    this.vpc = ec2.Vpc.fromLookup(this, 'Vpc', { vpcId: props.vpcId });

    const taskDef = new ecs.FargateTaskDefinition(this, 'TaskDef', {
      cpu: props.service.cpu ?? 512,
      memoryLimitMiB: props.service.memoryMiB ?? 1024,
      taskRole: props.taskRole,
      family: props.service.name + '-task',
      volumes: [{ name: 'scratch', }],
    });

    taskDef.addToExecutionRolePolicy(new iam.PolicyStatement({
      actions: [
        'ssmmessages:CreateControlChannel',
        'ssmmessages:CreateDataChannel',
        'ssmmessages:OpenControlChannel',
        'ssmmessages:OpenDataChannel',
      ],
      resources: ['*'],
    }));

    const logGroup = new logs.LogGroup(this, 'LogGroup', {
      logGroupName: `/ecs/${props.service.name}`,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    const container = taskDef.addContainer('AppContainer', {
      image: ecs.ContainerImage.fromRegistry(props.service.image),
      command: props.service.command,
      healthCheck: props.service.healthCheck,
      logging: ecs.LogDrivers.awsLogs({ logGroup, streamPrefix: props.service.name }),
      user: '0', // run as root to allow bind mounts to work
      environment: {
        OTEL_SERVICE_NAME: props.service.name,
        TMPDIR: '/scratch',
        HOME: '/scratch',
        STORAGE_PROFILE_FILE: 'env:STORAGE_PROFILES_ENV',
        API_KEYS_FILE: 'env:API_KEYS_ENV',
        SQS_QUEUE_URL: props.queue.queueUrl,
        SQS_REGION: this.region,
        ECS_WORKER_CLUSTER_NAME: props.cluster.clusterName,
        ECS_WORKER_SERVICE_NAME: 'lakerunner-query-worker', // TODO make configurable
        ...props.dbEnv,
        ...props.service.environment,
      },
      secrets: {
        STORAGE_PROFILES_ENV: ecs.Secret.fromSsmParameter(props.storageProfilesParam),
        API_KEYS_ENV: ecs.Secret.fromSsmParameter(props.apiKeysParam),
        LRDB_PASSWORD: ecs.Secret.fromSecretsManager(props.dbSecret, 'password'),
      },
    });

    container.addMountPoints({
      containerPath: '/scratch',
      readOnly: false,
      sourceVolume: 'scratch',
    });

    if (props.service.bindMounts) {
      for (const mount of props.service.bindMounts) {
        container.addMountPoints(mount);
      }
    }

    const ecsService = new ecs.FargateService(this, 'Service', {
      cluster: props.cluster,
      securityGroups: [props.taskSecurityGroup],
      taskDefinition: taskDef,
      desiredCount: props.service.replicas ?? 1,
      minHealthyPercent: 100,
      maxHealthyPercent: 200,
      serviceName: props.service.name,
      enableExecuteCommand: true,
    });

    if (props.service.ingress) {
      const { port, desc } = props.service.ingress;

      ecsService.connections.allowInternally(
        ec2.Port.tcp(port),
        desc ?? `ingress for ${props.service.name}:${port}`,
      );

      container.addPortMappings({ containerPort: port });

      if (props.service.ingress.attachAlb && props.alb) {
        const listener = props.alb.addListener(`${props.service.name}-listener`, {
          port,
          open: true,
          protocol: elbv2.ApplicationProtocol.HTTP,
        });

        listener.addTargets(`${props.service.name}-tg`, {
          port,
          targets: [ecsService],
          protocol: elbv2.ApplicationProtocol.HTTP,
          healthCheck: {
            path: props.service.ingress.healthcheckPath,
          },
        });
      }
    }
  }
}
