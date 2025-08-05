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
import { Fn } from 'aws-cdk-lib';
import * as elbv2 from 'aws-cdk-lib/aws-elasticloadbalancingv2';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import * as cr from 'aws-cdk-lib/custom-resources';
import * as efs from 'aws-cdk-lib/aws-efs';

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
  public readonly taskRole: iam.Role;
  public readonly storageProfilesParam: ssm.IStringParameter;
  public readonly apiKeysParam: ssm.IStringParameter;
  public readonly grafanaConfig: ssm.IStringParameter;
  public readonly taskSecurityGroup: ec2.ISecurityGroup;
  public readonly runMigration: cr.AwsCustomResource;
  public readonly alb: elbv2.ApplicationLoadBalancer;
  public readonly efs: efs.FileSystem;

  constructor(scope: Construct, id: string, props: CommonInfraProps) {
    super(scope, id, props);

    this.vpc = ec2.Vpc.fromLookup(this, 'Vpc', { vpcId: props.vpcId });

    new cdk.CfnOutput(this, 'PrivateSubnetIds', {
      value: this.vpc
        .selectSubnets({ subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS })
        .subnetIds
        .join(','),
      exportName: 'CommonInfraPrivateSubnetIds',
    });

    new cdk.CfnOutput(this, 'PublicSubnetIds', {
      value: this.vpc
        .selectSubnets({ subnetType: ec2.SubnetType.PUBLIC })
        .subnetIds
        .join(','),
      exportName: 'CommonInfraPublicSubnetIds',
    });

    this.cluster = new Cluster(this, 'Cluster', {
      vpc: this.vpc,
    });

    new cdk.CfnOutput(this, 'ClusterArn', {
      value: this.cluster.clusterArn,
      exportName: 'CommonInfraClusterArn',
    });

    // ── Security Group for all Fargate tasks ─────────────────────
    this.taskSecurityGroup = new ec2.SecurityGroup(this, 'TaskSG', {
      vpc: this.vpc,
      allowAllOutbound: true,
    });

    this.taskSecurityGroup.addIngressRule(
      this.taskSecurityGroup,
      ec2.Port.tcp(7101),
      'Allow query-api to query-worker on 7101'
    );

    this.taskSecurityGroup.addIngressRule(
      ec2.Peer.anyIpv4(),
      ec2.Port.tcp(7101),
      'Allow query-worker to query-api on 7101'
    );

    new cdk.CfnOutput(this, 'TaskSecurityGroupId', {
      value: this.taskSecurityGroup.securityGroupId,
      exportName: 'CommonInfraTaskSecurityGroupId',
    });

    const albSecurityGroup = new ec2.SecurityGroup(this, 'AlbSecurityGroup', {
      vpc: this.vpc,
      description: 'Allow HTTP/HTTPS from the internet',
      allowAllOutbound: true,
    });

    albSecurityGroup.addIngressRule(ec2.Peer.anyIpv4(), ec2.Port.tcp(3000), 'Allow HTTP from internet');
    albSecurityGroup.addIngressRule(ec2.Peer.anyIpv4(), ec2.Port.tcp(7101), 'Allow HTTPS from internet');

    this.taskSecurityGroup.addIngressRule(albSecurityGroup, ec2.Port.tcp(3000), 'Allow only ALB to grafana on port 3000');
    this.taskSecurityGroup.addIngressRule(albSecurityGroup, ec2.Port.tcp(7101), 'Allow only ALB to query-api on port 7101');

    this.alb = new elbv2.ApplicationLoadBalancer(this, 'query-api-requests', {
      vpc: this.vpc,
      internetFacing: true,
      securityGroup: this.taskSecurityGroup,
      vpcSubnets: { subnetType: ec2.SubnetType.PUBLIC },
    });

    // ── SQS Queue ─────────────────────────────────────────────
    this.queue = new sqs.Queue(this, 'IngestQueue', {
      queueName: 'lakerunner-ingest-queue',
      retentionPeriod: Duration.days(4),
      removalPolicy: RemovalPolicy.DESTROY,
    });

    // ── S3 Bucket + Notifications + Lifecycle ────────────────
    this.bucket = new s3.Bucket(this, 'IngestBucket', {
      bucketName: 'lakerunner-datalake',
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

    this.dbInstance.connections.allowDefaultPortFrom(this.taskSecurityGroup);

    this.taskRole = new iam.Role(this, 'TaskRole', {
      assumedBy: new iam.ServicePrincipal('ecs-tasks.amazonaws.com'),
      description: `Fargate task role: RW S3, consume SQS, read DB secret`,
    });

    this.bucket.grantReadWrite(this.taskRole);
    this.queue.grantConsumeMessages(this.taskRole);
    this.dbSecret.grantRead(this.taskRole);

    const serviceArn = cdk.Arn.format({
      service: 'ecs',
      resource: 'service',
      resourceName: `${this.cluster.clusterName}/*`,
      arnFormat: cdk.ArnFormat.SLASH_RESOURCE_NAME,
    }, this);

    this.taskRole.addToPolicy(new iam.PolicyStatement({
      actions: [
        'ecs:ListServices',
        'ecs:DescribeServices',
        'ecs:UpdateService',
      ],
      resources: [
        this.cluster.clusterArn,
        serviceArn,
      ],
    }));

    const containerInstanceArn = cdk.Arn.format({
      service: 'ecs',
      resource: 'container-instance',
      resourceName: `${this.cluster.clusterName}/*`,
      arnFormat: cdk.ArnFormat.COLON_RESOURCE_NAME,
    }, this);

    this.taskRole.addToPolicy(new iam.PolicyStatement({
      actions: ['ecs:ListTasks'],
      resources: ['*'],
      conditions: {
        ArnEquals: { 'ecs:cluster': this.cluster.clusterArn }
      }
    }));

    this.taskRole.addToPolicy(new iam.PolicyStatement({
      actions: ['ecs:DescribeTasks'],
      resources: ['*'],
    }));

    this.storageProfilesParam = new ssm.StringParameter(this, 'StorageProfilesParam', {
      parameterName: '/lakerunner/storage_profiles',
      stringValue: Fn.sub(
        [
          '- bucket: ${BucketName}',
          '  cloud_provider: aws',
          '  collector_name: lakerunner',
          '  insecure_tls: false',
          '  instance_num: 1',
          '  organization_id: 12340000-0000-4000-8000-000000000000',
          '  region: ${AWS::Region}',
          '  use_path_style: true',
        ].join('\n'),
        {
          BucketName: this.bucket.bucketName,
        }
      ),
      description: 'Storage profiles config',
      tier: ssm.ParameterTier.STANDARD,
    });
    this.storageProfilesParam.applyRemovalPolicy(RemovalPolicy.DESTROY);

    this.apiKeysParam = new ssm.StringParameter(this, 'ApiKeysParam', {
      parameterName: '/lakerunner/api_keys',
      stringValue: [
        '- organization_id: 12340000-0000-4000-8000-000000000000',
        '  keys:',
        '    - f70603aa00e6f67999cc66e336134887',
      ].join('\n'),
      description: 'API keys for Lakerunner',
      tier: ssm.ParameterTier.STANDARD,
    });
    this.apiKeysParam.applyRemovalPolicy(RemovalPolicy.DESTROY);

    this.grafanaConfig = new ssm.StringParameter(this, 'GrafanaConfig', {
      parameterName: '/lakerunner/grafana_config',
      stringValue: [
        'datasources:',
        '  - name: Cardinal',
        '    type: cardinalhq-lakerunner-datasource',
        '    access: proxy',
        '    isDefault: true',
        '    isEditable: true',
        '    jsonData:',
        '      customPath: http://' + this.alb.loadBalancerDnsName + ':7101',
        '    secureJsonData:',
        '      apiKey: f70603aa00e6f67999cc66e336134887',
      ].join('\n'),
      description: 'Grafana configuration for Lakerunner',
      tier: ssm.ParameterTier.STANDARD,
    });
    this.grafanaConfig.applyRemovalPolicy(RemovalPolicy.DESTROY);

    this.efs = new efs.FileSystem(this, 'lakerunner-efs', {
      vpc: this.vpc,
      securityGroup: this.taskSecurityGroup,
    });

    this.efs.connections.allowDefaultPortFrom(this.taskSecurityGroup);
  }
}
