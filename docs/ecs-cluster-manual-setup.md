# ECS Cluster - Manual AWS Console Setup Guide

This guide provides step-by-step instructions to manually create an Amazon ECS cluster for running Lakerunner containerized services using the AWS Management Console.

## Prerequisites

- [VPC Infrastructure](vpc-manual-setup.md) - VPC with private subnets configured
- [Security Groups](security-groups-manual-setup.md) - Compute security group created

## What This Creates

- ECS cluster for running Fargate tasks
- Cluster capacity providers
- CloudWatch Container Insights (optional)
- Auto Scaling configuration (optional)

## Create ECS Cluster

### 1. Create the Cluster

1. Navigate to **Amazon ECS → Clusters**
1. Click **Create cluster**
1. Cluster configuration:
   - **Cluster name**: `lakerunner-cluster`
1. Infrastructure:
   - **AWS Fargate (serverless)**: ✓ Selected
   - **Amazon EC2 instances**: ☐ Unselected (unless you need EC2)
1. Monitoring:
   - **Use Container Insights**: ✓ Enable (recommended for production)
   - This enables detailed CloudWatch metrics and logs
1. Tags:
   - **Name**: `lakerunner-cluster`
   - **Environment**: `lakerunner`
   - **Component**: `ECS`
1. Click **Create**

### 2. Configure Capacity Providers (Optional)

Capacity providers optimize task placement and cost:

1. Select your cluster
1. Go to **Infrastructure** tab
1. Click **Update cluster**
1. Capacity providers:
   - **FARGATE**: Already added
   - **FARGATE_SPOT**: Add for cost savings (optional)
1. Default capacity provider strategy:

   ```text
   Base: 1 (FARGATE)
   Weight: 1 (FARGATE)

   Base: 0 (FARGATE_SPOT)
   Weight: 4 (FARGATE_SPOT)
   ```

   This runs 1 task on regular Fargate, then 80% on Spot
1. Update cluster

### 3. Configure Cluster Settings

1. Select your cluster
1. Click **Update cluster**
1. Settings:
   - **Container Insights**: Enabled
   - **Default Service Connect namespace**: Leave empty for now
1. Update

## Configure for EC2 Launch Type (Optional)

If you need EC2 instances instead of or in addition to Fargate:

### 1. Create EC2 Launch Template

1. Navigate to **EC2 → Launch Templates**
1. Click **Create launch template**
1. Template details:
   - **Name**: `lakerunner-ecs-lt`
   - **Description**: `ECS container instances for Lakerunner`
1. Instance configuration:
   - **AMI**: Amazon ECS-Optimized AMI (search for latest)
   - **Instance type**: t3.medium (or as needed)
   - **Key pair**: Select existing or create new
1. Network settings:
   - **Subnet**: Don't include in template
   - **Security groups**: Select `lakerunner-compute-sg`
1. Storage:
   - **Volume 1**: 30 GiB, gp3, encrypted
1. Advanced details:
   - **IAM instance profile**: Create or select ECS instance profile
   - **User data**:

   ```bash
   #!/bin/bash
   echo ECS_CLUSTER=lakerunner-cluster >> /etc/ecs/ecs.config
   echo ECS_ENABLE_CONTAINER_METADATA=true >> /etc/ecs/ecs.config
   ```

1. Create template

### 2. Create Auto Scaling Group

1. Navigate to **EC2 → Auto Scaling Groups**
1. Click **Create Auto Scaling group**
1. Name: `lakerunner-ecs-asg`
1. Launch template: Select `lakerunner-ecs-lt`
1. Network:
   - **VPC**: Your VPC
   - **Subnets**: Select private subnets
1. Configure group size:
   - **Desired**: 2
   - **Minimum**: 1
   - **Maximum**: 10
1. Create

### 3. Create Capacity Provider for EC2

1. Go back to ECS cluster
1. **Infrastructure** → **Capacity providers** → **Create**
1. Configuration:
   - **Name**: `lakerunner-ec2-cp`
   - **Auto Scaling group**: Select `lakerunner-ecs-asg`
   - **Managed scaling**: Enabled
   - **Target capacity**: 100%
   - **Managed termination protection**: Enabled
1. Create and associate with cluster

## Configure Service Discovery (Optional)

For service-to-service communication:

### 1. Create Cloud Map Namespace

1. Navigate to **AWS Cloud Map → Namespaces**
1. Click **Create namespace**
1. Configuration:
   - **Name**: `lakerunner.local`
   - **Type**: API calls only (Private DNS)
   - **VPC**: Select your VPC
1. Create namespace

### 2. Associate with Cluster

1. Go to ECS cluster
1. **Update cluster** → **Service Connect**
1. Default namespace: Select `lakerunner.local`
1. Update

## Configure Cluster Auto Scaling (Optional)

For automatic scaling based on metrics:

### 1. Create Scaling Policies

1. Navigate to **ECS → Clusters** → Select cluster
1. Go to **Services** tab (after creating services)
1. Select a service → **Auto Scaling** tab
1. Configure scaling:
   - **Minimum tasks**: 1
   - **Desired tasks**: 2
   - **Maximum tasks**: 10
1. Add scaling policy:
   - **Policy type**: Target tracking
   - **Metric**: ECS service average CPU
   - **Target value**: 70%
   - **Scale out cooldown**: 60 seconds
   - **Scale in cooldown**: 300 seconds

## ECS Exec Configuration (Optional)

Enable SSH-like access to running containers:

### 1. Enable on Cluster

The cluster already supports ECS Exec with Fargate platform version 1.4.0+

### 2. Configure Task Role

Add to task role policy:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "ssmmessages:CreateControlChannel",
        "ssmmessages:CreateDataChannel",
        "ssmmessages:OpenControlChannel",
        "ssmmessages:OpenDataChannel"
      ],
      "Resource": "*"
    }
  ]
}
```

### 3. Enable on Service

When creating services, enable execute command:

```bash
aws ecs create-service \
  --enable-execute-command \
  ...
```

### 4. Access Container

```bash
aws ecs execute-command \
  --cluster lakerunner-cluster \
  --task <task-id> \
  --container app \
  --interactive \
  --command "/bin/bash"
```

## Outputs to Record

After completing cluster setup:

- **Cluster Name**: `lakerunner-cluster`
- **Cluster ARN**: `arn:aws:ecs:{region}:{accountId}:cluster/lakerunner-cluster`
- **Service Discovery Namespace**: `lakerunner.local` (if created)
- **Capacity Providers**: FARGATE, FARGATE_SPOT (if configured)

## Next Steps

With the ECS cluster ready:

1. [IAM Roles Setup](iam-roles-manual-setup.md) - Configure task execution and task roles
1. [Secrets Setup](secrets-manual-setup.md) - Store application secrets
1. [Services Setup](../services-manual-setup.md) - Deploy Lakerunner services

## Best Practices

### Security

1. **Use private subnets** for all tasks
1. **Enable CloudWatch Container Insights** for monitoring
1. **Use ECS Exec** instead of SSH for debugging
1. **Regularly update ECS agent** (for EC2 launch type)
1. **Use separate task roles** per service

### Reliability

1. **Spread tasks across AZs** for high availability
1. **Set appropriate health check grace periods**
1. **Use circuit breakers** in service configuration
1. **Configure proper task placement strategies**
1. **Monitor cluster capacity** and scale proactively

## Troubleshooting

### Common Issues

1. **Tasks failing to start:**
   - Check task definition for errors
   - Verify IAM roles have correct permissions
   - Check CloudWatch logs for container errors
   - Ensure subnet has route to internet (via NAT)

1. **Cannot pull container images:**
   - Verify task execution role has ECR permissions
   - Check image URI is correct
   - Ensure VPC has internet access or VPC endpoints
   - Verify ECR repository exists and has images

1. **Service not reaching desired count:**
   - Check cluster has sufficient capacity
   - Review task placement constraints
   - Check for port conflicts
   - Verify security groups and network configuration

1. **Resource issues:**
   - Review task CPU/memory allocations
   - Check cluster capacity
   - Review task placement
   - Clean up unused resources

1. **ECS Exec not working:**
   - Verify SSM endpoints are accessible
   - Check task role has SSM permissions
   - Ensure service has execute-command enabled
   - Verify Session Manager plugin is installed locally

## Fargate vs EC2 Comparison

| Aspect | Fargate | EC2 |
|--------|---------|-----|
| **Management** | Serverless, no instances | Manage EC2 instances |
| **Pricing** | Per task CPU/memory/duration | Per instance hour |
| **Scaling** | Instant, unlimited | Limited by instance capacity |
| **Startup time** | ~30 seconds | ~2-5 minutes |
| **Customization** | Limited | Full control |
| **GPU support** | No | Yes |
| **Steady workload** | Good | Better |
| **Variable workload** | Better | Good |

Choose Fargate for simplicity and variable workloads. Choose EC2 when you need specific instance features or more control.
