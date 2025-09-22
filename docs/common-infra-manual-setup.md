# CommonInfra Stack - Manual AWS Console Setup Guide

This guide provides an overview of the infrastructure components created by the CommonInfra CloudFormation stack. Each component has its own detailed setup guide.

## Overview

The CommonInfra stack creates shared infrastructure components needed by both ECS and EKS deployments. You can set up all components or only the ones you need.

## Infrastructure Components

### Core Infrastructure (Required)

1. **[VPC Infrastructure](vpc-manual-setup.md)**
   - VPC with public and private subnets
   - Internet Gateway and NAT Gateways
   - Route tables and VPC endpoints
   - Can use existing VPC if available

1. **[Security Groups](security-groups-manual-setup.md)**
   - Database security group
   - Compute security group for ECS/EKS
   - ALB security group (if using load balancer)
   - MSK security group (if using Kafka)

1. **[IAM Roles](iam-roles-manual-setup.md)**
   - ECS task execution roles
   - ECS task roles for applications
   - Lambda roles (if using Lambda)
   - Service-specific permissions

1. **[ECS Cluster](ecs-cluster-manual-setup.md)**
   - ECS cluster for Fargate or EC2
   - Capacity providers configuration
   - Container Insights setup

### Data Storage (Choose as Needed)

1. **[RDS PostgreSQL Database](rds-manual-setup.md)**
   - Aurora PostgreSQL cluster
   - Database subnet group
   - Automated backups and snapshots
   - Can use existing database

1. **[S3 and SQS Storage](s3-manual-setup.md)**
   - S3 bucket for data storage
   - SQS queue for event processing
   - Event notifications configuration
   - Can use existing resources

1. **[MSK Kafka Cluster](msk-manual-setup.md)** (Optional)
   - Managed Kafka for streaming
   - Topic configuration
   - IAM or TLS authentication

### Configuration and Secrets

1. **[Secrets Management](secrets-manual-setup.md)**
   - Database credentials in Secrets Manager
   - Application secrets (API keys)
   - Configuration in Parameter Store
   - Secret rotation setup

## Setup Order

Follow this recommended order for setting up infrastructure:

1. **VPC Infrastructure** - Set up networking first
1. **Security Groups** - Configure network access controls
1. **IAM Roles** - Create roles for service permissions
1. **ECS Cluster** - Set up container orchestration
1. **RDS Database** - Create database (if needed)
1. **S3 and SQS** - Set up storage and queuing (if needed)
1. **Secrets** - Store credentials and configuration
1. **MSK** - Set up Kafka (if needed)

## Quick Start Paths

### Minimal Setup (Using Existing Resources)

If you already have infrastructure:

1. Review [Security Groups](security-groups-manual-setup.md) - Add required security groups
1. Set up [IAM Roles](iam-roles-manual-setup.md) - Create service roles
1. Configure [Secrets](secrets-manual-setup.md) - Store credentials
1. Proceed to [Migration Setup](../migration-manual-setup.md)

### Full Setup (New Infrastructure)

For complete new deployment:

1. Follow all guides in the recommended order above
1. Record outputs from each component
1. Proceed to [Migration Setup](../migration-manual-setup.md)
1. Then deploy [Services](../services-manual-setup.md)

## Component Dependencies

```text
VPC
├── Security Groups
│   ├── RDS Database
│   ├── ECS Cluster
│   └── MSK Cluster
├── ECS Cluster
│   └── Services (later)
└── S3/SQS
    └── Services (later)

IAM Roles
├── ECS Tasks
├── Lambda Functions
└── EC2 Instances

Secrets Manager
├── Database Credentials
├── Application Secrets
└── Service Configuration
```

## Prerequisites Summary

Before starting, ensure you have:

- AWS account with appropriate IAM permissions
- Basic understanding of AWS services (VPC, ECS, RDS, S3)
- AWS CLI installed (optional, for testing)
- Decision on which components you need

## Outputs to Track

As you complete each component, record these key values:

- VPC ID and subnet IDs
- Security group IDs
- IAM role ARNs
- Database endpoints and credentials
- S3 bucket names and SQS queue URLs
- Secret ARNs

## Support and Troubleshooting

Each component guide includes:

- Detailed troubleshooting sections
- Common issues and solutions
- Best practices

## Next Steps

After completing CommonInfra setup:

1. **[Migration Setup](../migration-manual-setup.md)** - Run database migrations
1. **[Services Setup](../services-manual-setup.md)** - Deploy Lakerunner services
