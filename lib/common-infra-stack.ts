// lib/common-infra-stack.ts
import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import { Cluster } from 'aws-cdk-lib/aws-ecs';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as s3n from 'aws-cdk-lib/aws-s3-notifications';
import * as sqs from 'aws-cdk-lib/aws-sqs';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as rds from 'aws-cdk-lib/aws-rds';
import * as secretsmanager from 'aws-cdk-lib/aws-secretsmanager';
import { Duration, RemovalPolicy } from 'aws-cdk-lib';
import { CfnResource } from 'aws-cdk-lib';
import { Fn } from 'aws-cdk-lib';

import * as ssm from 'aws-cdk-lib/aws-ssm';

export interface CommonInfraProps extends cdk.StackProps {
  readonly vpcId: string;

  readonly dbConfig: {
    readonly username: string;
    readonly name: string;
    readonly port?: string;
    readonly sslmode?: string;
  };

  readonly dbSecretName: string;
}

export class CommonInfraStack extends cdk.Stack {
  public readonly dbInstance: rds.DatabaseInstance;
  public readonly dbSecret: secretsmanager.ISecret;
  public readonly vpc: ec2.IVpc;
  public readonly cluster: Cluster;
  public readonly bucket: s3.IBucket;
  public readonly queue: sqs.IQueue;
  public readonly taskRole: iam.IRole;
  public readonly StorageProfileParam: ssm.IStringParameter;

  constructor(scope: Construct, id: string, props: CommonInfraProps) {
    super(scope, id, props);

    // ── VPC & ECS Cluster ─────────────────────────────────────
    this.vpc = ec2.Vpc.fromLookup(this, 'Vpc', { vpcId: props.vpcId });
    this.cluster = new Cluster(this, 'Cluster', {
      vpc: this.vpc,
    });

    // ── SQS Queue ─────────────────────────────────────────────
    this.queue = new sqs.Queue(this, 'IngestQueue', {
      queueName: 'lakerunner-ingest-queue',
      retentionPeriod: Duration.days(4),
      removalPolicy: RemovalPolicy.DESTROY,
    });

    // ── S3 Bucket + Notifications + Lifecycle ────────────────
    this.bucket = new s3.Bucket(this, 'IngestBucket', {
      bucketName: 'lakerunner-datalake', // or omit for generated name
      removalPolicy: RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
      lifecycleRules: [
        {
          prefix: 'otel-raw/',
          expiration: Duration.days(10),
        },
      ],
    });

    // fire SNS → SQS on object created under each prefix
    for (const prefix of ['otel-raw/', 'logs-raw/', 'metrics-raw/']) {
      this.bucket.addEventNotification(
        s3.EventType.OBJECT_CREATED,
        new s3n.SqsDestination(this.queue),
        { prefix },
      );
    }

    // ── Create the Secret with your username in the template ─────────
    this.dbSecret = new secretsmanager.Secret(this, 'DbSecret', {
      secretName: props.dbSecretName,
      generateSecretString: {
        secretStringTemplate: JSON.stringify({ username: props.dbConfig.username }),
        generateStringKey: 'password',
        excludePunctuation: true,
      },
      removalPolicy: RemovalPolicy.DESTROY,
    });

    // ── RDS Instance using the secret (which now contains { username, password }) ──
    this.dbInstance = new rds.DatabaseInstance(this, 'MetadataDb', {
      engine: rds.DatabaseInstanceEngine.postgres({ version: rds.PostgresEngineVersion.VER_17 }),
      instanceType: ec2.InstanceType.of(ec2.InstanceClass.BURSTABLE3, ec2.InstanceSize.MEDIUM),
      vpc: this.vpc,
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      credentials: rds.Credentials.fromSecret(this.dbSecret),
      databaseName: props.dbConfig.name,
      removalPolicy: RemovalPolicy.DESTROY,
      deletionProtection: false,
      multiAz: false,
    });

    // ── IAM Role for Fargate Tasks ────────────────────────────
    this.taskRole = new iam.Role(this, 'TaskRole', {
      assumedBy: new iam.ServicePrincipal('ecs-tasks.amazonaws.com'),
      description: `Fargate task role: RW S3, consume SQS, read DB secret`,
    });

    // grant exactly the permissions the tasks need:
    this.bucket.grantReadWrite(this.taskRole);
    this.queue.grantConsumeMessages(this.taskRole);
    this.dbSecret.grantRead(this.taskRole);

    new CfnResource(this, 'StorageProfilesParam', {
      type: 'AWS::SSM::Parameter',
      properties: {
        Name: '/lakerunner/storage_profiles',  // your parameter name
        Type: 'String',
        Value: Fn.sub(`- bucket: ${this.bucket.bucketName}
  cloud_provider: aws
  collector_name: lakerunner
  insecure_tls: false
  instance_num: 1
  organization_id: b932c6f0-b968-4ff9-ae8f-365873c552f0
  region: \${AWS::Region}
  use_path_style: true`),
      },
    });

  }
}
