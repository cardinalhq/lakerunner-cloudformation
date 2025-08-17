# OTEL Collector for Lakerunner

This document covers deploying the OpenTelemetry (OTEL) Collector as a dedicated telemetry ingestion service for Lakerunner.

## Overview

The OTEL collector provides a standardized telemetry ingestion endpoint that can receive OTEL data via gRPC or HTTP and export it to Lakerunner's S3 storage for processing by the core services.

## Why Use the OTEL Collector?

- **Standardized telemetry ingestion** - Accept OTEL traces, metrics, and logs from any OTEL-compatible source
- **Separate scaling** - Scale telemetry collection independently from core Lakerunner services  
- **Configurable endpoints** - Internal or external ALB for different network access patterns
- **Data transformation** - Process and filter telemetry data before storage
- **High availability** - Dedicated ALB and ECS service for telemetry collection

## Prerequisites

Before deploying the OTEL collector, you must have:

1. **Core Lakerunner deployed** - The common infrastructure stack must be deployed first
2. **S3 bucket available** - For telemetry data storage (created by common infrastructure)
3. **VPC and networking** - Private subnets and security groups from common infrastructure

## Deployment

### Step 1: Deploy OTEL Collector Stack

Deploy `generated-templates/lakerunner-demo-otel-collector.yaml`. Required parameters:

- **CommonInfraStackName** – Name of the CommonInfra stack (e.g., "lakerunner-common")

Optional parameters:
- **LoadBalancerType** – ALB scheme: "internal" (default) or "internet-facing"  
- **OtelCollectorImage** – Container image for OTEL collector (default: public.ecr.aws/cardinalhq.io/cardinalhq-otel-collector:latest)
- **OrganizationId** – Organization ID for OTEL data routing (default: 12340000-0000-4000-8000-000000000000)
- **CollectorName** – Collector name for OTEL data routing (default: "lakerunner")
- **OtelConfigYaml** – Custom OTEL collector configuration in YAML format (optional)

**Example AWS CLI deployment:**

```bash
aws cloudformation create-stack \\
  --stack-name lakerunner-otel \\
  --template-body file://generated-templates/lakerunner-demo-otel-collector.yaml \\
  --parameters ParameterKey=CommonInfraStackName,ParameterValue=lakerunner-common \\
               ParameterKey=LoadBalancerType,ParameterValue=internal \\
  --capabilities CAPABILITY_IAM
```

### Step 2: Get OTEL Endpoints

After deployment, retrieve the ALB DNS name from the CloudFormation outputs:

```bash
aws cloudformation describe-stacks --stack-name lakerunner-otel --query 'Stacks[0].Outputs[?OutputKey==`AlbDnsName`].OutputValue' --output text
```

**OTEL endpoints:**
- **gRPC**: `http://<alb-dns>:4317` 
- **HTTP**: `http://<alb-dns>:4318`

## Configuration

### Default Configuration

The OTEL collector uses a default configuration that:

- **Receives telemetry** on ports 4317 (gRPC) and 4318 (HTTP)
- **Processes data** with memory limiting and batching
- **Exports metrics and logs** to S3 storage for Lakerunner processing
- **Discards traces** (sends to nop exporter)
- **Provides health checks** on port 13133

### Custom Configuration

To customize the OTEL collector configuration, pass a complete OTEL configuration as the `OtelConfigYaml` parameter during deployment. The configuration supports environment variable substitution:

- `${env:ORGANIZATION_ID}` - Organization ID from parameter
- `${env:COLLECTOR_NAME}` - Collector name from parameter  
- `${env:AWS_S3_BUCKET}` - S3 bucket from common infrastructure
- `${env:AWS_REGION}` - Current AWS region

**Example custom configuration:**

```yaml
receivers:
  otlp:
    protocols:
      grpc:
        endpoint: 0.0.0.0:4317
      http:
        endpoint: 0.0.0.0:4318

processors:
  memory_limiter:
    limit_mib: 2048
    check_interval: 1s
  batch:
    send_batch_max_size: 200
    send_batch_size: 20
    timeout: 2s

exporters:
  chqs3:
    s3uploader:
      customer_key: ${env:ORGANIZATION_ID}/${env:COLLECTOR_NAME}
      region: ${env:AWS_REGION}
      s3_bucket: ${env:AWS_S3_BUCKET}
      s3_prefix: otel-raw

extensions:
  health_check:
    endpoint: 0.0.0.0:13133

service:
  extensions:
    - health_check
  pipelines:
    metrics:
      receivers: [otlp]
      processors: [memory_limiter, batch]
      exporters: [chqs3]
    logs:
      receivers: [otlp]
      processors: [memory_limiter, batch]
      exporters: [chqs3]
```

### Updating Configuration

To update the OTEL collector configuration:

1. **Update the CloudFormation stack** with new `OtelConfigYaml` parameter value
2. **ECS will restart** the collector service automatically with the new configuration
3. **Monitor logs** in CloudWatch under `/ecs/otel-gateway` for any configuration errors

## Networking

### Security Groups

The OTEL collector creates security groups that allow:

- **ALB Security Group**: Inbound traffic on ports 4317, 4318 from 0.0.0.0/0
- **Task Security Group**: Inbound traffic from ALB security group on ports 4317, 4318, 13133

### Load Balancer

The ALB provides:
- **High availability** across multiple availability zones
- **Health checks** to OTEL collector instances on port 13133/healthz
- **gRPC and HTTP listeners** on ports 4317 and 4318

## Monitoring

### Health Checks

- **ALB health checks** - Monitor `/healthz` endpoint on port 13133
- **ECS health checks** - Currently disabled for simplified operation
- **CloudWatch logs** - Available under `/ecs/otel-gateway`

### Metrics

The OTEL collector exports its own metrics to the configured exporters, providing visibility into:
- Received telemetry volume
- Processing latency  
- Export success/failure rates
- Memory and CPU usage

## Troubleshooting

### Common Issues

1. **Service won't start**
   - Check CloudWatch logs for configuration errors
   - Verify environment variables are properly substituted
   - Ensure S3 bucket permissions are correct

2. **Health checks failing**
   - Verify port 13133 is accessible within security groups
   - Check that health check extension is enabled in configuration
   - Monitor ECS task status in console

3. **Data not appearing in S3**
   - Verify S3 bucket permissions in task role
   - Check CloudWatch logs for export errors
   - Ensure OTEL data is being sent to correct endpoints

4. **High memory usage**
   - Adjust `memory_limiter` processor configuration
   - Increase ECS task memory allocation
   - Tune batch processor settings

### Logs

Monitor OTEL collector logs in CloudWatch:

```bash
aws logs filter-log-events --log-group-name /ecs/otel-gateway --start-time $(date -d '1 hour ago' +%s)000
```

## Integration with Applications

To send telemetry data to the OTEL collector from your applications:

### Environment Variables

```bash
export OTEL_EXPORTER_OTLP_ENDPOINT="http://<alb-dns>:4317"
export OTEL_EXPORTER_OTLP_PROTOCOL="grpc"
```

### SDK Configuration

Configure your OTEL SDK to send data to the collector endpoints. The collector accepts:

- **gRPC protocol** on port 4317 (recommended)
- **HTTP/protobuf protocol** on port 4318
- **Standard OTEL signal types**: traces, metrics, logs

For examples of instrumented applications, see [README-DEMO-APPS.md](README-DEMO-APPS.md).

## See Also

- **[README.md](README.md)** - Main Lakerunner deployment guide  
- **[README-DEMO-APPS.md](README-DEMO-APPS.md)** - Demo applications and testing
- **[README-BUILDING.md](README-BUILDING.md)** - Building and development guide