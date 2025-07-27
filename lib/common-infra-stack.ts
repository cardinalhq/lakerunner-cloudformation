// lib/common-infra-stack.ts
import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as ec2    from 'aws-cdk-lib/aws-ec2';
import * as ecs    from 'aws-cdk-lib/aws-ecs';

export interface CommonInfraProps extends cdk.StackProps {
  /** The ID of the VPC to import */
  readonly vpcId: string;
}

export class CommonInfraStack extends cdk.Stack {
  public readonly vpc: ec2.IVpc;
  public readonly cluster: ecs.Cluster;

  constructor(scope: Construct, id: string, props: CommonInfraProps) {
    super(scope, id, props);

    // __Move the lookup into the stack constructor__
    this.vpc = ec2.Vpc.fromLookup(this, 'Vpc', {
      vpcId: props.vpcId,
    });

    this.cluster = new ecs.Cluster(this, 'Cluster', {
      vpc: this.vpc,
    });
  }
}
