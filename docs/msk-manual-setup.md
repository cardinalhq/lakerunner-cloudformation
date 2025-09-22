# MSK (Managed Kafka) - Manual AWS Console Setup Guide

This guide provides step-by-step instructions to manually create an Amazon MSK cluster for Lakerunner streaming data using the AWS Management Console.

## Prerequisites

- [VPC Infrastructure](vpc-manual-setup.md) - VPC with at least 2 private subnets
- [Security Groups](security-groups-manual-setup.md) - MSK security group configured

## What This Creates

- MSK cluster with Apache Kafka
- Custom configuration for Kafka
- CloudWatch logging
- IAM authentication (optional)
- Encryption in transit and at rest

## Option A: Create New MSK Cluster

### 1. Create MSK Configuration

Before creating the cluster, define Kafka configuration:

1. Navigate to **Amazon MSK → Configurations**
1. Click **Create configuration**
1. Configuration details:
   - **Configuration name**: `lakerunner-msk-config`
   - **Description**: `Kafka configuration for Lakerunner streaming`
1. Configuration properties - Add these settings:

```properties
# Auto-create topics when first referenced
auto.create.topics.enable=true

# Replication settings
default.replication.factor=2
min.insync.replicas=1

# Default partitions for auto-created topics
num.partitions=3

# Log retention (7 days in milliseconds)
log.retention.ms=604800000

# Log segment size (1GB)
log.segment.bytes=1073741824

# Compression
compression.type=snappy

# Group coordinator settings
offsets.topic.replication.factor=2
transaction.state.log.replication.factor=2
transaction.state.log.min.isr=1

# Connection settings
connections.max.idle.ms=600000
```

1. Click **Create configuration**

### 2. Create MSK Cluster

1. Navigate to **Amazon MSK → Clusters**
1. Click **Create cluster**
1. Creation method: **Custom create**

#### Step 1: Cluster settings

1. **Cluster name**: `lakerunner-msk-cluster`
1. **Cluster type**: Provisioned
1. **Apache Kafka version**: 2.8.1 (or latest 2.8.x)
1. **Configuration**: Select `lakerunner-msk-config`

#### Step 2: Networking

1. **VPC**: Select your VPC
1. **Number of zones**: 2 (or 3 for higher availability)
1. **Subnets**:
   - First zone: Select private subnet 1
   - Second zone: Select private subnet 2
1. **Security groups**: Select `lakerunner-msk-sg`

#### Step 3: Brokers

1. **Broker instance type**:
   - Development: `kafka.t3.small`
   - Production: `kafka.m5.large` or larger
1. **Number of brokers per zone**: 1 (minimum 2 total)
1. **Storage**:
   - **Storage volume size per broker**: 100 GiB (adjust based on needs)
   - **Storage throughput**: Default (or provision based on needs)

#### Step 4: Security

1. **Access control methods**:
   - ✓ **Unauthenticated access**: For development
   - ✓ **IAM role-based authentication**: For production
   - ✓ **TLS client authentication**: If using certificates
1. **Encryption**:
   - **Encryption in transit**:
     - Between clients and brokers: TLS
     - Within cluster: TLS
   - **Encryption at rest**: Enable with AWS managed key

#### Step 5: Monitoring

1. **Amazon CloudWatch logs**:
   - ✓ **Broker logs**: Enable
   - **Log group**: `/aws/msk/lakerunner-msk-cluster`
1. **Open monitoring with Prometheus**:
   - **JMX exporter**: Enable (optional)
   - **Node exporter**: Enable (optional)

#### Step 6: Tags

1. Add tags:
   - **Name**: `lakerunner-msk-cluster`
   - **Environment**: `lakerunner`
   - **Component**: `Streaming`

1. Click **Create cluster**

The cluster creation takes 15-30 minutes.

### 3. Get Cluster Connection Details

Once the cluster is **Active**:

1. Select your cluster
1. Click **View client information**
1. Note the bootstrap server strings:
   - **Plaintext**: `b-1.lakerunner-msk.xxx.kafka.{region}.amazonaws.com:9092,...`
   - **TLS**: `b-1.lakerunner-msk.xxx.kafka.{region}.amazonaws.com:9094,...`
   - **IAM**: `b-1.lakerunner-msk.xxx.kafka.{region}.amazonaws.com:9098,...`

### 4. Create Kafka Topics (Optional)

If auto.create.topics.enable=false or you want specific configurations:

#### Using Kafka CLI from EC2 Instance

1. Launch an EC2 instance in the same VPC
1. Attach the compute security group
1. Install Kafka client:

```bash
# Download Kafka
wget https://downloads.apache.org/kafka/3.5.1/kafka_2.13-3.5.1.tgz
tar -xzf kafka_2.13-3.5.1.tgz
cd kafka_2.13-3.5.1

# Set bootstrap servers
export BOOTSTRAP_SERVERS="b-1.lakerunner-msk.xxx.kafka.{region}.amazonaws.com:9092,..."

# Create topics
bin/kafka-topics.sh --create \
  --bootstrap-server $BOOTSTRAP_SERVERS \
  --topic lakerunner-logs \
  --partitions 6 \
  --replication-factor 2

bin/kafka-topics.sh --create \
  --bootstrap-server $BOOTSTRAP_SERVERS \
  --topic lakerunner-metrics \
  --partitions 3 \
  --replication-factor 2

# List topics
bin/kafka-topics.sh --list \
  --bootstrap-server $BOOTSTRAP_SERVERS
```

### 5. Configure IAM Authentication (Production)

If using IAM authentication:

#### Create IAM Policy for Kafka Access

1. Navigate to **IAM → Policies**
1. Click **Create policy**
1. Use JSON editor:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "kafka-cluster:Connect",
        "kafka-cluster:AlterCluster",
        "kafka-cluster:DescribeCluster"
      ],
      "Resource": [
        "arn:aws:kafka:{region}:{accountId}:cluster/lakerunner-msk-cluster/*"
      ]
    },
    {
      "Effect": "Allow",
      "Action": [
        "kafka-cluster:*Topic",
        "kafka-cluster:ReadData",
        "kafka-cluster:WriteData"
      ],
      "Resource": [
        "arn:aws:kafka:{region}:{accountId}:topic/lakerunner-msk-cluster/*/*"
      ]
    },
    {
      "Effect": "Allow",
      "Action": [
        "kafka-cluster:AlterGroup",
        "kafka-cluster:DescribeGroup"
      ],
      "Resource": [
        "arn:aws:kafka:{region}:{accountId}:group/lakerunner-msk-cluster/*/*"
      ]
    }
  ]
}
```

1. Name: `lakerunner-msk-access-policy`
1. Attach to Lakerunner service roles

## Option B: Use Existing MSK Cluster

If you have an existing MSK cluster:

### 1. Verify Cluster Configuration

Ensure your cluster meets these requirements:

- Apache Kafka version 3.9.0 or higher
- Accessible from private subnets
- Sufficient broker capacity for additional load

### 2. Update Security Groups

1. Add inbound rules to your MSK security group:
   - Port 9092 (plaintext) from compute security group
   - Port 9094 (TLS) from compute security group
   - Port 9098 (IAM) from compute security group

### 3. Create Topics for Lakerunner

Create dedicated topics with appropriate settings (see section 4 above).

### 4. Configure Access

Grant Lakerunner services access to your MSK cluster through IAM policies or security groups.

## Testing the Cluster

### 1. Test Producer

```bash
# From EC2 instance with Kafka client
echo "test message" | bin/kafka-console-producer.sh \
  --bootstrap-server $BOOTSTRAP_SERVERS \
  --topic test-topic
```

### 2. Test Consumer

```bash
bin/kafka-console-consumer.sh \
  --bootstrap-server $BOOTSTRAP_SERVERS \
  --topic test-topic \
  --from-beginning
```

### 3. Test with IAM Authentication

```bash
# Set IAM auth properties
cat > client.properties <<EOF
security.protocol=SASL_SSL
sasl.mechanism=AWS_MSK_IAM
sasl.jaas.config=software.amazon.msk.auth.iam.IAMLoginModule required;
sasl.client.callback.handler.class=software.amazon.msk.auth.iam.IAMClientCallbackHandler
EOF

# Use IAM bootstrap servers
export IAM_BOOTSTRAP="b-1.lakerunner-msk.xxx.kafka.{region}.amazonaws.com:9098,..."

# Test with IAM
bin/kafka-topics.sh --list \
  --bootstrap-server $IAM_BOOTSTRAP \
  --command-config client.properties
```

## Outputs to Record

After completing MSK setup, record these values:

- **Cluster ARN**: `arn:aws:kafka:{region}:{accountId}:cluster/lakerunner-msk-cluster/xxx`
- **Bootstrap Servers (Plaintext)**: `b-1.xxx:9092,b-2.xxx:9092`
- **Bootstrap Servers (TLS)**: `b-1.xxx:9094,b-2.xxx:9094`
- **Bootstrap Servers (IAM)**: `b-1.xxx:9098,b-2.xxx:9098`
- **Zookeeper Connection**: `z-1.xxx:2181,z-2.xxx:2181` (if needed)

## Next Steps

With MSK configured, proceed to:

1. [ECS Services Setup](../services-manual-setup.md) - Deploy services that use Kafka
1. Configure producers and consumers in your applications

## Scaling and Performance

### Broker Scaling

1. **Vertical Scaling** (change instance type):
   - Select cluster → **Actions** → **Edit broker type**
   - Choose new instance type
   - Apply changes (rolling update)

1. **Horizontal Scaling** (add brokers):
   - Select cluster → **Actions** → **Edit number of brokers**
   - Increase broker count
   - Rebalance partitions after scaling

### Storage Scaling

1. Select cluster → **Actions** → **Edit broker storage**
1. Increase storage size (cannot decrease)
1. Apply changes (no downtime)

### Performance Tuning

1. **Partition Count**: More partitions = higher parallelism
1. **Replication Factor**: Balance between durability and performance
1. **Batch Size**: Larger batches improve throughput
1. **Compression**: Reduces network and storage usage
1. **Buffer Memory**: Increase for better batching

## Troubleshooting

### Common Issues

1. **Cannot connect to cluster:**
   - Verify security groups allow required ports
   - Check network connectivity from client
   - Ensure using correct bootstrap servers
   - Verify DNS resolution works

1. **Authentication failures:**
   - Check IAM role has correct permissions
   - Verify SASL configuration
   - Ensure TLS certificates are valid
   - Check security protocol settings

1. **High latency:**
   - Check broker CPU and network metrics
   - Review partition distribution
   - Consider increasing broker count
   - Optimize batch and buffer settings

1. **Message loss:**
   - Check replication factor settings
   - Verify min.insync.replicas configuration
   - Review producer acknowledgment settings
   - Check for broker failures

1. **Disk full:**
   - Review retention policies
   - Increase storage size
   - Check for runaway producers
   - Verify cleanup policies are working
