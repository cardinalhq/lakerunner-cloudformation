# RDS PostgreSQL - Manual AWS Console Setup Guide

This guide provides step-by-step instructions to manually create an RDS PostgreSQL database cluster for Lakerunner using the AWS Management Console.

## Prerequisites

- [VPC Infrastructure](vpc-manual-setup.md) - VPC with at least 2 private subnets in different AZs
- [Security Groups](security-groups-manual-setup.md) - Database security group configured

## What This Creates

- RDS Aurora PostgreSQL cluster
- Database subnet group
- Database credentials in AWS Secrets Manager
- Initial database and schema

## Option A: Create New RDS Cluster

### 1. Create Database Subnet Group

1. Navigate to **RDS → Subnet groups**
1. Click **Create DB subnet group**
1. Configuration:
   - **Name**: `lakerunner-db-subnet-group`
   - **Description**: `Subnet group for Lakerunner RDS database`
   - **VPC**: Select your VPC
1. Add subnets:
   - Select at least 2 private subnets in different availability zones
   - Do NOT use public subnets for database
1. Tags:
   - **Name**: `lakerunner-db-subnet-group`
   - **Environment**: `lakerunner`
   - **Component**: `Database`
1. Click **Create**

### 2. Create Database Secret

Store the database credentials securely in Secrets Manager:

1. Navigate to **AWS Secrets Manager**
1. Click **Store a new secret**
1. Secret type: **Credentials for Amazon RDS database**
1. Credentials:
   - **Username**: `postgres`
   - **Password**: Click **Generate** (or enter your own)
   - Password requirements:
     - At least 32 characters
     - Exclude special characters: `"@/\`
1. Database: **Don't select a database yet**
1. Click **Next**
1. Secret name: `lakerunner-database-secret`
1. Description: `RDS PostgreSQL master credentials for Lakerunner`
1. Tags:
   - **Environment**: `lakerunner`
   - **Component**: `Database`
1. Review and click **Store**
1. **Save the Secret ARN** - You'll need it later

### 3. Create RDS Aurora PostgreSQL Cluster

1. Navigate to **RDS → Databases**
1. Click **Create database**
1. Choose a database creation method: **Standard create**
1. Engine options:
   - **Engine type**: Amazon Aurora
   - **Edition**: Amazon Aurora PostgreSQL-Compatible Edition
   - **Engine version**: Aurora PostgreSQL 15.4 (or latest 15.x)
1. Templates: **Production**
1. Settings:
   - **DB cluster identifier**: `lakerunner-db-cluster`
   - **Master username**: `postgres`
   - **Credentials management**:
     - Select **Manage master credentials in AWS Secrets Manager**
     - Select **Use an existing secret**
     - Choose `lakerunner-database-secret`
1. Instance configuration:
   - **DB instance class**: Burstable classes
   - Select `db.t3.medium` for dev/test or `db.r5.large` for production
1. Availability & durability:
   - **Multi-AZ deployment**: Create an Aurora Replica in a different AZ (recommended for production)
1. Connectivity:
   - **Virtual private cloud (VPC)**: Select your VPC
   - **DB subnet group**: `lakerunner-db-subnet-group`
   - **Public access**: No
   - **VPC security group**: Choose existing
   - Select `lakerunner-db-sg`
   - **Database port**: 5432
1. Database authentication:
   - **Database authentication options**: Password authentication
1. Additional configuration:
   - **Initial database name**: `lakerunner`
   - **DB cluster parameter group**: default.aurora-postgresql15
   - **Backup retention period**: 7 days
   - **Backup window**: Choose window → `03:00-04:00 UTC`
   - **Maintenance window**: Choose window → `sun:04:00-sun:05:00 UTC`
   - **Enable encryption**: Yes
   - **Master key**: aws/rds (or select a CMK)
   - **Enable Performance Insights**: Yes (recommended)
   - **Retention period**: 7 days
1. Click **Create database**

### 4. Create Additional Database

After the cluster is available, create the configdb database:

1. Wait for the cluster status to show **Available** (10-15 minutes)
1. Note the cluster endpoint from the Connectivity & security tab
1. Connect to the database using a bastion host or AWS Session Manager:

```bash
# Install PostgreSQL client if needed
sudo yum install -y postgresql15 # Amazon Linux 2
# or
sudo apt-get install -y postgresql-client # Ubuntu

# Get the password from Secrets Manager
aws secretsmanager get-secret-value \
  --secret-id lakerunner-database-secret \
  --query SecretString \
  --output text | jq -r .password

# Connect to the database
psql -h <cluster-endpoint> -U postgres -d lakerunner

# Create the configdb database
CREATE DATABASE configdb;
\q
```

## Option B: Use Existing RDS Database

If you have an existing PostgreSQL database (RDS or self-managed):

### 1. Verify Database Requirements

Ensure your existing database meets these requirements:

- PostgreSQL version 17 or higher
- At least 20GB storage
- SSL/TLS enabled
- Accessible from private subnets

### 2. Create Databases

Connect to your existing PostgreSQL instance and create required databases:

```sql
-- Connect as admin user
CREATE DATABASE lakerunner;
CREATE DATABASE configdb;
```

### 3. Create Application User (Optional)

For better security, create a dedicated application user:

```sql
-- Connect to lakerunner database
\c lakerunner

-- Create application user
CREATE USER lakerunner_app WITH PASSWORD 'secure-password-here';
GRANT ALL PRIVILEGES ON DATABASE lakerunner TO lakerunner_app;
GRANT ALL PRIVILEGES ON DATABASE configdb TO lakerunner_app;

-- Grant schema permissions
GRANT ALL ON SCHEMA public TO lakerunner_app;
```

### 4. Store Credentials in Secrets Manager

1. Navigate to **AWS Secrets Manager**
1. Click **Store a new secret**
1. Secret type: **Other type of secret**
1. Secret key/value:

   ```json
   {
     "username": "postgres",
     "password": "your-password-here",
     "engine": "postgres",
     "host": "your-db-endpoint",
     "port": 5432,
     "dbname": "lakerunner"
   }
   ```

1. Secret name: `lakerunner-database-secret`
1. Store the secret

### 5. Update Security Groups

Ensure your existing database security group allows connections:

1. Add inbound rule for port 5432 from `lakerunner-compute-sg`
1. Or add the database to `lakerunner-db-sg` if possible

## Outputs to Record

After completing RDS setup, record these values:

- **Database Endpoint**: `xxx.cluster-xxx.{region}.rds.amazonaws.com`
- **Database Port**: `5432`
- **Database Name**: `lakerunner`
- **ConfigDB Name**: `configdb`
- **Database Secret ARN**: `arn:aws:secretsmanager:{region}:{account}:secret:lakerunner-database-secret-xxx`
- **Database Security Group**: `sg-xxxxxxxxx`

## Performance Tuning

### Parameter Group Settings

For production workloads, create a custom parameter group:

1. Navigate to **RDS → Parameter groups**
1. Click **Create parameter group**
1. Configuration:
   - **Parameter group family**: aurora-postgresql15
   - **Type**: DB Cluster Parameter Group
   - **Name**: `lakerunner-db-params`
1. Edit parameters:

   ```properties
   shared_preload_libraries = 'pg_stat_statements'
   pg_stat_statements.track = 'all'
   log_statement = 'all'
   log_duration = 'on'
   max_connections = 500
   ```

### Instance Sizing Guidelines

- **Development**: `db.t3.medium` (2 vCPU, 4 GB RAM)
- **Small Production**: `db.r5.large` (2 vCPU, 16 GB RAM)
- **Medium Production**: `db.r5.xlarge` (4 vCPU, 32 GB RAM)
- **Large Production**: `db.r5.2xlarge` (8 vCPU, 64 GB RAM)

## Next Steps

With the database configured, proceed to:

1. [Database Migration](../migration-manual-setup.md) - Run schema migrations
1. [Secrets Setup](secrets-manual-setup.md) - Configure application secrets

## Backup and Recovery

### Automated Backups

RDS automatically creates daily backups during the backup window. To restore:

1. Navigate to **RDS → Databases**
1. Select your cluster
1. Click **Actions → Restore to point in time**
1. Choose restore point
1. Configure new cluster settings
1. Create restored cluster

### Manual Snapshots

To create a manual snapshot:

1. Select your database cluster
1. Click **Actions → Take snapshot**
1. Enter snapshot name
1. Click **Take snapshot**

## Troubleshooting

### Common Issues

1. **Cannot connect to database:**
   - Verify security group allows port 5432 from compute resources
   - Check database is in Available state
   - Ensure using correct endpoint (cluster endpoint for writes)
   - Verify SSL mode is set correctly

1. **Authentication failed:**
   - Check credentials in Secrets Manager
   - Verify username and password are correct
   - Ensure secret is accessible from compute resources

1. **Performance issues:**
   - Check Performance Insights for slow queries
   - Review connection count
   - Consider scaling instance class
   - Enable query performance insights

1. **Storage full:**
   - Enable storage autoscaling
   - Clean up old data
   - Increase allocated storage
