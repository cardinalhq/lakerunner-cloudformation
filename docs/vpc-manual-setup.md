# VPC Infrastructure - Manual AWS Console Setup Guide

This guide provides step-by-step instructions to manually create or configure a VPC for Lakerunner using the AWS Management Console.

## Prerequisites

None - this is typically the first infrastructure component to set up.

## What This Creates

- VPC with public and private subnets across multiple availability zones
- Internet Gateway for public subnet access
- NAT Gateways for private subnet internet access
- Route tables configured for public and private subnets
- VPC endpoints for AWS services (optional but recommended)

## Option A: Create New VPC

### 1. Create VPC

1. Navigate to **VPC → Your VPCs**
1. Click **Create VPC**
1. Configuration:
   - **Name tag**: `lakerunner-vpc`
   - **IPv4 CIDR**: `10.0.0.0/16` (or your preferred range)
   - **IPv6 CIDR block**: No IPv6 CIDR block
   - **Tenancy**: Default
1. Click **Create VPC**

### 2. Create Internet Gateway

1. Navigate to **VPC → Internet Gateways**
1. Click **Create internet gateway**
1. Configuration:
   - **Name tag**: `lakerunner-igw`
1. Click **Create internet gateway**
1. Select the new IGW and click **Actions → Attach to VPC**
1. Select your `lakerunner-vpc`
1. Click **Attach internet gateway**

### 3. Create Subnets

You need at least 2 private subnets in different AZs. Public subnets are optional but required for Application Load Balancers.

#### Private Subnets

1. Navigate to **VPC → Subnets**
1. Click **Create subnet**
1. Configuration:
   - **VPC**: Select `lakerunner-vpc`
   - **Subnet settings**:

**Private Subnet 1:**

- **Subnet name**: `lakerunner-private-subnet-1`
- **Availability Zone**: Choose first AZ (e.g., us-east-1a)
- **IPv4 CIDR block**: `10.0.1.0/24`

**Private Subnet 2:**

- **Subnet name**: `lakerunner-private-subnet-2`
- **Availability Zone**: Choose second AZ (e.g., us-east-1b)
- **IPv4 CIDR block**: `10.0.2.0/24`

1. Click **Create subnet**

#### Public Subnets (Optional - Required for ALB)

1. Navigate to **VPC → Subnets**
1. Click **Create subnet**
1. Configuration:
   - **VPC**: Select `lakerunner-vpc`
   - **Subnet settings**:

**Public Subnet 1:**

- **Subnet name**: `lakerunner-public-subnet-1`
- **Availability Zone**: Same as private subnet 1
- **IPv4 CIDR block**: `10.0.101.0/24`

**Public Subnet 2:**

- **Subnet name**: `lakerunner-public-subnet-2`
- **Availability Zone**: Same as private subnet 2
- **IPv4 CIDR block**: `10.0.102.0/24`

1. Click **Create subnet**
1. For each public subnet:
   - Select the subnet
   - Click **Actions → Modify auto-assign IP settings**
   - Check **Enable auto-assign public IPv4 address**
   - Save

### 4. Create NAT Gateways

NAT Gateways allow private subnet resources to access the internet.

1. Navigate to **VPC → NAT Gateways**
1. Click **Create NAT gateway**

**NAT Gateway 1:**

- **Name**: `lakerunner-nat-1`
- **Subnet**: Select `lakerunner-public-subnet-1`
- **Elastic IP allocation**: Click **Allocate Elastic IP**

**NAT Gateway 2 (for HA):**

- **Name**: `lakerunner-nat-2`
- **Subnet**: Select `lakerunner-public-subnet-2`
- **Elastic IP allocation**: Click **Allocate Elastic IP**

### 5. Configure Route Tables

#### Public Route Table

1. Navigate to **VPC → Route Tables**
1. Click **Create route table**
1. Configuration:
   - **Name**: `lakerunner-public-rt`
   - **VPC**: Select `lakerunner-vpc`
1. Click **Create route table**
1. Select the new route table
1. Go to **Routes** tab → **Edit routes**
1. Add route:
   - **Destination**: `0.0.0.0/0`
   - **Target**: Internet Gateway → Select `lakerunner-igw`
1. Save routes
1. Go to **Subnet associations** tab
1. Click **Edit subnet associations**
1. Select both public subnets
1. Save associations

#### Private Route Tables

Create separate route tables for each private subnet (for independent NAT gateway failure handling):

**Private Route Table 1:**

1. Navigate to **VPC → Route Tables**
1. Click **Create route table**
1. Configuration:
   - **Name**: `lakerunner-private-rt-1`
   - **VPC**: Select `lakerunner-vpc`
1. Select the route table → **Routes** → **Edit routes**
1. Add route:
   - **Destination**: `0.0.0.0/0`
   - **Target**: NAT Gateway → Select `lakerunner-nat-1`
1. **Subnet associations**: Associate with `lakerunner-private-subnet-1`

**Private Route Table 2:**

- Repeat above with `lakerunner-private-rt-2` and `lakerunner-nat-2`
- Associate with `lakerunner-private-subnet-2`

### 6. Create VPC Endpoints (Optional but Recommended)

VPC endpoints reduce costs and improve security by keeping traffic within AWS network.

#### S3 Endpoint

1. Navigate to **VPC → Endpoints**
1. Click **Create endpoint**
1. Configuration:
   - **Service category**: AWS services
   - **Service**: Search for `com.amazonaws.{region}.s3`
   - **Type**: Gateway
   - **VPC**: Select `lakerunner-vpc`
   - **Route tables**: Select all private route tables
1. Create endpoint

#### Other Recommended Endpoints

Create Interface endpoints for:

- `com.amazonaws.{region}.secretsmanager`
- `com.amazonaws.{region}.ecr.api`
- `com.amazonaws.{region}.ecr.dkr`
- `com.amazonaws.{region}.logs`
- `com.amazonaws.{region}.sts`

For each:

1. Select **Interface** type
1. Choose your VPC
1. Select private subnets
1. Select or create security group allowing HTTPS (443) from VPC CIDR

## Option B: Use Existing VPC

If you already have a VPC, ensure it meets these requirements:

### 1. Verify VPC Configuration

1. Navigate to **VPC → Your VPCs**
1. Select your VPC and verify:
   - Has sufficient IP space for new resources
   - DNS resolution and DNS hostnames are enabled
   - Has at least 2 availability zones configured

### 2. Identify or Create Required Subnets

**Private Subnets:**

- Need at least 2 private subnets in different AZs
- Should have route to NAT gateway or instance for internet access
- Should not have direct route to Internet Gateway

**Public Subnets (if using ALB):**

- Need at least 2 public subnets in different AZs
- Must have route to Internet Gateway
- Should have auto-assign public IP enabled

### 3. Verify NAT Gateway/Instance

1. Navigate to **VPC → NAT Gateways**
1. Ensure at least one NAT gateway exists
1. Verify private subnets route to NAT gateway for `0.0.0.0/0`

### 4. Document Subnet IDs

Record the following for use in subsequent setup:

- VPC ID: `vpc-xxxxxxxxx`
- Private Subnet 1 ID: `subnet-xxxxxxxxx`
- Private Subnet 2 ID: `subnet-xxxxxxxxx`
- Public Subnet 1 ID: `subnet-xxxxxxxxx` (if applicable)
- Public Subnet 2 ID: `subnet-xxxxxxxxx` (if applicable)

## Outputs to Record

After completing VPC setup, record these values for use in other components:

- **VPC ID**: The ID of your VPC
- **VPC CIDR**: The CIDR block of your VPC
- **Private Subnet IDs**: List of private subnet IDs
- **Public Subnet IDs**: List of public subnet IDs (if created)
- **Availability Zones**: The AZs used for your subnets

## Next Steps

With VPC infrastructure in place, proceed to:

1. [Security Groups Setup](security-groups-manual-setup.md) - Configure security groups for compute and database resources
1. [ECS Cluster Setup](ecs-cluster-manual-setup.md) - Create ECS cluster for container workloads

## Troubleshooting

### Common Issues

1. **Resources can't reach internet from private subnet:**
   - Verify NAT Gateway is in AVAILABLE state
   - Check route table has 0.0.0.0/0 → NAT Gateway
   - Ensure security groups allow outbound traffic

1. **Cannot create resources in subnet:**
   - Check subnet has available IP addresses
   - Verify subnet is in correct availability zone
   - Ensure VPC has sufficient IP space

1. **DNS resolution not working:**
   - Enable DNS resolution and DNS hostnames on VPC
   - Check DHCP options set configuration
