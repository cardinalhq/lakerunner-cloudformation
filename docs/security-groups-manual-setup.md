# Security Groups - Manual AWS Console Setup Guide

This guide provides step-by-step instructions to manually create security groups for Lakerunner using the AWS Management Console.

## Prerequisites

- [VPC Infrastructure](vpc-manual-setup.md) - You need a VPC configured with subnets

## What This Creates

- Database Security Group - Controls access to RDS PostgreSQL
- Compute Security Group - For ECS tasks and compute resources
- ALB Security Group - For Application Load Balancer (optional)
- MSK Security Group - For Kafka cluster (optional)

## 1. Database Security Group

This security group controls access to the RDS database cluster.

### Create Database Security Group

1. Navigate to **EC2 → Security Groups**
1. Click **Create security group**
1. Basic details:
   - **Security group name**: `lakerunner-db-sg`
   - **Description**: `Security group for Lakerunner RDS database`
   - **VPC**: Select your VPC
1. Inbound rules - Leave empty for now (will update after creating Compute SG)
1. Outbound rules - Keep default (all traffic allowed)
1. Tags:
   - **Name**: `lakerunner-db-sg`
   - **Environment**: `lakerunner`
   - **Component**: `Database`
1. Click **Create security group**
1. **Save the Security Group ID** - You'll need it later

## 2. Compute Security Group

This security group is used by ECS tasks and other compute resources.

### Create Compute Security Group

1. Navigate to **EC2 → Security Groups**
1. Click **Create security group**
1. Basic details:
   - **Security group name**: `lakerunner-compute-sg`
   - **Description**: `Security group for Lakerunner compute resources (ECS tasks)`
   - **VPC**: Select your VPC
1. Inbound rules:

   **Rule 1 - HTTP from VPC:**
   - **Type**: HTTP
   - **Protocol**: TCP
   - **Port range**: 80
   - **Source**: Custom → `10.0.0.0/8` (or your VPC CIDR)
   - **Description**: `HTTP from private networks`

   **Rule 2 - HTTPS from VPC:**
   - **Type**: HTTPS
   - **Protocol**: TCP
   - **Port range**: 443
   - **Source**: Custom → `10.0.0.0/8` (or your VPC CIDR)
   - **Description**: `HTTPS from private networks`

   **Rule 3 - Query API Port (if using):**
   - **Type**: Custom TCP
   - **Protocol**: TCP
   - **Port range**: 7101
   - **Source**: Custom → Select the ALB Security Group (if created)
   - **Description**: `Query API from ALB`

   **Rule 4 - Inter-service Communication:**
   - **Type**: All TCP
   - **Protocol**: TCP
   - **Port range**: 0-65535
   - **Source**: Custom → Select this same security group (self-reference)
   - **Description**: `Inter-service communication`

1. Outbound rules - Keep default (all traffic allowed)
1. Tags:
   - **Name**: `lakerunner-compute-sg`
   - **Environment**: `lakerunner`
   - **Component**: `Compute`
1. Click **Create security group**
1. **Save the Security Group ID**

### Update Database Security Group

Now that the Compute Security Group exists, update the Database Security Group:

1. Navigate to **EC2 → Security Groups**
1. Select `lakerunner-db-sg`
1. Click **Actions → Edit inbound rules**
1. Add rule:
   - **Type**: PostgreSQL
   - **Protocol**: TCP
   - **Port range**: 5432
   - **Source**: Custom → Select `lakerunner-compute-sg`
   - **Description**: `PostgreSQL access from compute resources`
1. Save rules

## 3. ALB Security Group (Optional)

Create this if you plan to use an Application Load Balancer for the Query API.

### Create ALB Security Group

1. Navigate to **EC2 → Security Groups**
1. Click **Create security group**
1. Basic details:
   - **Security group name**: `lakerunner-alb-sg`
   - **Description**: `Security group for Lakerunner Application Load Balancer`
   - **VPC**: Select your VPC
1. Inbound rules:

   **For Internet-facing ALB:**
   - **Type**: HTTP
   - **Protocol**: TCP
   - **Port range**: 80
   - **Source**: Anywhere IPv4 (`0.0.0.0/0`)
   - **Description**: `HTTP from internet`

   - **Type**: HTTPS
   - **Protocol**: TCP
   - **Port range**: 443
   - **Source**: Anywhere IPv4 (`0.0.0.0/0`)
   - **Description**: `HTTPS from internet`

   **For Internal ALB:**
   - **Type**: HTTP
   - **Protocol**: TCP
   - **Port range**: 80
   - **Source**: Custom → Your VPC CIDR or specific IP ranges
   - **Description**: `HTTP from internal networks`

1. Outbound rules:
   - **Type**: Custom TCP
   - **Protocol**: TCP
   - **Port range**: 7101
   - **Destination**: Custom → Select `lakerunner-compute-sg`
   - **Description**: `Query API traffic to ECS tasks`
1. Tags:
   - **Name**: `lakerunner-alb-sg`
   - **Environment**: `lakerunner`
   - **Component**: `LoadBalancer`
1. Click **Create security group**

### Update Compute Security Group for ALB

If you created an ALB Security Group:

1. Navigate to **EC2 → Security Groups**
1. Select `lakerunner-compute-sg`
1. Click **Actions → Edit inbound rules**
1. Update or add rule:
   - **Type**: Custom TCP
   - **Protocol**: TCP
   - **Port range**: 7101
   - **Source**: Custom → Select `lakerunner-alb-sg`
   - **Description**: `Query API from ALB`
1. Save rules

## 4. MSK Security Group (Optional)

Create this if you plan to use Amazon MSK (Managed Kafka).

### Create MSK Security Group

1. Navigate to **EC2 → Security Groups**
1. Click **Create security group**
1. Basic details:
   - **Security group name**: `lakerunner-msk-sg`
   - **Description**: `Security group for Lakerunner MSK cluster`
   - **VPC**: Select your VPC
1. Inbound rules:

   **Kafka Broker Communication:**
   - **Type**: Custom TCP
   - **Protocol**: TCP
   - **Port range**: 9092
   - **Source**: Custom → Select `lakerunner-compute-sg`
   - **Description**: `Kafka plaintext from compute`

   **Kafka TLS:**
   - **Type**: Custom TCP
   - **Protocol**: TCP
   - **Port range**: 9094
   - **Source**: Custom → Select `lakerunner-compute-sg`
   - **Description**: `Kafka TLS from compute`

   **Zookeeper (if using):**
   - **Type**: Custom TCP
   - **Protocol**: TCP
   - **Port range**: 2181
   - **Source**: Custom → Select this same security group (self-reference)
   - **Description**: `Zookeeper inter-node`

   **Inter-broker Communication:**
   - **Type**: All TCP
   - **Protocol**: TCP
   - **Port range**: 0-65535
   - **Source**: Custom → Select this same security group (self-reference)
   - **Description**: `MSK inter-broker communication`

1. Outbound rules - Keep default
1. Tags:
   - **Name**: `lakerunner-msk-sg`
   - **Environment**: `lakerunner`
   - **Component**: `MSK`
1. Click **Create security group**

## 5. VPC Endpoint Security Group (Optional)

If you're using VPC endpoints for AWS services:

### Create VPC Endpoint Security Group

1. Navigate to **EC2 → Security Groups**
1. Click **Create security group**
1. Basic details:
   - **Security group name**: `lakerunner-vpce-sg`
   - **Description**: `Security group for VPC endpoints`
   - **VPC**: Select your VPC
1. Inbound rules:
   - **Type**: HTTPS
   - **Protocol**: TCP
   - **Port range**: 443
   - **Source**: Custom → Your VPC CIDR
   - **Description**: `HTTPS from VPC`
1. Outbound rules - Keep default
1. Tags:
   - **Name**: `lakerunner-vpce-sg`
   - **Environment**: `lakerunner`
   - **Component**: `VPCEndpoints`
1. Click **Create security group**

## Security Group Dependencies

Here's the relationship between security groups:

```text
ALB SG → Compute SG (port 7101)
Compute SG → Database SG (port 5432)
Compute SG → MSK SG (ports 9092, 9094)
Compute SG ↔ Compute SG (all ports, self-reference)
MSK SG ↔ MSK SG (all ports, self-reference)
```

## Outputs to Record

After completing security group setup, record these values:

- **Database Security Group ID**: `sg-xxxxxxxxx`
- **Compute Security Group ID**: `sg-xxxxxxxxx`
- **ALB Security Group ID**: `sg-xxxxxxxxx` (if created)
- **MSK Security Group ID**: `sg-xxxxxxxxx` (if created)
- **VPC Endpoint Security Group ID**: `sg-xxxxxxxxx` (if created)

## Next Steps

With security groups configured, proceed to:

1. [RDS Setup](rds-manual-setup.md) - Create the PostgreSQL database
1. [ECS Cluster Setup](ecs-cluster-manual-setup.md) - Create the ECS cluster

## Best Practices

1. **Principle of Least Privilege**: Only open required ports
1. **Use Security Group References**: Reference other security groups instead of IP ranges where possible
1. **Document Rules**: Always add descriptions to rules
1. **Regular Review**: Periodically review and remove unused rules
1. **Avoid 0.0.0.0/0**: Limit use of "anywhere" source to only when necessary (e.g., public-facing ALB)

## Troubleshooting

### Common Issues

1. **Connection timeouts between services:**
   - Verify security group rules allow the required ports
   - Check both inbound on destination and outbound on source
   - Ensure security groups are attached to the correct resources

1. **Database connection fails:**
   - Verify Database SG allows port 5432 from Compute SG
   - Check that ECS tasks are using Compute SG
   - Confirm database is listening on port 5432

1. **ALB health checks failing:**
   - Ensure Compute SG allows traffic from ALB SG on application port
   - Verify health check port matches application port
   - Check application is binding to all interfaces (0.0.0.0)

1. **Cannot modify security group:**
   - Check if security group is referenced by other rules
   - Ensure you have appropriate IAM permissions
   - Verify the security group isn't attached to running resources that prevent modification
